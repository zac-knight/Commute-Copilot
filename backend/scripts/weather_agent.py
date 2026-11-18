"""Ask the weather agent a plain weather question, live: local Ollama + real Open-Meteo.

    uv run python -m scripts.weather_agent "What's the weather like for the next few hours?"
(Trip questions need a planned journey: see scripts/commute_advice.py.)
"""

from __future__ import annotations

import argparse
import asyncio

from app.agents.weather.cli import build_service
from app.agents.weather.prompts import PROMPT_VERSION, SYSTEM_PROMPT, task_message
from app.agents.weather.tools import AGENT_NAME, register_weather_tools
from app.config import ROLES, get_settings
from app.core.agent import Agent, RequestBudget
from app.core.llm import OllamaLLM
from app.core.tools import ToolRegistry
from app.core.tracing import Tracer, TraceStore
from scripts.hello_agent import print_tree


async def main(question: str) -> None:
    settings = get_settings()
    weather, client = build_service(settings)
    registry = ToolRegistry()
    register_weather_tools(registry, weather, lambda: [])
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
            res = await agent.run(task_message(question, weather.clock()), budget=budget)
    finally:
        await client.aclose()
        await llm.aclose()
    print(f"status={res.status} tools={res.tool_calls}\nanswer: {res.text}")
    if res.error:
        print(f"error: {res.error}")
    print(f"\ntrace {rid}:")
    print_tree(store.get_spans(rid))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    asyncio.run(main(ap.parse_args().question))
