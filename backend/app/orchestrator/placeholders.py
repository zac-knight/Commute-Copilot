"""Placeholders in plan tasks: ``{s1.start}``, ``{s1.end-15m}``, ``{s2.arrive+10m}``.

Why: observed live, the 8B router/agents misuse times from earlier results (the model asked for
'arrive 15 minutes before the slot' and used the slot's END time). Time arithmetic is therefore done
here, deterministically, from the earlier step's STRUCTURED tool output, and the later step only
ever sees a finished ISO time.

Supported fields (taken from the first usable result of the referenced step):
  start / end  : first free slot (find_free_slots), first event (list_events),
                 first journey depart/arrive (plan_trip), first departure (next_departures)
  depart / arrive : first journey (plan_trip)
Offsets: ``+15m``, ``-15m``, ``+2h``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from app.orchestrator.events import StepOutcome

PLACEHOLDER = re.compile(r"\{(s\d+)\.(start|end|depart|arrive)(?:([+-])(\d{1,4})([mh]))?\}")


class PlaceholderError(ValueError):
    pass


def referenced_steps(task: str) -> list[str]:
    return list(dict.fromkeys(m[1] for m in PLACEHOLDER.finditer(task)))


def _first(outcome: StepOutcome, field: str) -> str | None:
    for t in outcome.tool_outputs:
        out = t.output
        if t.is_error or not isinstance(out, dict):
            continue
        row: dict[str, Any] | None = None
        key = ""
        if t.tool == "find_free_slots" and out.get("slots"):
            row, key = out["slots"][0], {"start": "start_iso", "end": "end_iso"}.get(field, "")
        elif t.tool == "list_events" and out.get("events"):
            row, key = out["events"][0], {"start": "start_iso", "end": "end_iso"}.get(field, "")
        elif t.tool == "plan_trip" and out.get("journeys"):
            key = {"start": "depart_iso", "depart": "depart_iso", "end": "arrive_iso",
                   "arrive": "arrive_iso"}[field]  # fmt: skip
            row = out["journeys"][0]
        elif t.tool == "next_departures" and out.get("departures") and field in ("start", "depart"):
            row = out["departures"][0]
            key = "predicted_iso" if row.get("predicted_iso") else "scheduled_iso"
        if row is not None and key and row.get(key):
            return str(row[key])
    return None


def resolve(task: str, outcomes: Mapping[str, StepOutcome]) -> str:
    """Replace every placeholder with a concrete ISO time or raise PlaceholderError."""

    def sub(m: re.Match[str]) -> str:
        step, field, sign, amount, unit = m.groups()
        outcome = outcomes.get(step)
        if outcome is None:
            raise PlaceholderError(f"{m[0]}: step {step} has not run")
        value = _first(outcome, field)
        if value is None:
            raise PlaceholderError(
                f"{m[0]}: step {step} produced no {field} time to use "
                "(for example no free slot or no journey was found)"
            )
        when = datetime.fromisoformat(value)
        if sign:
            delta = timedelta(minutes=int(amount)) if unit == "m" else timedelta(hours=int(amount))
            when = when + delta if sign == "+" else when - delta
        return when.isoformat(timespec="minutes")

    return PLACEHOLDER.sub(sub, task)
