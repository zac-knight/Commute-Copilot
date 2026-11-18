"""CalendarReader: READ-ONLY access to the user's calendar for the agent's tools.

Sources: the local calendar (JSON) and any ``*.ics`` files in the import directory (read-only
copies exported from Google/Apple/Outlook). This class holds no writer and has no write methods.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

from app.agents.calendar.ics import parse_ics
from app.agents.calendar.models import CalEvent
from app.agents.calendar.slots import Interval, conflicts_for, find_free_slots
from app.agents.calendar.store import CalendarStoreError, LocalCalendarReader
from app.core.memory import UserProfile
from app.core.timeutil import TZ

MAX_ICS_BYTES = 5 * 1024 * 1024
MAX_RANGE_DAYS = 400


def parse_hhmm(text: str) -> time:
    h, _, m = text.strip().partition(":")
    try:
        return time(int(h), int(m or 0))
    except ValueError as exc:
        raise ValueError(f"bad time of day {text!r}; use HH:MM") from exc


@dataclass
class EventsResult:
    events: list[CalEvent]
    warnings: list[str] = field(default_factory=list)


class CalendarReader:
    def __init__(
        self,
        local: LocalCalendarReader,
        import_dir: Path | None,
        profile: UserProfile,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._local = local
        self._import_dir = import_dir
        self.profile = profile
        self.clock = clock or (lambda: datetime.now(TZ))

    @property
    def default_working_hours(self) -> tuple[time, time]:
        return parse_hhmm(self.profile.working_hours_start), parse_hhmm(
            self.profile.working_hours_end
        )

    def _check_range(self, start: datetime, end: datetime) -> None:
        if end <= start:
            raise ValueError("end must be after start")
        if end - start > timedelta(days=MAX_RANGE_DAYS):
            raise ValueError(f"range too long (max {MAX_RANGE_DAYS} days)")

    def events_between(self, start: datetime, end: datetime) -> EventsResult:
        self._check_range(start, end)
        return self._events_between(start, end)

    def _events_between(self, start: datetime, end: datetime) -> EventsResult:
        warnings: list[str] = []
        events = list(self._local.between(start, end))  # CalendarStoreError propagates: loud
        if self._import_dir is not None and self._import_dir.is_dir():
            for path in sorted(self._import_dir.glob("*.ics")):
                try:
                    if path.stat().st_size > MAX_ICS_BYTES:
                        warnings.append(
                            f"{path.name}: larger than {MAX_ICS_BYTES // 2**20} MB, skipped"
                        )
                        continue
                    found, warns = parse_ics(path.read_bytes(), path.name, start, end)
                except (OSError, ValueError) as exc:
                    warnings.append(str(exc))
                    continue
                events += [e for e in found if e.overlaps(start, end)]
                warnings += warns
        events.sort(key=lambda e: (e.start, e.title, e.id))
        return EventsResult(events, warnings)

    def find(self, event_id: str) -> CalEvent | None:
        """Look an event up by id (local first; imported ones via a +-1 year scan)."""
        if (hit := self._local.get(event_id)) is not None:
            return hit
        if event_id.startswith("ics-"):
            now = self.clock()
            res = self._events_between(now - timedelta(days=365), now + timedelta(days=365))
            return next((e for e in res.events if e.id == event_id), None)
        return None

    def free_slots(
        self,
        start: datetime,
        end: datetime,
        duration_minutes: int,
        *,
        working_hours: tuple[time, time] | None = None,
        whole_day: bool = False,
        buffer_minutes: int | None = None,
        all_day_blocks: bool = False,
    ) -> tuple[list[Interval], list[str]]:
        """Free slots. ``working_hours=None`` uses the profile's; ``whole_day`` ignores them."""
        self._check_range(start, end)
        res = self._events_between(start - timedelta(days=1), end + timedelta(days=1))
        hours = None if whole_day else (working_hours or self.default_working_hours)
        buf = self.profile.buffer_minutes if buffer_minutes is None else buffer_minutes
        slots = find_free_slots(
            res.events,
            start,
            end,
            duration_minutes,
            working_hours=hours,
            buffer_minutes=buf,
            all_day_blocks=all_day_blocks,
        )
        return slots, res.warnings

    def conflicts(
        self, start: datetime, end: datetime, buffer_minutes: int | None = None
    ) -> tuple[list[CalEvent], list[CalEvent], list[str]]:
        buf = self.profile.buffer_minutes if buffer_minutes is None else buffer_minutes
        self._check_range(start, end)
        res = self._events_between(start - timedelta(days=1), end + timedelta(days=1))
        overlapping, close = conflicts_for(res.events, start, end, buffer_minutes=buf)
        return overlapping, close, res.warnings


__all__ = ["CalendarReader", "CalendarStoreError", "EventsResult", "parse_hhmm"]
