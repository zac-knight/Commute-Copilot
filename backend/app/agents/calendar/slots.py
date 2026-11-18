"""find_free_slots: a pure function (no I/O, no clock) so it is easy to test exhaustively.

Rules
-----
* Only *busy* timed events block time. All-day events do not block unless ``all_day_blocks`` is set;
  then every all-day event blocks the whole local day(s) it covers. A timed event marked free (ICS
  TRANSP:TRANSPARENT) never blocks.
* Overlapping and back-to-back busy events merge into one block.
* ``buffer_minutes`` keeps that much clear time before and after every *timed* busy event. It is not
  applied at the edges of the working-hours window or the search range.
* Free time is computed inside ``working_hours`` on each local day (``None`` = the whole day),
  clipped to [start, end]. A free interval is only returned if it is at least ``duration_minutes``.
* Everything is converted to Australia/Brisbane (no daylight saving, so every day is 24 h).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.agents.calendar.models import CalEvent
from app.core.timeutil import TZ


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def _merge(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for iv in sorted(intervals, key=lambda i: (i.start, i.end)):
        if out and iv.start <= out[-1].end:  # overlapping or touching
            if iv.end > out[-1].end:
                out[-1] = Interval(out[-1].start, iv.end)
        else:
            out.append(iv)
    return out


def busy_intervals(
    events: Sequence[CalEvent], *, buffer_minutes: int = 0, all_day_blocks: bool = False
) -> list[Interval]:
    """Merged blocks of time that are not available."""
    buf = timedelta(minutes=buffer_minutes)
    raw: list[Interval] = []
    for e in events:
        if e.end <= e.start:
            continue  # zero-length: blocks nothing
        if e.all_day:
            if all_day_blocks:
                raw.append(Interval(e.start, e.end))
        elif e.busy:
            raw.append(Interval(e.start - buf, e.end + buf))
    return _merge(raw)


def _windows(
    start: datetime, end: datetime, working_hours: tuple[time, time] | None
) -> list[Interval]:
    out: list[Interval] = []
    d: date = start.astimezone(TZ).date()
    last: date = end.astimezone(TZ).date()
    while d <= last:
        if working_hours is None:
            w0 = datetime.combine(d, time(0, 0), tzinfo=TZ)
            w1 = w0 + timedelta(days=1)
        else:
            w0 = datetime.combine(d, working_hours[0], tzinfo=TZ)
            w1 = datetime.combine(d, working_hours[1], tzinfo=TZ)
        lo, hi = max(w0, start), min(w1, end)
        if hi > lo:
            out.append(Interval(lo, hi))
        d += timedelta(days=1)
    return out


def find_free_slots(
    events: Sequence[CalEvent],
    start: datetime,
    end: datetime,
    duration_minutes: int,
    *,
    working_hours: tuple[time, time] | None = (time(9, 0), time(17, 0)),
    buffer_minutes: int = 0,
    all_day_blocks: bool = False,
) -> list[Interval]:
    """Free intervals of at least ``duration_minutes`` between ``start`` and ``end``."""
    if duration_minutes < 1:
        raise ValueError("duration_minutes must be at least 1")
    if buffer_minutes < 0:
        raise ValueError("buffer_minutes cannot be negative")
    if end <= start:
        raise ValueError("end must be after start")
    if working_hours is not None and working_hours[1] <= working_hours[0]:
        raise ValueError("working hours must end after they start (overnight windows unsupported)")
    start, end = start.astimezone(TZ), end.astimezone(TZ)
    blocks = busy_intervals(events, buffer_minutes=buffer_minutes, all_day_blocks=all_day_blocks)
    need = timedelta(minutes=duration_minutes)
    free: list[Interval] = []
    for w in _windows(start, end, working_hours):
        cursor = w.start
        for b in blocks:
            if b.end <= cursor:
                continue
            if b.start >= w.end:
                break
            if b.start - cursor >= need:
                free.append(Interval(cursor, b.start))
            cursor = max(cursor, b.end)
            if cursor >= w.end:
                break
        if w.end - cursor >= need:
            free.append(Interval(cursor, w.end))
    return free


def conflicts_for(
    events: Sequence[CalEvent], start: datetime, end: datetime, *, buffer_minutes: int = 0
) -> tuple[list[CalEvent], list[CalEvent]]:
    """(overlapping, too_close): busy timed events that overlap [start, end], and those that do
    not overlap but sit within ``buffer_minutes`` of it. All-day/free events never conflict."""
    if end <= start:
        raise ValueError("end must be after start")
    buf = timedelta(minutes=buffer_minutes)
    overlapping: list[CalEvent] = []
    close: list[CalEvent] = []
    for e in events:
        if not e.busy or e.all_day or e.end <= e.start:
            continue
        if e.overlaps(start, end):
            overlapping.append(e)
        elif buffer_minutes and e.overlaps(start - buf, end + buf):
            close.append(e)
    return overlapping, close
