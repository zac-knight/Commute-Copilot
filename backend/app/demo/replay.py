"""Demo mode: replay RECORDED answers from the real system.

Each transcript was recorded once with the real local model against frozen weather, frozen realtime
and a fake calendar (see scripts/record_demo.py and fixtures/meta.json). At replay time:
  * the user's message is matched to the closest recorded question (no model is ever called);
  * the recorded events stream out with realistic, capped delays;
  * every recorded approval card is re-created in the REAL approval gate, so Approve / Reject /
    Edit work for real (against the fake calendar);
  * the recorded trace spans are re-inserted under a fresh request id, so the trace viewer shows
    the real recorded tokens, latencies and tool calls.
Unmatched messages get a friendly list of what the demo can answer.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, TypeAdapter

from app.core.approvals import ApprovalError, ApprovalGate
from app.core.tracing import Span, TraceStore
from app.demo.frozen import FIXTURES
from app.orchestrator.events import ApprovalRequired, Event, Final, PlanEvent, TripEvent

TRANSCRIPTS = FIXTURES / "transcripts"
_EVENT = TypeAdapter(Annotated[Event, Field(discriminator="type")])
_STOP = {"a", "an", "the", "me", "my", "i", "to", "of", "and", "is", "it", "for", "in", "on", "at",
         "do", "can", "you", "please", "what", "whats", "how"}  # fmt: skip
MATCH_THRESHOLD = 0.45


@dataclass(frozen=True)
class Transcript:
    name: str
    prompts: list[str]
    events: list[dict[str, Any]]
    spans: list[dict[str, Any]]


def load_transcripts(directory: Path = TRANSCRIPTS) -> list[Transcript]:
    out = []
    for path in sorted(directory.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        out.append(Transcript(d["name"], d["prompts"], d["events"], d["spans"]))
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP}


def match_transcript(message: str, transcripts: list[Transcript]) -> Transcript | None:
    """Closest recorded question by token overlap (Jaccard). None if nothing is close."""
    mine = _tokens(message)
    if not mine:
        return None
    best: tuple[float, Transcript | None] = (0.0, None)
    for t in transcripts:
        for prompt in t.prompts:
            theirs = _tokens(prompt)
            score = len(mine & theirs) / len(mine | theirs) if theirs else 0.0
            if score > best[0]:
                best = (score, t)
    return best[1] if best[0] >= MATCH_THRESHOLD else None


def _demo_trip() -> dict[str, Any] | None:
    path = FIXTURES / "trips" / "home_chermside.json"
    return json.loads(path.read_text()) if path.exists() else None


def _plan_agents(t: Transcript) -> set[str]:
    for rec in t.events:
        if rec["event"].get("type") == "plan":
            return {s["agent"] for s in rec["event"]["plan"]["steps"]}
    return set()


class DemoRunner:
    """Same ``run`` interface as the real Orchestrator; never calls a model."""

    def __init__(
        self,
        transcripts: list[Transcript],
        gate: ApprovalGate,
        store: TraceStore,
        router: Any,
        *,
        speed: float = 1.0,
        max_gap_s: float = 1.2,
    ) -> None:
        self.transcripts, self.gate, self.store = transcripts, gate, store
        self.router = router  # exposes .role for the settings page
        self.speed, self.max_gap = speed, max_gap_s

    def _for_agent(self, agent: str | None) -> list[Transcript]:
        """Recordings whose plan used only ``agent`` (all of them for Auto)."""
        if agent is None:
            return self.transcripts
        return [t for t in self.transcripts if _plan_agents(t) == {agent}]

    def examples(self, agent: str | None = None) -> list[str]:
        return [t.prompts[0] for t in self._for_agent(agent)]

    async def run(
        self,
        session_id: str,
        message: str,
        agent: str | None = None,
        reasoning: bool = False,
        detail: bool = False,
    ) -> AsyncIterator[Event]:
        rid = uuid.uuid4().hex
        pool = self._for_agent(agent)
        t = match_transcript(message, pool)
        if t is None:
            menu = "\n".join(f"- {p}" for p in self.examples(agent))
            who = f"the {agent} agent alone" if agent else "Auto"
            text = f"This is demo mode: I can only answer questions I recorded earlier for {who}."
            text += (
                f" Try one of:\n{menu}" if menu else " None were recorded for this agent; try Auto."
            )
            yield Final(request_id=rid, status="clarify", text=text)
            return
        self._insert_spans(t, rid)
        created: list[str] = []
        last = 0.0
        for rec in t.events:
            gap = min(max(rec["t"] - last, 0.0) * self.speed, self.max_gap * self.speed)
            last = rec["t"]
            if gap > 0:
                await asyncio.sleep(gap)
            ev = _EVENT.validate_python(rec["event"])
            if isinstance(ev, PlanEvent):
                ev = ev.model_copy(update={"request_id": rid})
            elif isinstance(ev, ApprovalRequired):
                try:  # a REAL pending action in the real gate, so Approve genuinely works
                    action = self.gate.create(ev.action.type, ev.action.payload)
                except (ApprovalError, ValueError):
                    continue
                created.append(action.id)
                ev = ApprovalRequired(action=action)
            elif isinstance(ev, Final):
                if detail and any(sp["name"] == "plan_trip" for sp in t.spans):
                    trip = (
                        _demo_trip()
                    )  # real timetable, recorded once (scripts/record_trip_fixture)
                    if trip is not None:
                        yield TripEvent(step="s2", agent="transit", artifact=trip)
                ev = ev.model_copy(update={"request_id": rid, "pending_approvals": created})
            yield ev

    def _insert_spans(self, t: Transcript, rid: str) -> None:
        now = time.time()
        ids = {s["span_id"]: uuid.uuid4().hex[:16] for s in t.spans}
        for s in t.spans:
            self.store.write(Span.model_validate({
                **s, "request_id": rid, "span_id": ids[s["span_id"]],
                "parent_id": ids.get(s["parent_id"]) if s.get("parent_id") else None,
                "start": now + s["start"], "end": now + s["end"],
            }))  # fmt: skip
