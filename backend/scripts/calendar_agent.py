"""DEV HARNESS: run the calendar agent live (local Ollama) against a THROWAWAY demo calendar.

    uv run python -m scripts.calendar_agent "Find me a free 1 hour slot tomorrow afternoon."
    uv run python -m scripts.calendar_agent "Add 'Dentist' Thursday 2pm" --approve
    uv run python -m scripts.calendar_agent "What's on tomorrow?"   # includes a hostile event

The calendar lives in a temp directory (your real data is never touched). The agent can only
PROPOSE; with --approve this script plays the human and approves + executes every pending card.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app.agents.calendar.executor import register_calendar_handlers
from app.agents.calendar.prompts import PROMPT_VERSION, SYSTEM_PROMPT, task_message
from app.agents.calendar.service import CalendarReader
from app.agents.calendar.store import LocalCalendarReader, LocalCalendarWriter
from app.agents.calendar.tools import AGENT_NAME, register_calendar_tools
from app.config import ROLES, get_settings
from app.core.agent import Agent, RequestBudget
from app.core.approvals import ActionStatus, ApprovalGate
from app.core.llm import OllamaLLM
from app.core.memory import UserProfile
from app.core.timeutil import TZ
from app.core.tools import ToolRegistry
from app.core.tracing import Tracer, TraceStore
from app.demo.seed import seed_calendar as seed
from scripts.hello_agent import print_tree


async def main(question: str, approve: bool) -> None:
    settings = get_settings()
    tmp = Path(tempfile.mkdtemp(prefix="demo-calendar-"))
    writer = LocalCalendarWriter(tmp / "calendar.json")
    now = datetime.now(TZ)
    seed(writer, now)
    reader = CalendarReader(LocalCalendarReader(writer.path), None, UserProfile())
    gate = ApprovalGate(
        tmp / "approvals.sqlite",
        ttl=timedelta(minutes=30),
        max_pending=settings.max_pending_actions,
    )
    register_calendar_handlers(gate, reader, writer)
    registry = ToolRegistry()
    register_calendar_tools(registry, reader, gate.proposal_sink())

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
    before = [e.title for e in writer.events()]
    try:
        with tracer.request() as rid:
            res = await agent.run(
                task_message(question, now),
                budget=RequestBudget(settings.max_request_tokens, settings.max_request_seconds),
            )
    finally:
        await llm.aclose()
    print(f"status={res.status} tools={res.tool_calls}\nanswer: {res.text}")
    if res.error:
        print(f"error: {res.error}")
    pending = gate.list_actions(ActionStatus.PENDING)
    after_agent = [e.title for e in writer.events()]
    print(
        f"\ncalendar changed by the agent alone? {'YES (BUG!)' if after_agent != before else 'no'}"
    )
    print(f"pending approval cards: {len(pending)}")
    for a in pending:
        print(
            f"  [{a.id}] {a.summary}\n" + "\n".join(f"      {line}" for line in a.diff.splitlines())
        )
    if approve and pending:
        print("\n-- the HUMAN approves everything pending --")
        for a in pending:
            gate.approve(a.id, a.payload_hash)
            done = gate.execute_approved_action(a.id)
            print(f"  {a.summary} -> {done.status.value} {done.result or done.error}")
        print("calendar now:", [e.title for e in writer.events()])
    print(f"\ntokens in/out: {res.tokens_in}/{res.tokens_out}\ntrace {rid}:")
    print_tree(store.get_spans(rid))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--approve", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.question, a.approve))
