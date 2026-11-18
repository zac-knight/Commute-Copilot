"""Build the application in demo mode (``DEMO=1``): no model, no network, no real data."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from app.agents.calendar.store import LocalCalendarWriter
from app.agents.weather.service import WeatherService
from app.config import Settings
from app.core.llm import LLMError
from app.core.memory import UserProfile
from app.demo.frozen import FROZEN_NOW, FixtureWeatherClient, frozen_clock
from app.demo.replay import DemoRunner, load_transcripts
from app.demo.seed import seed_calendar
from app.orchestrator.wiring import AppState, build_state


class NoLLM:
    """Demo mode never calls a model; if anything tries to, fail loudly."""

    async def complete(self, **_: Any) -> Any:
        raise LLMError("demo", "demo mode has no model")


def build_demo_state(settings: Settings, *, speed: float = 1.0) -> AppState:
    """Everything lives in a temp directory: the real profile, calendar and database are never read
    or written in demo mode."""
    tmp = Path(tempfile.mkdtemp(prefix="commute-demo-"))
    cfg = settings.model_copy(update={
        "calendar_dir": tmp / "cal", "db_path": tmp / "app.sqlite",
        "profile_path": tmp / "profile.json", "ui_settings_path": tmp / "ui.json",
        "analytics_db_path": tmp / "analytics.sqlite",
        "gtfs_auto_refresh": False,
    })  # fmt: skip
    weather = WeatherService(FixtureWeatherClient(), UserProfile(), clock=frozen_clock)  # type: ignore[arg-type]
    state = build_state(cfg, llm=NoLLM(), weather=weather, clock=frozen_clock)
    seed_calendar(LocalCalendarWriter(cfg.calendar_dir / "calendar.json"), FROZEN_NOW)
    state.available = {"transit", "weather", "calendar"}  # the recordings cover all three
    state.orchestrator = DemoRunner(  # type: ignore[assignment]
        load_transcripts(), state.gate, state.store, state.orchestrator.router, speed=speed
    )
    return state
