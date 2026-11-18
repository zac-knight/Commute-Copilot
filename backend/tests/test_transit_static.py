from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from app.agents.transit.gtfs_static import (
    FeedNotLoadedError,
    GtfsStatic,
    at_service_time,
    format_gtfs_time,
    haversine_m,
    ingest_zip,
    normalise_name,
    parse_gtfs_time,
    parse_yyyymmdd,
    refresh_feed,
    service_midnight_epoch,
)
from app.core.timeutil import TZ
from tests.transit_fixtures import MON, build_static, write_gtfs_zip


@pytest.fixture
def static(tmp_path: Path) -> GtfsStatic:
    return build_static(tmp_path)


# --- time -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secs"),
    [
        ("00:00:00", 0),
        ("08:12:30", 29550),
        ("24:00:00", 86400),
        ("25:10:00", 90600),
        ("48:00:00", 172800),
    ],
)
def test_parse_gtfs_time_including_past_24h(text: str, secs: int) -> None:
    assert parse_gtfs_time(text) == secs
    assert format_gtfs_time(secs) == text


@pytest.mark.parametrize("bad", ["", "8:12", "aa:bb:cc", "10:61:00", "10:00:61", "-1:00:00"])
def test_parse_gtfs_time_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_gtfs_time(bad)


def test_service_time_past_midnight_rolls_into_next_calendar_day() -> None:
    dt = at_service_time(date(2026, 11, 17), 24 * 3600 + 35 * 60)
    assert dt == datetime(2026, 11, 18, 0, 35, tzinfo=TZ)
    assert service_midnight_epoch(date(2026, 11, 17)) == int(
        datetime(2026, 11, 17, tzinfo=TZ).timestamp()
    )  # Brisbane has no DST, so 'noon minus 12h' is local midnight


def test_small_helpers() -> None:
    assert parse_yyyymmdd("20261109") == MON == parse_yyyymmdd(20261109)
    assert normalise_name("  North-Lakes  Stn, (A) ") == "north lakes stn a"
    assert 90 < haversine_m(-27.21, 153.04, -27.2091, 153.04) < 110


# --- ingest -----------------------------------------------------------------------------


def test_ingest_keeps_bus_rail_tram_and_ferry(static: GtfsStatic) -> None:
    with static.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 4  # ferry kept
        trips = {r[0] for r in c.execute("SELECT trip_id FROM trips")}
        assert {"T1", "T3", "T6", "T8"} <= trips
        assert (
            c.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0] == 19
        )  # incl. the ferry trip
    assert static.meta()["etag"] == '"v1"'
    assert static.feed_range() == (date(2026, 11, 2), date(2026, 11, 30))


def test_stop_modes_and_parent_inheritance(static: GtfsStatic) -> None:
    s = static.stops()
    assert s["A"].modes == ("bus", "ferry")
    assert s["C2"].modes == ("train",)
    assert s["DSTN"].modes == ("train", "bus")  # station inherits its platforms' modes
    assert s["F"].modes == ("ferry",) and s["E"].modes == ()  # ferry-only / unserved


def test_trip_start_secs_and_patterns(static: GtfsStatic) -> None:
    assert static.trip_start_secs("T4") == 23 * 3600 + 50 * 60
    assert static.trip_start_secs("T5") == 24 * 3600 + 30 * 60
    assert static.trip_start_secs("nope") is None
    with static.connect() as c:
        pats = c.execute("SELECT trip_id, pattern_idx FROM trips").fetchall()
    by = {r[0]: r[1] for r in pats}
    assert by["T1"] == by["T2"] == by["T4"]  # same route + stop list = same pattern
    assert by["T1"] != by["T8"]


def test_ingest_failure_leaves_existing_db_untouched(tmp_path: Path) -> None:
    good = write_gtfs_zip(tmp_path / "good.zip")
    db = tmp_path / "gtfs.sqlite"
    ingest_zip(good, db)
    before = db.read_bytes()
    import zipfile

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("routes.txt", "route_id\n")  # missing files / columns
    with pytest.raises(KeyError):
        ingest_zip(bad, db)
    assert db.read_bytes() == before and not db.with_suffix(".tmp").exists()


def test_ungrouped_stop_times_rejected(tmp_path: Path) -> None:
    import zipfile

    src = write_gtfs_zip(tmp_path / "a.zip")
    bad = tmp_path / "ungrouped.zip"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(bad, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name).decode()
            if name == "stop_times.txt":
                lines = data.strip().split("\n")
                lines.insert(3, lines[-1])  # a T9 row in the middle of T1
                data = "\n".join(lines) + "\n"
            zout.writestr(name, data)
    with pytest.raises(ValueError, match="not grouped"):
        ingest_zip(bad, tmp_path / "x.sqlite")


def test_missing_db_gives_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FeedNotLoadedError, match="refresh"):
        GtfsStatic(tmp_path / "none.sqlite").meta()


# --- service days -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("d", "expect_wk", "expect_sat"),
    [
        (date(2026, 11, 9), True, False),  # Monday
        (date(2026, 11, 14), False, True),  # Saturday
        (date(2026, 11, 15), False, False),  # Sunday
        (date(2026, 11, 11), False, False),  # Wednesday removed by calendar_dates (type 2)
        (date(2026, 11, 12), True, True),  # Thursday: Saturday service ADDED (type 1)
        (date(2026, 12, 1), False, False),  # after end_date
        (date(2026, 11, 1), False, False),  # before start_date (a Sunday anyway)
    ],
)
def test_active_services(static: GtfsStatic, d: date, expect_wk: bool, expect_sat: bool) -> None:
    with static.connect() as c:
        ids = {r[0]: r[1] for r in c.execute("SELECT service_idx, service_id FROM services")}
    active = {ids[i] for i in static.active_services(d)}
    assert ("SVC_WK" in active) is expect_wk
    assert ("SVC_SAT" in active) is expect_sat


# --- departures -------------------------------------------------------------------------


def at(h: int, m: int, d: date = MON) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


def test_scheduled_departures_window_and_pickup(static: GtfsStatic) -> None:
    deps = static.scheduled_departures(["A"], at(8, 0), 3600)
    assert [(d.trip_id, d.scheduled.strftime("%H:%M")) for d in deps] == [
        ("T1", "08:00"),
        ("T8", "08:05"),
        ("T2", "08:30"),
    ]
    assert static.scheduled_departures(["A"], at(8, 1), 3600)[0].trip_id == "T8"
    # last stop has pickup_type=1: nothing departs from C
    assert static.scheduled_departures(["C"], at(0, 0), 86400) == []


def test_departures_filter_by_route_name(static: GtfsStatic) -> None:
    deps = static.scheduled_departures(["A"], at(8, 0), 3600, route_short_names=["200"])
    assert [d.trip_id for d in deps] == ["T8"]


def test_after_midnight_trip_found_from_previous_service_date(static: GtfsStatic) -> None:
    tue = date(2026, 11, 10)
    # T4 (Monday service) is at B at 24:05 = Tuesday 00:05.
    deps = static.scheduled_departures(["B"], at(0, 0, tue), 3600)
    assert [(d.trip_id, d.service_date, d.scheduled) for d in deps] == [
        ("T4", MON, datetime(2026, 11, 10, 0, 5, tzinfo=TZ))
    ]


def test_rt_start_date_uses_calendar_date_for_trips_starting_after_midnight(
    static: GtfsStatic,
) -> None:
    (t4,) = static.scheduled_departures(["B"], at(0, 0, date(2026, 11, 10)), 3600)
    assert t4.rt_start_date == MON  # starts 23:50 on Monday: realtime start_date = Monday
    (t5,) = static.scheduled_departures(["C2"], at(0, 0, date(2026, 11, 10)), 3600 * 2)
    assert t5.trip_id == "T5" and t5.service_date == MON
    assert t5.rt_start_date == date(2026, 11, 10)  # starts 24:30: realtime says Tuesday


def test_no_service_on_removed_day(static: GtfsStatic) -> None:
    assert static.scheduled_departures(["A"], at(8, 0, date(2026, 11, 11)), 3600) == []


# --- stops ------------------------------------------------------------------------------


def test_find_stops_exact_prefix_typo_and_modes(static: GtfsStatic) -> None:
    assert static.find_stops("alpha street")[0].stop.stop_id == "A"
    assert static.find_stops("brav")[0].stop.stop_id == "B"  # prefix
    assert static.find_stops("charlei plaza")[0].stop.name.startswith("Charlie")  # typo
    assert static.find_stops("zzzz") == []
    assert static.find_stops("") == []
    assert [m.stop.stop_id for m in static.find_stops("dee station", modes=["bus"])] != []
    assert static.find_stops("charlie", modes=["bus"])[0].stop.stop_id == "C"
    assert all("train" in m.stop.modes for m in static.find_stops("charlie", modes=["train"]))
    assert [m.stop.stop_id for m in static.find_stops("ferry")] == ["F"]
    assert static.find_stops("ferry", modes=["bus", "train"]) == []  # ferry-only stop


def test_find_stops_sorted_by_distance_when_near_given(static: GtfsStatic) -> None:
    a = static.get_stop("A")
    assert a is not None
    res = static.find_stops("charlie", near=(a.lat, a.lon))
    assert [m.stop.stop_id for m in res] == ["C", "C2"]  # C is nearer to A than C2
    assert res[0].distance_m is not None and res[1].distance_m is not None
    assert res[0].distance_m < res[1].distance_m


def test_station_expands_to_platforms(static: GtfsStatic) -> None:
    assert sorted(static.platform_stops("DSTN")) == ["D1", "D2"]
    assert static.platform_stops("A") == ["A"]


def test_stops_within_radius(static: GtfsStatic) -> None:
    ids = {s.stop_id for s in static.stops_within(-27.21, 153.04, 150)}
    assert ids == {"C", "C2"}


# --- refresh (conditional GET) ----------------------------------------------------------


async def test_refresh_downloads_then_reports_unchanged_then_survives_failure(
    tmp_path: Path,
) -> None:
    zip_bytes = write_gtfs_zip(tmp_path / "src.zip").read_bytes()
    calls: list[dict[str, str]] = []
    mode = {"fail": False}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(dict(req.headers))
        if mode["fail"]:
            return httpx.Response(500)
        if req.headers.get("if-none-match") == '"abc"':
            return httpx.Response(304)
        return httpx.Response(200, content=zip_bytes, headers={"etag": '"abc"'})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data, db = tmp_path / "data", tmp_path / "data" / "gtfs.sqlite"

    first = await refresh_feed(data, db, client=client, url="http://x/feed.zip")
    assert first.status == "updated" and first.stats and first.stats.trips == 8
    assert GtfsStatic(db).meta()["etag"] == '"abc"'

    second = await refresh_feed(data, db, client=client, url="http://x/feed.zip")
    assert second.status == "up_to_date" and calls[-1]["if-none-match"] == '"abc"'

    forced = await refresh_feed(data, db, client=client, url="http://x/feed.zip", force=True)
    assert forced.status == "updated" and "if-none-match" not in calls[-1]

    before = db.read_bytes()
    mode["fail"] = True
    failed = await refresh_feed(data, db, client=client, url="http://x/feed.zip", force=True)
    assert failed.status == "failed" and "500" in failed.detail
    assert db.read_bytes() == before  # a failed refresh never clobbers a working database
    await client.aclose()
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 8


# --- route types and timezones ------------------------------------------------------------


BASIC = [(0, 0), (1, 2), (2, 2), (3, 3), (4, 4), (5, 0), (11, 3), (12, 2)]
EXTENDED = [
    (101, 2),
    (109, 2),
    (204, 3),
    (401, 2),
    (700, 3),
    (714, 3),
    (900, 0),
    (1000, 4),
    (1200, 4),
]
UNSUPPORTED = [(6, None), (7, None), (1100, None), (1300, None)]


@pytest.mark.parametrize(("raw", "expected"), BASIC + EXTENDED + UNSUPPORTED)
def test_route_types_are_normalised(raw: int, expected: int | None) -> None:
    from app.agents.transit.gtfs_static import normalise_route_type

    assert normalise_route_type(raw) == expected


def test_service_times_follow_the_agencys_timezone() -> None:
    from zoneinfo import ZoneInfo

    from app.agents.transit.gtfs_static import at_service_time

    d = date(2026, 1, 15)  # summer: Melbourne is on daylight saving, Brisbane is not
    brisbane = at_service_time(d, 8 * 3600)
    melbourne = at_service_time(d, 8 * 3600, ZoneInfo("Australia/Melbourne"))
    assert brisbane.hour == 8 and melbourne.hour == 8  # both read 08:00 on their own clock
    assert (
        brisbane - melbourne
    ).total_seconds() == 3600  # but Melbourne's 08:00 is an hour earlier
    winter = date(2026, 7, 15)
    assert (
        at_service_time(winter, 0, ZoneInfo("Australia/Melbourne")).utcoffset()
        == brisbane.utcoffset()
    )
