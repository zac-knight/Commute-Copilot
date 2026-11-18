"""GTFS static feed: time parsing, service-day resolution, SQLite ingest and queries.

Conventions (all verified against the real TransLink SEQ feed, 2026-11-17):

* GTFS times are ``HH:MM:SS`` measured from "noon minus 12h" of the *service day*, and can
  exceed 24:00:00 (42,684 of 4.6M stop times do). We store them as integer seconds and convert
  to absolute datetimes with :func:`at_service_time`.
* A trip belongs to a *service date*, not a calendar date: a 25:10:00 departure on service date
  Mon is 01:10 Tue. Queries therefore look at yesterday's, today's and tomorrow's services.
* Timezone is Australia/Brisbane (no daylight saving).
* Only bus (route_type 3) and rail (route_type 2) trips are ingested; ferry/tram are dropped.
"""

from __future__ import annotations

import asyncio
import csv
import difflib
import io
import math
import re
import sqlite3
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import httpx

from app.core.timeutil import TZ

# route_type -> our mode name. Everything else (ferry=4, tram=0, ...) is excluded.
ROUTE_TYPE_MODE = {0: "tram", 2: "train", 3: "bus", 4: "ferry"}  # the engine's route types
TYPE_OF_MODE = {v: k for k, v in ROUTE_TYPE_MODE.items()}
MODE_BIT = {"train": 1, "bus": 2, "tram": 4, "ferry": 8}
_BIT_MODE = {v: k for k, v in MODE_BIT.items()}

SCHEMA_VERSION = "2"


# --- time helpers --------------------------------------------------------------------------


def normalise_route_type(rt: int) -> int | None:
    """Map GTFS basic and extended (HVT) route types onto the engine's four: 0 tram / light rail,
    2 train / metro, 3 bus / coach, 4 ferry. None for modes the planner does not handle (air,
    funicular, cable car): those routes are skipped at ingest."""
    basic = {0: 0, 1: 2, 2: 2, 3: 3, 4: 4, 5: 0, 11: 3, 12: 2}
    if rt in basic:
        return basic[rt]
    if 100 <= rt < 200 or 300 <= rt < 700:  # railway, suburban railway, urban railway, metro
        return 2
    if 200 <= rt < 300 or 700 <= rt < 900:  # coach, bus, trolleybus
        return 3
    if 900 <= rt < 1000:  # tram
        return 0
    if 1000 <= rt < 1100 or 1200 <= rt < 1300:  # water transport, ferry
        return 4
    return None


def parse_gtfs_time(text: str) -> int:
    """``"25:10:00"`` -> 90600. Hours may exceed 23 (trips running past midnight)."""
    parts = text.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"bad GTFS time {text!r}")
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"bad GTFS time {text!r}") from exc
    if h < 0 or not (0 <= m < 60) or not (0 <= s < 60):
        raise ValueError(f"bad GTFS time {text!r}")
    return h * 3600 + m * 60 + s


def format_gtfs_time(secs: int) -> str:
    return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def service_midnight_epoch(d: date, tz: tzinfo = TZ) -> int:
    """Epoch seconds of GTFS 'time zero' for service date ``d`` (noon local minus 12 h)."""
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=tz)
    return int(noon.timestamp()) - 12 * 3600


def at_service_time(d: date, secs: int, tz: tzinfo = TZ) -> datetime:
    return datetime.fromtimestamp(service_midnight_epoch(d, tz) + secs, tz)


def yyyymmdd(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def parse_yyyymmdd(text: str | int) -> date:
    s = str(text)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


# --- ingest --------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE routes (
    route_idx INTEGER PRIMARY KEY, route_id TEXT UNIQUE NOT NULL, short_name TEXT NOT NULL,
    long_name TEXT NOT NULL, route_type INTEGER NOT NULL);
CREATE TABLE stops (
    stop_id TEXT PRIMARY KEY, code TEXT, name TEXT NOT NULL, name_norm TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, location_type INTEGER NOT NULL,
    parent_station TEXT, platform_code TEXT, mode_mask INTEGER NOT NULL DEFAULT 0);
CREATE TABLE services (
    service_idx INTEGER PRIMARY KEY, service_id TEXT UNIQUE NOT NULL,
    mon INT, tue INT, wed INT, thu INT, fri INT, sat INT, sun INT,
    start_date INTEGER NOT NULL, end_date INTEGER NOT NULL);
CREATE TABLE service_exceptions (
    service_idx INTEGER NOT NULL, date INTEGER NOT NULL, exception_type INTEGER NOT NULL);
CREATE TABLE trips (
    trip_idx INTEGER PRIMARY KEY, trip_id TEXT UNIQUE NOT NULL, route_idx INTEGER NOT NULL,
    service_idx INTEGER NOT NULL, headsign TEXT NOT NULL, direction_id INTEGER,
    pattern_idx INTEGER NOT NULL, start_secs INTEGER NOT NULL DEFAULT 0);
CREATE TABLE stop_times (
    trip_idx INTEGER NOT NULL, stop_sequence INTEGER NOT NULL, stop_id TEXT NOT NULL,
    arr INTEGER NOT NULL, dep INTEGER NOT NULL, pickup_type INTEGER NOT NULL,
    drop_off_type INTEGER NOT NULL, PRIMARY KEY (trip_idx, stop_sequence)) WITHOUT ROWID;
"""
_INDEXES = """
CREATE INDEX idx_st_stop_dep ON stop_times(stop_id, dep);
CREATE INDEX idx_exc ON service_exceptions(date, service_idx);
CREATE INDEX idx_trips_service ON trips(service_idx);
CREATE INDEX idx_stops_parent ON stops(parent_station);
"""


def normalise_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", name.lower())).strip()


@dataclass
class IngestStats:
    routes: int = 0
    trips: int = 0
    stop_times: int = 0
    stops: int = 0
    patterns: int = 0
    feed_start: str = ""
    feed_end: str = ""


@dataclass
class _Base:
    """Running offsets so several feeds (e.g. Victoria's per-mode zips) merge into one database."""

    route: int = 0
    service: int = 0
    trip: int = 0
    pattern: int = 0


def _rows_optional(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    if name in zf.namelist():
        yield from _rows(zf, name)


def _rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        # some publishers (NT's Alice Springs feed) put a blank line before the header row
        reader = csv.DictReader(ln for ln in text if ln.strip())
        # WA's stops.txt pads its header ("location_type, parent_station, stop_id"): trim the names
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]
        yield from reader


def ingest_zip(
    zip_path: Path,
    db_path: Path,
    *,
    meta: dict[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> IngestStats:
    """Build a fresh SQLite database from a GTFS zip. Written to a temp file and renamed on
    success, so a failed ingest never clobbers a working database."""
    say = progress or (lambda _m: None)
    tmp = db_path.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    stats = IngestStats()
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(_SCHEMA)
        with zipfile.ZipFile(zip_path) as zf:
            _ingest(zf, conn, stats, say)
        conn.executemany(
            "INSERT INTO meta VALUES (?,?)",
            [
                ("schema_version", SCHEMA_VERSION),
                ("feed_start", stats.feed_start),
                ("feed_end", stats.feed_end),
                ("ingested_at", datetime.now(TZ).isoformat(timespec="seconds")),
                *(meta or {}).items(),
            ],
        )
        say("building indexes")
        conn.executescript(_INDEXES)
        conn.execute("ANALYZE")
        conn.commit()
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    conn.close()
    tmp.replace(db_path)
    return stats


def _ingest(
    zf: zipfile.ZipFile,
    conn: sqlite3.Connection,
    stats: IngestStats,
    say: Callable[[str], None],
    prefix: str = "",
    base: _Base | None = None,
) -> None:
    base = base or _Base()
    # routes (bus + rail only)
    route_info: dict[str, tuple[int, int]] = {}
    for r in _rows(zf, "routes.txt"):
        rtype = normalise_route_type(int(r["route_type"]))
        if rtype is None:
            continue
        idx = base.route + len(route_info) + 1
        route_info[r["route_id"]] = (idx, rtype)
        long_name = r.get("route_long_name", "")
        conn.execute(
            "INSERT INTO routes VALUES (?,?,?,?,?)",
            (idx, prefix + r["route_id"], r.get("route_short_name") or long_name, long_name, rtype),
        )
    base.route += len(route_info)
    stats.routes += len(route_info)

    # services (calendar + calendar_dates)
    service_idx: dict[str, int] = {}
    for c in _rows_optional(zf, "calendar.txt"):
        idx = service_idx.setdefault(c["service_id"], base.service + len(service_idx) + 1)
        conn.execute(
            "INSERT INTO services VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                idx,
                prefix + c["service_id"],
                *(int(c[d]) for d in _DAYS),
                int(c["start_date"]),
                int(c["end_date"]),
            ),
        )
    for e in _rows_optional(zf, "calendar_dates.txt"):
        sid = e["service_id"]
        if sid not in service_idx:  # service defined only through calendar_dates
            idx = service_idx[sid] = base.service + len(service_idx) + 1
            conn.execute(
                "INSERT INTO services VALUES (?,?,0,0,0,0,0,0,0,?,?)",
                (idx, prefix + sid, int(e["date"]), int(e["date"])),
            )
        conn.execute(
            "INSERT INTO service_exceptions VALUES (?,?,?)",
            (service_idx[sid], int(e["date"]), int(e["exception_type"])),
        )

    # trips (kept in memory until patterns are known)
    trips: dict[str, list[Any]] = {}
    for t in _rows(zf, "trips.txt"):
        if t["route_id"] not in route_info or t["service_id"] not in service_idx:
            continue
        trips[t["trip_id"]] = [
            base.trip + len(trips) + 1,
            route_info[t["route_id"]][0],
            service_idx[t["service_id"]],
            t.get("trip_headsign", ""),
            int(t["direction_id"]) if t.get("direction_id") else None,
            0,
            route_info[t["route_id"]][1],
            0,
        ]  # idx, route_idx, service_idx, headsign, direction, pattern_idx, route_type, start_secs
    base.service += len(service_idx)
    stats.trips += len(trips)

    # stop_times: stream; the real feed is grouped by trip_id (verified), which we require.
    say(f"ingesting stop_times for {len(trips)} trips")
    patterns: dict[tuple[Any, ...], int] = {}
    stop_mask: dict[str, int] = {}
    batch: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    current: str | None = None
    cur_rows: list[tuple[Any, ...]] = []

    def flush_trip() -> None:
        if current is None or current not in trips:
            return
        t = trips[current]
        cur_rows.sort(key=lambda r: r[1])
        key = (t[1], t[4], tuple(r[2] for r in cur_rows))
        t[5] = patterns.setdefault(key, base.pattern + len(patterns) + 1)
        t[7] = cur_rows[0][4]  # departure (s) from the first stop
        bit = MODE_BIT[ROUTE_TYPE_MODE[t[6]]]
        for r in cur_rows:
            stop_mask[r[2]] = stop_mask.get(r[2], 0) | bit
        batch.extend(cur_rows)
        stats.stop_times += len(cur_rows)

    for row in _rows(zf, "stop_times.txt"):
        tid = row["trip_id"]
        if tid != current:
            flush_trip()
            cur_rows = []
            if tid in seen:
                raise ValueError(f"stop_times.txt is not grouped by trip_id (at {tid!r})")
            seen.add(tid)
            current = tid
            if len(batch) >= 100_000:
                conn.executemany("INSERT INTO stop_times VALUES (?,?,?,?,?,?,?)", batch)
                batch.clear()
        if tid not in trips:
            continue
        arr = parse_gtfs_time(row["arrival_time"] or row["departure_time"])
        dep = parse_gtfs_time(row["departure_time"] or row["arrival_time"])
        cur_rows.append(
            (
                trips[tid][0],
                int(row["stop_sequence"]),
                prefix + row["stop_id"],
                arr,
                dep,
                int(row["pickup_type"] or 0),
                int(row["drop_off_type"] or 0),
            )
        )
    flush_trip()
    conn.executemany("INSERT INTO stop_times VALUES (?,?,?,?,?,?,?)", batch)
    base.pattern += len(patterns)
    stats.patterns += len(patterns)

    conn.executemany(
        "INSERT INTO trips VALUES (?,?,?,?,?,?,?,?)",
        [(v[0], prefix + k, v[1], v[2], v[3], v[4], v[5], v[7]) for k, v in trips.items()],
    )
    base.trip += len(trips)

    # stops; parent stations inherit the modes of their children
    rows = list(_rows(zf, "stops.txt"))
    for r in rows:
        parent = r.get("parent_station") or ""
        if parent:
            pk = prefix + parent
            stop_mask[pk] = stop_mask.get(pk, 0) | stop_mask.get(prefix + r["stop_id"], 0)
    conn.executemany(
        "INSERT OR IGNORE INTO stops VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                prefix + r["stop_id"],
                r.get("stop_code", ""),
                r["stop_name"],
                normalise_name(r["stop_name"]),
                float(r["stop_lat"]),
                float(r["stop_lon"]),
                int(r.get("location_type") or 0),
                (prefix + r["parent_station"]) if r.get("parent_station") else None,
                r.get("platform_code") or None,
                stop_mask.get(prefix + r["stop_id"], 0),
            )
            for r in rows
            if r.get("stop_lat") and r.get("stop_lon")
        ],
    )
    stats.stops += len(rows)

    feed = list(_rows_optional(zf, "feed_info.txt"))
    if feed and feed[0].get("feed_start_date"):
        start, end = feed[0]["feed_start_date"], feed[0].get("feed_end_date", "")
        stats.feed_start = min(filter(None, [stats.feed_start, start]))
        stats.feed_end = max(filter(None, [stats.feed_end, end]))


_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_COLS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --- queries -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stop:
    stop_id: str
    code: str
    name: str
    lat: float
    lon: float
    location_type: int  # 0 stop/platform, 1 station
    parent_station: str | None
    platform_code: str | None
    modes: tuple[str, ...]


@dataclass(frozen=True)
class StopMatch:
    stop: Stop
    score: float
    distance_m: float | None = None


@dataclass(frozen=True)
class ScheduledDeparture:
    trip_id: str
    service_date: date
    stop_id: str
    stop_sequence: int
    route_id: str
    route_short_name: str
    route_long_name: str
    route_type: int
    headsign: str
    scheduled: datetime
    platform: str | None
    trip_start_secs: int = 0

    @property
    def rt_start_date(self) -> date:
        """The ``start_date`` TransLink's realtime feed uses for this trip.

        Verified live: for trips that START after midnight (static first departure >= 24:00:00)
        realtime reports the *calendar* date of the start, not the GTFS service date.
        """
        return self.service_date + timedelta(days=self.trip_start_secs // 86400)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


class FeedNotLoadedError(Exception):
    pass


class GtfsStatic:
    def __init__(self, db_path: Path, tz: tzinfo = TZ) -> None:
        self.db_path = db_path
        self.tz = (
            tz  # GTFS times are local to the agency: every service-day conversion uses this zone
        )
        self._stops: dict[str, Stop] | None = None
        self._children: dict[str, list[str]] | None = None
        self._norm: list[tuple[str, str, frozenset[str]]] | None = None
        self._services: dict[date, list[int]] = {}

    def reset_caches(self) -> None:
        """Forget in-memory copies after the database file was replaced by a refresh."""
        self._stops = self._children = self._norm = None
        self._services = {}

    # connection per call: cheap, thread-safe, read-only
    def connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FeedNotLoadedError(f"GTFS database not found at {self.db_path}; run refresh")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def meta(self) -> dict[str, str]:
        with self.connect() as c:
            return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM meta")}

    def feed_range(self) -> tuple[date, date]:
        m = self.meta()
        return parse_yyyymmdd(m["feed_start"]), parse_yyyymmdd(m["feed_end"])

    # -- services ------------------------------------------------------------------------

    def active_services(self, d: date) -> list[int]:
        """service_idx values running on service date ``d``: calendar weekday flag within
        [start_date, end_date], then calendar_dates adds (1) and removals (2)."""
        if d in self._services:
            return self._services[d]
        n, col = yyyymmdd(d), _DAY_COLS[d.weekday()]
        with self.connect() as c:
            active = {
                r[0]
                for r in c.execute(
                    f"SELECT service_idx FROM services WHERE {col}=1 AND start_date<=? "
                    "AND end_date>=?",
                    (n, n),
                )
            }
            for idx, exc in c.execute(
                "SELECT service_idx, exception_type FROM service_exceptions WHERE date=?", (n,)
            ):
                if exc == 1:
                    active.add(idx)
                elif exc == 2:
                    active.discard(idx)
        self._services[d] = sorted(active)
        return self._services[d]

    # -- stops ---------------------------------------------------------------------------

    def stops(self) -> dict[str, Stop]:
        if self._stops is None:
            with self.connect() as c:
                rows = c.execute("SELECT * FROM stops").fetchall()
            self._stops = {
                r["stop_id"]: Stop(
                    r["stop_id"],
                    r["code"] or "",
                    r["name"],
                    r["lat"],
                    r["lon"],
                    r["location_type"],
                    r["parent_station"],
                    r["platform_code"],
                    tuple(m for bit, m in _BIT_MODE.items() if r["mode_mask"] & bit),
                )
                for r in rows
            }
            kids: dict[str, list[str]] = {}
            for s in self._stops.values():
                if s.parent_station:
                    kids.setdefault(s.parent_station, []).append(s.stop_id)
            self._children = kids
            self._norm = [
                (s.stop_id, normalise_name(s.name), frozenset(normalise_name(s.name).split()))
                for s in self._stops.values()
                if s.modes
            ]
        return self._stops

    def get_stop(self, stop_id: str) -> Stop | None:
        return self.stops().get(stop_id)

    def platform_stops(self, stop_id: str) -> list[str]:
        """A station expands to its child platforms; a plain stop is itself."""
        self.stops()
        assert self._children is not None
        return self._children.get(stop_id) or [stop_id]

    def find_stops(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
        modes: Sequence[str] | None = None,
        limit: int = 10,
    ) -> list[StopMatch]:
        """Fuzzy name search over stops that are actually served by bus/train."""
        stops = self.stops()
        assert self._norm is not None
        nq = normalise_name(query)
        if not nq:
            return []
        q_tokens = nq.split()
        scored: list[tuple[float, str]] = []
        for sid, name, tokens in self._norm:
            if name == nq:
                scored.append((1.0, sid))
            elif all(any(t.startswith(q) for t in tokens) for q in q_tokens):
                scored.append((0.7 + 0.3 * min(1.0, len(nq) / len(name)), sid))
        if not scored:  # typo tolerance, only when nothing matched exactly/by prefix
            for sid, name, _ in self._norm:
                ratio = difflib.SequenceMatcher(None, nq, name).ratio()
                if ratio >= 0.75:
                    scored.append((0.6 * ratio, sid))
        out: list[StopMatch] = []
        for score, sid in scored:
            s = stops[sid]
            if modes and not set(modes) & set(s.modes):
                continue
            dist = haversine_m(near[0], near[1], s.lat, s.lon) if near else None
            out.append(StopMatch(s, score, dist))
        if near:
            out.sort(key=lambda m: (m.distance_m or 0.0, -m.score))
        else:
            out.sort(key=lambda m: (-m.score, -m.stop.location_type, m.stop.name))
        return out[:limit]

    def stops_within(self, lat: float, lon: float, radius_m: float) -> list[Stop]:
        return [
            s
            for s in self.stops().values()
            if s.modes and s.location_type == 0 and haversine_m(lat, lon, s.lat, s.lon) <= radius_m
        ]

    # -- departures ----------------------------------------------------------------------

    def scheduled_departures(
        self,
        stop_ids: Iterable[str],
        start: datetime,
        window_s: int,
        *,
        route_short_names: Sequence[str] | None = None,
    ) -> list[ScheduledDeparture]:
        """Scheduled departures with start <= time < start+window, across service dates
        (yesterday's trips can run past midnight into today)."""
        ids = list(dict.fromkeys(stop_ids))
        if not ids:
            return []
        start_epoch = int(start.timestamp())
        out: list[ScheduledDeparture] = []
        d0 = start.astimezone(self.tz).date()
        with self.connect() as c:
            for d in (d0 - timedelta(days=1), d0, d0 + timedelta(days=1)):
                lo = start_epoch - service_midnight_epoch(d, self.tz)
                hi = lo + window_s
                if hi < 0 or lo > 60 * 3600:
                    continue
                services = self.active_services(d)
                if not services:
                    continue
                for chunk in _chunks(ids, 400):
                    sql = (
                        "SELECT t.trip_id, st.stop_id, st.stop_sequence, st.dep, r.route_id,"
                        " r.short_name, r.long_name, r.route_type, t.headsign,"
                        " s.platform_code, t.start_secs"
                        " FROM stop_times st JOIN trips t ON t.trip_idx=st.trip_idx"
                        " JOIN routes r ON r.route_idx=t.route_idx"
                        " JOIN stops s ON s.stop_id=st.stop_id"
                        f" WHERE st.stop_id IN ({_ph(chunk)}) AND st.dep>=? AND st.dep<?"
                        f" AND st.pickup_type!=1 AND t.service_idx IN ({_ph(services)})"
                    )
                    params: list[Any] = [*chunk, lo, hi, *services]
                    if route_short_names:
                        sql += f" AND r.short_name COLLATE NOCASE IN ({_ph(route_short_names)})"
                        params += [n for n in route_short_names]
                    for r in c.execute(sql, params):
                        out.append(
                            ScheduledDeparture(
                                r["trip_id"],
                                d,
                                r["stop_id"],
                                r["stop_sequence"],
                                r["route_id"],
                                r["short_name"],
                                r["long_name"],
                                r["route_type"],
                                r["headsign"],
                                at_service_time(d, r["dep"], self.tz),
                                r["platform_code"],
                                r["start_secs"],
                            )
                        )
        out.sort(key=lambda x: x.scheduled)
        return out

    def trip_stop_times(self, trip_id: str) -> list[tuple[int, str, int, int]]:
        """[(stop_sequence, stop_id, arr_s, dep_s)] for one trip."""
        with self.connect() as c:
            return [
                (r[0], r[1], r[2], r[3])
                for r in c.execute(
                    "SELECT st.stop_sequence, st.stop_id, st.arr, st.dep FROM stop_times st"
                    " JOIN trips t ON t.trip_idx=st.trip_idx WHERE t.trip_id=?"
                    " ORDER BY st.stop_sequence",
                    (trip_id,),
                )
            ]

    def trip_start_secs(self, trip_id: str) -> int | None:
        with self.connect() as c:
            r = c.execute("SELECT start_secs FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
        return int(r[0]) if r else None

    def route_by_id(self, route_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            r = c.execute(
                "SELECT route_id, short_name, long_name, route_type FROM routes WHERE route_id=?",
                (route_id,),
            ).fetchone()
        return dict(r) if r else None

    def unknown_route_names(self, names: Sequence[str]) -> list[str]:
        """Route short names (e.g. '680', 'BNSH') that do not exist (case-insensitive)."""
        if not names:
            return []
        with self.connect() as c:
            known = {
                r[0].lower()
                for r in c.execute(
                    "SELECT short_name FROM routes WHERE short_name COLLATE NOCASE IN "
                    f"({_ph(names)})",
                    list(names),
                )
            }
        return [n for n in names if n.lower() not in known]

    def serves_later(self, trip_id: str, after_seq: int, stop_ids: Sequence[str]) -> bool:
        """Does this trip call at any of ``stop_ids`` AFTER stop_sequence ``after_seq``?"""
        if not stop_ids:
            return False
        with self.connect() as c:
            row = c.execute(
                "SELECT 1 FROM stop_times st JOIN trips t ON t.trip_idx=st.trip_idx"
                f" WHERE t.trip_id=? AND st.stop_sequence>? AND st.stop_id IN ({_ph(stop_ids)})"
                " AND st.drop_off_type!=1 LIMIT 1",
                [trip_id, after_seq, *stop_ids],
            ).fetchone()
        return row is not None

    def route_ids_for_names(self, short_names: Sequence[str]) -> list[str]:
        with self.connect() as c:
            return [
                r[0]
                for r in c.execute(
                    f"SELECT route_id FROM routes WHERE short_name IN ({_ph(short_names)})",
                    list(short_names),
                )
            ]


def _ph(items: Sequence[Any]) -> str:
    return ",".join("?" * len(items))


def _chunks(items: list[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


# --- download / refresh --------------------------------------------------------------------

GTFS_URL = "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip"


@dataclass
class RefreshResult:
    status: str  # "updated" | "up_to_date" | "failed"
    detail: str = ""
    stats: IngestStats | None = None


def _prepare_refresh(data_dir: Path, db_path: Path, force: bool) -> dict[str, str]:
    """Blocking setup for a refresh: create the data dir, build conditional-GET headers."""
    data_dir.mkdir(parents=True, exist_ok=True)
    headers: dict[str, str] = {}
    if not force and db_path.exists():
        old = GtfsStatic(db_path).meta()
        if old.get("etag"):
            headers["If-None-Match"] = old["etag"]
        elif old.get("last_modified"):
            headers["If-Modified-Since"] = old["last_modified"]
    return headers


async def refresh_feed(
    data_dir: Path,
    db_path: Path,
    *,
    url: str = GTFS_URL,
    client: httpx.AsyncClient | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> RefreshResult:
    """Conditional-GET the GTFS zip (ETag / Last-Modified) and re-ingest only if it changed.
    A failure never touches the existing database."""
    own = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0), follow_redirects=True)
    say = progress or (lambda _m: None)
    zip_path = data_dir / "SEQ_GTFS.zip"
    try:
        headers = await asyncio.to_thread(_prepare_refresh, data_dir, db_path, force)
        async with http.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 304:
                return RefreshResult("up_to_date", "server reports feed unchanged (304)")
            resp.raise_for_status()
            meta = {
                "etag": resp.headers.get("etag", ""),
                "last_modified": resp.headers.get("last-modified", ""),
                "downloaded_at": datetime.now(TZ).isoformat(timespec="seconds"),
                "source_url": url,
            }
            say("downloading feed")
            tmp = zip_path.with_suffix(".part")
            with tmp.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
        tmp.replace(zip_path)
        stats = await asyncio.to_thread(ingest_zip, zip_path, db_path, meta=meta, progress=say)
        return RefreshResult(
            "updated", f"{stats.trips} trips, {stats.stop_times} stop times", stats
        )
    except Exception as exc:
        return RefreshResult("failed", f"{type(exc).__name__}: {exc}")
    finally:
        if own:
            await http.aclose()
