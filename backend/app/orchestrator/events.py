"""Typed events streamed to the UI (SSE): plan, step_started, step_result, approval_required,
final (plus error)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel

from app.core.approvals import PendingAction
from app.orchestrator.planner import Plan


class ToolOutput(BaseModel):
    tool: str
    input: dict[str, Any]
    output: Any  # parsed JSON when possible, else the raw text
    is_error: bool = False


class StepOutcome(BaseModel):
    id: str
    agent: str
    task: str
    status: Literal["ok", "failed", "skipped"]
    text: str = ""  # the specialist's own wording (not trusted as a source of facts)
    error: str | None = None
    tool_calls: list[str] = []
    tool_outputs: list[ToolOutput] = []
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0


class PlanEvent(BaseModel):
    type: Literal["plan"] = "plan"
    request_id: str
    plan: Plan


class StepStarted(BaseModel):
    type: Literal["step_started"] = "step_started"
    id: str
    agent: str
    task: str


class StepResult(BaseModel):
    type: Literal["step_result"] = "step_result"
    id: str
    agent: str
    status: Literal["ok", "failed", "skipped"]
    text: str
    error: str | None = None
    tool_calls: list[str] = []
    duration_ms: int = 0


class ApprovalRequired(BaseModel):
    type: Literal["approval_required"] = "approval_required"
    action: PendingAction


class Final(BaseModel):
    type: Literal["final"] = "final"
    request_id: str
    status: Literal["ok", "clarify", "out_of_scope", "partial", "failed"]
    text: str
    pending_approvals: list[str] = []
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0


class _Scoped(BaseModel):
    step: str = ""  # "router" | "synth" | a plan step id
    agent: str = ""


class LlmStarted(_Scoped):
    type: Literal["llm_started"] = "llm_started"
    n: int = 1
    model: str = ""
    think: bool | None = None
    prompt_version: str = ""
    messages: int = 0
    tools: list[str] = []


class Thought(_Scoped):
    """A reasoning delta (only when the model is allowed to think)."""

    type: Literal["thought"] = "thought"
    delta: str


class Token(_Scoped):
    """An answer/output delta as the model writes it."""

    type: Literal["token"] = "token"
    phase: str = ""  # router | agent | synth
    delta: str


class ToolCallEvent(_Scoped):
    type: Literal["tool_call"] = "tool_call"
    call_id: str = ""
    name: str
    input: dict[str, Any] = {}


class ToolResultEvent(_Scoped):
    type: Literal["tool_result"] = "tool_result"
    call_id: str = ""
    name: str
    output: str = ""  # truncated for the wire
    is_error: bool = False
    duration_ms: int = 0


class LlmUsage(_Scoped):
    type: Literal["llm_usage"] = "llm_usage"
    n: int = 1
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    thinking_chars: int = 0


class TripEvent(_Scoped):
    """A map-ready trip (paths, distances, stops), built in code from a plan_trip result."""

    type: Literal["trip"] = "trip"
    artifact: dict[str, Any]


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    request_id: str | None = None
    message: str


Event = (
    PlanEvent
    | StepStarted
    | StepResult
    | ApprovalRequired
    | Final
    | ErrorEvent
    | LlmStarted
    | TripEvent
    | Thought
    | Token
    | ToolCallEvent
    | ToolResultEvent
    | LlmUsage
)

MAX_WIRE_OUTPUT = 6000  # tool output shown in the Inspector; the full text stays in the trace

_LIVE: dict[str, type[BaseModel]] = {
    "llm_started": LlmStarted,
    "thought": Thought,
    "token": Token,
    "tool_call": ToolCallEvent,
    "tool_result": ToolResultEvent,
    "llm_usage": LlmUsage,
}


def live_event(raw: dict[str, Any], redact: Callable[[str], str] = lambda t: t) -> Event | None:
    """A live-stream dict (see ``core/livestream``) -> a typed event. Strings are redacted and long
    tool output truncated; an unknown or malformed record is dropped, never raised."""
    cls = _LIVE.get(str(raw.get("kind")))
    if cls is None:
        return None
    data = {k: v for k, v in raw.items() if k != "kind" and k in cls.model_fields}
    for key in ("delta", "output"):
        if isinstance(data.get(key), str):
            text = redact(data[key])
            if key == "output" and len(text) > MAX_WIRE_OUTPUT:
                text = f"{text[:MAX_WIRE_OUTPUT]}…[+{len(text) - MAX_WIRE_OUTPUT} chars]"
            data[key] = text
    if isinstance(data.get("input"), dict):
        data["input"] = json.loads(redact(json.dumps(data["input"], default=str)))
    try:
        return cls.model_validate(data)  # type: ignore[return-value]
    except ValueError:
        return None


def to_sse(event: Event) -> str:
    """One Server-Sent Event frame."""
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
