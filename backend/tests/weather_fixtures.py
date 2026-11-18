"""Builders for forecasts and plans. 2026-11-09 is a Monday. No network."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.agents.weather.advice import PlanLeg, PlanLegRealtime, RoutePlan
from app.agents.weather.client import CurrentConditions, Forecast, HourPoint
from app.core.timeutil import TZ

DAY = datetime(2026, 11, 9, tzinfo=TZ)
NOW = DAY.replace(hour=7, minute=50)


def at(h: int, m: int = 0, day: int = 0) -> datetime:
    return (DAY + timedelta(days=day)).replace(hour=h, minute=m)


def forecast(
    overrides: dict[int, dict[str, Any]] | None = None,
    *,
    days: int = 3,
    fetched_at: float = 0.0,
    **defaults: Any,
) -> Forecast:
    """Hourly slots from 00:00 on 2026-11-09. ``overrides`` maps hour-of-forecast (0 = 00:00 day 1,
    24 = 00:00 day 2, ...) to field overrides. Defaults: a mild, dry day."""
    base: dict[str, Any] = dict(
        temperature_c=22.0,
        apparent_c=23.0,
        rain_prob_pct=0.0,
        rain_mm=0.0,
        weather_code=1,
        wind_kmh=10.0,
        gust_kmh=20.0,
        uv_index=0.0,
    )
    base.update(defaults)
    hours = []
    for i in range(days * 24):
        row = {**base, **(overrides or {}).get(i, {})}
        hours.append(HourPoint(time=DAY + timedelta(hours=i), **row))
    cur = CurrentConditions(
        time=NOW,
        temperature_c=22.0,
        apparent_c=23.0,
        humidity_pct=60.0,
        rain_mm=0.0,
        weather_code=1,
        wind_kmh=10.0,
        gust_kmh=20.0,
        is_day=True,
    )
    return Forecast(-27.24, 153.01, fetched_at, cur, tuple(hours))


def leg(
    kind: str,
    dep: datetime,
    arr: datetime,
    *,
    route: str | None = None,
    status: str | None = None,
    delay: float | None = None,
    walk_m: int | None = None,
) -> PlanLeg:
    rt = PlanLegRealtime(status=status, delay_min=delay) if status else None
    return PlanLeg(
        kind=kind,
        depart_iso=dep.isoformat(),
        arrive_iso=arr.isoformat(),  # type: ignore[arg-type]
        route=route,
        mode="bus" if kind == "transit" else None,
        walk_m=walk_m,
        realtime=rt,
    )


def simple_plan(dep: datetime | None = None, arr: datetime | None = None, **kw: Any) -> RoutePlan:
    """One bus leg, 08:00-08:30 by default."""
    return RoutePlan(legs=[leg("transit", dep or at(8, 0), arr or at(8, 30), route="680", **kw)])
