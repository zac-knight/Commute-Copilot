"""A full orchestrator wired to mocked LLM + the fixture transit/weather/calendar data."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core.llm import LLMResponse
from app.core.testing import text_response
from app.orchestrator.events import Event
from app.orchestrator.wiring import AppState, build_state
from tests.calendar_fixtures import NOW, default_events
from tests.test_transit_service import FakeRealtime, make
from tests.test_transit_service import upd as rt_update
from tests.test_weather_service import FakeClient as FakeWeatherClient
from tests.weather_fixtures import forecast

Script = LLMResponse | Callable[[list[dict[str, Any]]], LLMResponse]

# Each system prompt is identified by its OPENING sentence (a prompt may mention another agent).
ROLE_MARKERS = {
    "router": "You are the routing component",
    "synth": "You write the final reply",
    "transit": "You are the transit specialist",
    "weather": "You are the weather and commute specialist",
    "calendar": "You are the calendar specialist",
}


def role_of(system: str) -> str:
    for role, marker in ROLE_MARKERS.items():
        if system.startswith(marker):
            return role
    raise AssertionError(f"unrecognised system prompt: {system[:80]!r}")


class RoleLLM:
    """Answers each role from its own queue, so concurrent specialists cannot interleave scripts."""

    def __init__(self) -> None:
        self.queues: dict[str, list[Script]] = defaultdict(list)
        self.requests: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.hooks: dict[str, Callable[[], Any]] = {}

    def script(self, role: str, *items: Script) -> RoleLLM:
        self.queues[role].extend(items)
        return self

    async def complete(self, **kw: Any) -> LLMResponse:
        role = role_of(kw["system"])
        self.requests[role].append({**kw, "messages": [dict(m) for m in kw["messages"]]})
        if role in self.hooks:
            await self.hooks[role]()
        if not self.queues[role]:
            raise AssertionError(f"RoleLLM: no scripted response left for role {role!r}")
        item = self.queues[role].pop(0)
        return item(kw["messages"]) if callable(item) else item


def plan_response(
    intent: str, steps: list[dict[str, Any]] | None = None, question: str | None = None
) -> LLMResponse:
    return text_response(
        json.dumps({"intent": intent, "steps": steps or [], "clarification_question": question})
    )


def step(sid: str, agent: str, task: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {"id": sid, "agent": agent, "task": task, "depends_on": deps or []}


def build_test_state(
    tmp_path: Path, llm: RoleLLM | None = None, *, transit_rt: FakeRealtime | None = None,
    with_transit: bool = True, **settings: Any,
) -> tuple[AppState, RoleLLM]:  # fmt: skip
    llm = llm or RoleLLM()
    cfg = Settings(
        db_path=tmp_path / "app.sqlite", calendar_dir=tmp_path / "cal", gtfs_dir=tmp_path / "gtfs",
        profile_path=tmp_path / "profile.json", gtfs_auto_refresh=False,
        ui_settings_path=tmp_path / "ui_settings.json", **settings,
    )  # fmt: skip
    transit = None
    if with_transit:
        rt = transit_rt or FakeRealtime(
            [
                {
                    "trip_id": "T1",
                    "start_date": "20261109",
                    "updates": [rt_update(1, "A", dep_delay=300)],
                }
            ]
        )
        transit = make(tmp_path, rt, NOW)
    from app.agents.weather.service import WeatherService
    from app.core.memory import UserProfile

    weather = WeatherService(FakeWeatherClient(forecast()), UserProfile(),  # type: ignore[arg-type]
                             clock=lambda: NOW)  # fmt: skip
    state = build_state(cfg, llm=llm, transit=transit, weather=weather, clock=lambda: NOW)
    for e in default_events():
        state.calendar_writer.add(e)
    return state, llm


async def collect(state: AppState, message: str, session: str = "s1") -> list[Event]:
    return [e async for e in state.orchestrator.run(session, message)]
