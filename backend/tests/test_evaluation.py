from __future__ import annotations

import re

import pytest

from app.evaluation.grading import grade_router, plan_edges
from app.evaluation.router_cases import ROUTER_CASES, C
from app.evaluation.stats import (
    latency_summary,
    paired_differences,
    percentile,
    rate,
    sign_test_p,
    wilson,
)
from app.orchestrator.planner import Plan, PlanStep
from app.orchestrator.router import ROUTER_PROMPT

# --- statistics ---------------------------------------------------------------------------


def test_wilson_interval_is_sane_at_the_edges_and_shrinks_with_n() -> None:
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and 0.7 < lo < 0.75  # 10/10 is NOT "100% for sure"
    lo0, hi0 = wilson(0, 10)
    assert lo0 == 0.0 and 0.25 < hi0 < 0.3
    assert wilson(90, 100)[1] - wilson(90, 100)[0] < wilson(9, 10)[1] - wilson(9, 10)[0]
    r = rate(45, 50)
    assert r["lo"] < 0.9 < r["hi"]


def test_percentiles_and_latency_summary() -> None:
    assert percentile([], 0.5) == 0.0
    assert percentile([1, 2, 3, 4], 0.5) == 2.5 and percentile([5], 0.95) == 5
    s = latency_summary([10, 20, 30])
    assert s["p50"] == 20 and s["mean"] == 20 and 20 < s["p95"] <= 30
    assert latency_summary([]) == {"p50": 0.0, "p95": 0.0, "mean": 0.0}


def test_paired_comparison_and_sign_test() -> None:
    d = paired_differences([True, True, False, False], [True, False, True, False])
    assert d == {"both": 1, "only_a": 1, "only_b": 1, "neither": 1}
    assert sign_test_p(0, 0) == 1.0
    assert sign_test_p(5, 5) == 1.0
    assert sign_test_p(10, 0) < 0.01  # a lopsided record is significant
    assert 0.3 < sign_test_p(6, 4) <= 1.0  # a small edge is not


# --- router grading -----------------------------------------------------------------------


def plan(
    *steps: tuple[str, str, list[str]],
    intent: str = "multi_agent",
    repairs: list[str] | None = None,
) -> Plan:
    return Plan(
        intent=intent,  # type: ignore[arg-type]
        steps=[PlanStep(id=i, agent=a, task="do the thing", depends_on=d) for i, a, d in steps],  # type: ignore[arg-type]
        repairs=repairs or [],
    )


def test_dependency_edges_are_expressed_by_agent() -> None:
    p = plan(("s1", "calendar", []), ("s2", "transit", ["s1"]))
    assert plan_edges(p) == ["calendar>transit"]


def test_a_correct_plan_passes_all_three_checks() -> None:
    case = C("x", "c", "m", "multi_agent", "transit weather", "transit>weather")
    g = grade_router(case, plan(("s1", "transit", []), ("s2", "weather", ["s1"])))
    assert g.passed and g.intent_ok and g.agents_ok and g.deps_ok and not g.over_acted


def test_missing_dependency_fails_even_when_the_agents_are_right() -> None:
    case = C("x", "c", "m", "multi_agent", "transit weather", "transit>weather")
    g = grade_router(case, plan(("s1", "transit", []), ("s2", "weather", [])))
    assert g.intent_ok and g.agents_ok and not g.deps_ok and not g.passed


def test_an_extra_dependency_also_fails() -> None:
    case = C("x", "c", "m", "multi_agent", "calendar transit", "")
    g = grade_router(case, plan(("s1", "calendar", []), ("s2", "transit", ["s1"])))
    assert not g.deps_ok and not g.passed


def test_acting_when_a_clarification_was_expected_is_flagged_as_the_risky_error() -> None:
    case = C("x", "c", "m", "clarify")
    g = grade_router(case, plan(("s1", "transit", []), intent="transit"))
    assert g.over_acted and not g.passed
    ok = grade_router(case, Plan(intent="clarify", clarification_question="Where?"))
    assert ok.passed and not ok.over_acted


def test_asking_when_an_action_was_expected_is_flagged_separately() -> None:
    case = C("x", "c", "m", "weather", "weather")
    g = grade_router(case, Plan(intent="clarify", clarification_question="Where?"))
    assert g.over_clarified and not g.over_acted and not g.passed


def test_alternative_intents_and_errors_and_repairs() -> None:
    case = C("x", "c", "ok", "clarify|out_of_scope")
    assert grade_router(case, Plan(intent="out_of_scope")).passed
    assert grade_router(case, Plan(intent="clarify", clarification_question="?")).passed
    assert not grade_router(case, plan(("s1", "weather", []), intent="weather")).passed
    err = grade_router(case, None, "router gave up")
    assert err.error == "router gave up" and not err.passed
    fixed = grade_router(
        C("y", "c", "m", "transit", "transit"),
        plan(("s1", "transit", []), intent="transit", repairs=["renamed ids"]),
    )
    assert fixed.passed and fixed.repaired


# --- the dataset itself -------------------------------------------------------------------


def test_dataset_is_big_enough_unique_and_well_formed() -> None:
    assert len(ROUTER_CASES) >= 80
    ids = [c.id for c in ROUTER_CASES]
    assert len(ids) == len(set(ids))
    messages = [c.message.strip().lower() for c in ROUTER_CASES if not c.history]
    assert len(messages) == len(set(messages))
    valid = {"transit", "weather", "calendar", "multi_agent", "clarify", "out_of_scope"}
    for c in ROUTER_CASES:
        assert set(c.intent.split("|")) <= valid, c.id
        assert set(c.agents) <= {"transit", "weather", "calendar"}, c.id
        for d in c.deps:
            a, b = d.split(">")
            assert a in c.agents and b in c.agents, c.id
        if c.intent in {"clarify", "out_of_scope"}:
            assert not c.agents and not c.deps, c.id
    assert {c.category for c in ROUTER_CASES} >= {
        "clarify",
        "out_of_scope",
        "followup",
        "commute_advice",
        "messy",
    }


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def few_shot_messages() -> list[str]:
    return re.findall(r"^User: (.+)$", ROUTER_PROMPT.text, flags=re.M)


def test_no_eval_case_is_a_near_duplicate_of_a_few_shot_example() -> None:
    shots = few_shot_messages()
    assert len(shots) >= 8  # the regex really found the examples
    for c in ROUTER_CASES:
        a = _tokens(c.message)
        for shot in shots:
            b = _tokens(shot)
            jaccard = len(a & b) / len(a | b)
            assert jaccard < 0.6, (
                f"{c.id} {c.message!r} is too close to few-shot {shot!r} ({jaccard:.2f})"
            )


def test_the_leakage_check_would_actually_catch_a_copy() -> None:
    shot = few_shot_messages()[0]
    a, b = _tokens(shot), _tokens(shot.replace("Chermside", "Southbank"))
    assert len(a & b) / len(a | b) >= 0.6


@pytest.mark.parametrize("cid", ["fu01", "fu02", "fu03"])
def test_followup_cases_carry_the_question_they_answer(cid: str) -> None:
    case = next(c for c in ROUTER_CASES if c.id == cid)
    assert case.history and case.history[0][1].strip().endswith(("?", "."))
