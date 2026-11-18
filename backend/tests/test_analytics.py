"""Delay analytics: collector mapping, 'final' rows, each statistic, and the time-split model test.
All data are synthetic (written into a temp store); the app itself never simulates data."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.transit.models import Departure
from app.analytics import analysis as A
from app.analytics.collector import poll_once, to_obs
from app.analytics.store import DelayStore, Obs
from app.core.timeutil import TZ

BASE = datetime(2026, 11, 2, 0, 0, tzinfo=TZ)  # a Monday


def obs(
    day: int,
    hour: int,
    minute: int,
    delay: int | None,
    *,
    route: str = "680",
    rel: str = "SCHEDULED",
    trip: str | None = None,
    ts_offset: int = 30,
) -> Obs:
    sched = int((BASE + timedelta(days=day, hours=hour, minutes=minute)).timestamp())
    return Obs(sched - 60 + ts_offset, trip or f"{route}-{day}-{hour}-{minute}", route, "S1",
               (BASE + timedelta(days=day)).strftime("%Y-%m-%d"), sched,
               None if delay is None else sched + delay, delay, rel, "reported")  # fmt: skip


def dep(**kw: Any) -> Departure:
    base: dict[str, Any] = dict(
        route="680",
        route_name="x",
        mode="bus",
        headsign="h",
        stop_id="S1",
        stop_name="Stop",
        scheduled="08:00",
        scheduled_iso="2026-11-18T08:00:00+10:00",
        status="on_time",
        trip_id="T1",
        service_date="2026-11-18",
    )
    return Departure(**{**base, **kw})


# --- collector ----------------------------------------------------------------------------


def test_to_obs_maps_status_to_schedule_relationship_and_seconds() -> None:
    o = to_obs(dep(status="delayed", delay_min=3.0, predicted_iso="2026-11-18T08:03:00+10:00",
                   delay_basis="reported"), 1000)  # fmt: skip
    assert (o.delay_s, o.rel, o.basis) == (180, "SCHEDULED", "reported") and o.pred - o.sched == 180
    assert to_obs(dep(status="cancelled"), 1).rel == "CANCELED"
    n = to_obs(dep(status="scheduled_only"), 1)
    assert n.rel == "NO_DATA" and n.delay_s is None and n.pred is None
    assert to_obs(dep(status="added"), 1).rel == "ADDED"


class FakeTransit:
    def __init__(self, deps: list[Departure] | Exception, source: str = "realtime") -> None:
        self.deps, self.source = deps, source

    async def _departures(self, *_: Any, **__: Any) -> Any:
        if isinstance(self.deps, Exception):
            raise self.deps
        return self.deps, SimpleNamespace(source=self.source, note=None)


async def test_poll_once_stores_nearby_departures_and_records_the_poll() -> None:
    now = datetime(2026, 11, 18, 7, 58, tzinfo=TZ)
    deps = [dep(trip_id="near"), dep(trip_id="far", scheduled_iso="2026-11-18T12:00:00+10:00"),
            dep(trip_id="old", scheduled_iso="2026-11-18T07:00:00+10:00")]  # fmt: skip
    store = DelayStore(":memory:")
    n = await poll_once(FakeTransit(deps), store, ["S1"], None, now)  # type: ignore[arg-type]
    assert n == 1 and [o.trip_id for o in store.observations()] == ["near"]
    assert store.poll_stats()["ok_polls"] == 1


async def test_a_failing_poll_is_recorded_not_raised() -> None:
    store = DelayStore(":memory:")
    n = await poll_once(FakeTransit(RuntimeError("feed down")), store, ["S1"], None)  # type: ignore[arg-type]
    s = store.poll_stats()
    assert n == 0 and s["polls"] == 1 and s["ok_polls"] == 0 and s["rows"] == 0
    await poll_once(FakeTransit([], source="scheduled_only"), store, ["S1"], None)  # type: ignore[arg-type]
    assert (
        store.poll_stats()["ok_polls"] == 0
    )  # scheduled-only fallback does not count as a good poll


def test_store_roundtrip_and_rain(tmp_path: Path) -> None:
    s = DelayStore(tmp_path / "a.sqlite")
    s.add_obs([obs(0, 8, 0, 60)])
    s.add_rain({3600: 1.5, 7200: 0.0})
    s.add_rain({3600: 2.0})  # replace
    assert len(s.observations()) == 1 and s.rain() == {3600: 2.0, 7200: 0.0}


# --- final rows ---------------------------------------------------------------------------


def test_final_row_is_the_last_observation_before_departure_and_ignores_later_ones() -> None:
    sched = int(BASE.timestamp()) + 8 * 3600
    rows = [
        Obs(sched - 900, "T", "680", "S", "d", sched, sched + 60, 60, "SCHEDULED"),  # 15 min before
        Obs(
            sched - 300, "T", "680", "S", "d", sched, sched + 240, 240, "SCHEDULED"
        ),  # 5 min before
        Obs(sched + 3000, "T", "680", "S", "d", sched, sched + 999, 999, "SCHEDULED"),
    ]  # long after
    (f,) = A.final_rows(rows)
    assert (
        f.delay == 240 and f.known is not None and f.known.delay_s == 60
    )  # known = 10+ min before


def test_cancelled_and_no_data_rows_have_no_delay() -> None:
    fs = A.final_rows([obs(0, 8, 0, None, rel="CANCELED"), obs(0, 9, 0, None, rel="NO_DATA")])
    assert [f.delay for f in fs] == [None, None]


# --- statistics ---------------------------------------------------------------------------


def synthetic(days: int = 14, seed: int = 1) -> list[Obs]:
    rng = random.Random(seed)
    out = []
    for d in range(days):
        for h in (7, 8, 9, 16, 17):
            for m in (0, 20, 40):
                for route, base in (("680", 120), ("687", 0)):
                    out.append(obs(d, h, m, base + rng.randint(-30, 30), route=route))
    return out


def test_distributions_report_route_medians_with_intervals() -> None:
    d = A.distributions(A.final_rows(synthetic()))
    assert d is not None
    by = {r["key"]: r for r in d["route"]}
    assert 100 <= by["680"]["median_s"] <= 140 and -20 <= by["687"]["median_s"] <= 20
    assert by["680"]["median_lo_s"] <= by["680"]["median_s"] <= by["680"]["median_hi_s"]
    assert {r["key"] for r in d["hour"]} == {7, 8, 9, 16, 17}


def test_too_little_data_gives_none_not_an_estimate() -> None:
    few = A.final_rows(synthetic(days=1)[:20])
    assert A.distributions(few) is None and A.cancellations(few) is None


def test_cancellation_rate_with_wilson_interval() -> None:
    rows = synthetic(days=5)
    rows += [obs(5, 8, m, None, rel="CANCELED", trip=f"c{m}") for m in range(10)]
    c = A.cancellations(A.final_rows(rows))
    assert c is not None and c["k"] == 10 and c["lo"] < c["rate"] < c["hi"]


def test_rain_effect_detects_a_planted_difference_and_requires_both_groups() -> None:
    rows, rain = [], {}
    rng = random.Random(3)
    for d in range(6):
        for h in range(6, 20):
            wet = h % 2 == 0
            slot = int((BASE + timedelta(days=d, hours=h + 1)).timestamp())
            rain[slot] = 1.0 if wet else 0.0
            for m in (0, 30):
                rows.append(obs(d, h, m, (200 if wet else 0) + rng.randint(-20, 20)))
    fx = A.rain_effect(A.final_rows(rows), rain)
    assert (
        fx is not None and fx["diff_s"] > 150 and fx["ci_lo_s"] > 0 and "association" in fx["note"]
    )
    assert A.rain_effect(A.final_rows(rows), {}) is None
    assert (
        A.rain_effect(A.final_rows(rows), {k: 0.0 for k in rain}) is None
    )  # never wet: no comparison


# --- the model, judged on a later period --------------------------------------------------


def test_model_needs_enough_days_and_says_how_many() -> None:
    m = A.model_eval(A.final_rows(synthetic(days=3)))
    assert m["available"] is False and "3 day(s)" in m["reason"]


def test_model_beats_baselines_when_there_is_a_route_and_hour_pattern() -> None:
    rng = random.Random(5)
    rows = []
    for d in range(14):
        for h in (7, 8, 9, 16, 17):
            for m in (0, 20, 40):
                for route in ("680", "687"):
                    base = 300 if (route == "680" and h in (8, 17)) else 0  # rush-hour pattern
                    rows.append(obs(d, h, m, base + rng.randint(-20, 20), route=route))
    m = A.model_eval(A.final_rows(rows))
    assert m["available"] and m["beats_baselines"] is True
    assert m["mae_s"]["model"] < m["mae_s"][m["best_baseline"]] and m["gain_ci_s"][0] > 0
    assert m["train_rows"] > m["test_rows"] > 0  # the test period is strictly later


def test_model_does_not_claim_a_win_on_pure_noise() -> None:
    rng = random.Random(7)
    rows = [obs(d, h, m, rng.randint(-60, 60), route=r)
            for d in range(14) for h in (7, 8, 9, 16, 17) for m in (0, 20, 40) for r in ("680", "687")]  # fmt: skip
    m = A.model_eval(A.final_rows(rows))
    assert m["available"] and m["beats_baselines"] is False


def test_the_test_period_never_overlaps_training() -> None:
    m = A.model_eval(A.final_rows(synthetic()))
    assert m["available"]
    assert datetime.fromisoformat(m["train_until"]).date() < (BASE + timedelta(days=13)).date()


def test_solver_handles_singular_systems() -> None:
    assert A._solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]) is None
    assert A._solve([[2.0, 0.0], [0.0, 4.0]], [2.0, 8.0]) == [1.0, 2.0]


def test_summary_on_an_empty_store_is_honest(tmp_path: Path) -> None:
    s = DelayStore(tmp_path / "e.sqlite")
    out = A.summarise(s.observations(), s.rain(), s.poll_stats(), 0.0)
    assert out["distributions"] is None and "at least 50" in out["distributions_reason"]
    assert out["coverage"]["collector_running"] is False and out["model"]["available"] is False


def test_summary_marks_a_recent_poll_as_running() -> None:
    out = A.summarise(
        [], {}, {"polls": 5, "ok_polls": 5, "first_ts": 1000, "last_ts": 1300, "rows": 0}, 1400.0
    )
    assert out["coverage"]["collector_running"] is True and out["coverage"][
        "hours_collected"
    ] == pytest.approx(0.08, abs=0.01)


def test_api_endpoint_serves_the_summary(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.orchestrator_fixtures import build_test_state

    state, _ = build_test_state(tmp_path)
    state.settings = state.settings.model_copy(update={"analytics_db_path": tmp_path / "an.sqlite"})
    body = TestClient(create_app(state)).get("/analytics/summary").json()
    assert body["distributions"] is None and body["coverage"]["rows"] == 0
