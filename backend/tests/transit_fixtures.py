"""A tiny hand-made GTFS feed and realtime builders. Nothing here touches the network.

Map (all in Brisbane, ~1 km apart unless stated):
    A "Alpha Street" --R1 bus 100--> B "Bravo Road" --> C "Charlie Plaza"
    C2 "Charlie Plaza stop 2" is ~100 m from C (walking transfer)
    C2 --R2 train RAIL--> D1 "Dee Station, platform 1"        (station DSTN has D1 and D2)
    A --R4 bus 200 (direct, slow)--> D2 "Dee Station, platform 2"
    F "Ferry Wharf": ferry only, must not be ingested.
Calendar: SVC_WK = Mon-Fri 2026-11-02..30 except Wed 2026-11-11; SVC_SAT = Sat, plus Thu 2026-11-12.
2026-11-09 is a Monday.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as pb

from app.agents.transit.gtfs_static import GtfsStatic, at_service_time, ingest_zip

MON = date(2026, 11, 9)


def _csv(header: list[str], rows: list[list[object]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


STOPS = [
    # id, code, name, desc, lat, lon, type, parent, platform
    ["A", "A", "Alpha Street", "", -27.2300, 153.0200, 0, "", ""],
    ["B", "B", "Bravo Road", "", -27.2200, 153.0300, 0, "", ""],
    ["C", "C", "Charlie Plaza", "", -27.2100, 153.0400, 0, "", ""],
    ["C2", "C2", "Charlie Plaza stop 2", "", -27.2091, 153.0400, 0, "", ""],
    ["DSTN", "DSTN", "Dee Station", "", -27.1900, 153.0500, 1, "", ""],
    ["D1", "D1", "Dee Station, platform 1", "", -27.1900, 153.0500, 0, "DSTN", "1"],
    ["D2", "D2", "Dee Station, platform 2", "", -27.1901, 153.0500, 0, "DSTN", "2"],
    ["F", "F", "Ferry Wharf", "", -27.3000, 153.1000, 0, "", ""],
    ["E", "E", "Echo Far", "", -27.0000, 153.0000, 0, "", ""],
]
ROUTES = [
    ["R1", "100", "Alpha - Charlie", "", 3],
    ["R2", "RAIL", "Charlie - Dee", "", 2],
    ["R3", "FERRY", "Ferry", "", 4],
    ["R4", "200", "Alpha - Dee direct", "", 3],
]
CALENDAR = [
    ["SVC_WK", 1, 1, 1, 1, 1, 0, 0, 20261102, 20261130],
    ["SVC_SAT", 0, 0, 0, 0, 0, 1, 0, 20261102, 20261130],
]
CALENDAR_DATES = [["SVC_WK", 20261111, 2], ["SVC_SAT", 20261112, 1]]
# route, service, trip_id, headsign, direction
TRIPS = [
    ["R1", "SVC_WK", "T1", "Charlie Plaza", 0],
    ["R1", "SVC_WK", "T2", "Charlie Plaza", 0],
    ["R2", "SVC_WK", "T3", "Dee Station", 0],
    ["R1", "SVC_WK", "T4", "Charlie Plaza", 0],  # night, runs past midnight
    ["R2", "SVC_WK", "T5", "Dee Station", 0],  # starts after midnight (24:30)
    ["R3", "SVC_WK", "T6", "Ferry", 0],
    ["R4", "SVC_WK", "T8", "Dee Station", 0],
    ["R1", "SVC_SAT", "T9", "Charlie Plaza", 0],
]
# trip, stop, arr, dep, seq, pickup, dropoff  (last stop of each trip: pickup=1)
STOP_TIMES = [
    ["T1", "A", "08:00:00", "08:00:00", 1, 0, 0],
    ["T1", "B", "08:10:00", "08:10:00", 2, 0, 0],
    ["T1", "C", "08:20:00", "08:20:00", 3, 1, 0],
    ["T2", "A", "08:30:00", "08:30:00", 1, 0, 0],
    ["T2", "B", "08:40:00", "08:40:00", 2, 0, 0],
    ["T2", "C", "08:50:00", "08:50:00", 3, 1, 0],
    ["T3", "C2", "08:25:00", "08:25:00", 1, 0, 0],
    ["T3", "D1", "08:45:00", "08:45:00", 2, 1, 0],
    ["T4", "A", "23:50:00", "23:50:00", 1, 0, 0],
    ["T4", "B", "24:05:00", "24:05:00", 2, 0, 0],
    ["T4", "C", "24:20:00", "24:20:00", 3, 1, 0],
    ["T5", "C2", "24:30:00", "24:30:00", 1, 0, 0],
    ["T5", "D1", "24:50:00", "24:50:00", 2, 1, 0],
    ["T6", "F", "09:00:00", "09:00:00", 1, 0, 0],
    ["T6", "A", "09:10:00", "09:10:00", 2, 1, 0],
    ["T8", "A", "08:05:00", "08:05:00", 1, 0, 0],
    ["T8", "D2", "09:30:00", "09:30:00", 2, 1, 0],
    ["T9", "A", "10:00:00", "10:00:00", 1, 0, 0],
    ["T9", "B", "10:10:00", "10:10:00", 2, 1, 0],
]


def write_gtfs_zip(path: Path) -> Path:
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
            CALENDAR,
        ),
        "calendar_dates.txt": _csv(["service_id", "date", "exception_type"], CALENDAR_DATES),
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
            [[r[0], r[1], r[2], r[3], r[4], r[5], r[6]] for r in STOP_TIMES],
        ),
        "feed_info.txt": _csv(
            ["feed_publisher_name", "feed_start_date", "feed_end_date"],
            [["test", 20261102, 20261130]],
        ),
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


def build_static(tmp: Path) -> GtfsStatic:
    zip_path = write_gtfs_zip(tmp / "mini.zip")
    db = tmp / "gtfs.sqlite"
    ingest_zip(zip_path, db, meta={"etag": '"v1"'})
    return GtfsStatic(db)


# --- realtime builders ---------------------------------------------------------------------


def epoch(d: date, hhmmss: str) -> int:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return int(at_service_time(d, h * 3600 + m * 60 + s).timestamp())


def trip_update_feed(trips: list[dict[str, object]], *, timestamp: int = 1_000_000_000) -> bytes:
    """trips: [{trip_id, start_date 'YYYYMMDD', rel 'SCHEDULED'|..., route_id?, updates: [
    {seq?, stop_id, rel?, arr_delay?, arr_time?, dep_delay?, dep_time?}]}]"""
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = timestamp
    for i, t in enumerate(trips):
        ent = feed.entity.add()
        ent.id = str(i)
        tu = ent.trip_update
        tu.trip.trip_id = str(t["trip_id"])
        tu.trip.start_date = str(t.get("start_date", ""))
        tu.trip.schedule_relationship = pb.TripDescriptor.ScheduleRelationship.Value(
            str(t.get("rel", "SCHEDULED"))
        )
        if t.get("route_id"):
            tu.trip.route_id = str(t["route_id"])
        for u in t.get("updates", []):  # type: ignore[attr-defined]
            s = tu.stop_time_update.add()
            if "seq" in u:
                s.stop_sequence = u["seq"]
            s.stop_id = u["stop_id"]
            s.schedule_relationship = pb.TripUpdate.StopTimeUpdate.ScheduleRelationship.Value(
                u.get("rel", "SCHEDULED")
            )
            if "arr_delay" in u:
                s.arrival.delay = u["arr_delay"]
            if "arr_time" in u:
                s.arrival.time = u["arr_time"]
            if "dep_delay" in u:
                s.departure.delay = u["dep_delay"]
            if "dep_time" in u:
                s.departure.time = u["dep_time"]
    return feed.SerializeToString()


def alert_feed(alerts: list[dict[str, object]], *, timestamp: int = 1_000_000_000) -> bytes:
    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = timestamp
    for i, a in enumerate(alerts):
        ent = feed.entity.add()
        ent.id = str(a.get("id", i))
        al = ent.alert
        for text, field in (
            (a.get("header", ""), al.header_text),
            (a.get("desc", ""), al.description_text),
        ):
            for lang, value in (a.get("translations") or {"en": text}).items():  # type: ignore[attr-defined]
                tr = field.translation.add()
                tr.text, tr.language = value, lang
        for start, end in a.get("periods", []):  # type: ignore[attr-defined]
            p = al.active_period.add()
            p.start = start
            if end is not None:
                p.end = end
        for r in a.get("routes", []):  # type: ignore[attr-defined]
            al.informed_entity.add().route_id = r
        for s in a.get("stops", []):  # type: ignore[attr-defined]
            al.informed_entity.add().stop_id = s
        al.effect = pb.Alert.Effect.Value(str(a.get("effect", "DETOUR")))
        al.cause = pb.Alert.Cause.Value(str(a.get("cause", "UNKNOWN_CAUSE")))
    return feed.SerializeToString()
