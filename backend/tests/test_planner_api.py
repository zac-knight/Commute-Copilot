"""The journey-planner endpoints used by the web UI (places, plan, departures, alerts)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import planner_api
from app.main import create_app
from tests.orchestrator_fixtures import NOW, build_test_state


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    state, _ = build_test_state(tmp_path)
    return TestClient(create_app(state))


def when(hhmm: str = "08:00") -> str:
    return f"{NOW.date().isoformat()}T{hhmm}"


def test_places_collapse_platforms_into_their_station(client: TestClient) -> None:
    hits = client.get("/places", params={"q": "dee"}).json()["places"]
    dee = [p for p in hits if "Dee" in p["name"]]
    assert len(dee) == 1 and dee[0]["id"] == "DSTN" and dee[0]["name"] == "Dee Station"
    assert dee[0]["kind"] in {"station", "stop"} and isinstance(dee[0]["lat"], float)


def test_places_lists_saved_places_first_when_query_is_empty(client: TestClient) -> None:
    hits = client.get("/places").json()["places"]
    assert hits and all(p["kind"] == "saved" for p in hits)
    assert "home" in {p["alias"] for p in hits}


def test_places_geocode_is_opt_in(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake(q: str, limit: int) -> list[dict[str, Any]]:
        calls.append(q)
        return [{"id": "-27.1,153.0", "name": "Mini Golf", "kind": "address", "modes": [],
                 "lat": -27.1, "lon": 153.0}]  # fmt: skip

    monkeypatch.setattr(planner_api, "_geocode", fake)
    assert not any(
        p["kind"] == "address"
        for p in client.get("/places", params={"q": "mini golf"}).json()["places"]
    )
    assert calls == []  # nothing leaves the machine unless asked
    hits = client.get("/places", params={"q": "mini golf", "geocode": 1}).json()["places"]
    assert hits[-1]["kind"] == "address" and calls == ["mini golf"]


def test_plan_returns_a_map_ready_artifact(client: TestClient) -> None:
    r = client.get("/plan", params={"origin": "A", "destination": "C", "when": when()})
    assert r.status_code == 200
    body = r.json()
    art = body["artifact"]
    assert art and art["journeys"] and art["bbox"]
    j = art["journeys"][0]
    assert j["legs"][0]["path"] and j["legs"][0]["depart_iso"]
    assert body["now"].startswith(NOW.date().isoformat())


def test_plan_merges_rounds_without_duplicates(client: TestClient) -> None:
    one = client.get(
        "/plan", params={"origin": "A", "destination": "C", "when": when("00:00"), "rounds": 1}
    ).json()
    many = client.get(
        "/plan", params={"origin": "A", "destination": "C", "when": when("00:00"), "rounds": 4}
    ).json()
    n1, n4 = len(one["artifact"]["journeys"]), len(many["artifact"]["journeys"])
    assert n4 >= n1
    keys = [(j["depart"], j["arrive"], j["transfers"]) for j in many["artifact"]["journeys"]]
    assert len(keys) == len(set(keys))
    deps = [leg["depart_iso"] for j in many["artifact"]["journeys"] for leg in j["legs"][:1]]
    assert deps == sorted(deps)


def test_plan_arrive_by(client: TestClient) -> None:
    r = client.get(
        "/plan", params={"origin": "A", "destination": "C", "when": when("09:00"), "by": "arrive"}
    )
    assert r.status_code == 200


def test_plan_errors_are_typed(client: TestClient) -> None:
    unknown = client.get("/plan", params={"origin": "Nowhere Land", "destination": "C"})
    assert unknown.status_code == 404 and unknown.json()["error"]["type"] == "not_found"
    bad = client.get("/plan", params={"origin": "A", "destination": "C", "when": "tomorrow"})
    assert bad.status_code == 422 and bad.json()["error"]["type"] == "validation_error"
    assert (
        client.get(
            "/plan", params={"origin": "A", "destination": "C", "max_transfers": 9}
        ).status_code
        == 422
    )


def test_departures_and_alerts(client: TestClient) -> None:
    d = client.get("/departures", params={"stop": "A", "limit": 3})
    assert d.status_code == 200 and "departures" in d.json()
    a = client.get("/alerts", params={"routes": "100"})
    assert a.status_code == 200 and "alerts" in a.json()
    assert client.get("/departures", params={"stop": "Nowhere Land"}).status_code == 404


def test_endpoints_report_unavailable_without_timetable(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path, with_transit=False)
    c = TestClient(create_app(state))
    for path, params in [
        ("/places", {"q": "a"}),
        ("/plan", {"origin": "A", "destination": "C"}),
        ("/departures", {"stop": "A"}),
    ]:
        r = c.get(path, params=params)
        assert r.status_code == 503 and r.json()["error"]["type"] == "unavailable"


# ---------------------------------------------------------------- live vehicles


def _vehicle_feed() -> bytes:
    from google.transit import gtfs_realtime_pb2 as pb

    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = 1_000_000_000
    rows = [
        ("v1", "T1", "R1", -27.225, 153.025, 90.0),  # inside the box, on route 100
        ("v2", "TX", "R2", -27.2, 153.04, None),  # inside, a train
        ("v3", "TFAR", "R1", -20.0, 140.0, None),  # far away
    ]
    for i, (vid, trip, route, lat, lon, brg) in enumerate(rows):
        e = feed.entity.add()
        e.id = str(i)
        e.vehicle.vehicle.id = vid
        e.vehicle.vehicle.label = vid.upper()
        e.vehicle.trip.trip_id = trip
        e.vehicle.trip.route_id = route
        e.vehicle.position.latitude = lat
        e.vehicle.position.longitude = lon
        if brg is not None:
            e.vehicle.position.bearing = brg
        e.vehicle.current_status = pb.VehiclePosition.IN_TRANSIT_TO
        e.vehicle.timestamp = 1_000_000_000
    bad = feed.entity.add()  # no position: must be skipped
    bad.id = "bad"
    bad.vehicle.vehicle.id = "nopos"
    return feed.SerializeToString()


@pytest.fixture
def vclient(tmp_path: Path) -> TestClient:
    from app.agents.transit.gtfs_realtime import FeedResult, parse_vehicle_positions

    state, _ = build_test_state(tmp_path)
    snap = parse_vehicle_positions(_vehicle_feed(), 0.0)

    async def vehicles() -> Any:
        return FeedResult(snap, 5.0)

    state.transit.realtime.vehicles = vehicles  # type: ignore[union-attr,attr-defined]
    return TestClient(create_app(state))


def test_vehicles_inside_bbox_with_route_and_mode(vclient: TestClient) -> None:
    r = vclient.get("/vehicles", params={"bbox": "-27.3,152.9,-27.1,153.2"}).json()
    assert r["available"] and r["age_s"] == 5.0
    ids = {v["id"]: v for v in r["vehicles"]}
    assert set(ids) == {"v1", "v2"}  # the far one and the position-less one are out
    assert ids["v1"]["route_name"] == "Alpha - Charlie"
    assert isinstance(ids["v1"]["headsign"], str)
    assert ids["v1"]["route"] == "100" and ids["v1"]["mode"] == "bus" and ids["v1"]["bearing"] == 90
    assert ids["v2"]["mode"] == "train" and ids["v2"]["bearing"] is None


def test_vehicles_on_my_trip_always_included_and_flagged(vclient: TestClient) -> None:
    r = vclient.get("/vehicles", params={"bbox": "-27.3,152.9,-27.1,153.2", "trips": "TFAR"}).json()
    by = {v["id"]: v for v in r["vehicles"]}
    assert by["v3"]["on_trip"] is True and by["v1"]["on_trip"] is False
    assert r["vehicles"][0]["id"] == "v3"  # mine first


def test_vehicles_validates_bbox(vclient: TestClient) -> None:
    assert vclient.get("/vehicles", params={"bbox": "1,2,3"}).status_code == 422
    assert vclient.get("/vehicles", params={"bbox": "-27.1,152.9,-27.3,153.2"}).status_code == 422


def test_trip_path_returns_the_stop_sequence(client: TestClient) -> None:
    r = client.get("/trip-path", params={"trip_id": "T1"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "bus" and body["route"] == "100" and len(body["stops"]) >= 2
    assert {"name", "lat", "lon"} <= set(body["stops"][0])
    assert client.get("/trip-path", params={"trip_id": "nope"}).status_code == 404


def test_plan_legs_follow_the_road_shape_when_available(client: TestClient) -> None:
    class FakeShapes:
        def path_for_trip(self, trip_id: str) -> list[tuple[float, float]] | None:
            if trip_id != "T1":
                return None
            # A -> a detour far east -> B -> C
            return [(-27.23, 153.02), (-27.225, 153.5), (-27.22, 153.03), (-27.21, 153.04)]

    svc = client.app.state.app_state.transit  # type: ignore[attr-defined]
    svc.shapes = FakeShapes()
    r = client.get("/plan", params={"origin": "A", "destination": "C", "when": when()}).json()
    legs = [
        leg for j in r["artifact"]["journeys"] for leg in j["legs"] if leg.get("trip_id") == "T1"
    ]
    assert legs, "expected a leg on trip T1"
    assert any(abs(p[1] - 153.5) < 1e-6 for p in legs[0]["path"])  # followed the shape's detour
    tp = client.get("/trip-path", params={"trip_id": "T1"}).json()
    assert len(tp["path"]) == 4 and len(tp["stops"]) >= 2


def test_plan_via_stops_chains_the_hops_and_marks_the_stopover(client: TestClient) -> None:
    r = client.get(
        "/plan",
        params={"origin": "A", "destination": "C", "via": "B", "when": when("00:00"), "stay": 10},
    )
    assert r.status_code == 200, r.text
    journeys = r.json()["artifact"]["journeys"]
    assert journeys, "expected a journey through B"
    for j in journeys:
        marked = [leg for leg in j["legs"] if leg.get("stopover")]
        assert len(marked) == 1 and marked[0]["stay_min"] >= 10  # waited at the via stop
        assert marked[0]["stopover_index"] == 1
        assert j["legs"][0]["from"] != j["legs"][-1]["to"]


def test_plan_via_validates_and_reports_unknown_stops(client: TestClient) -> None:
    assert (
        client.get(
            "/plan", params={"origin": "A", "destination": "C", "via": "Nowhere Land"}
        ).status_code
        == 404
    )
    too_many = [("origin", "A"), ("destination", "C")] + [("via", "B")] * 4
    assert client.get("/plan", params=too_many).status_code == 422
    assert (
        client.get(
            "/plan", params={"origin": "A", "destination": "C", "via": "B", "stay": 999}
        ).status_code
        == 422
    )


def test_plan_via_arrive_by(client: TestClient) -> None:
    r = client.get(
        "/plan",
        params={
            "origin": "A",
            "destination": "C",
            "via": "B",
            "by": "arrive",
            "when": when("23:00"),
            "stay": 0,
        },
    )
    assert r.status_code == 200, r.text


def test_vehicles_can_be_limited_to_modes(vclient: TestClient) -> None:
    r = vclient.get(
        "/vehicles", params={"bbox": "-27.3,152.9,-27.1,153.2", "modes": "train"}
    ).json()
    assert {v["id"] for v in r["vehicles"]} == {"v2"}


def test_train_lines_returns_one_path_per_pattern(client: TestClient) -> None:
    r = client.get("/train-lines")
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert lines and {"key", "route", "route_name", "path"} <= set(lines[0])
    assert len({line["key"] for line in lines}) == len(lines)  # a line and its reverse count once
    assert all(len(line["path"]) >= 2 for line in lines)
