"""DEV HARNESS: ask the full assistant (router -> agents -> synthesiser) live.

    uv run python -m scripts.ask "Next 3 buses from home to Chermside" "and the one after?"
Uses the local Ollama model, the real TransLink/Open-Meteo data (run the GTFS refresh once) and a
THROWAWAY demo calendar + database in a temp directory: your real data is never touched. Several
questions run in one session, so follow-ups can use history.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from app.agents.calendar.store import LocalCalendarWriter
from app.config import get_settings
from app.core.timeutil import TZ
from app.demo.seed import seed_calendar as seed
from app.orchestrator.events import (
    ApprovalRequired,
    ErrorEvent,
    Final,
    PlanEvent,
    StepResult,
    StepStarted,
)
from app.orchestrator.wiring import build_state


async def main(questions: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ask-"))
    settings = get_settings().model_copy(
        update={"calendar_dir": tmp / "cal", "db_path": tmp / "app.sqlite"}
    )
    state = build_state(settings)
    seed(LocalCalendarWriter(tmp / "cal" / "calendar.json"), datetime.now(TZ))
    print(f"agents available: {sorted(state.available)}   (temp dir {tmp})")
    try:
        for q in questions:
            print(f"\n=== USER: {q}")
            async for ev in state.orchestrator.run("cli", q):
                if isinstance(ev, PlanEvent):
                    p = ev.plan
                    steps = "; ".join(
                        f"{s.id}:{s.agent}"
                        + (f"<-{','.join(s.depends_on)}" if s.depends_on else "")
                        for s in p.steps
                    )
                    print(
                        f"[plan] intent={p.intent} {steps}"
                        + (f"  repairs={p.repairs}" if p.repairs else "")
                    )
                    for s in p.steps:
                        print(f"       {s.id} task: {s.task}")
                elif isinstance(ev, StepStarted):
                    print(f"[start] {ev.id} ({ev.agent})")
                elif isinstance(ev, StepResult):
                    print(
                        f"[done ] {ev.id} {ev.status} tools={ev.tool_calls} {ev.duration_ms}ms"
                        + (f" ERROR={ev.error}" if ev.error else "")
                    )
                elif isinstance(ev, ApprovalRequired):
                    print(f"[approval card] {ev.action.summary}")
                elif isinstance(ev, ErrorEvent):
                    print(f"[error] {ev.message}")
                elif isinstance(ev, Final):
                    print(
                        f"[final:{ev.status}] ({ev.duration_ms / 1000:.1f}s, "
                        f"tokens {ev.tokens_in}/{ev.tokens_out})\n{ev.text}"
                    )
    finally:
        await state.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("questions", nargs="+")
    asyncio.run(main(ap.parse_args().questions))
