"""Deterministic graders. No model is involved in grading routing or tool use."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.evaluation.router_cases import RouterCase
from app.orchestrator.planner import Plan

ACTING = {"transit", "weather", "calendar", "multi_agent"}


@dataclass
class RouterGrade:
    id: str
    category: str
    message: str
    expected_intent: str
    expected_agents: list[str]
    expected_deps: list[str]
    actual_intent: str
    actual_agents: list[str]
    actual_deps: list[str]
    intent_ok: bool
    agents_ok: bool
    deps_ok: bool
    passed: bool
    over_acted: (
        bool  # expected a clarification / refusal but the plan runs agents (the risky error)
    )
    over_clarified: bool  # expected an action but the router asked a question
    repaired: bool  # validation had to fix the plan
    error: str | None = None
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def plan_edges(plan: Plan) -> list[str]:
    agent = {s.id: s.agent for s in plan.steps}
    return sorted(f"{agent[d]}>{s.agent}" for s in plan.steps for d in s.depends_on if d in agent)


def grade_router(case: RouterCase, plan: Plan | None, error: str | None = None) -> RouterGrade:
    accepted = case.intent.split("|")
    expect_agents = sorted(case.agents)
    expect_deps = sorted(case.deps)
    if plan is None:
        return RouterGrade(
            case.id, case.category, case.message, case.intent, expect_agents, expect_deps,
            "error", [], [], False, False, False, False, False, False, False,
            error=error or "no plan",
        )  # fmt: skip
    agents: list[str] = sorted(str(s.agent) for s in plan.steps)
    deps = plan_edges(plan)
    intent_ok = plan.intent in accepted
    agents_ok = agents == expect_agents
    deps_ok = deps == expect_deps
    expects_no_action = not (set(accepted) & ACTING)
    return RouterGrade(
        case.id, case.category, case.message, case.intent, expect_agents, expect_deps,
        plan.intent, agents, deps, intent_ok, agents_ok, deps_ok,
        passed=intent_ok and agents_ok and deps_ok,
        over_acted=expects_no_action and bool(plan.steps),
        over_clarified=(not set(accepted) & {"clarify"}) and plan.intent == "clarify",
        repaired=bool(plan.repairs),
    )  # fmt: skip
