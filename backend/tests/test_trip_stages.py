"""Multi-stage trips: two buses with a walking transfer, wait times, the map artifact, and the
guarantee that asking for departures TOWARDS a place always yields a map."""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.agents.transit.geo import build_trip_artifact
from app.agents.transit.gtfs_static import GtfsStatic, ingest_zip
from app.agents.transit.planner import JourneyPlanner
from app.agents.transit.service import TransitService
from app.core.memory import UserProfile
from app.core.testing import text_response, tool_use_response
from app.core.timeutil import TZ
from tests.orchestrator_fixtures import build_test_state, plan_response, step
from tests.test_transit_service import FakeRealtime
from tests.transit_fixtures import _csv

# P --bus B1--> Q (arrive 08:20) ; walk Q -> Q2 (~80 m) ; Q2 --bus B2--> R (08:30 -> 08:50)
# There is NO direct service from P to R, so the only way is two buses.
STOPS = [
    ["P", "P", "Pine Road", "", -27.2300, 153.0200, 0, "", ""],
    ["Q", "Q", "Quarry Interchange", "", -27.2000, 153.0400, 0, "", ""],
    ["Q2", "Q2", "Quarry Interchange stop 2", "", -27.1993, 153.0400, 0, "", ""],
    ["R", "R", "Rosewood Plaza", "", -27.1700, 153.0600, 0, "", ""],
]
ROUTES = [["B1", "111", "Pine - Quarry", "", 3], ["B2", "222", "Quarry - Rosewood", "", 3]]
CAL = [["WK", 1, 1, 1, 1, 1, 1, 1, 20261102, 20261130]]
TRIPS = [["B1", "WK", "X1", "Quarry", 0], ["B2", "WK", "X2", "Rosewood", 0]]
ST = [
    ["X1", "P", "08:00:00", "08:00:00", 1, 0, 0],
    ["X1", "Q", "08:20:00", "08:20:00", 2, 1, 0],
    ["X2", "Q2", "08:30:00", "08:30:00", 1, 0, 0],
    ["X2", "R", "08:50:00", "08:50:00", 2, 1, 0],
]
NOW = datetime(2026, 11, 9, 7, 50, tzinfo=TZ)


def two_bus_service(tmp: Path) -> TransitService:
    zp = tmp / "two.zip"
    files = {
        "stops.txt": _csv(
            [
                "stop_id",
                "stop_code",
                "stop_name",
                "stop_desc",
                "stop_lat",
                "stop_lon",
                "location_type",
                "parent_station",
                "platform_code",
            ],
            STOPS,
        ),
        "routes.txt": _csv(
            ["route_id", "route_short_name", "route_long_name", "route_desc", "route_type"], ROUTES
        ),
        "calendar.txt": _csv(
            [
                "service_id",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                "start_date",
                "end_date",
            ],
            CAL,
        ),
        "calendar_dates.txt": _csv(["service_id", "date", "exception_type"], []),
        "trips.txt": _csv(
            ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id"], TRIPS
        ),
        "stop_times.txt": _csv(
            [
                "trip_id",
                "stop_id",
                "arrival_time",
                "departure_time",
                "stop_sequence",
                "pickup_type",
                "drop_off_type",
            ],
            ST,
        ),
        "feed_info.txt": _csv(
            ["feed_publisher_name", "feed_start_date", "feed_end_date"], [["t", 20261102, 20261130]]
        ),
    }
    with zipfile.ZipFile(zp, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    db = tmp / "two.sqlite"
    ingest_zip(zp, db, meta={"etag": '"v"'})
    static = GtfsStatic(db)
    return TransitService(
        static,
        FakeRealtime(),
        JourneyPlanner(static),
        UserProfile(home="Pine Road"),
        clock=lambda: NOW,
    )  # type: ignore[arg-type]


async def test_the_planner_chains_two_buses_with_a_walking_transfer(tmp_path: Path) -> None:
    svc = two_bus_service(tmp_path)
    res = await svc.plan_trip("Pine Road", "Rosewood Plaza")
    (j,) = res.journeys
    assert [leg.kind for leg in j.legs] == ["transit", "walk", "transit"]
    assert [leg.route for leg in j.legs if leg.kind == "transit"] == ["111", "222"]
    assert j.transfers == 1 and j.arrive == "08:50"


async def test_next_departures_towards_a_place_with_no_direct_bus_offers_the_two_bus_option(
    tmp_path: Path,
) -> None:
    svc = two_bus_service(tmp_path)
    res = await svc.next_departures("Pine Road", limit=3, towards="Rosewood Plaza", modes=["bus"])
    assert res.departures == [] or all(d.route != "222" for d in res.departures)
    assert len(res.connections) == 1 and res.connections[0].transfers == 1
    assert res.note and "transfers" in res.note


async def test_the_map_artifact_numbers_stages_and_measures_the_wait(tmp_path: Path) -> None:
    svc = two_bus_service(tmp_path)
    res = await svc.plan_trip("Pine Road", "Rosewood Plaza")
    art = build_trip_artifact(svc.static, res.model_dump(mode="json"))
    assert art is not None
    j = art["journeys"][0]
    s = j["summary"]
    assert (s["stages"], s["all_bus"], s["wait_min"]) == (
        2,
        True,
        9,
    )  # off the bus 08:20, walk 1 min, next bus 08:30
    assert [leg.get("stage") for leg in j["legs"]] == [1, None, 2]
    assert [leg.get("wait_before_min") for leg in j["legs"]][2] == 9
    bus1, walk, bus2 = j["legs"]
    assert (
        3000 < bus1["distance_m"] < 4000 and 60 < walk["distance_m"] < 120
    )  # great-circle P->Q, ~80 m walk
    assert s["total_m"] == bus1["distance_m"] + walk["distance_m"] + bus2["distance_m"]
    assert (
        s["by_mode_m"] == {"bus": bus1["distance_m"] + bus2["distance_m"]}
        and s["walk_m"] == walk["distance_m"]
    )
    assert all(len(leg["path"]) >= 2 for leg in j["legs"]) and art["context_stops"]


def test_a_plan_with_no_journeys_has_no_artifact(tmp_path: Path) -> None:
    svc = two_bus_service(tmp_path)
    assert build_trip_artifact(svc.static, {"journeys": []}) is None


async def test_departures_towards_a_place_always_produce_a_map_event(tmp_path: Path) -> None:
    """The model only called next_departures (no plan_trip); the orchestrator still computes the
    journey in code, so the Result pane is never empty for a trip question."""
    state, llm = build_test_state(tmp_path)
    llm.script("router", plan_response("transit", [step("s1", "transit", "buses from A to C")]))
    llm.script(
        "transit",
        tool_use_response([("next_departures", {"stop_id": "A", "towards": "C", "limit": 1})]),
        text_response("Bus at 08:00."),
    )
    llm.script("synth", text_response("Bus at 08:00."))
    events: list[Any] = [
        e async for e in state.orchestrator.run("s", "buses from A to C", None, False, True)
    ]
    trips = [e for e in events if e.type == "trip"]
    assert trips and trips[0].artifact["journeys"][0]["legs"]
    assert [e.type for e in events][-1] == "final"
    # and not for clients that did not ask for detail
    (tmp_path / "b").mkdir()
    state2, llm2 = build_test_state(tmp_path / "b")
    llm2.script("router", plan_response("transit", [step("s1", "transit", "buses from A to C")]))
    llm2.script(
        "transit",
        tool_use_response([("next_departures", {"stop_id": "A", "towards": "C"})]),
        text_response("ok"),
    )
    llm2.script("synth", text_response("ok"))
    assert not [e async for e in state2.orchestrator.run("s", "x") if e.type == "trip"]


@pytest.mark.parametrize("bad", [{}, {"journeys": None}])
def test_garbage_plans_do_not_crash_the_builder(tmp_path: Path, bad: dict[str, Any]) -> None:
    assert build_trip_artifact(two_bus_service(tmp_path).static, bad) is None
