"""Trip geometry for the UI: turns a ``plan_trip`` result into a map-ready artifact.

The model never sees this (it would cost thousands of tokens of coordinates for no benefit); the
orchestrator builds it in code from the planner's structured output and the GTFS stop tables.

What is and is not measured (stated in the artifact, shown in the UI):
  * Transit legs follow the trip's own stop sequence, so the path is the stop-to-stop polyline of
    the real service (GTFS ``shapes.txt`` is not ingested, so curves between stops are straight).
  * Distances are great-circle (haversine) sums along those polylines, not road or track length.
  * Walking distance is the planner's own estimate between stops (straight line).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise
from typing import Any

from app.agents.transit.gtfs_static import GtfsStatic, Stop, haversine_m
from app.agents.transit.shapes import ShapeStore, clip

MAX_CONTEXT_STOPS = 320
NOTES = (
    "Paths follow each service's road/track shape from the timetable feed (straight between stops "
    "where no shape is available); distances are great-circle sums along them; walks are "
    "straight-line estimates."
)


def _pt(s: Stop) -> list[float]:
    return [round(s.lat, 6), round(s.lon, 6)]


def _length(points: Sequence[Sequence[float]]) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in pairwise(points))


def _leg_stops(static: GtfsStatic, leg: dict[str, Any]) -> list[Stop]:
    """The stops a transit leg passes, boarding stop to alighting stop (inclusive)."""
    times = static.trip_stop_times(leg["trip_id"])
    ids = [t[1] for t in times]
    try:
        i = ids.index(leg["from_stop_id"])
        j = ids.index(leg["to_stop_id"], i + 1)
    except ValueError:
        return []
    out = [static.get_stop(sid) for sid in ids[i : j + 1]]
    return [s for s in out if s is not None]


def _build_leg(
    static: GtfsStatic, leg: dict[str, Any], shapes: ShapeStore | None = None
) -> dict[str, Any]:
    a, b = static.get_stop(leg["from_stop_id"]), static.get_stop(leg["to_stop_id"])
    row: dict[str, Any] = {
        "kind": leg["kind"], "from": leg["from_stop"], "to": leg["to_stop"],
        "depart_iso": leg.get("depart_iso"), "arrive_iso": leg.get("arrive_iso"),
        "depart": leg["depart"], "arrive": leg["arrive"], "mode": leg.get("mode"),
        "stopover": leg.get("stopover"), "stay_min": leg.get("stay_min"),
        "stopover_index": leg.get("stopover_index"),
        "route": leg.get("route"), "route_name": leg.get("route_name"), "headsign": leg.get("headsign"), "trip_id": leg.get("trip_id"),
        "realtime": leg.get("realtime"),
    }  # fmt: skip
    stops: list[Stop] = []
    if leg["kind"] == "transit" and leg.get("trip_id"):
        stops = _leg_stops(static, leg)
    if len(stops) >= 2:
        path = [_pt(s) for s in stops]
        full = shapes.path_for_trip(leg["trip_id"]) if shapes else None
        if full:  # follow the real road/track between boarding and alighting
            road = clip(full, (stops[0].lat, stops[0].lon), (stops[-1].lat, stops[-1].lon))
            if road:
                path = [[round(a, 6), round(b, 6)] for a, b in road]
        row["stops"] = [
            {"name": s.name, "lat": round(s.lat, 6), "lon": round(s.lon, 6)} for s in stops
        ]
        row["distance_m"] = round(_length(path))
        row["num_stops"] = len(stops) - 1
    elif a and b:
        path = [_pt(a), _pt(b)]
        row["distance_m"] = round(leg.get("walk_m") or _length(path))
        row["num_stops"] = 0
    else:
        return {**row, "path": [], "distance_m": 0, "num_stops": 0}
    row["path"] = path
    return row


def _annotate_stages(legs: list[dict[str, Any]]) -> tuple[int, int]:
    """Number the transit legs (stage 1, 2, ...) and record the wait before each leg. Walk legs
    carry their own times, so a gap between consecutive legs is pure waiting. Returns
    (stages, total_wait_min)."""
    stage, wait_total = 0, 0
    prev_arrive: datetime | None = None
    for row in legs:
        if row["kind"] == "transit":
            stage += 1
            row["stage"] = stage
        if prev_arrive is not None and row.get("depart_iso"):
            gap = round(
                (datetime.fromisoformat(row["depart_iso"]) - prev_arrive).total_seconds() / 60
            )
            row["wait_before_min"] = max(0, gap)
            wait_total += max(0, gap)
        if row.get("arrive_iso"):
            prev_arrive = datetime.fromisoformat(row["arrive_iso"])
    return stage, wait_total


def _journey_summary(
    legs: list[dict[str, Any]], out: Stop, dest: Stop, duration_min: int
) -> dict[str, Any]:
    walk = sum(row["distance_m"] for row in legs if row["kind"] == "walk")
    by_mode: dict[str, int] = {}
    for row in legs:
        if row["kind"] == "transit":
            by_mode[row["mode"] or "transit"] = (
                by_mode.get(row["mode"] or "transit", 0) + row["distance_m"]
            )
    transit = sum(by_mode.values())
    total = walk + transit
    straight = round(haversine_m(out.lat, out.lon, dest.lat, dest.lon))
    stages, wait = _annotate_stages(legs)
    modes = [row["mode"] for row in legs if row["kind"] == "transit"]
    return {
        "stages": stages, "wait_min": wait, "all_bus": bool(modes) and all(m == "bus" for m in modes),
        "total_m": total, "walk_m": walk, "transit_m": transit, "by_mode_m": by_mode,
        "straight_line_m": straight, "circuity": round(total / straight, 2) if straight else None,
        "stops_passed": sum(row["num_stops"] for row in legs),
        "avg_speed_kmh": round(total / 1000 / (duration_min / 60), 1) if duration_min else None,
    }  # fmt: skip


def _context_stops(
    static: GtfsStatic, bbox: tuple[float, float, float, float]
) -> list[dict[str, Any]]:
    s0, w0, n0, e0 = bbox
    inside = [
        s for s in static.stops().values()
        if s.modes and s0 <= s.lat <= n0 and w0 <= s.lon <= e0 and s.location_type == 0
    ]  # fmt: skip
    # thin to one stop per ~120 m cell so the backdrop is readable, trains first
    seen: set[tuple[int, int]] = set()
    out = []
    for s in sorted(inside, key=lambda x: (0 if "train" in x.modes else 1, x.name)):
        cell = (round(s.lat / 0.0011), round(s.lon / 0.0013))
        if cell in seen:
            continue
        seen.add(cell)
        out.append({"lat": round(s.lat, 5), "lon": round(s.lon, 5), "train": "train" in s.modes})
        if len(out) >= MAX_CONTEXT_STOPS:
            break
    return out


def build_trip_artifact(
    static: GtfsStatic, plan: dict[str, Any], shapes: ShapeStore | None = None
) -> dict[str, Any] | None:
    """``plan`` is the parsed ``plan_trip`` tool output. None if it has no usable journey."""
    journeys_in = plan.get("journeys") or []
    if not journeys_in:
        return None
    journeys: list[dict[str, Any]] = []
    for ji in journeys_in:
        legs = [_build_leg(static, leg, shapes) for leg in ji["legs"]]
        first, last = ji["legs"][0], ji["legs"][-1]
        a, b = static.get_stop(first["from_stop_id"]), static.get_stop(last["to_stop_id"])
        if a is None or b is None:
            continue
        journeys.append({
            "depart": ji["depart"], "arrive": ji["arrive"], "duration_min": ji["duration_min"],
            "transfers": ji["transfers"], "legs": legs,
            "summary": _journey_summary(legs, a, b, ji["duration_min"]),
            "origin": {"name": first["from_stop"], "lat": a.lat, "lon": a.lon},
            "destination": {"name": last["to_stop"], "lat": b.lat, "lon": b.lon},
        })  # fmt: skip
    if not journeys:
        return None
    pts = [p for j in journeys for row in j["legs"] for p in row["path"]]
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    pad_lat, pad_lon = (
        (max(lats) - min(lats)) * 0.12 + 0.004,
        (max(lons) - min(lons)) * 0.12 + 0.004,
    )
    bbox = (min(lats) - pad_lat, min(lons) - pad_lon, max(lats) + pad_lat, max(lons) + pad_lon)
    return {
        "origin_label": (plan.get("origin") or [{}])[0].get("name", journeys[0]["origin"]["name"]),
        "destination_label": (plan.get("destination") or [{}])[0].get(
            "name", journeys[0]["destination"]["name"]
        ),
        "journeys": journeys, "bbox": [round(x, 5) for x in bbox],
        "context_stops": _context_stops(static, bbox),
        "realtime": plan.get("realtime"), "note": plan.get("note"), "measurement_notes": NOTES,
    }  # fmt: skip
