"""Open-Meteo client: parsing, WMO weather codes and a TTL cache.

Verified against the live API (2026-11-18): free, no key, 600 calls/min (X-RateLimit headers).
Units requested are the defaults: degC, %, mm, km/h. Times come back as *local naive* strings
because we pass ``timezone=Australia/Brisbane``; ``utc_offset_seconds`` attaches the offset.

Hourly semantics (Open-Meteo docs, "Hourly Parameter Definition"; the 15-minute cross-check I tried
was inconclusive because drizzle rounds to 0.0 there). The slot labelled T means:
  * ``precipitation``, ``precipitation_probability`` : the PRECEDING hour (T-1h, T]
  * ``wind_gusts_10m``                               : max of the preceding hour
  * ``uv_index``                                     : mean of the preceding hour
  * ``temperature_2m``, ``apparent_temperature``, ``wind_speed_10m``, ``weather_code``:
                                                         instantaneous at T
Coordinates are snapped to the model grid (asked -27.23, 153.02 -> got -27.2408, 153.0100).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.timeutil import TZ

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,"
    "wind_speed_10m,wind_gusts_10m,is_day"
)
HOURLY_VARS = (
    "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,"
    "wind_speed_10m,wind_gusts_10m,uv_index"
)
FORECAST_DAYS = 3

WMO_TEXT = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}  # fmt: skip


def weather_text(code: int | None) -> str:
    if code is None:
        return "unknown"
    return WMO_TEXT.get(code, f"weather code {code}")


@dataclass(frozen=True)
class HourPoint:
    time: datetime  # slot label T (see module docstring for what it covers)
    temperature_c: float | None
    apparent_c: float | None
    rain_prob_pct: float | None
    rain_mm: float | None
    weather_code: int | None
    wind_kmh: float | None
    gust_kmh: float | None
    uv_index: float | None


@dataclass(frozen=True)
class CurrentConditions:
    time: datetime
    temperature_c: float | None
    apparent_c: float | None
    humidity_pct: float | None
    rain_mm: float | None
    weather_code: int | None
    wind_kmh: float | None
    gust_kmh: float | None
    is_day: bool | None


@dataclass(frozen=True)
class Forecast:
    latitude: float  # model grid point actually used
    longitude: float
    fetched_at: float
    current: CurrentConditions | None
    hours: tuple[HourPoint, ...] = field(default_factory=tuple)


def _t(text: str, offset_s: int) -> datetime:
    return (
        datetime.fromisoformat(text)
        .replace(tzinfo=timezone(timedelta(seconds=offset_s)))
        .astimezone(TZ)
    )


def _num(arr: list[Any] | None, i: int) -> float | None:
    if arr is None or i >= len(arr) or arr[i] is None:
        return None
    return float(arr[i])


def _code(arr: list[Any] | None, i: int) -> int | None:
    v = _num(arr, i)
    return None if v is None else int(v)


def parse_forecast(data: dict[str, Any], fetched_at: float) -> Forecast:
    """Raises ValueError/KeyError if the payload is not an Open-Meteo forecast."""
    if "error" in data and data.get("error"):
        raise ValueError(f"Open-Meteo error: {data.get('reason', data['error'])}")
    off = int(data["utc_offset_seconds"])
    h = data["hourly"]
    hours = tuple(
        HourPoint(
            time=_t(t, off),
            temperature_c=_num(h.get("temperature_2m"), i),
            apparent_c=_num(h.get("apparent_temperature"), i),
            rain_prob_pct=_num(h.get("precipitation_probability"), i),
            rain_mm=_num(h.get("precipitation"), i),
            weather_code=_code(h.get("weather_code"), i),
            wind_kmh=_num(h.get("wind_speed_10m"), i),
            gust_kmh=_num(h.get("wind_gusts_10m"), i),
            uv_index=_num(h.get("uv_index"), i),
        )
        for i, t in enumerate(h["time"])
    )
    cur = data.get("current")
    current = None
    if cur:
        wrap = {k: [v] for k, v in cur.items()}
        current = CurrentConditions(
            time=_t(cur["time"], off),
            temperature_c=_num(wrap.get("temperature_2m"), 0),
            apparent_c=_num(wrap.get("apparent_temperature"), 0),
            humidity_pct=_num(wrap.get("relative_humidity_2m"), 0),
            rain_mm=_num(wrap.get("precipitation"), 0),
            weather_code=_code(wrap.get("weather_code"), 0),
            wind_kmh=_num(wrap.get("wind_speed_10m"), 0),
            gust_kmh=_num(wrap.get("wind_gusts_10m"), 0),
            is_day=None if cur.get("is_day") is None else bool(cur["is_day"]),
        )
    return Forecast(float(data["latitude"]), float(data["longitude"]), fetched_at, current, hours)


@dataclass(frozen=True)
class FetchResult[T]:
    data: T | None
    age_s: float | None  # seconds since the data was fetched
    stale: bool = False  # True when serving a cached copy because the refresh failed
    error: str | None = None


class WeatherClient:
    """On-demand fetch with a TTL cache (default 10 min). On a failed refresh the last copy is
    served as ``stale`` for up to ``max_stale_s``; beyond that the data is reported unavailable."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        url: str = OPEN_METEO_URL,
        ttl_s: float = 600.0,
        max_stale_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = client or httpx.AsyncClient(timeout=15.0)
        self._url = url
        self._ttl = ttl_s
        self._max_stale = max_stale_s
        self._clock = clock
        self._cache: dict[tuple[float, float], Forecast] = {}
        self._lock = asyncio.Lock()

    async def forecast(self, lat: float, lon: float) -> FetchResult[Forecast]:
        key = (round(lat, 2), round(lon, 2))
        async with self._lock:  # concurrent callers share one fetch
            now = self._clock()
            cached = self._cache.get(key)
            if cached is not None and now - cached.fetched_at < self._ttl:
                return FetchResult(cached, now - cached.fetched_at)
            params = {
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "timezone": "Australia/Brisbane",
                "forecast_days": str(FORECAST_DAYS),
                "current": CURRENT_VARS,
                "hourly": HOURLY_VARS,
            }
            try:
                resp = await self._http.get(self._url, params=params)
                resp.raise_for_status()
                fc = parse_forecast(resp.json(), now)
            except Exception as exc:  # network, HTTP status, JSON or schema error
                err = f"{type(exc).__name__}: {exc}"
                if cached is not None and now - cached.fetched_at < self._max_stale:
                    return FetchResult(cached, now - cached.fetched_at, stale=True, error=err)
                return FetchResult(None, None, error=err)
            self._cache[key] = fc
            return FetchResult(fc, 0.0)

    async def aclose(self) -> None:
        await self._http.aclose()
