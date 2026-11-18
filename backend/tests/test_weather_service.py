from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.weather import cli
from app.agents.weather.advice import RoutePlan
from app.agents.weather.client import FetchResult, Forecast
from app.agents.weather.prompts import PROMPT_VERSION, SYSTEM_PROMPT, task_message
from app.agents.weather.service import UnknownPlaceError, WeatherService
from app.agents.weather.tools import AGENT_NAME, register_weather_tools
from app.config import Settings
from app.core.agent import Agent
from app.core.memory import UserProfile
from app.core.testing import ScriptedLLM, text_response, tool_use_response
from app.core.tools import Access, ToolRegistry
from app.core.tracing import Tracer, TraceStore
from tests.weather_fixtures import NOW, forecast, simple_plan

WET = {"rain_prob_pct": 80.0, "rain_mm": 2.0, "weather_code": 63}


class FakeClient:
    def __init__(
        self,
        fc: Forecast | None = None,
        *,
        stale: bool = False,
        error: str | None = None,
        age: float = 30.0,
    ) -> None:
        self.fc, self.stale, self.error, self.age = fc, stale, error, age
        self.calls: list[tuple[float, float]] = []

    async def forecast(self, lat: float, lon: float) -> FetchResult[Forecast]:
        self.calls.append((lat, lon))
        if self.fc is None:
            return FetchResult(None, None, error=self.error or "down")
        return FetchResult(self.fc, self.age, stale=self.stale, error=self.error)

    async def aclose(self) -> None: ...


def make(client: FakeClient) -> WeatherService:
    return WeatherService(
        client,
        UserProfile(latitude=-27.0, longitude=153.0),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


async def test_forecast_hours_start_after_now_and_respect_the_limit() -> None:
    svc = make(FakeClient(forecast({8: WET})))
    res = await svc.get_forecast(hours=3)
    assert [h.time for h in res.hours] == ["08:00", "09:00", "10:00"]  # slot 07:00 ended before now
    first = res.hours[0]
    assert (first.rain_chance_pct, first.rain_mm, first.conditions) == (80.0, 2.0, "rain")
    assert res.weather.source == "live" and res.weather.data_age_s == 30
    far = await svc.get_forecast(hours=48)
    assert far.hours[-1].time.startswith("Wed 11 Nov")  # dates appear when not today


async def test_place_resolution() -> None:
    client = FakeClient(forecast())
    svc = WeatherService(
        client,
        UserProfile(latitude=-27.0, longitude=153.0, home="North Lakes station"),  # type: ignore[arg-type]
        clock=lambda: NOW,
        resolve_place=lambda name: (-27.47, 153.02) if name.lower() == "south bank" else None,
    )
    await svc.get_forecast()  # default -> home
    await svc.get_forecast("home")
    await svc.get_forecast("North Lakes station")  # the home place by name
    await svc.get_forecast("-27.5, 153.1")  # explicit 'lat,lon' (CLI / code use)
    await svc.get_forecast("south bank")  # resolver
    assert client.calls == [(-27.0, 153.0)] * 3 + [(-27.5, 153.1), (-27.47, 153.02)]


async def test_unknown_place_is_an_actionable_error_not_a_guess() -> None:
    with pytest.raises(UnknownPlaceError, match="do not guess coordinates"):
        await make(FakeClient(forecast())).get_forecast("Atlantis")
    with pytest.raises(UnknownPlaceError):
        await make(FakeClient(forecast())).get_current("somewhere")


async def test_advice_uses_the_plans_start_location_else_home() -> None:
    client = FakeClient(forecast())
    svc = make(client)
    plan = simple_plan(status="on_time", delay=0.0)
    await svc.advice(plan)
    await svc.advice(plan.model_copy(update={"latitude": -27.47, "longitude": 153.02}))
    assert client.calls == [(-27.0, 153.0), (-27.47, 153.02)]


async def test_unavailable_weather_is_explicit_not_guessed() -> None:
    res = await make(FakeClient(None, error="ConnectError")).get_forecast()
    assert res.hours == [] and res.weather.source == "unavailable"
    assert "ConnectError" in (res.weather.note or "") and "Not guessing" in (res.weather.note or "")
    with pytest.raises(RuntimeError, match="unavailable"):
        await make(FakeClient(None)).get_current()


async def test_stale_copy_reports_its_age() -> None:
    res = await make(FakeClient(forecast(), stale=True, error="503", age=1500)).get_forecast()
    assert (
        res.weather.stale and res.weather.data_age_s == 1500 and "503" in (res.weather.note or "")
    )


async def test_current_conditions() -> None:
    cur = await make(FakeClient(forecast())).get_current()
    assert cur.conditions == "mainly clear" and cur.temperature_c == 22.0 and cur.as_of == "07:50"


async def test_advice_through_the_service() -> None:
    a = await make(FakeClient(forecast({8: WET}))).advice(simple_plan(status="on_time", delay=0.0))
    assert a.leave_now is False and "umbrella" in a.bring
    down = await make(FakeClient(None)).advice(simple_plan(status="on_time", delay=0.0))
    assert down.weather.source == "unavailable" and down.confidence == 0.25


# --- tools --------------------------------------------------------------------------------


@pytest.fixture
def reg_and_plans() -> tuple[ToolRegistry, list[RoutePlan]]:
    plans: list[RoutePlan] = []
    reg = ToolRegistry()
    register_weather_tools(reg, make(FakeClient(forecast({8: WET}))), lambda: plans)
    return reg, plans


def test_no_tool_lets_the_model_supply_coordinates(reg_and_plans: Any) -> None:
    """Regression: qwen3:8b invented lat/lon for an optional parameter (Brisbane CBD instead of
    the user's area) and the advice was silently for the wrong place."""
    reg, _ = reg_and_plans
    for d in reg.toolset_for(AGENT_NAME).definitions():
        props = set(d["input_schema"]["properties"])
        assert not props & {"latitude", "longitude", "lat", "lon", "lng"}, d["name"]
    props = reg.toolset_for(AGENT_NAME).definitions()
    assert "place" in {p for d in props for p in d["input_schema"]["properties"]}


def test_weather_agent_has_only_read_tools(reg_and_plans: Any) -> None:
    reg, _ = reg_and_plans
    assert reg.toolset_for(AGENT_NAME).names == ["commute_advice", "get_current", "get_forecast"]
    assert {a for _, a in reg.permissions()[AGENT_NAME]} == {Access.READ}
    defs = {d["name"]: d for d in reg.toolset_for(AGENT_NAME).definitions()}
    assert all(d["description"] for d in defs.values())
    assert all(
        "description" in p for d in defs.values() for p in d["input_schema"]["properties"].values()
    )
    assert defs["get_forecast"]["input_schema"]["properties"]["hours"]["maximum"] == 48


async def test_tools_return_json(reg_and_plans: Any) -> None:
    reg, plans = reg_and_plans
    ts = reg.toolset_for(AGENT_NAME)
    f = json.loads((await ts.call("get_forecast", {"hours": 2})).content)
    assert len(f["hours"]) == 2 and f["weather"]["source"] == "live"
    assert json.loads((await ts.call("get_current", {})).content)["conditions"] == "mainly clear"
    plans.append(simple_plan(status="on_time", delay=0.0))
    adv = json.loads((await ts.call("commute_advice", {})).content)
    assert adv["leave_now"] is False and "umbrella" in adv["bring"] and 0 <= adv["confidence"] <= 1


async def test_advice_tool_errors_are_actionable(reg_and_plans: Any) -> None:
    reg, plans = reg_and_plans
    ts = reg.toolset_for(AGENT_NAME)
    none = await ts.call("commute_advice", {})
    assert none.is_error and "transit specialist must plan a trip first" in none.content
    plans.append(simple_plan())
    oob = await ts.call("commute_advice", {"journey_index": 3})
    assert oob.is_error and "out of range" in oob.content
    bad = await ts.call("get_forecast", {"hours": 100})
    assert bad.error_type == "invalid_input"
    unknown = await ts.call("get_forecast", {"place": "Atlantis"})
    assert unknown.is_error and "Unknown place" in unknown.content


async def test_agent_loop_reports_the_tool_decision(reg_and_plans: Any, store: TraceStore) -> None:
    reg, plans = reg_and_plans
    plans.append(simple_plan(status="on_time", delay=0.0))
    llm = ScriptedLLM(
        [
            tool_use_response([("commute_advice", {})]),
            text_response("Wait about 15 minutes; rain is likely. Bring an umbrella."),
        ]
    )
    tracer = Tracer(store)
    agent = Agent(
        name=AGENT_NAME,
        system_prompt=SYSTEM_PROMPT,
        tools=reg.toolset_for(AGENT_NAME),
        model="test",
        llm=llm,
        tracer=tracer,
        prompt_version=PROMPT_VERSION,
    )
    with tracer.request():
        res = await agent.run(task_message("Should I leave now?", NOW))
    assert res.ok and res.tool_calls == ["commute_advice"]
    tool_result = json.loads(llm.requests[1]["messages"][-1]["content"][0]["content"])
    assert tool_result["suggested_leave_time"] == "08:10"


def test_prompt_makes_the_tool_the_decision_maker() -> None:
    assert "Decisions come from the `commute_advice` tool" in SYSTEM_PROMPT
    assert "ENDING" in SYSTEM_PROMPT and "never guess the weather" in SYSTEM_PROMPT
    assert "Monday 09 November 2026 07:50" in task_message("q", NOW)


# --- CLI ----------------------------------------------------------------------------------


def test_cli(monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(profile_path=tmp_path / "p.json"))
    monkeypatch.setattr(
        cli, "build_service", lambda s: (make(FakeClient(forecast({8: WET}))), FakeClient())
    )
    assert cli.main(["forecast", "--hours", "2"]) == 0
    out = capsys.readouterr().out
    assert "08:00" in out and "rain" in out and "live" in out
    assert cli.main(["current"]) == 0 and "mainly clear" in capsys.readouterr().out
    assert (
        cli.main(["--json", "current"]) == 0
        and json.loads(capsys.readouterr().out)["is_day"] is True
    )
    monkeypatch.setattr(cli, "build_service", lambda s: (make(FakeClient(None)), FakeClient()))
    assert cli.main(["forecast"]) == 0 and "UNAVAILABLE" in capsys.readouterr().out
    assert cli.main(["current"]) == 1 and "unavailable" in capsys.readouterr().err
