"""Hello-world agent: one tool, full trace.

uv run python -m scripts.hello_agent            # live: calls local Ollama
uv run python -m scripts.hello_agent --offline  # scripted responses, NO API call
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from app.config import ROLES, get_settings
from app.core.agent import Agent, RequestBudget
from app.core.llm import LLM, OllamaLLM
from app.core.testing import ScriptedLLM, text_response, tool_use_response
from app.core.tools import Access, Tool, ToolRegistry
from app.core.tracing import Span, Tracer, TraceStore


class TimeInput(BaseModel):
    timezone: str = Field(description="IANA timezone name, e.g. Australia/Brisbane")


def current_time(args: TimeInput) -> dict[str, str]:
    try:
        now = datetime.now(ZoneInfo(args.timezone))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone {args.timezone!r}") from exc
    return {"timezone": args.timezone, "local_time": now.isoformat(timespec="seconds")}


def print_tree(spans: list[Span]) -> None:
    children: dict[str | None, list[Span]] = {}
    for s in spans:
        children.setdefault(s.parent_id, []).append(s)

    def walk(parent: str | None, depth: int) -> None:
        for s in children.get(parent, []):
            ms = ((s.end or s.start) - s.start) * 1000
            flag = " ERROR" if s.status == "error" else ""
            print(
                f"{'  ' * depth}{s.kind.value:<12}{s.name}  {ms:.0f}ms"
                f" in={s.tokens_in} out={s.tokens_out}{flag}"
            )
            walk(s.span_id, depth + 1)

    walk(None, 0)


async def main(offline: bool, question: str) -> None:
    settings = get_settings()
    registry = ToolRegistry()
    registry.register(
        Tool(
            "current_time",
            "Get the current local time in an IANA timezone.",
            TimeInput,
            current_time,
            Access.READ,
        )
    )
    registry.grant("hello", ["current_time"])

    llm: LLM
    if offline:
        print("[OFFLINE: scripted responses, no API call]")
        llm = ScriptedLLM(
            [
                tool_use_response(
                    [
                        ("current_time", {"timezone": "Australia/Brisbane"}),
                        ("current_time", {"timezone": "Europe/London"}),
                    ]
                ),
                text_response("Scripted answer: see the tool results above."),
            ]
        )
    else:
        llm = OllamaLLM(
            settings.ollama_base_url,
            timeout=settings.llm_timeout_seconds,
            num_ctx=settings.num_ctx,
        )

    store = TraceStore(settings.db_path)
    tracer = Tracer(
        store, preview_chars=settings.trace_preview_chars, secrets=settings.secret_values()
    )
    role = ROLES["agent"]
    agent = Agent(
        name="hello",
        system_prompt="You are a concise assistant. Use tools for facts.",
        tools=registry.toolset_for("hello"),
        model=role.model,
        think=role.think,
        llm=llm,
        tracer=tracer,
        max_steps=settings.max_agent_steps,
        prompt_version="hello-v1",
    )
    budget = RequestBudget(settings.max_request_tokens, settings.max_request_seconds)
    with tracer.request() as rid:
        result = await agent.run(question, budget=budget)
    print(f"\nstatus={result.status} steps={result.steps} tools={result.tool_calls}")
    print(f"answer: {result.text}")
    if result.error:
        print(f"error: {result.error}")
    print(f"tokens in/out: {result.tokens_in}/{result.tokens_out}")
    print(f"\ntrace {rid}:")
    print_tree(store.get_spans(rid))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("question", nargs="?", default="What time is it in Brisbane and in London?")
    ns = ap.parse_args()
    asyncio.run(main(ns.offline, ns.question))
