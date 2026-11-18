"""Frozen stand-ins for live data, so recordings and the demo are deterministic and offline.

* ``FixtureWeatherClient``: serves a saved Open-Meteo response (see fixtures/meta.json for what was
  edited: a synthetic shower was added to a real capture).
* ``FrozenRealtime``: a hand-made GTFS-Realtime snapshot on top of the REAL static timetable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.agents.transit.gtfs_realtime import (
    AlertSnapshot,
    FeedResult,
    RealtimeSnapshot,
    StopTimeUpdate,
    TripRT,
)
from app.agents.transit.gtfs_static import GtfsStatic
from app.agents.weather.client import FetchResult, Forecast, parse_forecast
from app.core.timeutil import TZ

FIXTURES = Path(__file__).parent / "fixtures"
FROZEN_NOW = datetime(2026, 11, 18, 7, 45, tzinfo=TZ)


def frozen_clock() -> datetime:
    return FROZEN_NOW


class FixtureWeatherClient:
    def __init__(self, path: Path = FIXTURES / "forecast.json") -> None:
        self._data = json.loads(path.read_text())

    async def forecast(self, lat: float, lon: float) -> FetchResult[Forecast]:
        fc: Forecast = parse_forecast(self._data, FROZEN_NOW.timestamp())
        return FetchResult(fc, 0.0)

    async def aclose(self) -> None: ...


class FrozenRealtime:
    def __init__(self, snapshot: RealtimeSnapshot, feed_age_s: float = 8.0) -> None:
        self._snap, self._age = snapshot, feed_age_s

    async def trip_updates(self) -> FeedResult[RealtimeSnapshot]:
        return FeedResult(self._snap, self._age)

    async def alerts(self) -> FeedResult[AlertSnapshot]:
        stamp = int(FROZEN_NOW.timestamp()) - int(self._age)
        return FeedResult(AlertSnapshot(stamp, FROZEN_NOW.timestamp(), ()), self._age)

    def cache_ages(self) -> dict[str, float]:
        return {"trips": self._age}

    async def aclose(self) -> None: ...


def build_frozen_snapshot(static: GtfsStatic) -> RealtimeSnapshot:
    """Delays/cancellation for the real 680 trips leaving North Lakes after the frozen 'now':
    the 1st runs +3 min, the 2nd is on time (no update), the 3rd is +7 min, the 4th is cancelled."""
    stops = [s.stop_id for s in static.stops().values() if s.name.startswith("North Lakes station")]
    deps = static.scheduled_departures(stops, FROZEN_NOW, 2 * 3600, route_short_names=["680"])
    trips: dict[tuple[str, object], TripRT] = {}
    plan = {0: 180, 2: 420}
    for i, d in enumerate(deps[:4]):
        key = (d.trip_id, d.rt_start_date)
        if i == 3:
            trips[key] = TripRT(d.trip_id, d.rt_start_date, d.route_id, "CANCELED")
        elif i in plan:
            sched = int(d.scheduled.timestamp())
            upd = StopTimeUpdate(
                d.stop_sequence, d.stop_id, "SCHEDULED",
                departure_delay=plan[i], departure_time=sched + plan[i],
            )  # fmt: skip
            trips[key] = TripRT(d.trip_id, d.rt_start_date, d.route_id, "SCHEDULED", (upd,))
    stamp = int(FROZEN_NOW.timestamp()) - 8
    return RealtimeSnapshot(stamp, FROZEN_NOW.timestamp(), trips)  # type: ignore[arg-type]
