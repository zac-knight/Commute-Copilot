"""WeatherService: forecast / current / commute advice with data-age reporting."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from app.agents.weather.advice import (
    CommuteAdvice,
    RoutePlan,
    Thresholds,
    WeatherStatus,
    commute_advice,
)
from app.agents.weather.client import FetchResult, Forecast, WeatherClient, weather_text
from app.agents.weather.models import CurrentResult, ForecastResult, HourOut
from app.core.memory import UserProfile
from app.core.timeutil import TZ, fmt_local

_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
# place name -> (lat, lon), or None if unknown. The orchestrator supplies one backed by the
# transit stop search; the LLM never provides coordinates (it invents them).
PlaceResolver = Callable[[str], tuple[float, float] | None]


class UnknownPlaceError(ValueError):
    pass


class WeatherService:
    def __init__(
        self,
        client: WeatherClient,
        profile: UserProfile,
        *,
        clock: Callable[[], datetime] | None = None,
        thresholds: Thresholds | None = None,
        resolve_place: PlaceResolver | None = None,
    ) -> None:
        self.client = client
        self.profile = profile
        self.clock = clock or (lambda: datetime.now(TZ))
        self.thresholds = thresholds or Thresholds()
        self._resolve = resolve_place

    def _where(self, place: str | None) -> tuple[float, float]:
        """'home'/None -> profile location; 'lat,lon' -> parsed; else the injected resolver."""
        home = (self.profile.latitude, self.profile.longitude)
        if place is None or self.profile.resolve_alias(place).strip().lower() in (
            "home",
            self.profile.home.lower(),
        ):
            return home
        if m := _LATLON.match(place):
            return float(m[1]), float(m[2])
        found = self._resolve(self.profile.resolve_alias(place)) if self._resolve else None
        if found is None:
            raise UnknownPlaceError(
                f"Unknown place {place!r}. Use 'home' or a stop/place name the system can look "
                "up; do not guess coordinates."
            )
        return found

    @staticmethod
    def status(res: FetchResult[Forecast]) -> WeatherStatus:
        if res.data is None:
            return WeatherStatus(
                source="unavailable", note=f"Weather unavailable ({res.error}). Not guessing."
            )
        age = round(res.age_s or 0)
        note = f"Refresh failed ({res.error}); using a copy {age}s old." if res.stale else None
        return WeatherStatus(source="live", data_age_s=age, stale=res.stale, note=note)

    async def get_forecast(self, place: str | None = None, hours: int = 12) -> ForecastResult:
        la, lo = self._where(place)
        res = await self.client.forecast(la, lo)
        st = self.status(res)
        if res.data is None:
            return ForecastResult(latitude=la, longitude=lo, hours=[], weather=st)
        now = self.clock()
        rows = [h for h in res.data.hours if h.time > now][:hours]
        return ForecastResult(
            latitude=res.data.latitude,
            longitude=res.data.longitude,
            weather=st,
            hours=[
                HourOut(
                    time=fmt_local(h.time, now),
                    temperature_c=h.temperature_c,
                    feels_like_c=h.apparent_c,
                    rain_chance_pct=h.rain_prob_pct,
                    rain_mm=h.rain_mm,
                    wind_kmh=h.wind_kmh,
                    gust_kmh=h.gust_kmh,
                    uv_index=h.uv_index,
                    conditions=weather_text(h.weather_code),
                )
                for h in rows
            ],
            note=None if rows else "The forecast has no hours after now.",
        )

    async def get_current(self, place: str | None = None) -> CurrentResult:
        la, lo = self._where(place)
        res = await self.client.forecast(la, lo)
        st = self.status(res)
        if res.data is None or res.data.current is None:
            raise RuntimeError(st.note or "no current conditions available")
        c, now = res.data.current, self.clock()
        return CurrentResult(
            latitude=res.data.latitude,
            longitude=res.data.longitude,
            as_of=fmt_local(c.time, now),
            temperature_c=c.temperature_c,
            feels_like_c=c.apparent_c,
            humidity_pct=c.humidity_pct,
            rain_mm=c.rain_mm,
            wind_kmh=c.wind_kmh,
            gust_kmh=c.gust_kmh,
            conditions=weather_text(c.weather_code),
            is_day=c.is_day,
            weather=st,
        )

    async def advice(self, plan: RoutePlan) -> CommuteAdvice:
        """Advice for a plan; weather is taken at the plan's start (or home if it has none)."""
        la, lo = (
            (plan.latitude, plan.longitude)
            if plan.latitude is not None and plan.longitude is not None
            else self._where(None)
        )
        res = await self.client.forecast(la, lo)
        return commute_advice(
            plan, res.data, self.clock(), thresholds=self.thresholds, weather=self.status(res)
        )
