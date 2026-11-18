"""The local calendar: a JSON file (git-ignored). Stands in for the Google Calendar API, which was
not built (no OAuth setup available); see docs/decisions.md.

STRUCTURAL SAFETY: reading and writing are different classes. Agents are only ever given a
:class:`LocalCalendarReader`, which has no write methods at all. The :class:`LocalCalendarWriter`
is constructed in exactly one place (the approval executor) and is never passed to a tool.
Writes are atomic (temp file + rename) and idempotent by event id. A corrupt file raises
:class:`CalendarStoreError` and is NEVER overwritten.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agents.calendar.models import CalEvent

MAX_EVENTS = 10_000
_FIELDS = ("title", "start", "end", "all_day", "busy", "location", "description")


class CalendarStoreError(Exception):
    pass


class EventChangedError(CalendarStoreError):
    """The event no longer matches what the user reviewed (its etag differs)."""


class LocalCalendarReader:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> list[CalEvent]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [CalEvent.model_validate(e) for e in raw["events"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CalendarStoreError(
                f"local calendar file {self.path} is unreadable ({type(exc).__name__}); "
                "not touching it"
            ) from exc

    def events(self) -> list[CalEvent]:
        return sorted(self._load(), key=lambda e: (e.start, e.id))

    def between(self, start: datetime, end: datetime) -> list[CalEvent]:
        return [e for e in self.events() if e.overlaps(start, end) or e.start == e.end == start]

    def get(self, event_id: str) -> CalEvent | None:
        return next((e for e in self._load() if e.id == event_id), None)


class LocalCalendarWriter(LocalCalendarReader):
    """Write access. Only the approval executor may hold one."""

    _lock = threading.Lock()

    def _save(self, events: list[CalEvent]) -> None:
        if len(events) > MAX_EVENTS:
            raise CalendarStoreError(f"calendar is full ({MAX_EVENTS} events)")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "events": [json.loads(e.model_dump_json()) for e in events]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, event: CalEvent) -> tuple[CalEvent, bool]:
        """Insert; idempotent by id. Returns (stored event, created?)."""
        with self._lock:
            events = self._load()
            existing = next((e for e in events if e.id == event.id), None)
            if existing is not None:
                return existing, False
            events.append(event)
            self._save(events)
            return event, True

    def update(self, event_id: str, changes: dict[str, Any], expected_etag: str | None) -> CalEvent:
        with self._lock:
            events = self._load()
            idx = next((i for i, e in enumerate(events) if e.id == event_id), None)
            if idx is None:
                raise CalendarStoreError(f"event {event_id} no longer exists")
            old = events[idx]
            if expected_etag is not None and old.etag != expected_etag:
                if all(getattr(old, k) == v for k, v in changes.items()):
                    return old  # idempotent: this update is already applied
                raise EventChangedError("the event changed since you reviewed it")
            bad = set(changes) - set(_FIELDS)
            if bad:
                raise CalendarStoreError(f"cannot change fields: {sorted(bad)}")
            new = CalEvent.model_validate({**old.model_dump(), **changes})
            events[idx] = new
            self._save(events)
            return new

    def delete(self, event_id: str, expected_etag: str | None) -> bool:
        """Remove; returns False if it was already gone (idempotent)."""
        with self._lock:
            events = self._load()
            idx = next((i for i, e in enumerate(events) if e.id == event_id), None)
            if idx is None:
                return False
            if expected_etag is not None and events[idx].etag != expected_etag:
                raise EventChangedError("the event changed since you reviewed it")
            del events[idx]
            self._save(events)
            return True
