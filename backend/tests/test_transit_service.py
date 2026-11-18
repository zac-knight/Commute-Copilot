from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agents.transit.gtfs_realtime import FeedResult, parse_alerts, parse_trip_updates
from app.agents.transit.gtfs_static import GtfsStatic
from app.agents.transit.planner import JourneyPlanner
from app.agents.transit.service import PlaceNotFoundError, TransitService
from app.core.memory import UserProfile
from app.core.timeutil import TZ, parse_when
from tests.transit_fixtures import MON, alert_feed, build_static, epoch, trip_update_feed

TUE = date(2026, 11, 10)


def dt(h: int, m: int, d: date = MON) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


class FakeRealtime:
    """Stands in for RealtimeClient: a fixed snapshot, or a failure."""

    def __init__(
        self,
        trips: list[dict[str, Any]] | None = None,
        alerts: list[dict[str, Any]] | None = None,
        *,
        error: str | None = None,
        stale: bool = False,
        age: float = 12.0,
    ) -> None:
        self.error, self.stale, self.age = error, stale, age
        self.snap = parse_trip_updates(trip_update_feed(trips or []), 0.0)
        self.alert_snap = parse_alerts(alert_feed(alerts or []), 0.0)

    def cache_ages(self) -> dict[str, float]:
        return {"trips": self.age}

    async def trip_updates(self) -> FeedResult[Any]:
        if self.error and not self.stale:
            return FeedResult(None, None, error=self.error)
        return FeedResult(self.snap, self.age, stale=self.stale, error=self.error)

    async def alerts(self) -> FeedResult[Any]:
        if self.error and not self.stale:
            return FeedResult(None, None, error=self.error)
        return FeedResult(self.alert_snap, self.age, stale=self.stale, error=self.error)


def make(
    tmp_path: Path, rt: FakeRealtime | None = None, now: datetime | None = None, **kw: Any
) -> TransitService:
    static: GtfsStatic = build_static(tmp_path)
    return TransitService(
        static,
        rt or FakeRealtime(),
        JourneyPlanner(static),  # type: ignore[arg-type]
        UserProfile(home="Alpha Street", destinations={"dee": "Dee Station"}),
        clock=lambda: now or dt(7, 50),
        **kw,
    )


def upd(seq: int, stop: str, **kw: Any) -> dict[str, Any]:
    return {"seq": seq, "stop_id": stop, **kw}


# --- parse_when -------------------------------------------------------------------------


def test_parse_when_variants() -> None:
    now = dt(7, 50)  # Monday 2026-11-09
    assert parse_when("2026-11-09T08:12", now) == dt(8, 12)
    assert parse_when("2026-11-09T08:12:00+10:00", now) == dt(8, 12)
    assert parse_when("2026-11-08T22:12:00+00:00", now) == dt(8, 12)  # UTC converted to Brisbane
    assert parse_when("08:12", now) == dt(8, 12)
    assert parse_when("8:12", now) == dt(8, 12)
    assert parse_when("25:10:00", now) == datetime(2026, 11, 10, 1, 10, tzinfo=TZ)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today 8am", dt(8, 0)),
        ("TOMORROW 07:30", dt(7, 30, TUE)),
        ("tomorrow 9pm", dt(21, 0, TUE)),
        ("tomorrow 12am", dt(0, 0, TUE)),
        ("tomorrow 12pm", dt(12, 0, TUE)),
        ("7:30am", dt(7, 30)),
        ("5 pm", dt(17, 0)),
        ("friday 18:05", dt(18, 5, date(2026, 11, 13))),
        ("Fri 8am", dt(8, 0, date(2026, 11, 13))),
        ("wednesday 09:00", dt(9, 0, date(2026, 11, 11))),
        ("monday 9am", dt(9, 0)),  # today is Monday and 09:00 has not passed
        ("monday 7am", dt(7, 0, date(2026, 11, 16))),  # 07:00 has passed: next Monday
    ],
)
def test_parse_when_natural_forms(text: str, expected: datetime) -> None:
    assert parse_when(text, dt(7, 50)) == expected


@pytest.mark.parametrize(
    "bad", ["tomorrow-ish", "", "tomorrow", "13pm", "0am", "someday 8am", "8:75", "tomorrow 25am"]
)
def test_parse_when_rejects_garbage_with_a_helpful_message(bad: str) -> None:
    with pytest.raises(ValueError, match=r"time|day"):
        parse_when(bad, dt(7, 50))


# --- places -----------------------------------------------------------------------------


def test_resolve_place_variants(tmp_path: Path) -> None:
    svc = make(tmp_path)
    assert [s.stop_id for s in svc.resolve_place("home").stops] == ["A"]  # profile alias
    assert {s.stop_id for s in svc.resolve_place("dee").stops} == {
        "D1",
        "D2",
    }  # nickname -> station
    assert {s.stop_id for s in svc.resolve_place("DSTN").stops} == {"D1", "D2"}  # station id
    assert [s.stop_id for s in svc.resolve_place("B").stops] == ["B"]  # stop id
    assert {s.stop_id for s in svc.resolve_place("-27.2095,153.04").stops} == {"C", "C2"}  # lat,lon
    assert "C" in {s.stop_id for s in svc.resolve_place("charlie plaza").stops}  # fuzzy name
    with pytest.raises(PlaceNotFoundError):
        svc.resolve_place("Nowhere At All")
    with pytest.raises(PlaceNotFoundError):
        svc.resolve_place("0,0")


def test_find_stops_tool_logic(tmp_path: Path) -> None:
    svc = make(tmp_path)
    res = svc.find_stops("charlie", near="home", modes=None)
    assert next(s.stop_id for s in res.stops) == "C" and res.stops[0].distance_m
    assert svc.find_stops("dee", None, ["train"]).stops[0].is_station


# --- next_departures: realtime merge ----------------------------------------------------


async def test_without_realtime_everything_is_labelled_scheduled_only(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRealtime(error="ConnectError: down"))
    res = await svc.next_departures("A", limit=5)
    assert res.realtime.source == "scheduled_only" and "down" in (res.realtime.note or "")
    assert [(d.trip_id, d.status, d.predicted) for d in res.departures] == [
        ("T1", "scheduled_only", None),
        ("T8", "scheduled_only", None),
        ("T2", "scheduled_only", None),
    ]
    assert res.departures[0].scheduled == "08:00"


async def test_delay_cancel_ontime_and_ordering(tmp_path: Path) -> None:
    rt = FakeRealtime(
        [
            {"trip_id": "T1", "start_date": "20261109", "updates": [upd(1, "A", dep_delay=300)]},
            {"trip_id": "T8", "start_date": "20261109", "rel": "CANCELED"},
            {"trip_id": "T2", "start_date": "20261109", "updates": [upd(1, "A", dep_delay=20)]},
        ]
    )
    res = await make(tmp_path, rt).next_departures("A", limit=5)
    by = {d.trip_id: d for d in res.departures}
    assert (by["T1"].status, by["T1"].delay_min, by["T1"].predicted, by["T1"].delay_basis) == (
        "delayed",
        5.0,
        "08:05",
        "reported",
    )
    assert by["T8"].status == "cancelled" and by["T8"].predicted is None
    assert by["T2"].status == "on_time" and by["T2"].delay_min == pytest.approx(0.3, abs=0.05)
    assert res.realtime.source == "realtime" and res.realtime.feed_age_s == 12
    assert "Cancelled" in (res.note or "")
    # the last departure by effective time is T2 (08:30)
    assert [d.trip_id for d in res.departures][-1] == "T2"


async def test_propagated_delay_is_flagged_as_estimate(tmp_path: Path) -> None:
    rt = FakeRealtime(
        [{"trip_id": "T1", "start_date": "20261109", "updates": [upd(1, "A", dep_delay=240)]}]
    )
    res = await make(tmp_path, rt).next_departures("B", limit=1)  # B has no explicit update
    (d,) = res.departures
    assert (d.trip_id, d.status, d.delay_basis, d.delay_min) == (
        "T1",
        "delayed",
        "estimated_from_earlier_stop",
        4.0,
    )


async def test_late_running_trip_scheduled_before_now_is_still_listed(tmp_path: Path) -> None:
    now = dt(8, 10)
    rt = FakeRealtime(
        [{"trip_id": "T1", "start_date": "20261109", "updates": [upd(1, "A", dep_delay=900)]}]
    )  # scheduled 08:00, +15 min
    res = await make(tmp_path, rt, now).next_departures("A", limit=5)
    ids = [d.trip_id for d in res.departures]
    assert "T1" in ids and "T8" not in ids  # T8 (08:05, no delay) has gone; T1 has not
    assert next(d for d in res.departures if d.trip_id == "T1").predicted == "08:15"


async def test_skipped_stop_is_not_offered(tmp_path: Path) -> None:
    rt = FakeRealtime(
        [{"trip_id": "T1", "start_date": "20261109", "updates": [upd(1, "A", rel="SKIPPED")]}]
    )
    res = await make(tmp_path, rt).next_departures("A", limit=5)
    assert "T1" not in [d.trip_id for d in res.departures]


async def test_added_trip_appears_from_realtime_only(tmp_path: Path) -> None:
    rt = FakeRealtime(
        [
            {
                "trip_id": "EXTRA1",
                "start_date": "20261109",
                "rel": "ADDED",
                "route_id": "R2",
                "updates": [upd(1, "C2", dep_time=epoch(MON, "08:12:00"))],
            }
        ]
    )
    res = await make(tmp_path, rt).next_departures("C2", limit=5)
    extra = next(d for d in res.departures if d.trip_id == "EXTRA1")
    assert (extra.status, extra.route, extra.mode, extra.predicted) == (
        "added",
        "RAIL",
        "train",
        "08:12",
    )
    only_buses = await make(tmp_path, rt).next_departures("C2", limit=5, route_filter=["100"])
    assert all(d.trip_id != "EXTRA1" for d in only_buses.departures)


async def test_after_midnight_trips_match_realtime_by_calendar_start_date(tmp_path: Path) -> None:
    now = datetime(2026, 11, 10, 0, 0, tzinfo=TZ)
    rt = FakeRealtime(
        [
            # T4 starts 23:50 Monday -> realtime start_date is Monday
            {"trip_id": "T4", "start_date": "20261109", "updates": [upd(2, "B", dep_delay=120)]},
            # T5 starts 24:30 (Tue 00:30): realtime start_date is TUESDAY, service date Monday
            {"trip_id": "T5", "start_date": "20261110", "updates": [upd(1, "C2", dep_delay=60)]},
        ]
    )
    svc = make(tmp_path, rt, now)
    (t4,) = (await svc.next_departures("B", limit=3)).departures
    assert (t4.trip_id, t4.scheduled, t4.predicted, t4.status) == (
        "T4",
        "00:05",
        "00:07",
        "delayed",
    )
    assert t4.service_date == "2026-11-09"
    (t5,) = (await svc.next_departures("C2", limit=3)).departures
    assert (t5.trip_id, t5.predicted, t5.delay_min) == ("T5", "00:31", 1.0)

    wrong = FakeRealtime(
        [
            {
                "trip_id": "T5",
                "start_date": "20261109",  # the GTFS service date
                "updates": [upd(1, "C2", dep_delay=60)],
            }
        ]
    )
    (t5b,) = (await make(tmp_path, wrong, now).next_departures("C2", limit=3)).departures
    assert t5b.status == "scheduled_only"  # not matched: the date convention is the calendar date


async def test_dates_are_shown_when_not_today(tmp_path: Path) -> None:
    res = await make(tmp_path, FakeRealtime()).next_departures(
        "A", limit=2, after=dt(8, 0, date(2026, 11, 12))
    )
    assert res.departures[0].scheduled.startswith("Thu 12 Nov")


async def test_limit_route_filter_and_empty_note(tmp_path: Path) -> None:
    svc = make(tmp_path)
    assert len((await svc.next_departures("A", limit=1)).departures) == 1
    only = await svc.next_departures("A", limit=5, route_filter=["200"])
    assert [d.trip_id for d in only.departures] == ["T8"]
    gap = await svc.next_departures("A", limit=5, after=dt(12, 0))  # nothing until 23:50
    assert [d.trip_id for d in gap.departures] == ["T4"]  # the next service is still reported
    assert "No departures found from Mon 09 Nov 12:00 within 3 h." in (gap.note or "")
    assert "The next departure is 23:50 (route 100 to Charlie Plaza, from Alpha Street)" in (
        gap.note or ""
    )
    never = await make(tmp_path, FakeRealtime(), dt(8, 0, date(2026, 12, 20))).next_departures("A")
    assert never.departures == [] and "None found in the next 24 hours either" in (never.note or "")


async def test_station_query_covers_all_platforms(tmp_path: Path) -> None:
    now = dt(8, 0)
    res = await make(tmp_path, FakeRealtime(), now).next_departures("C2", limit=5)
    assert res.departures[0].platform is None
    # nothing departs the Dee platforms (terminus), but the stop list covers both platforms
    res = await make(tmp_path, FakeRealtime(), now).next_departures("DSTN", limit=5)
    assert {s.stop_id for s in res.stops} == {"D1", "D2"}


async def test_stale_realtime_is_reported(tmp_path: Path) -> None:
    rt = FakeRealtime(error="ReadTimeout", stale=True, age=95)
    res = await make(tmp_path, rt).next_departures("A")
    assert res.realtime.stale and res.realtime.feed_age_s == 95
    assert "refresh failed" in (res.realtime.note or "")


# --- can_i_catch ------------------------------------------------------------------------


@pytest.fixture
def delayed_rt() -> FakeRealtime:
    return FakeRealtime(
        [
            {"trip_id": "T1", "start_date": "20261109", "updates": [upd(1, "A", dep_delay=300)]},
            {"trip_id": "T8", "start_date": "20261109", "rel": "CANCELED"},
        ]
    )


async def test_can_catch_uses_the_delay(tmp_path: Path, delayed_rt: FakeRealtime) -> None:
    svc = make(tmp_path, delayed_rt)
    ok = await svc.can_i_catch("A", dt(8, 3), scheduled_departure=dt(8, 0))
    assert ok.can_catch is True and ok.slack_min == 2.0 and ok.basis == "realtime"
    assert "2 min to spare" in ok.reason and ok.departure and ok.departure.predicted == "08:05"
    late = await svc.can_i_catch("A", dt(8, 6), scheduled_departure=dt(8, 0))
    assert late.can_catch is False and late.slack_min == -1.0 and "too late" in late.reason


async def test_timetable_only_catch_is_labelled(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRealtime(error="down"))
    res = await svc.can_i_catch("A", dt(7, 55), scheduled_departure=dt(8, 0))
    assert res.can_catch is True and res.slack_min == 5.0 and res.basis == "scheduled_only"
    assert "timetable only" in res.reason


async def test_cancelled_trip_cannot_be_caught(tmp_path: Path, delayed_rt: FakeRealtime) -> None:
    res = await make(tmp_path, delayed_rt).can_i_catch("A", dt(7, 0), trip_id="T8")
    assert res.can_catch is False and "cancelled" in res.reason


async def test_catch_by_trip_id_and_unknown_cases(tmp_path: Path, delayed_rt: FakeRealtime) -> None:
    svc = make(tmp_path, delayed_rt)
    assert (await svc.can_i_catch("A", dt(8, 0), trip_id="T2")).slack_min == 30.0
    assert (
        await svc.can_i_catch("A", dt(8, 0))
    ).can_catch is None  # nothing identifies the service
    nope = await svc.can_i_catch("A", dt(8, 0), scheduled_departure=dt(3, 0))
    assert nope.can_catch is None and "No matching departure" in nope.reason


async def test_catch_flags_estimated_delay(tmp_path: Path, delayed_rt: FakeRealtime) -> None:
    res = await make(tmp_path, delayed_rt).can_i_catch(
        "B", dt(8, 10), scheduled_departure=dt(8, 10)
    )
    assert res.departure and res.departure.delay_basis == "estimated_from_earlier_stop"
    assert "estimated" in res.reason


# --- alerts -----------------------------------------------------------------------------


async def test_alerts_filtering_and_activity(tmp_path: Path) -> None:
    now_epoch = int(dt(7, 50).timestamp())
    rt = FakeRealtime(
        alerts=[
            {
                "id": "route",
                "header": "R1 detour",
                "routes": ["R1"],
                "periods": [(now_epoch - 60, now_epoch + 60)],
            },
            {"id": "stop", "header": "Stop A closed", "stops": ["A"], "periods": []},
            {
                "id": "expired",
                "header": "old",
                "routes": ["R1"],
                "periods": [(now_epoch - 999, now_epoch - 99)],
            },
            {"id": "other", "header": "rail", "routes": ["R2"]},
        ]
    )
    svc = make(tmp_path, rt)
    allr = await svc.alerts(None, None)
    assert {a.alert_id for a in allr.alerts} == {
        "route",
        "stop",
        "other",
    } and allr.total_matching == 3
    by_name = await svc.alerts(["100"], None)  # short name resolves to route id R1
    assert {a.alert_id for a in by_name.alerts} == {"route"}
    assert {a.alert_id for a in (await svc.alerts(["R2"], None)).alerts} == {"other"}
    assert {a.alert_id for a in (await svc.alerts(None, ["A"])).alerts} == {"stop"}
    assert len((await svc.alerts(None, None, limit=1)).alerts) == 1


async def test_alerts_feed_down_is_explicit(tmp_path: Path) -> None:
    res = await make(tmp_path, FakeRealtime(error="boom")).alerts(None, None)
    assert res.alerts == [] and "unknown" in (res.realtime.note or "")


# --- plan_trip --------------------------------------------------------------------------


async def test_plan_trip_with_realtime_on_legs(tmp_path: Path) -> None:
    rt = FakeRealtime(
        [{"trip_id": "T1", "start_date": "20261109", "updates": [upd(1, "A", dep_delay=300)]}]
    )
    res = await make(tmp_path, rt).plan_trip("home", "dee")
    first, second = res.journeys
    assert (first.depart, first.arrive, first.transfers, first.duration_min) == (
        "08:00",
        "08:45",
        1,
        45,
    )
    transit = [leg for leg in first.legs if leg.kind == "transit"]
    assert transit[0].realtime and transit[0].realtime.status == "delayed"
    assert (
        transit[0].realtime.delay_min == 5.0 and transit[0].realtime.predicted_departure == "08:05"
    )
    assert transit[1].realtime is None  # no realtime for T3
    assert second.transfers == 0 and "NOT re-validated" in res.basis
    assert {s.stop_id for s in res.destination} == {"D1", "D2"}


async def test_plan_trip_flags_cancelled_leg(tmp_path: Path) -> None:
    rt = FakeRealtime([{"trip_id": "T1", "start_date": "20261109", "rel": "CANCELED"}])
    res = await make(tmp_path, rt).plan_trip("A", "DSTN")
    leg = next(leg for leg in res.journeys[0].legs if leg.kind == "transit")
    assert leg.realtime and leg.realtime.status == "cancelled"


async def test_plan_trip_arrive_by_and_none_found(tmp_path: Path) -> None:
    svc = make(tmp_path)
    res = await svc.plan_trip("A", "DSTN", arrive_by=dt(9, 0))
    assert res.journeys[0].arrive_iso <= dt(9, 0).isoformat() and res.journeys[0].depart == "08:00"
    none = await svc.plan_trip("A", "DSTN", arrive_by=dt(8, 40))
    assert none.journeys == [] and "No journey" in (none.note or "")


async def test_plan_trip_without_realtime_says_so(tmp_path: Path) -> None:
    res = await make(tmp_path, FakeRealtime(error="offline")).plan_trip("A", "DSTN")
    assert res.realtime.source == "scheduled_only" and res.journeys
    assert all(leg.realtime is None for j in res.journeys for leg in j.legs)


async def test_past_start_time_is_called_out(tmp_path: Path) -> None:
    # 'HH:MM' = today: asking for 07:30 at 23:59 refers to a morning that has already gone.
    now = dt(23, 59)
    svc = make(tmp_path, FakeRealtime(), now)
    res = await svc.next_departures("A", after=dt(7, 30))
    assert res.note and "WARNING" in res.note and "already in the past" in res.note
    assert "tomorrow 07:30" in res.note  # tells the model how to say it
    plan = await svc.plan_trip("A", "DSTN", depart_after=dt(7, 30))
    assert plan.note and "already in the past" in plan.note
    fine = await svc.next_departures("A", after=dt(23, 55))  # 4 min ago: tolerated
    assert "WARNING" not in (fine.note or "")
    assert "WARNING" not in ((await svc.next_departures("A")).note or "")


# --- towards / modes / route_filter validation (found live: model passed route_filter=['bus']) ---


async def test_towards_keeps_only_services_that_go_on_to_that_place(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRealtime())
    all_a = await svc.next_departures("A", limit=10)
    assert {d.trip_id for d in all_a.departures} >= {"T1", "T8", "T2"}
    to_charlie = await svc.next_departures("A", limit=10, towards="Charlie Plaza")
    assert {d.trip_id for d in to_charlie.departures} == {"T1", "T2"}  # the bus 100 line
    to_dee = await svc.next_departures("A", limit=10, towards="dee")
    assert {d.trip_id for d in to_dee.departures} == {"T8"}  # only the direct 200
    mid = await svc.next_departures("A", limit=10, towards="B")  # an intermediate stop counts
    assert {d.trip_id for d in mid.departures} == {"T1", "T2"}
    nowhere = await svc.next_departures(
        "A", limit=10, towards="Charlie Plaza stop 2"
    )  # served, not from A
    assert nowhere.departures == [] and "No departures" in (nowhere.note or "")
    with pytest.raises(PlaceNotFoundError):
        await svc.next_departures("A", towards="Atlantis")
    with pytest.raises(PlaceNotFoundError):  # a stop nothing serves cannot be a destination
        await svc.next_departures("A", towards="Echo Far")


async def test_towards_does_not_count_stops_before_the_boarding_point(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRealtime())
    # from B, the 100 goes on to Charlie but never to Alpha (which is BEFORE B on that line)
    assert {
        d.trip_id
        for d in (await svc.next_departures("B", limit=10, towards="Charlie Plaza")).departures
    } == {"T1", "T2"}
    assert (await svc.next_departures("B", limit=10, towards="Alpha Street")).departures == []


async def test_modes_filter(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRealtime(), dt(8, 0))
    assert (await svc.next_departures("C2", limit=5, modes=["bus"])).departures == []
    trains = await svc.next_departures("C2", limit=5, modes=["train"])
    assert {d.mode for d in trains.departures} == {"train"}
    both = await svc.next_departures("A", limit=10, modes=["bus", "train"])
    assert both.departures


async def test_unknown_route_filter_is_a_helpful_error_not_an_empty_answer(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRealtime())
    for bad in (["bus"], ["train"], ["Chermside"], ["100", "nope"]):
        with pytest.raises(ValueError, match=r"use `modes`.*`towards`|No route named"):
            await svc.next_departures("A", route_filter=bad)
    with pytest.raises(ValueError, match="No route named"):
        await svc.can_i_catch("A", dt(8, 0), scheduled_departure=dt(8, 0), route_filter=["bus"])
    ok = await svc.next_departures("A", route_filter=["100"])  # a real route number works
    assert {d.route for d in ok.departures} == {"100"}
    assert (
        await svc.next_departures("C2", route_filter=["rail"], after=dt(8, 0))
    ).departures  # case-insensitive


async def test_towards_reaches_a_terminal_far_from_the_main_stop_and_ignores_narrow_nicknames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (found live): the 680 terminates at 'Chermside Shopping Centre', which the
    default place cluster missed, and the nickname 'chermside' narrowed the query to one station."""
    import tests.transit_fixtures as fx

    # a terminal 'Charlie Plaza North Terminal' ~700 m from C, served only by a new bus T10
    monkeypatch.setattr(
        fx,
        "STOPS",
        [
            *fx.STOPS,
            ["CN", "CN", "Charlie Plaza North Terminal", "", -27.2037, 153.0400, 0, "", ""],
        ],
    )
    monkeypatch.setattr(fx, "TRIPS", [*fx.TRIPS, ["R1", "SVC_WK", "T10", "North Terminal", 0]])
    monkeypatch.setattr(
        fx,
        "STOP_TIMES",
        [
            *fx.STOP_TIMES,
            ["T10", "A", "09:00:00", "09:00:00", 1, 0, 0],
            ["T10", "CN", "09:30:00", "09:30:00", 2, 1, 0],
        ],
    )
    svc = make(tmp_path, FakeRealtime(), dt(8, 50))
    svc.profile.destinations["charlie"] = "Charlie Plaza stop 2"  # a narrow nickname

    narrow = {s.stop_id for s in svc.resolve_place("Charlie Plaza").stops}
    assert "CN" not in narrow and {"C", "C2"} <= narrow  # default: the 500 m precinct only
    broad = {s.stop_id for s in svc.resolve_place("Charlie Plaza", radius_m=2500, window=0.4).stops}
    assert "CN" in broad  # the wide cluster reaches the far terminal

    got = await svc.next_departures("A", limit=10, towards="Charlie Plaza", after=dt(8, 50))
    assert "T10" in {
        d.trip_id for d in got.departures
    }  # the bus that only goes to the far terminal

    # a nickname resolves narrowly (right for 'take me to'); towards uses the user's own words
    assert {s.stop_id for s in svc.resolve_place("charlie").stops} == {"C2"}
    via_nickname = await svc.next_departures("A", limit=10, towards="charlie", after=dt(7, 50))
    assert {"T1", "T2", "T10"} <= {d.trip_id for d in via_nickname.departures}


# --- no service soon: say when it resumes ---------------------------------------------------


async def test_plan_trip_looks_further_ahead_when_nothing_leaves_soon(tmp_path: Path) -> None:
    svc = make(tmp_path, FakeRealtime(), dt(17, 0))  # the mini feed has no service until 23:50
    res = await svc.plan_trip("A", "DSTN")
    assert res.journeys and res.journeys[0].depart == "23:50"
    assert res.note and "No journey leaves in the next 4 hours" in res.note
    assert "earliest one leaves 23:50" in res.note
    soon = await make(tmp_path, FakeRealtime(), dt(7, 50)).plan_trip("A", "DSTN")
    assert soon.note is None  # a normal plan carries no such note


async def test_a_trip_that_never_runs_still_says_none(tmp_path: Path) -> None:
    svc = make(
        tmp_path, FakeRealtime(), dt(8, 0, date(2026, 12, 20))
    )  # after the feed's calendar ends
    res = await svc.plan_trip("A", "DSTN")
    assert res.journeys == [] and "No journey found" in (res.note or "")


@pytest.mark.parametrize(
    ("route_type", "mode"), [(0, "tram"), (2, "train"), (3, "bus"), (4, "ferry")]
)
def test_leg_mode_follows_route_type_including_tram(
    tmp_path: Path, route_type: int, mode: str
) -> None:
    """Route type 0 (tram) is falsy: `route_type or 3` once turned every tram into a bus."""
    from app.agents.transit.planner import Leg

    svc = make(tmp_path)
    now = datetime(2026, 11, 9, 7, 50, tzinfo=TZ)
    leg = Leg(
        kind="transit", from_stop="A", to_stop="B", depart=now, arrive=now + timedelta(minutes=5),
        route_short_name="96", route_type=route_type, headsign="x", trip_id="T1",
    )  # fmt: skip
    assert svc._leg_out(leg, None, now).mode == mode
