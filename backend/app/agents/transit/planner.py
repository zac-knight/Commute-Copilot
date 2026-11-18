"""Own-built journey planner over the GTFS static data (bus + rail).

Algorithm: RAPTOR-style round-based earliest-arrival search.

* Round k finds the best arrival at every stop using exactly k vehicle trips, so the number of
  transfers is explicit and bounded (``max_transfers`` -> ``max_transfers + 1`` rounds).
* Each round: for every stop improved in the previous round, board the earliest trip of each
  stop *pattern* (route + direction + ordered stop list) departing after we arrive (plus a
  minimum transfer time after the first leg), ride it, and record better arrivals downstream.
  Then relax walking footpaths to stops within ``transfer_radius_m`` at ``walk_speed_m_per_min``.
* Timetable rows are fetched lazily from SQLite (indexed on stop_id, dep) instead of loading
  4.6M stop times into memory. Trips are evaluated across yesterday/today/tomorrow service dates
  so after-midnight (24:00+) trips work.
* The search is on the *schedule*. Realtime delays are layered on afterwards by the service.
* ``arrive_by`` is solved by binary-searching the departure time of the forward search (arrival
  time is monotone in departure time), not by a separate backward search.

Known simplifications: no pure-walking journeys, no fare/comfort criteria, one best trip per
pattern per boarding stop, footpaths are straight-line distance.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from app.agents.transit.gtfs_static import (
    GtfsStatic,
    Stop,
    haversine_m,
    service_midnight_epoch,
)

INF = 1 << 60


@dataclass(frozen=True)
class Leg:
    kind: Literal["walk", "transit"]
    from_stop: str
    to_stop: str
    depart: datetime
    arrive: datetime
    walk_m: float = 0.0
    # transit-only
    trip_id: str | None = None
    service_date: date | None = None
    route_id: str | None = None
    route_short_name: str | None = None
    route_long_name: str | None = None
    route_type: int | None = None
    headsign: str | None = None
    board_sequence: int | None = None
    alight_sequence: int | None = None
    num_stops: int = 0


@dataclass(frozen=True)
class Journey:
    legs: tuple[Leg, ...]
    depart: datetime
    arrive: datetime

    @property
    def transfers(self) -> int:
        return max(0, sum(1 for leg in self.legs if leg.kind == "transit") - 1)


@dataclass(frozen=True)
class _TripRef:
    trip_idx: int
    trip_id: str
    service_date: date
    board_seq: int
    board_stop: str
    board_epoch: int
    alight_seq: int


@dataclass
class _Label:
    arrival: int
    kind: Literal["origin", "walk", "transit"]
    prev_stop: str | None = None
    prev_round: int = 0
    trip: _TripRef | None = None
    walk_m: float = 0.0


class StopGrid:
    """Spatial hash of served platforms for fast 'stops within R metres' lookups."""

    CELL = 0.004  # degrees (~440 m lat)

    def __init__(self, stops: Sequence[Stop]) -> None:
        self._cells: dict[tuple[int, int], list[Stop]] = defaultdict(list)
        for s in stops:
            self._cells[self._key(s.lat, s.lon)].append(s)

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return int(lat // self.CELL), int(lon // self.CELL)

    def within(self, lat: float, lon: float, radius_m: float) -> list[tuple[Stop, float]]:
        kx, ky = self._key(lat, lon)
        span = int(radius_m / 400) + 1
        out = []
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for s in self._cells.get((kx + dx, ky + dy), ()):
                    d = haversine_m(lat, lon, s.lat, s.lon)
                    if d <= radius_m:
                        out.append((s, d))
        return out


class JourneyPlanner:
    def __init__(
        self,
        static: GtfsStatic,
        *,
        walk_speed_m_per_min: float = 80.0,
        transfer_radius_m: float = 250.0,
        min_transfer_s: int = 60,
        horizon_s: int = 4 * 3600,
    ) -> None:
        self.static = static
        self.walk_speed = walk_speed_m_per_min
        self.radius = transfer_radius_m
        self.min_transfer_s = min_transfer_s
        self.horizon_s = horizon_s
        self.rebuild()

    def rebuild(self) -> None:
        """(Re)build the stop index; call after the static feed was refreshed."""
        platforms = [s for s in self.static.stops().values() if s.modes and s.location_type == 0]
        self._grid = StopGrid(platforms)
        self._stops = self.static.stops()
        self._neighbours: dict[str, list[tuple[str, float]]] = {}

    def walk_seconds(self, metres: float) -> int:
        return round(metres / self.walk_speed * 60)

    def _near(self, stop_id: str) -> list[tuple[str, float]]:
        if stop_id not in self._neighbours:
            s = self._stops[stop_id]
            self._neighbours[stop_id] = [
                (n.stop_id, d)
                for n, d in self._grid.within(s.lat, s.lon, self.radius)
                if n.stop_id != stop_id
            ]
        return self._neighbours[stop_id]

    # ------------------------------------------------------------------ public API

    def plan(
        self,
        origins: Mapping[str, int],
        destinations: Mapping[str, int],
        depart_after: datetime,
        max_transfers: int = 2,
        *,
        horizon_s: int | None = None,
    ) -> list[Journey]:
        """Pareto set of journeys (fewest transfers vs earliest arrival), earliest-arrival
        first. ``origins``/``destinations`` map stop_id -> walking seconds to/from the true
        place. Empty list when nothing is reachable within the horizon."""
        return _Search(self, origins, destinations, depart_after, max_transfers, horizon_s).run()

    def plan_arrive_by(
        self,
        origins: Mapping[str, int],
        destinations: Mapping[str, int],
        arrive_by: datetime,
        max_transfers: int = 2,
        *,
        search_back_s: int = 3 * 3600,
    ) -> list[Journey]:
        """Journeys arriving no later than ``arrive_by``, leaving as late as possible."""
        lo = int(arrive_by.timestamp()) - search_back_s
        hi = int(arrive_by.timestamp())
        best: list[Journey] = []
        limit = arrive_by
        while hi - lo > 60:  # one-minute resolution
            mid = (lo + hi) // 2
            found = [
                j
                for j in self.plan(
                    origins,
                    destinations,
                    datetime.fromtimestamp(mid, self.static.tz),
                    max_transfers,
                )
                if j.arrive <= limit
            ]
            if found:
                best, lo = found, mid
            else:
                hi = mid
        return best


class _Search:
    def __init__(
        self,
        planner: JourneyPlanner,
        origins: Mapping[str, int],
        destinations: Mapping[str, int],
        depart_after: datetime,
        max_transfers: int,
        horizon_s: int | None = None,
    ) -> None:
        self.p = planner
        self.horizon_s = horizon_s or planner.horizon_s
        self.static = planner.static
        self.origins = {s: w for s, w in origins.items() if s in planner._stops}
        self.dests = {s: w for s, w in destinations.items() if s in planner._stops}
        self.t0 = int(depart_after.timestamp())
        self.max_rounds = max_transfers + 1
        self.best: dict[str, int] = {}
        self.rounds: list[dict[str, _Label]] = []
        self.target = INF  # best known arrival at the destination place
        self._trip_cache: dict[int, list[tuple[str, int, int, int]]] = {}
        self._conn = self.static.connect()

    # -- helpers --

    def _dest_arrival(self, stop: str, arrival: int) -> int:
        return arrival + self.dests[stop] if stop in self.dests else INF

    def _boardings(self, stop: str, ready: int) -> list[tuple[int, str, date, int, int]]:
        """Earliest trip per pattern departing ``stop`` at/after ``ready``.
        Returns (trip_idx, trip_id, service_date, stop_sequence, dep_epoch)."""
        d0 = datetime.fromtimestamp(ready, self.static.tz).date()
        best: dict[int, tuple[int, str, date, int, int]] = {}
        for d in (d0 - timedelta(days=1), d0, d0 + timedelta(days=1)):
            base = service_midnight_epoch(d, self.static.tz)
            lo = ready - base
            hi = lo + self.horizon_s
            if hi < 0:
                continue
            services = self.static.active_services(d)
            if not services:
                continue
            ph = ",".join("?" * len(services))
            rows = self._conn.execute(
                "SELECT t.pattern_idx, st.trip_idx, t.trip_id, st.stop_sequence, MIN(st.dep)"
                " FROM stop_times st JOIN trips t ON t.trip_idx=st.trip_idx"
                " WHERE st.stop_id=? AND st.dep>=? AND st.dep<? AND st.pickup_type!=1"
                f" AND t.service_idx IN ({ph}) GROUP BY t.pattern_idx",
                [stop, lo, hi, *services],
            ).fetchall()
            for pat, trip_idx, trip_id, seq, dep in rows:
                epoch = base + dep
                cur = best.get(pat)
                if cur is None or epoch < cur[4]:
                    best[pat] = (trip_idx, trip_id, d, seq, epoch)
        return list(best.values())

    def _downstream(self, trip_idx: int, after_seq: int) -> list[tuple[str, int, int, int]]:
        """[(stop_id, seq, arr_secs, drop_off_type)] for stops after ``after_seq``."""
        if trip_idx not in self._trip_cache:
            self._trip_cache[trip_idx] = [
                (r[0], r[1], r[2], r[3])
                for r in self._conn.execute(
                    "SELECT stop_id, stop_sequence, arr, drop_off_type FROM stop_times"
                    " WHERE trip_idx=? ORDER BY stop_sequence",
                    (trip_idx,),
                )
            ]
        return [r for r in self._trip_cache[trip_idx] if r[1] > after_seq]

    def _relax_walks(self, labels: dict[str, _Label], k: int, from_stops: list[str]) -> list[str]:
        improved: list[str] = []
        for q in from_stops:
            tq = labels[q].arrival
            for r, dist in self.p._near(q):
                arr = tq + self.p.walk_seconds(dist)
                if arr < self.best.get(r, INF) and arr < self.target:
                    labels[r] = _Label(arr, "walk", q, k, None, dist)
                    self.best[r] = arr
                    improved.append(r)
        return improved

    def _update_target(self, labels: dict[str, _Label]) -> None:
        for s, lab in labels.items():
            self.target = min(self.target, self._dest_arrival(s, lab.arrival))

    # -- main loop --

    def run(self) -> list[Journey]:
        try:
            return self._run()
        finally:
            self._conn.close()

    def _run(self) -> list[Journey]:
        if not self.origins or not self.dests:
            return []
        r0: dict[str, _Label] = {}
        for s, w in self.origins.items():
            r0[s] = _Label(self.t0 + w, "origin")
            self.best[s] = self.t0 + w
        marked = list(r0)
        marked += self._relax_walks(r0, 0, list(r0))
        self.rounds.append(r0)
        self._update_target(r0)

        found: list[tuple[int, int, str]] = []  # (arrival, round, dest stop)
        for k in range(1, self.max_rounds + 1):
            labels: dict[str, _Label] = {}
            scanned: dict[tuple[int, date], int] = {}
            prev = self.rounds[k - 1]
            for p in marked:
                lab = prev[p]
                if lab.arrival >= self.target or lab.arrival > self.t0 + max(
                    12 * 3600, 2 * self.horizon_s
                ):
                    continue
                ready = lab.arrival + (self.p.min_transfer_s if lab.kind == "transit" else 0)
                for trip_idx, trip_id, d, seq, dep in self._boardings(p, ready):
                    key = (trip_idx, d)
                    if key in scanned and scanned[key] <= seq:
                        continue  # already ridden from an earlier (or same) boarding stop
                    scanned[key] = seq
                    base = service_midnight_epoch(d, self.static.tz)
                    for stop, sseq, arr_s, drop in self._downstream(trip_idx, seq):
                        if drop == 1:
                            continue
                        arr = base + arr_s
                        cur = labels.get(stop)
                        if (
                            arr < self.best.get(stop, INF)
                            and arr < self.target
                            and (cur is None or arr < cur.arrival)
                        ):
                            ref = _TripRef(trip_idx, trip_id, d, seq, p, dep, sseq)
                            labels[stop] = _Label(arr, "transit", p, k - 1, ref)
            if not labels:
                break
            for s, lab in labels.items():
                self.best[s] = min(self.best.get(s, INF), lab.arrival)
            improved = list(labels)
            improved += self._relax_walks(labels, k, list(labels))
            self.rounds.append(labels)
            for s, lab in labels.items():
                da = self._dest_arrival(s, lab.arrival)
                if da < INF:
                    found.append((da, k, s))
            self._update_target(labels)
            marked = improved

        # Pareto: keep a journey only if it arrives strictly earlier than every one with fewer
        # transfers.
        journeys: list[Journey] = []
        best_arr = INF
        for k in range(1, len(self.rounds)):
            cands = [(a, s) for a, kk, s in found if kk == k]
            if not cands:
                continue
            arr, stop = min(cands)
            if arr < best_arr:
                best_arr = arr
                journeys.append(self._build(k, stop))
        journeys.sort(key=lambda j: (j.arrive, j.transfers))
        return journeys

    # -- reconstruction --

    def _build(self, k: int, stop: str) -> Journey:
        legs: list[Leg] = []
        while True:
            lab = self.rounds[k][stop]
            if lab.kind == "origin":
                break
            assert lab.prev_stop is not None
            if lab.kind == "walk":
                prev_arr = self.rounds[lab.prev_round][lab.prev_stop].arrival
                legs.append(
                    Leg(
                        "walk",
                        lab.prev_stop,
                        stop,
                        datetime.fromtimestamp(prev_arr, self.static.tz),
                        datetime.fromtimestamp(lab.arrival, self.static.tz),
                        walk_m=round(lab.walk_m),
                    )
                )
                stop, k = lab.prev_stop, lab.prev_round
            else:
                ref = lab.trip
                assert ref is not None
                meta = self._conn.execute(
                    "SELECT r.route_id, r.short_name, r.long_name, r.route_type, t.headsign"
                    " FROM trips t JOIN routes r ON r.route_idx=t.route_idx WHERE t.trip_idx=?",
                    (ref.trip_idx,),
                ).fetchone()
                n_stops = sum(
                    1
                    for r in self._downstream(ref.trip_idx, ref.board_seq)
                    if r[1] <= ref.alight_seq
                )
                legs.append(
                    Leg(
                        "transit",
                        ref.board_stop,
                        stop,
                        datetime.fromtimestamp(ref.board_epoch, self.static.tz),
                        datetime.fromtimestamp(lab.arrival, self.static.tz),
                        trip_id=ref.trip_id,
                        service_date=ref.service_date,
                        route_id=meta[0],
                        route_short_name=meta[1],
                        route_long_name=meta[2],
                        route_type=meta[3],
                        headsign=meta[4],
                        board_sequence=ref.board_seq,
                        alight_sequence=ref.alight_seq,
                        num_stops=n_stops,
                    )
                )
                stop, k = ref.board_stop, lab.prev_round
        legs.reverse()
        return Journey(tuple(legs), legs[0].depart, legs[-1].arrive)
