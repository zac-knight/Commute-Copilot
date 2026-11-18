from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.agents.transit.gtfs_realtime import (
    RealtimeClient,
    StopTimeUpdate,
    TripRT,
    parse_alerts,
    parse_trip_updates,
    resolve_stop_departure,
)
from tests.transit_fixtures import MON, alert_feed, epoch, trip_update_feed

SCHED = epoch(MON, "08:10:00")  # scheduled departure at stop_sequence 2


def trip(updates: list[StopTimeUpdate], rel: str = "SCHEDULED") -> TripRT:
    return TripRT("T1", MON, "R1", rel, tuple(updates))


def upd(seq: int | None, **kw: object) -> StopTimeUpdate:
    return StopTimeUpdate(seq, str(kw.pop("stop_id", "B")), str(kw.pop("rel", "SCHEDULED")), **kw)  # type: ignore[arg-type]


# --- delay semantics --------------------------------------------------------------------


def test_explicit_delay_in_seconds() -> None:
    t = trip([upd(2, departure_delay=180)])
    r = resolve_stop_departure(t, 2, "B", SCHED)
    assert (r.kind, r.delay_s, r.predicted_epoch) == ("explicit", 180, SCHED + 180)


def test_time_takes_precedence_over_delay() -> None:
    # Replacement-style: delay field is relative to another timetable; absolute time wins.
    t = trip([upd(2, departure_delay=60, departure_time=SCHED + 600)])
    r = resolve_stop_departure(t, 2, "B", SCHED)
    assert (r.delay_s, r.predicted_epoch) == (600, SCHED + 600)


def test_departure_preferred_then_arrival_fallback() -> None:
    assert resolve_stop_departure(trip([upd(2, arrival_delay=30)]), 2, "B", SCHED).delay_s == 30
    both = trip([upd(2, arrival_delay=30, departure_delay=90)])
    assert resolve_stop_departure(both, 2, "B", SCHED).delay_s == 90


def test_early_is_negative() -> None:
    assert (
        resolve_stop_departure(trip([upd(2, departure_delay=-120)]), 2, "B", SCHED).delay_s == -120
    )


def test_delay_propagates_from_closest_preceding_update() -> None:
    t = trip([upd(1, stop_id="A", departure_delay=60), upd(2, stop_id="B", departure_delay=240)])
    r = resolve_stop_departure(t, 4, "X", SCHED)
    assert (r.kind, r.delay_s) == ("propagated", 240)  # last update before seq 4, not seq 1


def test_no_propagation_backwards_or_without_updates() -> None:
    t = trip([upd(5, stop_id="Z", departure_delay=300)])
    assert resolve_stop_departure(t, 2, "B", SCHED).kind == "no_data"  # stop before any update
    assert resolve_stop_departure(trip([]), 2, "B", SCHED).kind == "no_data"


def test_skipped_update_is_not_a_delay_source() -> None:
    t = trip([upd(1, stop_id="A", departure_delay=120), upd(2, stop_id="B", rel="SKIPPED")])
    assert resolve_stop_departure(t, 2, "B", SCHED).kind == "skipped"
    # a later stop must propagate from seq 1, skipping over the SKIPPED seq 2
    assert resolve_stop_departure(t, 3, "C", SCHED).delay_s == 120


def test_no_data_update_and_empty_event() -> None:
    assert resolve_stop_departure(trip([upd(2, rel="NO_DATA")]), 2, "B", SCHED).kind == "no_data"
    assert resolve_stop_departure(trip([upd(2)]), 2, "B", SCHED).kind == "no_data"


def test_cancelled_trip() -> None:
    t = trip([upd(2, departure_delay=0)], rel="CANCELED")
    assert resolve_stop_departure(t, 2, "B", SCHED).kind == "cancelled"
    assert resolve_stop_departure(trip([], rel="DELETED"), 2, "B", SCHED).kind == "cancelled"


def test_match_by_stop_id_when_sequences_differ_but_only_if_unique() -> None:
    t = trip(
        [upd(10, stop_id="B", departure_delay=75)]
    )  # replacement run numbers stops differently
    assert resolve_stop_departure(t, 2, "B", SCHED).delay_s == 75
    ambiguous = trip(
        [upd(10, stop_id="B", departure_delay=75), upd(11, stop_id="B", departure_delay=99)]
    )
    assert resolve_stop_departure(ambiguous, 2, "B", SCHED).kind == "no_data"


# --- parsing ----------------------------------------------------------------------------


def test_parse_trip_updates_keys_by_trip_and_start_date() -> None:
    data = trip_update_feed(
        [
            {
                "trip_id": "T1",
                "start_date": "20261109",
                "updates": [{"seq": 2, "stop_id": "B", "dep_delay": 120, "dep_time": SCHED + 120}],
            },
            {"trip_id": "T1", "start_date": "20261110", "rel": "CANCELED"},
            {
                "trip_id": "X9",
                "start_date": "20261109",
                "rel": "ADDED",
                "route_id": "R2",
                "updates": [{"seq": 1, "stop_id": "C2", "dep_time": SCHED}],
            },
        ],
        timestamp=1234,
    )
    snap = parse_trip_updates(data, fetched_at=1240.0)
    assert snap.feed_timestamp == 1234 and len(snap.trips) == 3
    mon = snap.find("T1", MON)
    assert mon and mon.relationship == "SCHEDULED" and mon.updates[0].departure_delay == 120
    tue = snap.find("T1", date(2026, 11, 10))
    assert tue and tue.relationship == "CANCELED"  # same trip_id, other date: different record
    assert snap.find("T1", date(2026, 11, 11)) is None
    assert [t.trip_id for t in snap.added_trips()] == ["X9"]


def test_missing_events_parse_as_none() -> None:
    data = trip_update_feed(
        [
            {
                "trip_id": "T1",
                "start_date": "20261109",
                "updates": [{"seq": 1, "stop_id": "A", "dep_delay": 0}],
            }
        ]
    )
    u = parse_trip_updates(data, 0.0).find("T1", MON).updates[0]  # type: ignore[union-attr]
    assert (u.departure_delay, u.departure_time, u.arrival_delay, u.arrival_time) == (
        0,
        None,
        None,
        None,
    )


def test_alerts_parse_language_periods_and_entities() -> None:
    data = alert_feed(
        [
            {
                "id": "a1",
                "translations": {"fr": "bonjour", "en-AU": "hello"},
                "desc": "d",
                "periods": [(100, 200)],
                "routes": ["R1"],
                "stops": ["A"],
                "effect": "DETOUR",
            },
            {"id": "a2", "header": "always on", "periods": []},
            {"id": "a3", "header": "open ended", "periods": [(500, None)]},
        ]
    )
    snap = parse_alerts(data, 0.0)
    a1, a2, a3 = snap.alerts
    assert a1.header == "hello" and a1.effect == "DETOUR"
    assert a1.route_ids == {"R1"} and a1.stop_ids == {"A"}
    assert a1.active_at(150) and not a1.active_at(99) and not a1.active_at(201)
    assert a2.active_at(10**9)
    assert not a3.active_at(499) and a3.active_at(10**9)


# --- client / cache ---------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000_100.0

    def __call__(self) -> float:
        return self.t


def make_client(handler: object, clock: Clock, **kw: float) -> RealtimeClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return RealtimeClient(client=http, clock=clock, **kw)


async def test_ttl_cache_and_data_age() -> None:
    clock, hits = Clock(), []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.path)
        return httpx.Response(200, content=trip_update_feed([], timestamp=1_000_000_090))

    rc = make_client(handler, clock, ttl_s=25)
    r1 = await rc.trip_updates()
    assert r1.age_s == pytest.approx(10) and not r1.stale and r1.error is None
    clock.t += 10
    r2 = await rc.trip_updates()
    assert len(hits) == 1 and r2.age_s == pytest.approx(20)  # cached; age keeps growing
    clock.t += 20  # now past the TTL
    await rc.trip_updates()
    assert len(hits) == 2


async def test_failure_serves_recent_copy_as_stale_then_gives_up() -> None:
    clock, state = Clock(), {"ok": True}

    def handler(req: httpx.Request) -> httpx.Response:
        if state["ok"]:
            return httpx.Response(200, content=trip_update_feed([], timestamp=1_000_000_090))
        return httpx.Response(503)

    rc = make_client(handler, clock, ttl_s=25, max_stale_s=120)
    await rc.trip_updates()
    state["ok"] = False
    clock.t += 60
    stale = await rc.trip_updates()
    assert stale.data is not None and stale.stale and "503" in (stale.error or "")
    assert stale.age_s == pytest.approx(70)
    clock.t += 100  # copy is now too old to show
    gone = await rc.trip_updates()
    assert gone.data is None and gone.age_s is None and gone.error


async def test_bad_payload_and_no_cache_reports_error() -> None:
    rc = make_client(lambda r: httpx.Response(200, content=b"not protobuf"), Clock())
    res = await rc.trip_updates()
    assert res.data is None and res.error


async def test_concurrent_callers_share_one_fetch() -> None:
    clock, hits = Clock(), []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(200, content=alert_feed([], timestamp=1_000_000_090))

    import asyncio

    rc = make_client(handler, clock)
    results = await asyncio.gather(*(rc.alerts() for _ in range(5)))
    assert len(hits) == 1 and all(r.data is not None for r in results)
    await rc.aclose()
