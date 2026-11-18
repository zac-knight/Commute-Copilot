"""Deterministic evaluation suites: NO LLM, NO network. These run in CI on every push.

Each suite reads cases from ``evals/cases/<suite>.yaml`` and returns :class:`CaseResult` rows.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.agents.calendar.models import CalEvent
from app.agents.calendar.slots import Interval, conflicts_for, find_free_slots
from app.agents.transit.service import TransitService
from app.agents.weather.advice import RoutePlan, WeatherStatus, commute_advice
from app.agents.weather.client import Forecast
from app.core.timeutil import TZ
from evals import dsl
from evals.fixtures import build_transit, parse_dt
from tests.weather_fixtures import forecast as wx_forecast
from tests.weather_fixtures import leg as wx_leg

CASES_DIR = Path(__file__).parent / "cases"


@dataclass
class CaseResult:
    id: str
    suite: str
    desc: str
    passed: bool
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_cases(suite: str) -> list[dict[str, Any]]:
    doc = yaml.safe_load((CASES_DIR / f"{suite}.yaml").read_text())
    assert doc["suite"] == suite
    cases: list[dict[str, Any]] = doc["cases"]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), f"duplicate case ids in {suite}.yaml"
    return cases


# ---------------------------------------------------------------------------- transit

_TIME_ARGS = {"after", "my_arrival", "scheduled_departure", "depart_after", "arrive_by"}


async def _call_transit(svc: TransitService, call: dict[str, Any]) -> dict[str, Any]:
    args = {
        k: (parse_dt(v) if k in _TIME_ARGS and v else v) for k, v in call.get("args", {}).items()
    }
    method = getattr(svc, call["method"])
    out = method(**args)
    if asyncio.iscoroutine(out):
        out = await out
    dumped: dict[str, Any] = out.model_dump(mode="json")
    return dumped


async def run_transit() -> list[CaseResult]:
    results = []
    for c in load_cases("transit"):
        svc = build_transit(c["now"], c.get("rt"))
        failures: list[str] = []
        try:
            data = await _call_transit(svc, c["call"])
            if "expect_error" in c:
                failures.append("expected an error but the call succeeded")
            else:
                failures = dsl.check_all(data, c["expect"])
        except Exception as exc:
            want = c.get("expect_error")
            if want is None:
                failures = [f"unexpected {type(exc).__name__}: {exc}"]
            elif want["contains"].lower() not in str(exc).lower():
                failures = [f"error {exc!r} lacks {want['contains']!r}"]
        results.append(CaseResult(c["id"], "transit", c["desc"], not failures, failures))
    return results


# ---------------------------------------------------------------------------- advice


def _hm(text: str, day: int = 0) -> datetime:
    h, m = (int(x) for x in text.split(":"))
    return datetime(2026, 11, 9 + day, h, m, tzinfo=TZ)


def _advice_inputs(
    c: dict[str, Any],
) -> tuple[RoutePlan, Forecast | None, datetime, WeatherStatus | None]:
    now = parse_dt(c.get("now", "2026-11-09T07:50"))
    spec = c.get("plan") or {
        "legs": [
            {"kind": "transit", "dep": "08:00", "arr": "08:30", "route": "680", "status": "on_time"}
        ]
    }
    legs = [
        wx_leg(
            lg["kind"],
            _hm(lg["dep"], lg.get("day", 0)),
            _hm(lg["arr"], lg.get("day", 0)),
            route=lg.get("route"),
            status=lg.get("status"),
            delay=lg.get("delay"),
            walk_m=lg.get("walk_m"),
        )
        for lg in spec["legs"]
    ]
    fc_spec = c.get("forecast", {})
    forecast: Forecast | None = None
    if fc_spec is not None:
        forecast = wx_forecast(
            {int(k): v for k, v in fc_spec.get("hours", {}).items()}, **fc_spec.get("defaults", {})
        )
    weather = WeatherStatus(**c["weather"]) if c.get("weather") else None
    return RoutePlan(legs=legs), forecast, now, weather


def run_advice() -> list[CaseResult]:
    results = []
    for c in load_cases("advice"):
        plan, forecast, now, weather = _advice_inputs(c)
        out = commute_advice(plan, forecast, now, weather=weather).model_dump(mode="json")
        failures = dsl.check_all(out, c["expect"])
        results.append(CaseResult(c["id"], "advice", c["desc"], not failures, failures))
    return results


# ---------------------------------------------------------------------------- calendar


def _when(text: str, day: int = 0) -> datetime:
    """'HH:MM' on day 0 (Mon 2026-11-09) + ``day``, or a full ISO timestamp with offset."""
    if "T" in text:
        return datetime.fromisoformat(text)
    return _hm(text, day)


def _cal_event(e: dict[str, Any], i: int) -> CalEvent:
    return CalEvent(
        id=f"evt-{i}", title=e["title"],
        start=_when(e["start"], e.get("day", 0)), end=_when(e["end"], e.get("end_day", e.get("day", 0))),
        all_day=bool(e.get("all_day")), busy=bool(e.get("busy", True)),
    )  # fmt: skip


def _fmt_slot(iv: Interval) -> str:
    s, e = iv.start.astimezone(TZ), iv.end.astimezone(TZ)
    return f"{s:%m-%d %H:%M}-{e:%H:%M}"


def run_calendar() -> list[CaseResult]:
    results = []
    for c in load_cases("calendar"):
        events = [_cal_event(e, i) for i, e in enumerate(c["events"])]
        failures: list[str] = []
        try:
            if "free" in c:
                q = c["free"]
                slots = find_free_slots(
                    events, _when(q["start"], q.get("day", 0)), _when(q["end"], q.get("end_day", 0)),
                    q["duration"], buffer_minutes=q.get("buffer", 0),
                    all_day_blocks=bool(q.get("all_day_blocks")),
                )  # fmt: skip
                out: dict[str, Any] = {"slots": [_fmt_slot(s) for s in slots]}
            else:
                q = c["conflicts"]
                over, close = conflicts_for(
                    events, _when(q["start"]), _when(q["end"]), buffer_minutes=q.get("buffer", 0)
                )
                out = {"overlapping": [e.title for e in over], "close": [e.title for e in close]}
            if "expect_error" in c:
                failures = ["expected an error but the call succeeded"]
            else:
                failures = dsl.check_all(out, c["expect"])
        except Exception as exc:
            want = c.get("expect_error")
            if want is None:
                failures = [f"unexpected {type(exc).__name__}: {exc}"]
            elif want["contains"].lower() not in str(exc).lower():
                failures = [f"error {exc!r} lacks {want['contains']!r}"]
        results.append(CaseResult(c["id"], "calendar", c["desc"], not failures, failures))
    return results
