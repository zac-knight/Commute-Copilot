"""Deterministic application state for recording the demo and for the evaluation harness.

The same frozen inputs are used for both: a real TransLink timetable with a hand-made realtime
snapshot, a saved forecast, a fake calendar, and a fixed clock. Nothing here touches your real
profile, calendar or database (everything lives in a temp directory).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.agents.calendar.store import LocalCalendarWriter
from app.agents.transit.gtfs_realtime import AlertSnapshot, FeedResult
from app.agents.transit.gtfs_static import GtfsStatic
from app.agents.transit.planner import JourneyPlanner
from app.agents.transit.service import TransitService
from app.agents.weather.client import FetchResult, Forecast
from app.agents.weather.service import WeatherService
from app.config import Settings
from app.core.llm import LLM
from app.core.memory import UserProfile
from app.demo.frozen import (
    FROZEN_NOW,
    FixtureWeatherClient,
    FrozenRealtime,
    build_frozen_snapshot,
    frozen_clock,
)
from app.demo.seed import seed_calendar
from app.orchestrator.wiring import AppState, build_state


class DownWeather:
    """A weather source that is unavailable (to test that the assistant says so, not guesses)."""

    async def forecast(self, lat: float, lon: float) -> FetchResult[Forecast]:
        return FetchResult(None, None, error="weather service unreachable (simulated)")

    async def aclose(self) -> None: ...


class DownRealtime:
    """A realtime feed that is unavailable: the transit agent must fall back to the timetable."""

    async def trip_updates(self) -> FeedResult:  # type: ignore[type-arg]
        return FeedResult(None, None, error="realtime feed unreachable (simulated)")

    async def alerts(self) -> FeedResult[AlertSnapshot]:
        return FeedResult(None, None, error="realtime feed unreachable (simulated)")

    def cache_ages(self) -> dict[str, float]:
        return {}

    async def aclose(self) -> None: ...


def build_fixture_state(
    settings: Settings,
    *,
    llm: LLM | None = None,
    weather_down: bool = False,
    realtime_down: bool = False,
    preview_chars: int = 20000,
) -> tuple[AppState, Path]:
    """Return (state, temp_dir). The caller owns ``await state.aclose()``."""
    tmp = Path(tempfile.mkdtemp(prefix="commute-fixture-"))
    cfg = settings.model_copy(update={
        "calendar_dir": tmp / "cal", "db_path": tmp / "app.sqlite",
        "profile_path": tmp / "profile.json", "ui_settings_path": tmp / "ui.json",
        "analytics_db_path": tmp / "analytics.sqlite",
        "gtfs_auto_refresh": False, "trace_preview_chars": preview_chars,
    })  # fmt: skip
    profile = UserProfile()
    static = GtfsStatic(cfg.gtfs_dir / "gtfs.sqlite")
    rt = DownRealtime() if realtime_down else FrozenRealtime(build_frozen_snapshot(static))
    transit = TransitService(static, rt, JourneyPlanner(static), profile, clock=frozen_clock)  # type: ignore[arg-type]
    wx = DownWeather() if weather_down else FixtureWeatherClient()
    weather = WeatherService(wx, profile, clock=frozen_clock)  # type: ignore[arg-type]
    state = build_state(cfg, llm=llm, transit=transit, weather=weather, clock=frozen_clock)
    seed_calendar(LocalCalendarWriter(cfg.calendar_dir / "calendar.json"), FROZEN_NOW)
    return state, tmp
