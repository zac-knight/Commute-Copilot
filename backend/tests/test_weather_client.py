from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
import pytest

from app.agents.weather.client import (
    WeatherClient,
    parse_forecast,
    weather_text,
)
from app.core.timeutil import TZ


def payload(**over: Any) -> dict[str, Any]:
    """Shaped like the real Open-Meteo response captured 2026-11-18."""
    data: dict[str, Any] = {
        "latitude": -27.240774,
        "longitude": 153.01004,
        "utc_offset_seconds": 36000,
        "timezone": "Australia/Brisbane",
        "current": {
            "time": "2026-11-18T00:00",
            "interval": 900,
            "temperature_2m": 20.7,
            "apparent_temperature": 21.9,
            "relative_humidity_2m": 80,
            "precipitation": 0.0,
            "weather_code": 1,
            "wind_speed_10m": 8.4,
            "wind_gusts_10m": 19.8,
            "is_day": 0,
        },
        "hourly": {
            "time": ["2026-11-18T00:00", "2026-11-18T01:00", "2026-11-18T02:00"],
            "temperature_2m": [20.7, 20.0, None],
            "apparent_temperature": [21.9, 21.4, 20.9],
            "precipitation_probability": [0, 5, 60],
            "precipitation": [0.0, 0.1, 2.5],
            "weather_code": [1, 51, 63],
            "wind_speed_10m": [8.4, 7.6, 7.5],
            "wind_gusts_10m": [19.8, 15.5, 14.0],
            "uv_index": [0.0, 0.0, 0.15],
        },
    }
    data.update(over)
    return data


def test_parse_real_shaped_payload() -> None:
    fc = parse_forecast(payload(), fetched_at=123.0)
    assert (fc.latitude, fc.longitude, fc.fetched_at) == (-27.240774, 153.01004, 123.0)
    assert len(fc.hours) == 3
    h = fc.hours[1]
    assert h.time == datetime(2026, 11, 18, 1, 0, tzinfo=TZ)
    assert (h.temperature_c, h.apparent_c, h.rain_prob_pct, h.rain_mm) == (20.0, 21.4, 5.0, 0.1)
    assert (h.weather_code, h.wind_kmh, h.gust_kmh, h.uv_index) == (51, 7.6, 15.5, 0.0)
    assert fc.hours[2].temperature_c is None  # nulls stay None, never 0
    c = fc.current
    assert c and c.time == datetime(2026, 11, 18, 0, 0, tzinfo=TZ) and c.is_day is False
    assert (c.temperature_c, c.humidity_pct, c.weather_code) == (20.7, 80.0, 1)


def test_utc_offset_is_applied_not_assumed() -> None:
    fc = parse_forecast(payload(utc_offset_seconds=0), fetched_at=0)  # a UTC response
    assert fc.hours[0].time == datetime(
        2026, 11, 18, 10, 0, tzinfo=TZ
    )  # 00:00 UTC = 10:00 Brisbane


def test_missing_optional_pieces() -> None:
    d = payload()
    del d["current"]
    del d["hourly"]["uv_index"]
    fc = parse_forecast(d, 0)
    assert fc.current is None and all(h.uv_index is None for h in fc.hours)


def test_error_and_bad_payloads_raise() -> None:
    with pytest.raises(ValueError, match="Open-Meteo error"):
        parse_forecast({"error": True, "reason": "Latitude must be in range"}, 0)
    with pytest.raises(KeyError):
        parse_forecast({"latitude": 1}, 0)


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (0, "clear sky"),
        (3, "overcast"),
        (51, "light drizzle"),
        (63, "rain"),
        (95, "thunderstorm"),
        (None, "unknown"),
        (1234, "weather code 1234"),
    ],
)
def test_weather_text(code: int | None, text: str) -> None:
    assert weather_text(code) == text


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(handler: Any, clock: Clock, **kw: float) -> WeatherClient:
    return WeatherClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=clock, **kw
    )


async def test_request_parameters_and_ttl_cache() -> None:
    clock, seen = Clock(), []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=payload())

    wc = make(handler, clock, ttl_s=600)
    r1 = await wc.forecast(-27.2336, 153.0201)
    assert r1.data and r1.age_s == 0 and not r1.stale
    p = seen[0]
    assert p["latitude"] == "-27.2336" and p["timezone"] == "Australia/Brisbane"
    assert "precipitation_probability" in p["hourly"] and "wind_gusts_10m" in p["current"]
    clock.t += 300
    r2 = await wc.forecast(-27.2336, 153.0201)
    assert len(seen) == 1 and r2.age_s == 300  # cached
    assert (await wc.forecast(-27.2349, 153.0204)).data is not None and len(
        seen
    ) == 1  # same 0.01 deg cell
    await wc.forecast(-27.5, 153.0)
    assert len(seen) == 2  # different place
    clock.t += 400  # first entry now older than the 600 s TTL
    await wc.forecast(-27.2336, 153.0201)
    assert len(seen) == 3


async def test_failure_serves_stale_copy_then_gives_up() -> None:
    clock, ok = Clock(), {"v": True}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload()) if ok["v"] else httpx.Response(503)

    wc = make(handler, clock, ttl_s=600, max_stale_s=3600)
    await wc.forecast(-27.2, 153.0)
    ok["v"] = False
    clock.t += 1200
    stale = await wc.forecast(-27.2, 153.0)
    assert stale.data and stale.stale and stale.age_s == 1200 and "503" in (stale.error or "")
    clock.t += 3000
    gone = await wc.forecast(-27.2, 153.0)
    assert gone.data is None and gone.age_s is None and gone.error


async def test_bad_json_and_api_error_are_reported() -> None:
    wc = make(lambda r: httpx.Response(200, content=b"<html>nope"), Clock())
    assert (await wc.forecast(0, 0)).error
    wc2 = make(lambda r: httpx.Response(400, json={"error": True, "reason": "bad"}), Clock())
    assert "400" in ((await wc2.forecast(0, 0)).error or "")


async def test_concurrent_calls_share_one_fetch() -> None:
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(200, json=payload())

    wc = make(handler, Clock())
    results = await asyncio.gather(*(wc.forecast(-27.2, 153.0) for _ in range(5)))
    assert len(hits) == 1 and all(r.data for r in results)
    await wc.aclose()
