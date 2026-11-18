"""Read-only iCalendar (.ics) import. Handles recurrence (RRULE, EXDATE, RECURRENCE-ID overrides),
all-day events, floating/UTC/TZID times, TRANSP and STATUS:CANCELLED via the maintained
`icalendar` + `recurring-ical-events` libraries (behaviour checked against test files; NOT yet
checked against a real Google/Apple/Outlook export).

The file is untrusted input: it is size-capped by the caller, a bad file or a bad event becomes a
warning (never a crash), and every text field is length-capped here and delimited later at the
tool boundary.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Any

import icalendar
import recurring_ical_events

from app.agents.calendar.models import CalEvent
from app.core.timeutil import TZ

MAX_EVENTS = 5000
MAX_TITLE = 200
MAX_DESCRIPTION = 2000
# Recurrence-bomb guards. The expansion library materialises EVERY occurrence before we can cap the
# count (measured: FREQ=SECONDLY over 7 days = 10.7 s and 1.25 GB), so rules are costed BEFORE
# expanding. A person's meeting rule never exceeds a few occurrences a day.
MAX_RULE_PER_DAY = 48.0
MAX_RULE_OCCURRENCES = 5_000.0  # one rule, within the requested window
MAX_TOTAL_OCCURRENCES = 50_000.0  # the whole file, within the requested window

_BASE_PER_DAY = {
    "SECONDLY": 86_400.0, "MINUTELY": 1_440.0, "HOURLY": 24.0, "DAILY": 1.0,
    "WEEKLY": 1.0 / 7, "MONTHLY": 1.0 / 28, "YEARLY": 1.0 / 365,
}  # fmt: skip


def _vals(rule: Any, key: str) -> list[Any]:
    v = rule.get(key) or []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def estimate_rule(rule: Any, window_days: float) -> tuple[float, float]:
    """(upper-bound occurrences per day, upper-bound occurrences in the window) for one RRULE."""
    freq = str((_vals(rule, "FREQ") or ["DAILY"])[0]).upper()
    interval = max(1, int((_vals(rule, "INTERVAL") or [1])[0]))
    per_day = _BASE_PER_DAY.get(freq, 86_400.0) / interval  # unknown frequency: assume worst case
    if freq in ("WEEKLY", "MONTHLY", "YEARLY"):  # BYDAY etc. multiply the base rate
        per_day *= max(1, len(_vals(rule, "BYDAY")), len(_vals(rule, "BYMONTHDAY")))
    if freq not in ("SECONDLY", "MINUTELY"):  # BYHOUR/BYMINUTE/BYSECOND expand coarser frequencies
        for key in ("BYHOUR", "BYMINUTE", "BYSECOND"):
            if freq in ("HOURLY",) and key == "BYHOUR":
                continue
            per_day *= max(1, len(_vals(rule, key)))
    total = per_day * max(window_days, 1.0)
    count = _vals(rule, "COUNT")
    if count:
        total = min(total, float(count[0]))
        per_day = min(per_day, float(count[0]))
    return per_day, total


def _local(v: date | datetime) -> datetime:
    """date -> local midnight; floating (naive) -> assumed Brisbane; aware -> converted."""
    if isinstance(v, datetime):
        return v.replace(tzinfo=TZ) if v.tzinfo is None else v.astimezone(TZ)
    return datetime(v.year, v.month, v.day, tzinfo=TZ)


def _text(comp: Any, key: str, cap: int) -> str:
    return str(comp.get(key, "") or "")[:cap]


def parse_ics(
    data: bytes | str, source: str, window_start: datetime, window_end: datetime
) -> tuple[list[CalEvent], list[str]]:
    """Occurrences overlapping [window_start, window_end). Raises ValueError for an unreadable
    file; per-event problems are returned as warnings."""
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as exc:
        raise ValueError(f"{source}: not a valid iCalendar file ({type(exc).__name__})") from exc
    warnings: list[str] = []
    window_days = max(1.0, (window_end - window_start).total_seconds() / 86_400)
    budget = 0.0
    for comp in list(cal.walk("VEVENT")):
        try:
            rules = comp.get("RRULE")
            if rules is None:
                budget += 1
                continue
            est_day, est_total = 0.0, 0.0
            for rule in rules if isinstance(rules, list) else [rules]:
                d, t = estimate_rule(rule, window_days)
                est_day, est_total = est_day + d, est_total + t
        except Exception:  # a malformed rule (icalendar 'broken property') must not sink the file
            warnings.append(f"{source}: skipped an event with an invalid recurrence rule")
            cal.subcomponents.remove(comp)
            continue
        if est_day > MAX_RULE_PER_DAY or est_total > MAX_RULE_OCCURRENCES:
            warnings.append(
                f"{source}: skipped a recurring event that would repeat about {est_day:,.0f} times "
                "a day (not a realistic meeting rule)"
            )
            cal.subcomponents.remove(comp)
            continue
        budget += est_total
    if budget > MAX_TOTAL_OCCURRENCES:
        raise ValueError(f"{source}: would expand to about {budget:,.0f} events; refusing")
    try:
        comps = recurring_ical_events.of(cal).between(window_start, window_end)
    except Exception as exc:
        raise ValueError(
            f"{source}: could not expand events ({type(exc).__name__}: {exc})"
        ) from exc

    events: list[CalEvent] = []
    for comp in comps:
        if len(events) >= MAX_EVENTS:
            warnings.append(
                f"{source}: more than {MAX_EVENTS} events in range; the rest are ignored"
            )
            break
        try:
            if str(comp.get("STATUS", "")).upper() == "CANCELLED":
                continue
            raw_start, raw_end = comp.start, comp.end
            all_day = not isinstance(raw_start, datetime)
            start = _local(raw_start)
            end = _local(raw_end) if raw_end is not None else start
            if all_day and end <= start:
                end = start + timedelta(days=1)
            if end < start:
                end = start
            transp = str(comp.get("TRANSP", "")).upper()
            busy = transp != "TRANSPARENT" if transp else not all_day
            uid = str(comp.get("UID", "") or f"{start.isoformat()}-{comp.get('SUMMARY', '')}")
            digest = hashlib.sha256(f"{source}|{uid}".encode()).hexdigest()[:10]
            events.append(
                CalEvent(
                    id=f"ics-{digest}-{start:%Y%m%dT%H%M}",
                    title=_text(comp, "SUMMARY", MAX_TITLE) or "(no title)",
                    start=start, end=end, all_day=all_day, busy=busy,
                    location=_text(comp, "LOCATION", MAX_TITLE),
                    description=_text(comp, "DESCRIPTION", MAX_DESCRIPTION),
                    source=f"ics:{source}", writable=False,
                )
            )  # fmt: skip
        except Exception as exc:
            warnings.append(f"{source}: skipped one event ({type(exc).__name__}: {exc})")
    return events, warnings
