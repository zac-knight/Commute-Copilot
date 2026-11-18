"""Plan data model, validation and dependency scheduling. Pure code, no LLM.

The router (an 8B local model) proposes a plan; this module decides whether it is acceptable.
Observed live: with no context the model mislabelled a two-agent request as ``calendar`` and added
a spurious clarifying question. So the ``intent`` is DERIVED from the steps here, and conflicting
output is repaired (and the repair reported) rather than trusted.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

AgentName = Literal["transit", "weather", "calendar"]
Intent = Literal["multi_agent", "transit", "weather", "calendar", "clarify", "out_of_scope"]
MAX_STEPS = 4
_ID = re.compile(r"^[A-Za-z0-9_]{1,12}$")


class PlanStep(BaseModel):
    id: str
    agent: AgentName
    task: str = Field(min_length=3, max_length=600)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _id_str(cls, v: Any) -> str:
        return str(v)


class Plan(BaseModel):
    intent: Intent
    steps: list[PlanStep] = Field(default_factory=list)
    clarification_question: str | None = None
    repairs: list[str] = Field(default_factory=list)  # what validation had to fix (for traces)


class PlanError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# JSON schema handed to the model for constrained decoding (the model-facing subset of Plan).
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(Intent.__args__)},  # type: ignore[attr-defined]
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "agent": {"type": "string", "enum": list(AgentName.__args__)},  # type: ignore[attr-defined]
                    "task": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "agent", "task", "depends_on"],
            },
        },
        "clarification_question": {"type": ["string", "null"]},
    },
    "required": ["intent", "steps", "clarification_question"],
}


def derive_intent(steps: list[PlanStep]) -> Intent:
    agents = {s.agent for s in steps}
    if len(agents) > 1:
        return "multi_agent"
    return next(iter(agents))


def parse_plan(raw: str | dict[str, Any]) -> Plan:
    """Raw model output -> Plan. Raises PlanError with human-readable problems."""
    import json

    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            raise ValueError("the plan must be a JSON object")
        return Plan.model_validate(data)
    except (ValueError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            problems = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        else:
            problems = [f"not valid JSON/plan: {exc}"]
        raise PlanError(problems) from exc


def validate_plan(plan: Plan, available: Collection[str]) -> Plan:
    """Repair what can be repaired safely, reject the rest. Returns the normalised plan.

    Repairs (recorded in ``plan.repairs``): ids are renamed to s1, s2, ... ; the intent is derived
    from the steps; a clarification question next to real steps is dropped; steps for an
    unavailable agent are an ERROR (not silently dropped: the user must be told).
    """
    problems: list[str] = []
    repairs = list(plan.repairs)
    steps = list(plan.steps)

    if plan.intent == "out_of_scope":
        if steps:
            repairs.append("out_of_scope plan had steps: dropped")
        return Plan(intent="out_of_scope", repairs=repairs)

    if plan.intent == "clarify" or (not steps and plan.clarification_question):
        q = (plan.clarification_question or "").strip()
        if not q:
            raise PlanError(["a clarify plan needs a clarification_question"])
        if steps:
            repairs.append("clarify plan had steps: dropped")
        return Plan(intent="clarify", clarification_question=q, repairs=repairs)

    if not steps:
        raise PlanError(["the plan has no steps and no clarification question"])
    if len(steps) > MAX_STEPS:
        raise PlanError([f"too many steps ({len(steps)}; max {MAX_STEPS})"])

    ids = [s.id for s in steps]
    if len(set(ids)) != len(ids):
        problems.append("duplicate step ids")
    for s in steps:
        if not _ID.match(s.id):
            problems.append(f"bad step id {s.id!r}")
        if s.agent not in available:
            problems.append(f"agent {s.agent!r} is not available")
        for d in s.depends_on:
            if d == s.id:
                problems.append(f"step {s.id} depends on itself")
            elif d not in ids:
                problems.append(f"step {s.id} depends on unknown step {d!r}")
    if problems:
        raise PlanError(problems)

    # rename ids to s1..sn in the given order (small models use '1', 'step_one', ...)
    rename = {s.id: f"s{i + 1}" for i, s in enumerate(steps)}
    if any(old != new for old, new in rename.items()):
        repairs.append("renamed step ids to s1..sn")
    steps = [
        s.model_copy(update={"id": rename[s.id], "depends_on": [rename[d] for d in s.depends_on]})
        for s in steps
    ]
    # a task that uses {sN.field} needs step sN to have finished: add the missing edge
    from app.orchestrator.placeholders import referenced_steps

    for s in steps:
        for ref in referenced_steps(s.task):
            if ref not in rename.values():
                raise PlanError([f"step {s.id} refers to unknown step {ref}"])
            if ref == s.id:
                raise PlanError([f"step {s.id} refers to itself"])
            if ref not in s.depends_on:
                s.depends_on.append(ref)
                repairs.append(f"added dependency {s.id} -> {ref} (used in its task)")
    cycle = find_cycle(steps)
    if cycle:
        raise PlanError([f"dependency cycle: {' -> '.join(cycle)}"])

    intent = derive_intent(steps)
    if plan.intent != intent:
        repairs.append(f"intent {plan.intent!r} replaced by {intent!r} (derived from the steps)")
    if plan.clarification_question:
        repairs.append("dropped a clarification question that sat next to real steps")
    return Plan(intent=intent, steps=steps, clarification_question=None, repairs=repairs)


def find_cycle(steps: list[PlanStep]) -> list[str] | None:
    deps = {s.id: s.depends_on for s in steps}
    state: dict[str, int] = {}  # 1 visiting, 2 done

    def visit(node: str, path: list[str]) -> list[str] | None:
        if state.get(node) == 2:
            return None
        if state.get(node) == 1:
            return [*path[path.index(node) :], node]
        state[node] = 1
        for d in deps.get(node, []):
            if (found := visit(d, [*path, node])) is not None:
                return found
        state[node] = 2
        return None

    for s in steps:
        if (found := visit(s.id, [])) is not None:
            return found
    return None


def waves(steps: list[PlanStep]) -> list[list[PlanStep]]:
    """Dependency layers: every step in a wave can run concurrently once earlier waves are done."""
    remaining = {s.id: s for s in steps}
    done: set[str] = set()
    out: list[list[PlanStep]] = []
    while remaining:
        ready = [s for s in remaining.values() if all(d in done for d in s.depends_on)]
        if not ready:
            raise PlanError(["dependency cycle"])
        out.append(ready)
        for s in ready:
            done.add(s.id)
            del remaining[s.id]
    return out


def transitive_dependencies(step_id: str, steps: list[PlanStep]) -> list[str]:
    by_id = {s.id: s for s in steps}
    seen: list[str] = []

    def walk(sid: str) -> None:
        for d in by_id[sid].depends_on:
            if d not in seen:
                walk(d)
                seen.append(d)

    walk(step_id)
    return seen
