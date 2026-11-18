"""Shared time helpers. The user lives in Brisbane: Australia/Brisbane, no daylight saving."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Australia/Brisbane")


def fmt_local(dt: datetime, now: datetime, tz: tzinfo = TZ) -> str:
    """'08:12' if on the same local date as ``now``, else 'Wed 18 Nov 08:12' (in ``tz``)."""
    dt, now = dt.astimezone(tz), now.astimezone(tz)
    return dt.strftime("%H:%M") if dt.date() == now.date() else dt.strftime("%a %d %b %H:%M")


# 'Wednesday 01:35' said on Wednesday at 01:36 means NOW, not next week: a model or user quoting the
# current time is a few minutes behind by the time it is parsed (observed live).
_WEEKDAY_GRACE = timedelta(minutes=15)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_NATURAL = re.compile(
    r"^(?:(?P<day>today|tomorrow|[a-z]{3,9})\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?(?::(?P<s>\d{2}))?\s*(?P<ampm>am|pm)?$"
)


_DISPLAY = re.compile(
    r"^(?:[A-Za-z]{3,9},?\s+)?(\d{1,2})\s+([A-Za-z]{3,9})\.?(?:\s+(\d{4}))?[,\s]+(\d{1,2}):(\d{2})$"
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _from_display(m: re.Match[str], now: datetime, original: str) -> datetime:
    month_txt = m[2][:3].lower()
    if month_txt not in _MONTHS:
        raise ValueError(f"cannot understand time {original!r}; use e.g. 'tomorrow 07:30'")
    day, hour, minute = int(m[1]), int(m[4]), int(m[5])
    year = int(m[3]) if m[3] else now.year
    try:
        result = datetime(year, _MONTHS.index(month_txt) + 1, day, hour, minute, tzinfo=TZ)
    except ValueError as exc:
        raise ValueError(f"cannot understand time {original!r}: {exc}") from exc
    if not m[3] and result < now - timedelta(days=180):  # 'Jan 5' said in December means next year
        result = result.replace(year=year + 1)
    return result


def parse_when(text: str, now: datetime) -> datetime:
    """Parse a point in time given by a model or user. All deterministic, Brisbane time.

    Accepts: 'now'; display forms copied from tool output ('Thu 19 Nov 14:50', '19 Nov 2026
    14:50'); ISO datetimes ('2026-11-19T07:30', with or without offset); bare times ('07:30',
    '7:30am', '9pm', GTFS-style '25:10:00') meaning TODAY; and a day word plus time:
    'today 8am', 'tomorrow 07:30', 'friday 18:05' (a weekday name = its next occurrence, today
    only if that time has not yet passed). Raises ValueError otherwise.
    """
    raw = text.strip()
    now = now.astimezone(TZ)
    if raw.lower() in ("now", "right now", "immediately"):
        return now.replace(second=0, microsecond=0)
    if (shown := _DISPLAY.match(raw)) is not None:  # 'Thu 19 Nov 14:50' (what our tools print)
        return _from_display(shown, now, text)
    m = _NATURAL.match(raw.lower())
    if not m:
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(
                f"cannot understand time {text!r}; use e.g. 'tomorrow 07:30', '9am', or "
                "'2026-11-19T07:30'"
            ) from exc
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)

    hour, minute, second = int(m["h"]), int(m["m"] or 0), int(m["s"] or 0)
    if m["ampm"]:
        if not 1 <= hour <= 12:
            raise ValueError(f"bad 12-hour time {text!r}")
        hour = hour % 12 + (12 if m["ampm"] == "pm" else 0)
    if minute > 59 or second > 59:
        raise ValueError(f"bad time {text!r}")
    day = m["day"]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base = midnight
    if day == "tomorrow":
        base = midnight + timedelta(days=1)
    elif day and day != "today":
        names = [w for w in _WEEKDAYS if w.startswith(day[:3])]
        if not names:
            raise ValueError(f"unknown day {day!r} in {text!r}")
        ahead = (_WEEKDAYS.index(names[0]) - now.weekday()) % 7
        base = midnight + timedelta(days=ahead)
    result = base + timedelta(hours=hour, minutes=minute, seconds=second)
    if day and day not in ("today", "tomorrow") and result < now - _WEEKDAY_GRACE:
        result += timedelta(days=7)  # same weekday, but that time has passed: next week
    return result
