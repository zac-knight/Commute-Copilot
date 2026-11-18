"""Output models for the weather tools. Times are local; see client.py for hourly semantics."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.weather.advice import WeatherStatus


class HourOut(BaseModel):
    time: str  # slot label: rain/gust/UV describe the hour ENDING at this time
    temperature_c: float | None
    feels_like_c: float | None
    rain_chance_pct: float | None
    rain_mm: float | None
    wind_kmh: float | None
    gust_kmh: float | None
    uv_index: float | None
    conditions: str


class ForecastResult(BaseModel):
    latitude: float
    longitude: float
    hours: list[HourOut]
    weather: WeatherStatus
    note: str | None = None


class CurrentResult(BaseModel):
    latitude: float
    longitude: float
    as_of: str
    temperature_c: float | None
    feels_like_c: float | None
    humidity_pct: float | None
    rain_mm: float | None
    wind_kmh: float | None
    gust_kmh: float | None
    conditions: str
    is_day: bool | None
    weather: WeatherStatus
