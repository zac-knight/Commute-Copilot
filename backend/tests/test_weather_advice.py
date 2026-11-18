from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.agents.weather.advice import (
    RoutePlan,
    Thresholds,
    WeatherStatus,
    _build_windows,
    _round,
    commute_advice,
    rain_level,
    slots_for,
)
from app.agents.weather.client import HourPoint
from tests.weather_fixtures import NOW, at, forecast, leg, simple_plan

TH = Thresholds()
# Slot index = hour of day on 2026-11-09. Slot T covers the PRECEDING hour (T-1h, T].
ACCESS, EGRESS = (
    8,
    9,
)  # for simple_plan: access window 07:53-08:00 -> slot 08:00; egress 08:30-08:35 -> slot 09:00
WET = {"rain_prob_pct": 80.0, "rain_mm": 2.0, "weather_code": 63}


def advise(plan: RoutePlan | None = None, fc: Any = "default", now: Any = NOW, **kw: Any) -> Any:
    return commute_advice(
        plan or simple_plan(status="on_time", delay=0.0),
        forecast() if fc == "default" else fc,
        now,
        **kw,
    )


# --- slot mapping: the 'preceding hour' semantics -----------------------------------------


def test_slots_cover_the_preceding_hour() -> None:
    fc = forecast()
    got = lambda s, e: [h.time.hour for h in slots_for(fc, s, e)]  # noqa: E731
    assert got(at(7, 53), at(8, 0)) == [8]  # 07:53-08:00 lies in (07:00, 08:00]
    assert got(at(8, 30), at(8, 35)) == [9]  # lies in (08:00, 09:00]
    assert got(at(7, 55), at(8, 5)) == [8, 9]  # straddles the hour mark
    assert got(at(8, 0), at(8, 30)) == [9]  # starting exactly at 08:00: slot 08:00 is before it
    assert got(at(7, 0), at(7, 59)) == [
        8
    ]  # ends before 08:00: slot 08:00 covers it, 07:00 does not
    assert got(at(8, 5), at(10, 20)) == [9, 10, 11]


# --- rain level boundaries ----------------------------------------------------------------


def hp(prob: float | None = 0.0, mm: float | None = 0.0, code: int | None = 1) -> HourPoint:
    return HourPoint(at(8), 22, 23, prob, mm, code, 10, 20, 0)


@pytest.mark.parametrize(
    ("prob", "mm", "code", "level"),
    [
        (0, 0.0, 1, 0),
        (29.9, 0.49, 1, 0),
        (5, 0.1, 51, 0),  # trace drizzle is not rain
        (30, 0.0, 1, 1),
        (0, 0.5, 1, 1),
        (59.9, 1.49, 3, 1),
        (60, 0.0, 1, 2),
        (0, 1.5, 1, 2),
        (80, 4.9, 63, 2),
        (0, 5.0, 65, 3),
        (10, 0.0, 95, 3),
        (0, 0.0, 99, 3),
        (None, None, None, 0),
    ],
)
def test_rain_levels(prob: float, mm: float, code: int, level: int) -> None:
    assert rain_level(hp(prob, mm, code), TH) == level


# --- basic outcomes -----------------------------------------------------------------------


def test_clear_day_just_go() -> None:
    a = advise()
    assert a.leave_now is True and a.bring == []
    assert a.hazards.blocking_level == 0 and a.hazards.slots_used == 2
    assert "No significant rain expected while you are outside." in a.reasons
    assert a.weather.source == "live" and a.confidence == 0.90  # realtime present, near-term


def test_rain_while_walking_to_the_stop_waits_for_it_to_pass() -> None:
    a = advise(fc=forecast({ACCESS: WET}))
    assert a.leave_now is False
    assert "umbrella" in a.bring
    assert a.hazards.rain_level == 2 and a.hazards.rain_prob_pct == 80.0
    # shifted 15 min the windows use the dry 09:00 slot: 07:53 + 15 = 08:08, rounded up to 08:10
    assert a.suggested_leave_time == "08:10"
    assert any("Rain is likely" in r and "walking to the first stop" in r for r in a.reasons)
    assert any("Waiting about 15 min" in r for r in a.reasons)
    assert any("equivalent later service" in x for x in a.assumptions)


def test_rain_only_while_riding_does_not_matter() -> None:
    # ride 08:00-09:40; the wet slot 09:00 (08:00-09:00) is only ever under the vehicle roof
    plan = simple_plan(at(8, 0), at(9, 40), status="on_time", delay=0.0)
    a = advise(plan, forecast({9: WET}))
    assert a.leave_now is True and a.bring == [] and a.hazards.rain_level == 0


def test_persistent_rain_means_go_prepared_not_wait() -> None:
    fc = forecast(defaults_for_day(**WET))
    a = advise(fc=fc)
    assert a.leave_now is True and "umbrella" in a.bring
    assert any("would not help" in r for r in a.reasons)
    assert a.suggested_leave_time == "07:50"  # leave now (minute-truncated now)


def defaults_for_day(**kw: Any) -> dict[int, dict[str, Any]]:
    return {i: dict(kw) for i in range(72)}


def test_possible_showers_bring_umbrella_but_do_not_delay() -> None:
    a = advise(fc=forecast({ACCESS: {"rain_prob_pct": 40.0, "rain_mm": 0.2}}))
    assert a.leave_now is True and a.bring == ["umbrella"]
    assert a.hazards.rain_level == 1 and a.hazards.blocking_level < 2  # minor: not worth waiting
    assert any("Showers are possible" in r for r in a.reasons)


def test_thunderstorm_is_blocking_and_called_out() -> None:
    a = advise(fc=forecast({ACCESS: {"weather_code": 95, "rain_prob_pct": 90.0, "rain_mm": 6.0}}))
    assert a.hazards.thunderstorm and a.hazards.rain_level == 3 and a.leave_now is False
    assert any("Thunderstorm" in r for r in a.reasons) and "umbrella" in a.bring


def test_very_short_dash_ignores_rain_but_not_storms() -> None:
    quick = replace(TH, access_walk_min=1, wait_buffer_min=0, egress_walk_min=1)  # 2 min outside
    wet = forecast({ACCESS: WET, EGRESS: WET})
    assert advise(fc=wet, thresholds=quick).bring == []
    storm = forecast({ACCESS: {"weather_code": 95}, EGRESS: {"weather_code": 95}})
    assert advise(fc=storm, thresholds=quick).hazards.thunderstorm


# --- heat, UV, wind, cold -----------------------------------------------------------------


def test_hot_day_bring_water_but_still_go() -> None:
    a = advise(fc=forecast({ACCESS: {"apparent_c": 33.0}, EGRESS: {"apparent_c": 34.0}}))
    assert a.hazards.heat_level == 1 and a.leave_now is True and "water" in a.bring
    assert any("34°C" in r for r in a.reasons)


def test_extreme_heat_is_blocking_and_suggests_waiting_for_cooler() -> None:
    a = advise(fc=forecast({ACCESS: {"apparent_c": 38.0}}))  # only the access slot is extreme
    assert a.hazards.heat_level == 2 and a.leave_now is False and a.suggested_leave_time == "08:10"


def test_uv_needs_enough_time_outside() -> None:
    sunny = forecast({ACCESS: {"uv_index": 9.0}, EGRESS: {"uv_index": 9.0}})
    a = advise(fc=sunny)
    assert "sunscreen and a hat" in a.bring and any("UV index up to 9" in r for r in a.reasons)
    quick = replace(TH, access_walk_min=2, wait_buffer_min=0, egress_walk_min=2)  # 4 min < 10
    assert "sunscreen and a hat" not in advise(fc=sunny, thresholds=quick).bring
    assert "sunscreen and a hat" not in advise(fc=forecast({ACCESS: {"uv_index": 5.9}})).bring


def test_wind_swaps_umbrella_for_jacket_and_severe_gusts_block() -> None:
    windy_rain = forecast({ACCESS: {"rain_prob_pct": 40.0, "rain_mm": 0.3, "gust_kmh": 50.0}})
    a = advise(fc=windy_rain)
    assert a.bring == ["rain jacket (too windy for an umbrella)"] and a.hazards.wind_level == 1
    severe = advise(fc=forecast({ACCESS: {"gust_kmh": 70.0}}))
    assert severe.hazards.wind_level == 2 and severe.leave_now is False
    assert any("Strong gusts up to 70 km/h" in r for r in severe.reasons)


def test_cool_morning_suggests_a_jacket() -> None:
    a = advise(fc=forecast({ACCESS: {"apparent_c": 10.0}}))
    assert a.bring == ["light jacket"] and any("feel cool" in r for r in a.reasons)


def test_boundaries_are_inclusive() -> None:
    assert advise(fc=forecast({ACCESS: {"apparent_c": 32.0}})).hazards.heat_level == 1
    assert advise(fc=forecast({ACCESS: {"apparent_c": 31.9}})).hazards.heat_level == 0
    assert advise(fc=forecast({ACCESS: {"gust_kmh": 45.0}})).hazards.wind_level == 1
    assert advise(fc=forecast({ACCESS: {"apparent_c": 12.0}})).bring == ["light jacket"]
    assert advise(fc=forecast({ACCESS: {"uv_index": 6.0}})).bring == ["sunscreen and a hat"]


# --- timing -------------------------------------------------------------------------------


def test_future_trip_says_when_to_head_out_not_now() -> None:
    plan = simple_plan(at(10, 0), at(10, 30), status="on_time", delay=0.0)
    a = advise(plan)
    assert (
        a.leave_now is False and a.suggested_leave_time == "09:50"
    )  # 10:00 - 7 min = 09:53 -> floor 09:50
    assert a.reasons[0].startswith("Your first service leaves around 10:00")


def test_about_to_miss_it_means_go_now() -> None:
    plan = simple_plan(at(7, 52), at(8, 20), status="on_time", delay=0.0)  # leave_by 07:45 is past
    a = advise(plan)
    assert a.leave_now is True and a.suggested_leave_time == "07:50"


def test_first_leg_delay_moves_the_departure_and_is_explained() -> None:
    plan = simple_plan(at(8, 0), at(8, 30), status="delayed", delay=12.0)
    a = advise(plan)
    # effective departure 08:12 -> leave_by 08:05, i.e. 15 min from now: not 'now'
    assert a.leave_now is False and a.suggested_leave_time == "08:05"
    assert any("12 min late" in r for r in a.reasons)


def test_delay_shifts_the_exposure_window_into_a_different_forecast_hour() -> None:
    # 08:00 delayed 70 min -> boards 09:10, access window 09:03-09:10 -> slot 10:00 (wet)
    plan = simple_plan(at(8, 0), at(9, 30), status="delayed", delay=70.0)
    a = advise(plan, forecast({10: WET}))
    assert a.hazards.rain_level == 2 and "umbrella" in a.bring


def test_early_service_warning() -> None:
    plan = simple_plan(at(8, 0), at(8, 30), status="early", delay=-6.0)
    assert any("6 min early" in r for r in advise(plan).reasons)


def test_cancelled_leg_invalidates_the_plan() -> None:
    plan = RoutePlan(
        legs=[
            leg("transit", at(8, 0), at(8, 20), route="680", status="on_time", delay=0.0),
            leg("transit", at(8, 30), at(8, 50), route="RAIL", status="cancelled"),
        ]
    )
    a = advise(plan)
    assert a.leave_now is False and a.suggested_leave_time is None and a.confidence == 0.4
    assert a.bring == [] and "cancelled" in a.reasons[0] and "RAIL" in a.reasons[0]


def test_late_connecting_leg_is_flagged() -> None:
    plan = RoutePlan(
        legs=[
            leg("transit", at(8, 0), at(8, 20), route="680", status="on_time", delay=0.0),
            leg("transit", at(8, 30), at(8, 50), route="RAIL", status="delayed", delay=11.0),
        ]
    )
    assert any("RAIL leg is running 11 min late" in r for r in advise(plan).reasons)


# --- exposure windows ---------------------------------------------------------------------


def multi_leg_plan() -> RoutePlan:
    return RoutePlan(
        legs=[
            leg("transit", at(8, 0), at(8, 20), route="680", status="on_time", delay=0.0),
            leg("walk", at(8, 20), at(8, 22), walk_m=150),
            leg("transit", at(8, 40), at(9, 10), route="RAIL", status="on_time", delay=0.0),
        ]
    )


def test_windows_cover_access_walks_waits_and_egress_only() -> None:
    windows, leave_by = _build_windows(multi_leg_plan(), at(8, 0), TH)
    assert [(w.label, w.start, w.end) for w in windows] == [
        ("walking to the first stop", at(7, 53), at(8, 0)),
        ("walking between stops", at(8, 20), at(8, 22)),
        ("waiting at a stop to transfer", at(8, 22), at(8, 40)),
        ("walking from the last stop", at(9, 10), at(9, 15)),
    ]
    assert leave_by == at(7, 53)


def test_rain_during_a_long_transfer_wait_is_caught() -> None:
    # the 18 min wait 08:22-08:40 falls in slot 09:00 (08-09); the ride alone would not count
    a = advise(multi_leg_plan(), forecast({9: {"rain_prob_pct": 70.0, "rain_mm": 1.0}}))
    assert a.hazards.rain_level == 2 and "umbrella" in a.bring
    assert any("waiting at a stop to transfer" in r for r in a.reasons)


def test_plan_that_starts_with_a_walk() -> None:
    plan = RoutePlan(
        legs=[
            leg("walk", at(7, 55), at(8, 0), walk_m=400),
            leg("transit", at(8, 5), at(8, 30), route="680", status="on_time", delay=0.0),
        ]
    )
    windows, leave_by = _build_windows(plan, at(8, 5), TH)
    assert leave_by == at(7, 55) and windows[0].label == "walking between stops"


# --- degraded inputs ----------------------------------------------------------------------


def test_weather_unavailable_still_gives_schedule_advice() -> None:
    a = advise(fc=None)
    assert a.leave_now is True and a.bring == [] and a.confidence == 0.25
    assert a.weather.source == "unavailable"
    assert any("Weather data is unavailable" in r for r in a.reasons)
    assert a.hazards.slots_used == 0


def test_forecast_that_does_not_cover_the_trip_is_treated_as_missing() -> None:
    plan = simple_plan(at(8, 0, day=5), at(8, 30, day=5), status="on_time", delay=0.0)  # 5 days out
    a = advise(plan, forecast(days=3))
    assert a.confidence == 0.25 and "does not cover" in (a.weather.note or "")


def test_stale_weather_lowers_confidence_and_is_reported() -> None:
    st = WeatherStatus(source="live", data_age_s=2400, stale=True, note="Refresh failed (x).")
    a = advise(weather=st)
    assert a.confidence == 0.75 and a.weather.stale and a.weather.data_age_s == 2400


def test_confidence_factors() -> None:
    base = advise().confidence
    assert base == 0.90
    # timetable-only transit (no realtime on any leg): -0.10
    assert advise(simple_plan()).confidence == 0.80
    # uncertain 30-70 % rain: -0.15
    assert advise(fc=forecast({ACCESS: {"rain_prob_pct": 50.0}})).confidence == 0.75
    # forecast lead time > 12 h (-0.10) and > 24 h (-0.20)
    p13 = simple_plan(at(21, 0), at(21, 30), status="on_time", delay=0.0)
    p26 = simple_plan(at(10, 0, day=1), at(10, 30, day=1), status="on_time", delay=0.0)
    assert advise(p13).confidence == 0.80
    assert advise(p26).confidence == 0.70
    # clamped
    st = WeatherStatus(source="live", stale=True)
    low = advise(
        simple_plan(at(10, 0, day=1), at(10, 30, day=1)), forecast({ACCESS: {}}), weather=st
    )
    assert 0.10 <= low.confidence <= 0.95


# --- general properties -------------------------------------------------------------------


def test_deterministic_and_does_not_mutate_inputs() -> None:
    plan, fc = simple_plan(status="on_time", delay=0.0), forecast({ACCESS: WET})
    before = plan.model_dump()
    a1, a2 = commute_advice(plan, fc, NOW), commute_advice(plan, fc, NOW)
    assert a1 == a2 and plan.model_dump() == before


def test_thresholds_are_configurable() -> None:
    fc = forecast({ACCESS: {"rain_prob_pct": 65.0, "rain_mm": 0.4}})
    assert advise(fc=fc).hazards.rain_level == 2
    strict = replace(TH, rain_likely_prob_pct=90.0)
    assert advise(fc=fc, thresholds=strict).hazards.rain_level == 1


def test_output_has_the_documented_shape() -> None:
    a = advise(fc=forecast({ACCESS: WET}))
    d = a.model_dump()
    assert {"leave_now", "suggested_leave_time", "reasons", "bring", "confidence"} <= set(d)
    assert (
        isinstance(d["leave_now"], bool)
        and isinstance(d["reasons"], list)
        and 0 <= d["confidence"] <= 1
    )
    assert a.suggested_leave_time_iso and a.suggested_leave_time_iso.startswith("2026-11-09T08:10")


@pytest.mark.parametrize(
    ("minute", "up", "expected"),
    [(0, True, 0), (3, True, 5), (3, False, 0), (7, False, 5), (58, True, 60), (55, False, 55)],
)
def test_rounding(minute: int, up: bool, expected: int) -> None:
    from datetime import timedelta

    out = _round(at(8, 0) + timedelta(minutes=minute, seconds=20), 5, up=up)
    assert out == at(8, 0) + timedelta(minutes=expected)
