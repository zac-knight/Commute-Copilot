"""Weather tools exposed to the LLM. All read-only; the decision logic is in advice.py."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field

from app.agents.weather.advice import CommuteAdvice, RoutePlan
from app.agents.weather.models import CurrentResult, ForecastResult
from app.agents.weather.service import WeatherService
from app.core.tools import Access, Tool, ToolRegistry

AGENT_NAME = "weather"

# The orchestrator supplies the journeys planned by the transit agent (agents never call each
# other); the LLM only chooses which one.
PlanProvider = Callable[[], list[RoutePlan]]


_PLACE = Field(
    default=None,
    description="A place name the system can look up (e.g. a suburb or stop). Omit for the "
    "user's home area. Never pass coordinates.",
)


class ForecastInput(BaseModel):
    place: str | None = _PLACE
    hours: int = Field(default=12, ge=1, le=48, description="How many hours ahead (1-48).")


class CurrentInput(BaseModel):
    place: str | None = _PLACE


class AdviceInput(BaseModel):
    journey_index: int = Field(
        default=0, ge=0, description="Which planned journey to assess (0 = the first/best)."
    )


def register_weather_tools(
    registry: ToolRegistry, service: WeatherService, plans: PlanProvider
) -> None:
    async def get_forecast(a: ForecastInput) -> ForecastResult:
        return await service.get_forecast(a.place, a.hours)

    async def get_current(a: CurrentInput) -> CurrentResult:
        return await service.get_current(a.place)

    async def commute_advice(a: AdviceInput) -> CommuteAdvice:
        available = plans()
        if not available:
            raise ValueError(
                "No planned journey is available to assess. The transit specialist must plan a "
                "trip first."
            )
        if a.journey_index >= len(available):
            raise ValueError(
                f"journey_index {a.journey_index} out of range (0..{len(available) - 1})"
            )
        return await service.advice(available[a.journey_index])

    for tool in (
        Tool(
            "get_forecast",
            "Hourly weather forecast: temperature, feels-like, rain chance and "
            "amount, wind, gusts, UV, conditions. Rain/gust/UV are for the hour ENDING at each time.",
            ForecastInput,
            get_forecast,
            Access.READ,
        ),
        Tool(
            "get_current",
            "Current conditions at the location.",
            CurrentInput,
            get_current,
            Access.READ,
        ),
        Tool(
            "commute_advice",
            "Rule-based decision for the planned journey: whether to leave now "
            "or wait, suggested leave time, reasons, what to bring, and a confidence 0-1. Accounts "
            "for rain/heat/wind/UV while you are outside and for transit delays or cancellations.",
            AdviceInput,
            commute_advice,
            Access.READ,
        ),
    ):
        registry.register(tool)
    registry.grant(AGENT_NAME, ["get_forecast", "get_current", "commute_advice"])
