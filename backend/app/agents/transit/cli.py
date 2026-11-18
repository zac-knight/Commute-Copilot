"""Query the transit data layer WITHOUT an LLM.

    uv run python -m app.agents.transit.cli refresh
    uv run python -m app.agents.transit.cli stops "north lakes"
    uv run python -m app.agents.transit.cli departures home --limit 5 --route 680
    uv run python -m app.agents.transit.cli plan home chermside --depart 08:00
    uv run python -m app.agents.transit.cli plan home "south bank" --arrive 09:00
    uv run python -m app.agents.transit.cli alerts --route 680
    uv run python -m app.agents.transit.cli catch home 08:05 --scheduled 08:12
Add --json before the command for machine-readable output.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.agents.transit.gtfs_realtime import RealtimeClient
from app.agents.transit.gtfs_static import GtfsStatic, ingest_zip, refresh_feed
from app.agents.transit.models import (
    AlertsResult,
    CanICatchResult,
    DeparturesResult,
    FindStopsResult,
    PlanTripResult,
    RealtimeStatus,
)
from app.agents.transit.planner import JourneyPlanner
from app.agents.transit.service import TransitService
from app.config import Settings, get_settings
from app.core.memory import load_profile
from app.core.timeutil import parse_when


def build_service(settings: Settings) -> tuple[TransitService, RealtimeClient]:
    static = GtfsStatic(settings.gtfs_dir / "gtfs.sqlite")
    planner = JourneyPlanner(
        static,
        walk_speed_m_per_min=settings.walk_speed_m_per_min,
        transfer_radius_m=settings.transfer_radius_m,
        min_transfer_s=settings.min_transfer_s,
    )
    rt = RealtimeClient(ttl_s=settings.rt_ttl_seconds)
    svc = TransitService(
        static,
        rt,
        planner,
        load_profile(settings.profile_path),
        on_time_s=settings.on_time_threshold_s,
    )
    return svc, rt


def _rt_line(r: RealtimeStatus) -> str:
    if r.source == "scheduled_only":
        return f"!! TIMETABLE ONLY - {r.note}"
    age = f"{r.feed_age_s}s old" if r.feed_age_s is not None else "age unknown"
    return f"realtime ({age})" + (f" - {r.note}" if r.note else "")


def render(result: BaseModel) -> str:
    lines: list[str] = []
    if isinstance(result, FindStopsResult):
        for s in result.stops:
            extra = f" {s.distance_m} m" if s.distance_m is not None else ""
            kind = "station" if s.is_station else f"platform {s.platform}" if s.platform else "stop"
            lines.append(f"{s.stop_id:<14}{s.name}  [{','.join(s.modes)}; {kind}]{extra}")
    elif isinstance(result, DeparturesResult):
        lines.append(f"From: {', '.join(sorted({s.name for s in result.stops}))[:100]}")
        lines.append(_rt_line(result.realtime))
        for d in result.departures:
            when = d.scheduled + (
                f" -> {d.predicted}" if d.predicted and d.predicted != d.scheduled else ""
            )
            delay = f" ({d.delay_min:+.1f} min, {d.delay_basis})" if d.delay_min is not None else ""
            plat = f" plat {d.platform}" if d.platform else ""
            lines.append(
                f"  {d.route:<6}{d.headsign[:34]:<35}{when:<26}{d.status}{delay}  @{d.stop_name}{plat}"
            )
        if result.note:
            lines.append(f"note: {result.note}")
    elif isinstance(result, PlanTripResult):
        lines.append(_rt_line(result.realtime))
        for j in result.journeys:
            lines.append(
                f"== {j.depart} -> {j.arrive}  {j.duration_min} min, {j.transfers} transfer(s)"
            )
            for leg in j.legs:
                if leg.kind == "walk":
                    lines.append(
                        f"   walk {leg.walk_m} m  {leg.from_stop} -> {leg.to_stop}  {leg.depart}-{leg.arrive}"
                    )
                else:
                    rt = ""
                    if leg.realtime:
                        rt = f"  [{leg.realtime.status}"
                        rt += (
                            f" {leg.realtime.delay_min:+.1f} min"
                            if leg.realtime.delay_min is not None
                            else ""
                        )
                        rt += "]"
                    lines.append(
                        f"   {leg.route} to {leg.headsign}: {leg.from_stop} {leg.depart} -> {leg.to_stop} {leg.arrive} ({leg.num_stops} stops){rt}"
                    )
        if result.note:
            lines.append(f"note: {result.note}")
        lines.append(f"basis: {result.basis}")
    elif isinstance(result, AlertsResult):
        lines.append(_rt_line(result.realtime))
        lines.append(f"{result.total_matching} matching alert(s), showing {len(result.alerts)}")
        for a in result.alerts:
            lines.append(f"  [{a.effect}] {a.header}  routes={a.route_ids[:4]}")
    elif isinstance(result, CanICatchResult):
        verdict = {True: "YES", False: "NO", None: "UNKNOWN"}[result.can_catch]
        lines.append(f"{verdict}: {result.reason}")
    return "\n".join(lines)


async def _refresh(settings: Settings, args: argparse.Namespace) -> int:
    """Download the TransLink timetable (or, with --zip, ingest a file you already have)."""
    say = lambda m: print(f"  {m}", flush=True)  # noqa: E731
    db = settings.gtfs_dir / "gtfs.sqlite"
    if args.zip:
        settings.gtfs_dir.mkdir(parents=True, exist_ok=True)
        stats = await asyncio.to_thread(ingest_zip, Path(args.zip), db, progress=say)
        print(f"updated: {stats.trips} trips, {stats.stop_times} stop times, {stats.stops} stops")
        return 0
    res = await refresh_feed(settings.gtfs_dir, db, force=args.force, progress=say)
    print(f"{res.status}: {res.detail}")
    return 0 if res.status != "failed" else 1


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.cmd == "refresh":
        return await _refresh(settings, args)

    svc, rt = build_service(settings)
    try:
        now = svc.clock()
        result: BaseModel
        if args.cmd == "stops":
            result = svc.find_stops(args.query, args.near, args.mode or None)
        elif args.cmd == "departures":
            result = await svc.next_departures(
                args.place,
                args.limit,
                args.route or None,
                parse_when(args.after, now) if args.after else None,
            )
        elif args.cmd == "plan":
            result = await svc.plan_trip(
                args.origin,
                args.destination,
                depart_after=parse_when(args.depart, now) if args.depart else None,
                arrive_by=parse_when(args.arrive, now) if args.arrive else None,
                max_transfers=args.transfers,
            )
        elif args.cmd == "alerts":
            result = await svc.alerts(args.route or None, args.stop or None)
        else:  # catch
            result = await svc.can_i_catch(
                args.place,
                parse_when(args.arrival, now),
                scheduled_departure=parse_when(args.scheduled, now) if args.scheduled else None,
                trip_id=args.trip,
                route_filter=args.route or None,
            )
        print(result.model_dump_json(indent=2) if args.json else render(result))
        return 0
    finally:
        await rt.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh", help="download/refresh the GTFS static feed")
    r.add_argument("--force", action="store_true")
    r.add_argument("--zip", help="ingest an already-downloaded zip instead of downloading")
    s = sub.add_parser("stops")
    s.add_argument("query")
    s.add_argument("--near")
    s.add_argument("--mode", action="append", choices=["bus", "train"])
    d = sub.add_parser("departures")
    d.add_argument("place")
    d.add_argument("--limit", type=int, default=5)
    d.add_argument("--route", action="append")
    d.add_argument("--after")
    p = sub.add_parser("plan")
    p.add_argument("origin")
    p.add_argument("destination")
    p.add_argument("--depart")
    p.add_argument("--arrive")
    p.add_argument("--transfers", type=int, default=2)
    a = sub.add_parser("alerts")
    a.add_argument("--route", action="append")
    a.add_argument("--stop", action="append")
    c = sub.add_parser("catch")
    c.add_argument("place")
    c.add_argument("arrival")
    c.add_argument("--scheduled")
    c.add_argument("--trip")
    c.add_argument("--route", action="append")
    args: Any = ap.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # CLI boundary: print a readable message, not a traceback
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
