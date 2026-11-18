"""Background poller: every 60 s, record the merged scheduled+realtime state of departures at the
watched stops. Reuses ``TransitService`` so realtime semantics (delay propagation, cancellations,
after-midnight start dates) are exactly the ones the assistant itself uses.

    uv run python -m app.analytics.collector --place home --minutes 60
    make collect            # runs until Ctrl-C

Also refreshes hourly rain from Open-Meteo (free, public) so delays can be joined to weather.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime

import httpx

from app.agents.transit.models import Departure
from app.agents.transit.service import TransitService
from app.analytics.store import DelayStore, Obs
from app.core.timeutil import TZ

log = logging.getLogger("collector")
LOOKBACK_S, WINDOW_S = 5 * 60, 25 * 60  # what each poll records around 'now'
_REL = {"cancelled": "CANCELED", "scheduled_only": "NO_DATA", "added": "ADDED"}


def to_obs(d: Departure, ts: int) -> Obs:
    sched = int(datetime.fromisoformat(d.scheduled_iso).timestamp())
    pred = int(datetime.fromisoformat(d.predicted_iso).timestamp()) if d.predicted_iso else None
    delay = round(d.delay_min * 60) if d.delay_min is not None else None
    return Obs(ts, d.trip_id, d.route, d.stop_id, d.service_date, sched, pred, delay,
               _REL.get(d.status, "SCHEDULED"), d.delay_basis)  # fmt: skip


async def poll_once(
    transit: TransitService,
    store: DelayStore,
    stop_ids: Sequence[str],
    routes: Sequence[str] | None,
    now: datetime | None = None,
) -> int:
    """One poll. Never raises: a failed poll is recorded (so coverage gaps are visible)."""
    when = now or datetime.now(TZ)
    ts = int(when.timestamp())
    try:
        deps, status = await transit._departures(stop_ids, when, WINDOW_S, routes)
        keep = [d for d in deps if abs(datetime.fromisoformat(d.scheduled_iso).timestamp() - ts) <= WINDOW_S
                and datetime.fromisoformat(d.scheduled_iso).timestamp() >= ts - LOOKBACK_S]  # fmt: skip
        n = store.add_obs(to_obs(d, ts) for d in keep)
        store.add_poll(ts, status.source == "realtime", n, status.note)
        return n
    except Exception as exc:  # network, parsing, anything: record and carry on
        store.add_poll(ts, False, 0, f"{type(exc).__name__}: {exc}"[:300])
        log.warning("poll failed: %s", exc)
        return 0


async def fetch_rain(store: DelayStore, lat: float, lon: float, past_days: int = 7) -> int:
    """Hourly precipitation for the last days. Slot T = the hour ENDING at T (Open-Meteo docs)."""
    params: dict[str, str | int | float] = {"latitude": lat, "longitude": lon, "hourly": "precipitation", "past_days": past_days,
              "forecast_days": 1, "timezone": "Australia/Brisbane"}  # fmt: skip
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get("https://api.open-meteo.com/v1/forecast", params=params)
        r.raise_for_status()
        h = r.json()["hourly"]
    slots = {
        int(datetime.fromisoformat(t).replace(tzinfo=TZ).timestamp()): float(mm)
        for t, mm in zip(h["time"], h["precipitation"], strict=True) if mm is not None
    }  # fmt: skip
    store.add_rain(slots)
    return len(slots)


async def run(
    transit: TransitService,
    store: DelayStore,
    place: str,
    routes: Sequence[str] | None,
    interval_s: float,
    minutes: float | None,
) -> None:  # pragma: no cover (loop)
    stops = transit.resolve_place(place).stops
    ids = [s.stop_id for s in stops]
    lat = sum(s.lat for s in stops) / len(stops)
    lon = sum(s.lon for s in stops) / len(stops)
    print(f"watching {[s.name for s in stops]} routes={routes or 'all'} every {interval_s:.0f}s")
    t_end = None if minutes is None else asyncio.get_running_loop().time() + minutes * 60
    last_rain = 0.0
    while t_end is None or asyncio.get_running_loop().time() < t_end:
        n = await poll_once(transit, store, ids, routes)
        if asyncio.get_running_loop().time() - last_rain > 3600:
            try:
                print(f"rain slots stored: {await fetch_rain(store, lat, lon)}")
            except Exception as exc:
                print(f"rain fetch failed: {exc}")
            last_rain = asyncio.get_running_loop().time()
        print(f"{datetime.now(TZ):%H:%M:%S} stored {n} observations")
        await asyncio.sleep(interval_s)


def main() -> None:  # pragma: no cover
    from app.agents.transit.gtfs_realtime import RealtimeClient
    from app.agents.transit.gtfs_static import GtfsStatic
    from app.agents.transit.planner import JourneyPlanner
    from app.config import get_settings
    from app.core.memory import load_profile

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--place", default="home")
    ap.add_argument("--routes", default="", help="comma-separated route short names (default: all)")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--minutes", type=float, default=None, help="stop after N minutes")
    ns = ap.parse_args()
    s = get_settings()
    static = GtfsStatic(s.gtfs_dir / "gtfs.sqlite")
    transit = TransitService(
        static,
        RealtimeClient(ttl_s=s.rt_ttl_seconds),
        JourneyPlanner(static),
        load_profile(s.profile_path),
    )
    routes = [r for r in ns.routes.split(",") if r] or None
    asyncio.run(
        run(transit, DelayStore(s.analytics_db_path), ns.place, routes, ns.interval, ns.minutes)
    )


if __name__ == "__main__":  # pragma: no cover
    main()
