"""DEV HARNESS (not part of the app): plan a real trip, then ask the weather agent about it.

Agents never call each other; in the app the orchestrator passes the transit result to the weather
agent, as this script does by hand.

    uv run python -m scripts.commute_advice "Should I leave now or wait? It looks like rain." \
        --origin home --dest chermside --depart "tomorrow 8am"
    uv run python -m scripts.commute_advice --no-llm --origin home --dest chermside
"""

from __future__ import annotations

import argparse
import asyncio

from app.agents.transit.cli import build_service as build_transit
from app.agents.weather.advice import RoutePlan
from app.agents.weather.cli import build_service as build_weather
from app.agents.weather.prompts import PROMPT_VERSION, SYSTEM_PROMPT, task_message
from app.agents.weather.tools import AGENT_NAME, register_weather_tools
from app.config import ROLES, get_settings
from app.core.agent import Agent, RequestBudget
from app.core.llm import OllamaLLM
from app.core.timeutil import parse_when
from app.core.tools import ToolRegistry
from app.core.tracing import Tracer, TraceStore
from scripts.hello_agent import print_tree


async def main(question: str, origin: str, dest: str, depart: str | None, no_llm: bool) -> None:
    settings = get_settings()
    transit, t_rt = build_transit(settings)
    weather, w_client = build_weather(settings)

    def resolve(name: str) -> tuple[float, float] | None:
        try:
            st = transit.resolve_place(name).stops
        except ValueError:
            return None
        return sum(s.lat for s in st) / len(st), sum(s.lon for s in st) / len(st)

    weather._resolve = resolve  # dev harness: the orchestrator will inject this in Phase 5
    now = transit.clock()
    try:
        plan = await transit.plan_trip(
            origin, dest, depart_after=parse_when(depart, now) if depart else None
        )
        o = transit.resolve_place(origin).stops
        lat, lon = sum(s.lat for s in o) / len(o), sum(s.lon for s in o) / len(o)
        plans = [
            RoutePlan.model_validate({**j.model_dump(), "latitude": lat, "longitude": lon})
            for j in plan.journeys
        ]
        print(
            f"transit: {len(plans)} journey(s); first: "
            + (f"{plan.journeys[0].depart} -> {plan.journeys[0].arrive}" if plans else "none")
        )
        if not plans:
            return
        direct = await weather.advice(plans[0])
        print("\n--- commute_advice (direct call, ground truth) ---")
        print(direct.model_dump_json(indent=2, exclude={"assumptions"}))
        if no_llm:
            return

        registry = ToolRegistry()
        register_weather_tools(registry, weather, lambda: plans)
        llm = OllamaLLM(
            settings.ollama_base_url, timeout=settings.llm_timeout_seconds, num_ctx=settings.num_ctx
        )
        store = TraceStore(settings.db_path)
        tracer = Tracer(store, preview_chars=settings.trace_preview_chars)
        role = ROLES["agent"]
        agent = Agent(
            name=AGENT_NAME,
            system_prompt=SYSTEM_PROMPT,
            tools=registry.toolset_for(AGENT_NAME),
            model=role.model,
            think=role.think,
            llm=llm,
            tracer=tracer,
            max_steps=settings.max_agent_steps,
            prompt_version=PROMPT_VERSION,
        )
        budget = RequestBudget(settings.max_request_tokens, settings.max_request_seconds)
        try:
            with tracer.request() as rid:
                res = await agent.run(task_message(question, now), budget=budget)
        finally:
            await llm.aclose()
        print(f"\n--- weather agent ({res.status}, tools={res.tool_calls}) ---\n{res.text}")
        if res.error:
            print(f"error: {res.error}")
        print(f"\ntokens in/out: {res.tokens_in}/{res.tokens_out}\ntrace {rid}:")
        print_tree(store.get_spans(rid))
    finally:
        await t_rt.aclose()
        await w_client.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "question", nargs="?", default="Should I leave now or wait? Do I need an umbrella?"
    )
    ap.add_argument("--origin", default="home")
    ap.add_argument("--dest", default="chermside")
    ap.add_argument("--depart", default=None)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.question, a.origin, a.dest, a.depart, a.no_llm))
