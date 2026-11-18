"""Journey-planner endpoints for the web UI: place search, plan, departures board, alerts.

Thin wrappers over ``TransitService`` (no LLM involved): the same planner the assistant uses, so
results match what it would say. Read-only; nothing here can change state.

The planner returns a small Pareto set per search (fewest transfers vs earliest arrival). A trip
planner wants a list of *times*, so ``/plan`` searches in rounds, each starting just after the
previous best departure (or, for arrive-by, just before the previous best arrival), and merges.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from app.agents.transit.geo import build_trip_artifact
from app.agents.transit.service import PlaceNotFoundError, TransitService

log = logging.getLogger(__name__)
Text = Annotated[str, Query(min_length=1, max_length=120)]

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# South East Queensland box for address lookup (west, north, east, south): results outside it are
# useless to a SEQ transit planner.
SEQ_VIEWBOX = "152.3,-26.4,153.6,-28.4"
MAX_JOURNEYS = 8


def _station_key(svc: TransitService, stop_id: str) -> tuple[str, str]:
    """(id, display name) of the station a stop belongs to (platforms collapse to their station)."""
    st = svc.static.get_stop(stop_id)
    if st is not None and st.parent_station:
        parent = svc.static.get_stop(st.parent_station)
        if parent is not None:
            return parent.stop_id, parent.name
    return stop_id, st.name if st else stop_id


async def _geocode(q: str, limit: int) -> list[dict[str, Any]]:
    """Addresses and landmarks from OpenStreetMap Nominatim. Only called when the UI asks for it
    (the search text leaves the machine), with a generic client name."""
    try:
        async with httpx.AsyncClient(
            timeout=6.0, headers={"User-Agent": "commute-agents-local"}
        ) as c:
            r = await c.get(NOMINATIM, params={
                "q": q, "format": "jsonv2", "limit": limit, "viewbox": SEQ_VIEWBOX,
                "bounded": 1, "countrycodes": "au",
            })  # fmt: skip
            r.raise_for_status()
            rows = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("geocode failed: %s", exc)
        return []
    out = []
    for row in rows:
        name = str(row.get("display_name", "")).split(",")
        label = ", ".join(p.strip() for p in name[:3])
        out.append({
            "id": f"{float(row['lat']):.6f},{float(row['lon']):.6f}", "name": label,
            "kind": "address", "modes": [], "lat": float(row["lat"]), "lon": float(row["lon"]),
        })  # fmt: skip
    return out


def _mins(a: str, b: str) -> int:
    """Whole minutes from ISO time ``a`` to ``b``."""
    return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60)


def _signature(j: dict[str, Any]) -> tuple[Any, ...]:
    legs = tuple(
        (leg["kind"], leg.get("trip_id"), leg["from_stop_id"], leg["to_stop_id"])
        for leg in j["legs"]
    )
    return (j["depart_iso"], j["arrive_iso"], legs)


def make_router(get_state: Any) -> APIRouter:
    router = APIRouter()

    def transit(request: Request) -> TransitService:
        svc: TransitService | None = get_state(request).transit
        if svc is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No timetable data loaded. "
                    "Run `uv run python -m app.agents.transit.cli refresh`."
                ),
            )
        return svc

    @router.get("/places")
    async def places(
        request: Request,
        q: Annotated[str, Query(max_length=120)] = "",
        limit: Annotated[int, Query(ge=1, le=20)] = 8,
        geocode: bool = False,
    ) -> dict[str, Any]:
        """Autocomplete: saved places (home, destinations) first, then stops/stations by name,
        then (``geocode=1``) addresses/landmarks from OpenStreetMap. Platforms collapse."""
        s = get_state(request)
        svc = transit(request)
        q = q.strip()
        saved: list[dict[str, Any]] = []
        aliases = {"home": s.profile.home, **s.profile.destinations}
        for alias, target in aliases.items():
            if not q or q.lower() in alias.lower() or q.lower() in target.lower():
                try:
                    place = svc.resolve_place(alias)
                except PlaceNotFoundError:
                    continue
                saved.append({
                    "id": alias, "name": place.label, "alias": alias, "kind": "saved",
                    "modes": sorted({m for st in place.stops for m in st.modes}),
                    "lat": sum(st.lat for st in place.stops) / len(place.stops),
                    "lon": sum(st.lon for st in place.stops) / len(place.stops),
                })  # fmt: skip
        found: list[dict[str, Any]] = []
        if q:
            seen: set[str] = set()
            for m in await asyncio.to_thread(svc.static.find_stops, q, limit=limit * 5):
                sid, name = _station_key(svc, m.stop.stop_id)
                if sid in seen:
                    continue
                seen.add(sid)
                parent = svc.static.get_stop(sid) or m.stop
                same = next(
                    (
                        f
                        for f in found
                        if f["name"].lower() == name.lower()
                        and abs(f["lat"] - parent.lat) < 0.004
                        and abs(f["lon"] - parent.lon) < 0.005
                    ),
                    None,
                )
                if same is not None:  # the same place defined in another feed: one result, all ids
                    same["id"] = f"{same['id']}|{sid}"
                    same["modes"] = sorted({*same["modes"], *m.stop.modes})
                    continue
                found.append({
                    "id": sid, "name": name, "modes": sorted(m.stop.modes),
                    "kind": "station" if "train" in m.stop.modes else "stop",
                    "lat": parent.lat, "lon": parent.lon,
                })  # fmt: skip
                if len(found) >= limit:
                    break
        addresses = await _geocode(q, 5) if geocode and len(q) >= 3 else []
        return {"places": (saved[:3] + found + addresses)[: limit + 5]}

    async def segment(
        svc: TransitService,
        origin: str,
        destination: str,
        at: datetime | None,
        by: str,
        max_transfers: int,
        rounds: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Several options for one A -> B hop (searched in rounds, merged, de-duplicated)."""
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        base: dict[str, Any] | None = None
        cursor = at
        for _round in range(rounds):
            try:
                result = await svc.plan_trip(
                    origin, destination,
                    depart_after=cursor if by == "leave" else None,
                    arrive_by=cursor if by == "arrive" and cursor else None,
                    max_transfers=max_transfers,
                )  # fmt: skip
            except PlaceNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            res = result.model_dump()
            if base is None:
                base = res
            fresh = [j for j in res["journeys"] if _signature(j) not in merged]
            if not fresh:
                break
            for j in fresh:
                merged[_signature(j)] = j
            if by == "leave":
                nxt = min(datetime.fromisoformat(j["depart_iso"]) for j in fresh)
                cursor = nxt + timedelta(minutes=1)
            else:
                nxt = min(datetime.fromisoformat(j["arrive_iso"]) for j in fresh)
                cursor = nxt - timedelta(minutes=1)
            if len(merged) >= MAX_JOURNEYS:
                break
        assert base is not None
        journeys = sorted(merged.values(), key=lambda j: j["depart_iso"])
        if by == "arrive":
            journeys.sort(key=lambda j: j["arrive_iso"])
        return journeys[:MAX_JOURNEYS], base

    async def chain(
        svc: TransitService,
        points: list[str],
        first: dict[str, Any],
        by: str,
        stay: int,
        max_transfers: int,
        labels: list[str],
    ) -> dict[str, Any] | None:
        """Extend one option of the first hop (leave) / last hop (arrive) across the other hops,
        taking the best connection each time and allowing ``stay`` minutes at every stop."""
        parts = [first]
        gap = timedelta(minutes=stay)
        if by == "leave":
            for k in range(1, len(points) - 1):
                after = datetime.fromisoformat(parts[-1]["arrive_iso"]) + gap
                res = (await svc.plan_trip(points[k], points[k + 1], depart_after=after,
                                           max_transfers=max_transfers)).model_dump()  # fmt: skip
                if not res["journeys"]:
                    return None
                labels.append(res["origin"][0]["name"] if res["origin"] else points[k])
                parts.append(min(res["journeys"], key=lambda j: j["arrive_iso"]))
        else:
            for k in range(len(points) - 3, -1, -1):
                limit = datetime.fromisoformat(parts[0]["depart_iso"]) - gap
                res = (await svc.plan_trip(points[k], points[k + 1], arrive_by=limit,
                                           max_transfers=max_transfers)).model_dump()  # fmt: skip
                if not res["journeys"]:
                    return None
                parts.insert(0, max(res["journeys"], key=lambda j: j["depart_iso"]))
        legs: list[dict[str, Any]] = []
        for i, part in enumerate(parts):
            part_legs = [dict(leg) for leg in part["legs"]]
            if i > 0:
                gap_min = _mins(parts[i - 1]["arrive_iso"], part["depart_iso"])
                part_legs[0]["stopover"] = part_legs[0]["from_stop"]
                part_legs[0]["stopover_index"] = i
                part_legs[0]["stay_min"] = max(0, gap_min)
            legs.extend(part_legs)
        dep, arr = parts[0], parts[-1]
        return {
            "depart": dep["depart"], "arrive": arr["arrive"],
            "depart_iso": dep["depart_iso"], "arrive_iso": arr["arrive_iso"],
            "duration_min": _mins(dep["depart_iso"], arr["arrive_iso"]),
            "transfers": sum(p["transfers"] for p in parts), "legs": legs,
        }  # fmt: skip

    @router.get("/plan")
    async def plan(
        request: Request,
        origin: Text,
        destination: Text,
        when: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")] = None,
        by: Literal["leave", "arrive"] = "leave",
        max_transfers: Annotated[int, Query(ge=0, le=4)] = 2,
        rounds: Annotated[int, Query(ge=1, le=6)] = 4,
        via: Annotated[list[str] | None, Query(max_length=3)] = None,
        stay: Annotated[int, Query(ge=0, le=240)] = 5,
    ) -> dict[str, Any]:
        """``when``: Brisbane time ``YYYY-MM-DDTHH:MM`` (absent = now). ``via``: up to 3
        stops to visit in order (repeat the parameter), ``stay`` minutes at each."""
        svc = transit(request)
        now = svc.clock()
        at: datetime | None = None
        if when:
            try:
                at = datetime.fromisoformat(when).replace(tzinfo=svc.tz)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"bad time: {exc}") from exc
        stops = [v for v in (via or []) if v.strip()]
        if not stops:
            journeys, base = await segment(svc, origin, destination, at, by, max_transfers, rounds)
            base["journeys"] = journeys
        else:
            points = [origin, *stops, destination]
            if by == "leave":
                firsts, base = await segment(
                    svc, points[0], points[1], at or now, "leave", max_transfers, 3
                )
            else:
                firsts, base = await segment(
                    svc, points[-2], points[-1], at or now, "arrive", max_transfers, 3
                )
            labels: list[str] = []
            built = await asyncio.gather(
                *(chain(svc, points, f, by, stay, max_transfers, labels) for f in firsts[:4])
            )
            journeys = sorted((j for j in built if j), key=lambda j: j["depart_iso"])
            base["journeys"] = journeys
            base["note"] = (
                None if journeys else "No connection found through every stop at that time."
            )
        artifact = build_trip_artifact(svc.static, base, svc.shapes)
        return {
            "now": now.astimezone(svc.tz).isoformat(timespec="seconds"),
            "artifact": artifact, "note": base.get("note"), "basis": base.get("basis"),
            "realtime": base.get("realtime"),
        }  # fmt: skip

    @router.get("/departures")
    async def departures(
        request: Request,
        stop: Text,
        limit: Annotated[int, Query(ge=1, le=15)] = 8,
    ) -> dict[str, Any]:
        svc = transit(request)
        try:
            res = await svc.next_departures(stop, limit=limit)
        except PlaceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return res.model_dump()

    @router.get("/alerts")
    async def alerts(
        request: Request,
        routes: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=20)] = 8,
    ) -> dict[str, Any]:
        svc = transit(request)
        names = [r.strip() for r in routes.split(",") if r.strip()]
        res = await svc.alerts(names or None, None, limit)
        return res.model_dump()

    @router.get("/vehicles")
    async def vehicles(
        request: Request,
        bbox: Annotated[str | None, Query(pattern=r"^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$")] = None,
        trips: Annotated[str, Query(max_length=2000)] = "",
        routes: Annotated[str, Query(max_length=200)] = "",
        modes: Annotated[str, Query(max_length=60, pattern=r"^[a-z,]*$")] = "",
    ) -> dict[str, Any]:
        """Live buses and trains. ``bbox`` = south,west,north,east; ``trips`` = trip ids to flag
        (and always include); ``routes`` = route short names to always include."""
        svc = transit(request)
        box = tuple(float(x) for x in bbox.split(",")) if bbox else None
        if box is not None and not (box[0] < box[2] and box[1] < box[3]):
            raise HTTPException(status_code=422, detail="bbox must be south,west,north,east")
        return await svc.vehicles(
            box,  # type: ignore[arg-type]
            trip_ids=[t for t in trips.split(",") if t],
            routes=[r for r in routes.split(",") if r],
            modes=[m for m in modes.split(",") if m],
        )

    @router.get("/train-lines")
    async def train_lines(request: Request) -> dict[str, Any]:
        """Every train line's route (one path per pattern), for the network view."""
        svc = transit(request)
        lines = await asyncio.to_thread(svc.train_lines)
        return {"lines": lines}

    @router.get("/trip-path")
    async def trip_path(request: Request, trip_id: Text) -> dict[str, Any]:
        """Full stop sequence of one service (for highlighting a vehicle's route)."""
        svc = transit(request)
        out = await asyncio.to_thread(svc.trip_path, trip_id)
        if out is None:
            raise HTTPException(status_code=404, detail="unknown trip")
        return out

    return router
