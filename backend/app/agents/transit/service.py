"""TransitService: the single place where static timetable, realtime, planner and profile meet.
Every public method returns a pydantic result that carries its own data-age / scheduled-only
information, so callers (tools, orchestrator, synthesiser) can never present stale or
timetable-only data as live."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from app.agents.transit.gtfs_realtime import (
    FeedResult,
    RealtimeClient,
    RealtimeSnapshot,
    StopRT,
    resolve_stop_departure,
)
from app.agents.transit.gtfs_static import (
    ROUTE_TYPE_MODE,
    TYPE_OF_MODE,
    GtfsStatic,
    ScheduledDeparture,
    Stop,
)
from app.agents.transit.models import (
    AlertOut,
    AlertsResult,
    CanICatchResult,
    Departure,
    DeparturesResult,
    FindStopsResult,
    JourneyOut,
    LegOut,
    LegRealtime,
    PlanTripResult,
    RealtimeStatus,
    StopInfo,
    StopRef,
)
from app.agents.transit.planner import Journey, JourneyPlanner, Leg
from app.agents.transit.shapes import ShapeStore
from app.core.memory import UserProfile
from app.core.timeutil import TZ, fmt_local

_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


class PlaceNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class Place:
    label: str
    stops: list[Stop]  # platform-level stops (stations expanded)


class TransitService:
    def __init__(
        self,
        static: GtfsStatic,
        realtime: RealtimeClient,
        planner: JourneyPlanner,
        profile: UserProfile,
        *,
        clock: Callable[[], datetime] | None = None,
        on_time_s: int = 60,
        lookback_s: int = 30 * 60,
        window_s: int = 3 * 3600,
    ) -> None:
        self.static = static
        self.realtime = realtime
        self.planner = planner
        self.profile = profile
        self.tz = static.tz  # the agency's local zone (Brisbane for SEQ, Melbourne for VIC, ...)
        self.clock = clock or (lambda: datetime.now(static.tz))
        self.on_time_s = on_time_s
        self.shapes: ShapeStore | None = None  # set by the wiring when the feed zip is on disk
        self.lookback_s = lookback_s
        self.window_s = window_s

    def reload(self) -> None:
        """Pick up a freshly ingested GTFS database (called after a refresh)."""
        self.static.reset_caches()
        self.planner.rebuild()

    # ------------------------------------------------------------------ places

    def resolve_place(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
        radius_m: float = 500.0,
        window: float = 0.15,
        prefer_raw: bool = False,
    ) -> Place:
        """Stop id, station id, 'lat,lon', profile alias ('home') or a fuzzy name -> platforms.

        A name resolves to its best match plus similarly named stops within ``radius_m`` whose
        match score is within ``window`` of the best. The defaults suit "where do I go to"; use
        a wider radius/window for "does this service reach <suburb>" (a bus may terminate at a
        stop 600 m from the main interchange: observed live with the 680 and Chermside).
        ``prefer_raw``: match the user's own words rather than a profile nickname's target (the
        nickname 'chermside' -> 'Chermside bus station' is right for "take me to", too narrow
        for "does this service go to Chermside").
        """
        if "|" in query:  # several ids = one place (the same station defined in more than one feed)
            parts = [
                self.resolve_place(q, near=near, radius_m=radius_m) for q in query.split("|") if q
            ]
            merged = {st.stop_id: st for part in parts for st in part.stops}
            return Place(parts[0].label, list(merged.values()))
        original = query
        query = self.profile.resolve_alias(query)
        stops = self.static.stops()
        if query in stops:
            ids = self.static.platform_stops(query)
            return Place(stops[query].name, [stops[i] for i in ids if stops[i].modes])
        if ll := _LATLON.match(query):
            lat, lon = float(ll[1]), float(ll[2])
            # an address is rarely beside a stop: widen the search until some stops are found
            near_stops: list[Stop] = []
            for radius in (400, 800, 1500, 2500):
                near_stops = self.static.stops_within(lat, lon, radius)
                if near_stops:
                    break
            if not near_stops:
                raise PlaceNotFoundError(
                    f"no bus, train, tram or ferry stops within 2.5 km of {query}: "
                    "this spot is outside the area the timetable covers"
                )
            return Place(f"near {query}", near_stops)
        limit = 30 if radius_m <= 500 else 400
        matches = []
        if prefer_raw and original != query:
            matches = self.static.find_stops(original, near=near, limit=limit)
        matches = matches or self.static.find_stops(query, near=near, limit=limit)
        if not matches:
            raise PlaceNotFoundError(f"no stop found matching {query!r}")
        best = matches[0]
        # one place = the best match plus close-by stops whose names match about as well
        cluster = [
            m.stop
            for m in matches
            if m.score >= best.score - window
            and m.stop.location_type == 0
            and _dist(m.stop, best.stop) <= radius_m
        ]
        for m in matches:  # best match may be a station: add its platforms
            if m is best and best.stop.location_type == 1:
                cluster += [stops[i] for i in self.static.platform_stops(best.stop.stop_id)]
        uniq = {s.stop_id: s for s in cluster if s.modes}
        if not uniq:
            raise PlaceNotFoundError(f"{query!r} matched only non-served stops")
        return Place(best.stop.name, list(uniq.values()))

    # ------------------------------------------------------------------ find_stops

    def find_stops(
        self, query: str, near: str | None, modes: Sequence[str] | None, limit: int = 8
    ) -> FindStopsResult:
        near_ll: tuple[float, float] | None = None
        if near:
            place = self.resolve_place(near)
            near_ll = (
                sum(s.lat for s in place.stops) / len(place.stops),
                sum(s.lon for s in place.stops) / len(place.stops),
            )
        matches = self.static.find_stops(
            self.profile.resolve_alias(query), near=near_ll, modes=modes, limit=limit
        )
        return FindStopsResult(
            query=query,
            stops=[
                StopInfo(
                    stop_id=m.stop.stop_id,
                    name=m.stop.name,
                    modes=list(m.stop.modes),
                    is_station=m.stop.location_type == 1,
                    platform=m.stop.platform_code,
                    distance_m=round(m.distance_m) if m.distance_m is not None else None,
                )
                for m in matches
            ],
        )

    # ------------------------------------------------------------------ realtime status

    @staticmethod
    def _rt_status(feed: FeedResult[RealtimeSnapshot], what: str = "Realtime") -> RealtimeStatus:
        if feed.data is None:
            return RealtimeStatus(
                source="scheduled_only",
                note=f"{what} data unavailable ({feed.error}); showing the timetable only.",
            )
        age = round(feed.age_s) if feed.age_s is not None else None
        note = None
        if feed.stale:
            note = f"{what} refresh failed ({feed.error}); using a copy {age}s old."
        elif age is not None and age > 120:
            note = f"{what} feed timestamp is {age}s old."
        return RealtimeStatus(source="realtime", feed_age_s=age, stale=feed.stale, note=note)

    # ------------------------------------------------------------------ departures

    def _make_departure(
        self, d: ScheduledDeparture, rt: StopRT | None, now: datetime
    ) -> Departure | None:
        stop = self.static.get_stop(d.stop_id)
        assert stop is not None
        sched = d.scheduled
        mode = ROUTE_TYPE_MODE.get(d.route_type, "bus")
        status, predicted, delay, basis = "scheduled_only", None, None, None
        if rt is not None:
            if rt.kind == "skipped":
                return None  # the vehicle will not stop here
            if rt.kind == "cancelled":
                status = "cancelled"
            elif rt.kind in ("explicit", "propagated") and rt.delay_s is not None:
                delay = rt.delay_s
                assert rt.predicted_epoch is not None
                predicted = datetime.fromtimestamp(rt.predicted_epoch, self.tz)
                basis = "reported" if rt.kind == "explicit" else "estimated_from_earlier_stop"
                status = (
                    "delayed"
                    if delay > self.on_time_s
                    else "early"
                    if delay < -self.on_time_s
                    else "on_time"
                )
        return Departure(
            route=d.route_short_name,
            route_name=d.route_long_name,
            mode=mode,
            headsign=d.headsign,
            stop_id=d.stop_id,
            stop_name=stop.name,
            platform=d.platform,
            scheduled=fmt_local(sched, now, self.tz),
            scheduled_iso=sched.isoformat(timespec="seconds"),
            predicted=fmt_local(predicted, now, self.tz) if predicted else None,
            predicted_iso=predicted.isoformat(timespec="seconds") if predicted else None,
            delay_min=round(delay / 60, 1) if delay is not None else None,
            status=status,
            delay_basis=basis,
            trip_id=d.trip_id,
            service_date=d.service_date.isoformat(),
        )

    def check_route_filter(self, route_filter: Sequence[str] | None) -> None:
        """route_filter takes route NUMBERS. Anything else is an error, not an empty answer
        (observed live: the model passed ['bus'] and was told there were no buses)."""
        unknown = self.static.unknown_route_names(list(route_filter or []))
        if unknown:
            raise ValueError(
                f"No route named {unknown}. route_filter takes route numbers/names such as "
                "'680' or 'BNSH'. To limit to buses or trains use `modes`; to limit to services "
                "going to a place use `towards`."
            )

    async def _departures(
        self,
        stop_ids: Sequence[str],
        start: datetime,
        window_s: int,
        route_filter: Sequence[str] | None,
        *,
        towards: Sequence[str] | None = None,
        modes: Sequence[str] | None = None,
    ) -> tuple[list[Departure], RealtimeStatus]:
        """All departures from the stops in [start - lookback, start + window), merged with
        realtime. Unfiltered by predicted time: callers decide what counts as 'after'."""
        now = self.clock()
        sched = await asyncio.to_thread(
            self.static.scheduled_departures,
            stop_ids,
            start - timedelta(seconds=self.lookback_s),
            window_s + self.lookback_s,
            route_short_names=route_filter,
        )
        if modes:
            wanted = {TYPE_OF_MODE[m] for m in modes if m in TYPE_OF_MODE}
            sched = [d for d in sched if d.route_type in wanted]
        if towards:
            dest = list(towards)
            sched = await asyncio.to_thread(
                lambda: [
                    d for d in sched if self.static.serves_later(d.trip_id, d.stop_sequence, dest)
                ]
            )
        feed = await self.realtime.trip_updates()
        snap = feed.data
        out: list[Departure] = []
        for d in sched:
            rt = None
            if snap is not None:
                trip = snap.find(d.trip_id, d.rt_start_date)
                if trip is not None:
                    rt = resolve_stop_departure(
                        trip, d.stop_sequence, d.stop_id, int(d.scheduled.timestamp())
                    )
            dep = self._make_departure(d, rt, now)
            if dep is not None:
                out.append(dep)
        if snap is not None and not towards:  # ADDED trips have no stop list to check against
            out += self._added_departures(snap, set(stop_ids), route_filter, now, modes)
        out.sort(key=_effective)
        return out, self._rt_status(feed)

    def _added_departures(
        self,
        snap: RealtimeSnapshot,
        stop_ids: set[str],
        route_filter: Sequence[str] | None,
        now: datetime,
        modes: Sequence[str] | None = None,
    ) -> list[Departure]:
        """ADDED trips exist only in realtime (no timetable row)."""
        out = []
        for trip in snap.added_trips():
            route = self.static.route_by_id(trip.route_id) if trip.route_id else None
            if route_filter and (
                route is None
                or route["short_name"].lower() not in {r.lower() for r in route_filter}
            ):
                continue
            if (
                modes
                and route is not None
                and ROUTE_TYPE_MODE.get(route["route_type"], "bus") not in modes
            ):
                continue
            for u in trip.updates:
                t = u.departure_time if u.departure_time is not None else u.arrival_time
                if u.stop_id not in stop_ids or t is None or u.relationship != "SCHEDULED":
                    continue
                stop = self.static.get_stop(u.stop_id)
                if stop is None:
                    continue
                when = datetime.fromtimestamp(t, self.tz)
                out.append(
                    Departure(
                        route=route["short_name"] if route else (trip.route_id or "?"),
                        route_name=route["long_name"] if route else "",
                        mode=ROUTE_TYPE_MODE.get(route["route_type"], "bus") if route else "bus",
                        headsign="(added service)",
                        stop_id=u.stop_id,
                        stop_name=stop.name,
                        platform=stop.platform_code,
                        scheduled=fmt_local(when, now, self.tz),
                        scheduled_iso=when.isoformat(timespec="seconds"),
                        predicted=fmt_local(when, now, self.tz),
                        predicted_iso=when.isoformat(timespec="seconds"),
                        delay_min=None,
                        status="added",
                        delay_basis="reported",
                        trip_id=trip.trip_id,
                        service_date=(trip.start_date or when.date()).isoformat(),
                    )
                )
        return out

    async def next_departures(
        self,
        place: str,
        limit: int = 5,
        route_filter: Sequence[str] | None = None,
        after: datetime | None = None,
        *,
        towards: str | None = None,
        modes: Sequence[str] | None = None,
    ) -> DeparturesResult:
        now = self.clock()
        start = after or now
        self.check_route_filter(route_filter)
        resolved = self.resolve_place(place)
        dest = (
            [
                s.stop_id
                for s in self.resolve_place(
                    towards, radius_m=2500, window=0.4, prefer_raw=True
                ).stops
            ]
            if towards
            else None
        )
        deps, status = await self._departures(
            [s.stop_id for s in resolved.stops],
            start,
            self.window_s,
            route_filter,
            towards=dest,
            modes=modes,
        )
        upcoming = [d for d in deps if _effective(d) >= start]
        chosen = upcoming[:limit]
        note = _past_note(start, now, self.tz)
        if not chosen:
            note = (note + " " if note else "") + (
                f"No departures found from {start.astimezone(self.tz):%a %d %b %H:%M} within "
                f"{self.window_s // 3600} h."
            )
            # Say when service resumes instead of leaving the user with a bare 'none'.
            later, _ = await self._departures(
                [s.stop_id for s in resolved.stops], start, 24 * 3600, route_filter,
                towards=dest, modes=modes,
            )  # fmt: skip
            nxt = next(
                (d for d in later if _effective(d) >= start and d.status != "cancelled"), None
            )
            if nxt is not None:
                note += (
                    f" The next departure is {nxt.predicted or nxt.scheduled} "
                    f"(route {nxt.route} to {nxt.headsign}, from {nxt.stop_name})."
                )
                chosen = [nxt]
            else:
                note += " None found in the next 24 hours either."
        elif any(d.status == "cancelled" for d in chosen):
            note = (note + " " if note else "") + "Cancelled trips are included and marked."
        connections: list[JourneyOut] = []
        if towards and len(chosen) < limit:
            # Fewer direct services than asked for: also offer journeys WITH transfers (two buses,
            # bus then train, ...) so "buses to X" never hides the multi-stage options.
            try:
                plan = await self.plan_trip(place, towards, depart_after=start, max_transfers=2)
                connections = [j for j in plan.journeys if j.transfers >= 1][:2]
            except (PlaceNotFoundError, ValueError):
                connections = []
            if connections:
                note = (note + " " if note else "") + (
                    f"Only {len(chosen)} direct service(s); {len(connections)} option(s) with "
                    "transfers are listed under 'connections'."
                )
        return DeparturesResult(
            stops=[StopRef(stop_id=s.stop_id, name=s.name) for s in resolved.stops],
            departures=chosen,
            realtime=status,
            generated_at=now.isoformat(timespec="seconds"),
            note=note,
            connections=connections,
        )

    # ------------------------------------------------------------------ can_i_catch

    async def can_i_catch(
        self,
        place: str,
        my_arrival: datetime,
        *,
        trip_id: str | None = None,
        scheduled_departure: datetime | None = None,
        route_filter: Sequence[str] | None = None,
    ) -> CanICatchResult:
        self.check_route_filter(route_filter)
        resolved = self.resolve_place(place)
        anchor = scheduled_departure or my_arrival
        deps, status = await self._departures(
            [s.stop_id for s in resolved.stops],
            anchor - timedelta(minutes=5),
            self.window_s,
            route_filter,
        )
        now = self.clock()
        if trip_id:
            cands = [d for d in deps if d.trip_id == trip_id]
        elif scheduled_departure:
            cands = [
                d
                for d in deps
                if abs(
                    (datetime.fromisoformat(d.scheduled_iso) - scheduled_departure).total_seconds()
                )
                <= 3 * 60
            ]
        else:
            return CanICatchResult(
                can_catch=None,
                reason="Give either trip_id or scheduled_departure to identify the service.",
            )
        if not cands:
            return CanICatchResult(
                can_catch=None,
                reason="No matching departure found at that stop and time. It may not run, or "
                "the time/route may be wrong.",
            )
        dep = min(cands, key=lambda d: abs((_effective(d) - anchor).total_seconds()))
        mine = my_arrival.astimezone(self.tz)
        basis = (
            "realtime"
            if dep.status != "scheduled_only" and status.source == "realtime"
            else "scheduled_only"
        )
        if dep.status == "cancelled":
            return CanICatchResult(
                can_catch=False,
                departure=dep,
                my_arrival=fmt_local(mine, now, self.tz),
                basis=basis,
                reason="That trip is cancelled.",
            )
        slack = (_effective(dep) - mine).total_seconds() / 60
        reason = (
            f"Departs {dep.predicted or dep.scheduled} "
            f"({'realtime' if basis == 'realtime' else 'timetable only, no realtime'}); "
            f"you arrive {fmt_local(mine, now, self.tz)}: {abs(slack):.0f} min "
            f"{'to spare' if slack >= 0 else 'too late'}."
        )
        if dep.delay_basis == "estimated_from_earlier_stop":
            reason += " The delay is estimated from an earlier stop."
        return CanICatchResult(
            can_catch=slack >= 0,
            slack_min=round(slack, 1),
            departure=dep,
            my_arrival=fmt_local(mine, now, self.tz),
            basis=basis,
            reason=reason,
        )

    # ------------------------------------------------------------------ live vehicles

    def _route_info(self, route_id: str) -> tuple[str, str, str]:
        cache = self.__dict__.setdefault("_route_cache", {})
        if route_id not in cache:
            r = self.static.route_by_id(route_id)
            kind = {2: "train", 3: "bus", 4: "ferry", 0: "tram"}.get(
                int(r["route_type"]) if r else 3, "bus"
            )
            cache[route_id] = (
                (r["short_name"] if r else route_id) or route_id,
                kind,
                (r["long_name"] if r else "") or "",
            )
        return cache[route_id]  # type: ignore[no-any-return]

    def _headsigns(self, trip_ids: Sequence[str]) -> dict[str, str]:
        """Destination text per trip (cached: a trip's headsign never changes within a feed)."""
        cache: dict[str, str] = self.__dict__.setdefault("_headsign_cache", {})
        missing = [t for t in dict.fromkeys(trip_ids) if t and t not in cache]
        if missing:
            with self.static.connect() as c:
                for i in range(0, len(missing), 400):
                    chunk = missing[i : i + 400]
                    marks = ",".join("?" * len(chunk))
                    for row in c.execute(
                        f"SELECT trip_id, headsign FROM trips WHERE trip_id IN ({marks})", chunk
                    ):
                        cache[row["trip_id"]] = row["headsign"] or ""
            for t in missing:
                cache.setdefault(t, "")
        return {t: cache.get(t, "") for t in trip_ids}

    async def vehicles(
        self,
        bbox: tuple[float, float, float, float] | None,
        *,
        trip_ids: Sequence[str] = (),
        routes: Sequence[str] = (),
        modes: Sequence[str] = (),
        limit: int = 800,
    ) -> dict[str, Any]:
        """Live vehicle positions (GTFS-realtime VehiclePositions) inside ``bbox`` = (south, west,
        north, east). Vehicles on ``trip_ids`` (the user's own services) always come back and are
        flagged ``on_trip``; ``routes`` (short names) also always come back."""
        feed = await self.realtime.vehicles()
        if feed.data is None:
            return {"vehicles": [], "age_s": None, "available": False, "error": feed.error}
        wanted = set(trip_ids)
        want_routes = set(routes)
        out: list[dict[str, Any]] = []
        for v in feed.data.vehicles:
            mine = v.trip_id in wanted
            route, mode, long_name = self._route_info(v.route_id) if v.route_id else ("", "bus", "")
            inside = bbox is None or (bbox[0] <= v.lat <= bbox[2] and bbox[1] <= v.lon <= bbox[3])
            if not (mine or inside or route in want_routes):
                continue
            if modes and mode not in modes and not mine:
                continue
            out.append({
                "id": v.vehicle_id, "label": v.label, "route": route, "mode": mode,
                "route_name": long_name,
                "lat": round(v.lat, 6), "lon": round(v.lon, 6),
                "bearing": round(v.bearing) if v.bearing is not None else None,
                "speed_kmh": round(v.speed_ms * 3.6) if v.speed_ms is not None else None,
                "status": v.status, "timestamp": v.timestamp, "trip_id": v.trip_id,
                "on_trip": mine,
            })  # fmt: skip
        out.sort(key=lambda r: (not r["on_trip"], r["id"]))
        out = out[:limit]
        heads = self._headsigns([r["trip_id"] for r in out])
        for r in out:
            r["headsign"] = heads.get(r["trip_id"], "")
        return {
            "vehicles": out,
            "total": len(out),
            "age_s": round(feed.age_s or 0, 1),
            "stale": feed.stale,
            "available": True,
            "error": feed.error,
        }

    def train_lines(self) -> list[dict[str, Any]]:
        """One representative path per train line pattern (a line and its reverse count once), for
        the network view. Uses the road/track shape when ready, else the stops."""
        shapes = self.shapes
        ready = shapes is not None and shapes.db_path.exists()
        cached: list[dict[str, Any]] | None = self.__dict__.get("_train_lines")
        if cached is not None and (self.__dict__.get("_train_lines_road") or not ready):
            return cached
        with self.static.connect() as c:
            rows = c.execute(
                "SELECT r.short_name, r.long_name, MIN(t.trip_id) AS trip_id FROM routes r"
                " JOIN trips t ON t.route_idx=r.route_idx WHERE r.route_type=2"
                " GROUP BY r.route_idx ORDER BY r.short_name"
            ).fetchall()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            code = row["short_name"]
            key = "".join(sorted([code[:2], code[2:]])) if len(code) == 4 else code
            if key in seen:
                continue
            seen.add(key)
            road = (shapes.path_for_trip(row["trip_id"]) if shapes and ready else None) or []
            if not road:
                road = [
                    (s.lat, s.lon)
                    for s in (
                        self.static.get_stop(x[1])
                        for x in self.static.trip_stop_times(row["trip_id"])
                    )
                    if s is not None
                ]
            kept: list[list[float]] = []
            for lat, lon in road:  # thin to ~30 m spacing: plenty for a network map, far smaller
                if not kept or _approx_m(kept[-1], (lat, lon)) >= 30:
                    kept.append([round(lat, 5), round(lon, 5)])
            if road and kept[-1] != [round(road[-1][0], 5), round(road[-1][1], 5)]:
                kept.append([round(road[-1][0], 5), round(road[-1][1], 5)])
            out.append({
                "key": key, "route": code, "route_name": row["long_name"], "path": kept,
                "road": bool(shapes and ready),
            })  # fmt: skip
        self.__dict__["_train_lines"] = out
        self.__dict__["_train_lines_road"] = bool(shapes and ready)
        return out

    def trip_path(self, trip_id: str) -> dict[str, Any] | None:
        """A service's whole run: stop sequence as a polyline, for highlighting it on the map."""
        times = self.static.trip_stop_times(trip_id)
        with self.static.connect() as c:
            row = c.execute(
                "SELECT t.headsign, r.short_name, r.long_name, r.route_type FROM trips t"
                " JOIN routes r ON r.route_idx=t.route_idx WHERE t.trip_id=?",
                (trip_id,),
            ).fetchone()
        if not times or row is None:
            return None
        stops = []
        for _seq, sid, _arr, _dep in times:
            st = self.static.get_stop(sid)
            if st is not None:
                stops.append({"name": st.name, "lat": round(st.lat, 6), "lon": round(st.lon, 6)})
        kind = {2: "train", 3: "bus", 4: "ferry", 0: "tram"}.get(int(row["route_type"]), "bus")
        road = (self.shapes.path_for_trip(trip_id) if self.shapes else None) or []
        return {
            "trip_id": trip_id, "route": row["short_name"], "route_name": row["long_name"],
            "mode": kind,
            "headsign": row["headsign"], "stops": stops,
            "path": [list(p) for p in road],
        }  # fmt: skip

    # ------------------------------------------------------------------ alerts

    async def alerts(
        self, route_ids: Sequence[str] | None, stop_ids: Sequence[str] | None, limit: int = 10
    ) -> AlertsResult:
        feed = await self.realtime.alerts()
        if feed.data is None:
            return AlertsResult(
                alerts=[],
                realtime=RealtimeStatus(
                    source="scheduled_only",
                    note=f"Alerts feed unavailable ({feed.error}); alert status unknown.",
                ),
                total_matching=0,
            )
        routes: set[str] = set()
        for r in route_ids or []:  # accept route ids or short names ("66")
            routes |= {r} | set(self.static.route_ids_for_names([r]))
        stops: set[str] = set()
        for s in stop_ids or []:
            stops |= {s} | set(self.static.platform_stops(s))
            st = self.static.get_stop(s)
            if st and st.parent_station:
                stops.add(st.parent_station)
        now = self.clock().timestamp()
        matching = []
        for a in feed.data.alerts:
            if not a.active_at(now):
                continue
            if (routes or stops) and not (a.route_ids & routes or a.stop_ids & stops):
                continue
            matching.append(a)
        return AlertsResult(
            alerts=[
                AlertOut(
                    alert_id=a.alert_id,
                    effect=a.effect,
                    cause=a.cause,
                    header=a.header,
                    description=a.description[:600],
                    url=a.url,
                    route_ids=sorted(a.route_ids)[:10],
                    stop_ids=sorted(a.stop_ids)[:10],
                    active_now=True,
                )
                for a in matching[:limit]
            ],
            realtime=RealtimeStatus(
                source="realtime",
                feed_age_s=round(feed.age_s) if feed.age_s is not None else None,
                stale=feed.stale,
                note=f"Alerts refresh failed ({feed.error})." if feed.stale else None,
            ),
            total_matching=len(matching),
        )

    # ------------------------------------------------------------------ plan_trip

    async def plan_trip(
        self,
        origin: str,
        destination: str,
        *,
        depart_after: datetime | None = None,
        arrive_by: datetime | None = None,
        max_transfers: int = 2,
    ) -> PlanTripResult:
        now = self.clock()
        o, d = self.resolve_place(origin), self.resolve_place(destination)
        orig = {s.stop_id: 0 for s in o.stops}
        dest = {s.stop_id: 0 for s in d.stops}
        if arrive_by is not None:
            journeys = await asyncio.to_thread(
                self.planner.plan_arrive_by, orig, dest, arrive_by, max_transfers
            )
        else:
            journeys = await asyncio.to_thread(
                self.planner.plan, orig, dest, depart_after or now, max_transfers
            )
        feed = await self.realtime.trip_updates()
        note = _past_note(depart_after or arrive_by, now, self.tz)
        if not journeys and arrive_by is None:
            # Nothing soon (e.g. overnight, or a service gap): look much further ahead and say when
            # service resumes instead of just reporting 'none'.
            later = await asyncio.to_thread(
                self.planner.plan, orig, dest, depart_after or now, max_transfers,
                horizon_s=16 * 3600,
            )  # fmt: skip
            if later:
                journeys = later
                gap = later[0].depart.astimezone(self.tz)
                note = (note + " " if note else "") + (
                    f"No journey leaves in the next {self.planner.horizon_s // 3600} hours. The "
                    f"earliest one leaves {fmt_local(gap, now, self.tz)}."
                )
        out = [self._journey_out(j, feed.data, now) for j in journeys]
        if not out:
            note = (note + " " if note else "") + (
                "No journey found within the search window and transfer limit."
            )
        return PlanTripResult(
            origin=[StopRef(stop_id=s.stop_id, name=s.name) for s in o.stops],
            destination=[StopRef(stop_id=s.stop_id, name=s.name) for s in d.stops],
            journeys=out,
            basis=(
                "Timetable (scheduled) search; realtime delays/cancellations are attached per "
                "boarding but connections are NOT re-validated against them."
            ),
            realtime=self._rt_status(feed),
            note=note,
        )

    def _journey_out(self, j: Journey, snap: RealtimeSnapshot | None, now: datetime) -> JourneyOut:
        return JourneyOut(
            depart=fmt_local(j.depart, now, self.tz),
            arrive=fmt_local(j.arrive, now, self.tz),
            depart_iso=j.depart.isoformat(timespec="seconds"),
            arrive_iso=j.arrive.isoformat(timespec="seconds"),
            duration_min=round((j.arrive - j.depart).total_seconds() / 60),
            transfers=j.transfers,
            legs=[self._leg_out(leg, snap, now) for leg in j.legs],
        )

    def _leg_out(self, leg: Leg, snap: RealtimeSnapshot | None, now: datetime) -> LegOut:
        a, b = self.static.get_stop(leg.from_stop), self.static.get_stop(leg.to_stop)
        assert a is not None and b is not None
        rt_out = None
        if leg.kind == "transit" and snap is not None and leg.trip_id and leg.service_date:
            start = self.static.trip_start_secs(leg.trip_id) or 0
            trip = snap.find(leg.trip_id, leg.service_date + timedelta(days=start // 86400))
            if trip is not None and leg.board_sequence is not None:
                rt = resolve_stop_departure(
                    trip, leg.board_sequence, leg.from_stop, int(leg.depart.timestamp())
                )
                if rt.kind == "cancelled":
                    rt_out = LegRealtime(status="cancelled")
                elif rt.kind in ("explicit", "propagated") and rt.delay_s is not None:
                    assert rt.predicted_epoch is not None
                    rt_out = LegRealtime(
                        status="delayed"
                        if rt.delay_s > self.on_time_s
                        else "early"
                        if rt.delay_s < -self.on_time_s
                        else "on_time",
                        delay_min=round(rt.delay_s / 60, 1),
                        predicted_departure=fmt_local(
                            datetime.fromtimestamp(rt.predicted_epoch, self.tz), now, self.tz
                        ),
                        delay_basis="reported"
                        if rt.kind == "explicit"
                        else "estimated_from_earlier_stop",
                    )
        return LegOut(
            kind=leg.kind,
            from_stop=a.name,
            from_stop_id=a.stop_id,
            to_stop=b.name,
            to_stop_id=b.stop_id,
            depart=fmt_local(leg.depart, now, self.tz),
            arrive=fmt_local(leg.arrive, now, self.tz),
            depart_iso=leg.depart.isoformat(timespec="seconds"),
            arrive_iso=leg.arrive.isoformat(timespec="seconds"),
            route=leg.route_short_name,
            route_name=leg.route_long_name,
            mode=(
                ROUTE_TYPE_MODE.get(3 if leg.route_type is None else leg.route_type, "bus")
                if leg.kind == "transit"
                else None
            ),
            headsign=leg.headsign,
            num_stops=leg.num_stops or None,
            walk_m=round(leg.walk_m) if leg.kind == "walk" else None,
            trip_id=leg.trip_id,
            realtime=rt_out,
        )


def _past_note(start: datetime | None, now: datetime, tz: tzinfo = TZ) -> str | None:
    """Warn when a requested start time is already in the past (a bare 'HH:MM' means today, so
    a morning time asked about late at night silently refers to a morning that has gone)."""
    if start is None or start >= now - timedelta(minutes=10):
        return None
    return (
        f"WARNING: the requested time {start.astimezone(tz):%a %d %b %H:%M} is already in the "
        f"past (now {now.astimezone(tz):%a %d %b %H:%M}). If you meant a later day, call again "
        f"with the day included, e.g. 'tomorrow 07:30'."
    )


def _approx_m(a: Sequence[float], b: Sequence[float]) -> float:
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * 0.89
    return float((dx * dx + dy * dy) ** 0.5)


def _effective(d: Departure) -> datetime:
    return datetime.fromisoformat(d.predicted_iso or d.scheduled_iso)


def _dist(a: Stop, b: Stop) -> float:
    from app.agents.transit.gtfs_static import haversine_m

    return haversine_m(a.lat, a.lon, b.lat, b.lon)
