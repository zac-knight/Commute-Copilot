"""Ask the transit agent a question, live: local Ollama + real TransLink data.

    uv run python -m scripts.transit_agent "Next 3 buses from home tomorrow at 07:30?"
Requires: `ollama serve`, the model pulled, and `python -m app.agents.transit.cli refresh` run once.
"""

from __future__ import annotations

import argparse
import asyncio

from app.agents.transit.cli import build_service
from app.agents.transit.prompts import PROMPT_VERSION, SYSTEM_PROMPT, task_message
from app.agents.transit.tools import AGENT_NAME, register_transit_tools
from app.config import ROLES, get_settings
from app.core.agent import Agent, RequestBudget
from app.core.llm import OllamaLLM
from app.core.tools import ToolRegistry
from app.core.tracing import Tracer, TraceStore
from scripts.hello_agent import print_tree


async def main(question: str) -> None:
    settings = get_settings()
    service, rt = build_service(settings)
    registry = ToolRegistry()
    register_transit_tools(registry, service)
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
            result = await agent.run(task_message(question, service.clock()), budget=budget)
    finally:
        await rt.aclose()
        await llm.aclose()
    print(f"\nstatus={result.status} steps={result.steps} tools={result.tool_calls}")
    print(f"answer: {result.text}")
    if result.error:
        print(f"error: {result.error}")
    print(f"tokens in/out: {result.tokens_in}/{result.tokens_out}\n\ntrace {rid}:")
    print_tree(store.get_spans(rid))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    asyncio.run(main(ap.parse_args().question))
