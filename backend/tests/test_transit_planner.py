from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.agents.transit.gtfs_static import GtfsStatic
from app.agents.transit.planner import JourneyPlanner, StopGrid
from app.core.timeutil import TZ
from tests.transit_fixtures import MON, build_static


@pytest.fixture
def static(tmp_path: Path) -> GtfsStatic:
    return build_static(tmp_path)


def at(h: int, m: int, d: date = MON) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


A = {"A": 0}
DEE = {"D1": 0, "D2": 0}  # both platforms of Dee Station


def hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def test_transfer_with_walk_and_direct_are_both_offered_fastest_first(static: GtfsStatic) -> None:
    js = JourneyPlanner(static).plan(A, DEE, at(7, 50))
    assert [(hm(j.depart), hm(j.arrive), j.transfers) for j in js] == [
        ("08:00", "08:45", 1),  # T1 + walk C->C2 + T3
        ("08:05", "09:30", 0),  # direct 200: slower, but no transfer
    ]
    legs = js[0].legs
    assert [leg.kind for leg in legs] == ["transit", "walk", "transit"]
    assert (legs[0].route_short_name, legs[0].from_stop, legs[0].to_stop) == ("100", "A", "C")
    assert legs[0].num_stops == 2 and legs[0].headsign == "Charlie Plaza"
    assert legs[1].from_stop == "C" and legs[1].to_stop == "C2" and 90 <= legs[1].walk_m <= 110
    assert (hm(legs[1].depart), (legs[1].arrive - legs[1].depart).seconds) == (
        "08:20",
        75,
    )  # 100m @ 80 m/min
    assert (legs[2].route_short_name, legs[2].to_stop) == ("RAIL", "D1")
    assert legs[2].trip_id == "T3" and legs[2].service_date == MON


def test_max_transfers_zero_leaves_only_the_direct_service(static: GtfsStatic) -> None:
    js = JourneyPlanner(static).plan(A, DEE, at(7, 50), max_transfers=0)
    assert [(j.legs[0].route_short_name, hm(j.arrive)) for j in js] == [("200", "09:30")]


def test_walking_radius_is_configurable(static: GtfsStatic) -> None:
    js = JourneyPlanner(static, transfer_radius_m=50).plan(A, DEE, at(7, 50))
    assert [j.transfers for j in js] == [0]  # C and C2 (100 m apart) are no longer linked


def test_slower_walking_can_miss_the_connection(static: GtfsStatic) -> None:
    # arrive C 08:20; at 1 m/min the 100 m walk takes 100 min, so T3 (08:25) is missed
    js = JourneyPlanner(static, walk_speed_m_per_min=1).plan(A, DEE, at(7, 50))
    assert [j.transfers for j in js] == [0]


def test_departing_later_takes_the_next_trip(static: GtfsStatic) -> None:
    js = JourneyPlanner(static).plan(A, {"C": 0}, at(8, 1))
    assert [(hm(j.depart), hm(j.arrive)) for j in js] == [("08:30", "08:50")]  # T1 already left


def test_nothing_reachable_returns_empty(static: GtfsStatic) -> None:
    p = JourneyPlanner(static)
    assert p.plan(A, DEE, at(12, 0)) == []  # no service
    assert p.plan({"E": 0}, DEE, at(7, 50)) == []  # unserved stop
    assert p.plan({}, DEE, at(7, 50)) == []
    assert p.plan(A, {"nope": 0}, at(7, 50)) == []
    assert p.plan(A, DEE, at(7, 50, date(2026, 11, 11))) == []  # service removed that day


def test_after_midnight_trips_and_service_day(static: GtfsStatic) -> None:
    # Monday service: T4 leaves A 23:50, reaches C at 24:20 (= Tue 00:20); T5 leaves C2 at 24:30.
    (j,) = JourneyPlanner(static).plan(A, DEE, at(23, 45))
    assert j.depart == at(23, 50)
    assert j.arrive == datetime(2026, 11, 10, 0, 50, tzinfo=TZ)
    assert [leg.service_date for leg in j.legs if leg.kind == "transit"] == [MON, MON]


def test_planning_just_after_midnight_uses_yesterdays_service(static: GtfsStatic) -> None:
    tue = date(2026, 11, 10)
    js = JourneyPlanner(static).plan({"C2": 0}, DEE, at(0, 10, tue))
    assert [(hm(j.depart), hm(j.arrive)) for j in js] == [("00:30", "00:50")]
    assert js[0].legs[0].service_date == MON  # Monday's 24:30 departure


def test_origin_walk_offset_delays_the_start(static: GtfsStatic) -> None:
    # 20 minutes of walking to stop A means the 08:00 and 08:05 trips from 07:50 are missed
    js = JourneyPlanner(static).plan({"A": 20 * 60}, {"C": 0}, at(7, 50))
    assert [hm(j.depart) for j in js] == [("08:30")]


def test_arrive_by_chooses_latest_feasible_departure(static: GtfsStatic) -> None:
    p = JourneyPlanner(static)
    (j,) = p.plan_arrive_by(A, {"C": 0}, at(8, 55))
    assert (hm(j.depart), hm(j.arrive)) == ("08:30", "08:50")  # latest that still arrives in time
    (j,) = p.plan_arrive_by(A, {"C": 0}, at(8, 25))
    assert (hm(j.depart), hm(j.arrive)) == ("08:00", "08:20")
    assert p.plan_arrive_by(A, {"C": 0}, at(8, 10)) == []  # cannot arrive by 08:10


def test_arrive_by_with_transfer(static: GtfsStatic) -> None:
    p = JourneyPlanner(static)
    js = p.plan_arrive_by(A, {"D1": 0}, at(9, 0))
    assert js and js[0].arrive <= at(9, 0) and js[0].transfers == 1
    assert p.plan_arrive_by(A, {"D1": 0}, at(8, 40)) == []  # earliest possible arrival is 08:45


def test_stop_grid_within() -> None:
    from app.agents.transit.gtfs_static import Stop

    def s(i: str, lat: float) -> Stop:
        return Stop(i, i, i, lat, 153.0, 0, None, None, ("bus",))

    grid = StopGrid([s("a", -27.2000), s("b", -27.2009), s("c", -27.2500)])
    near = {st.stop_id for st, _ in grid.within(-27.2000, 153.0, 150)}
    assert near == {"a", "b"}
