from __future__ import annotations

import json

import pytest

from app.orchestrator.planner import (
    PLAN_SCHEMA,
    Plan,
    PlanError,
    PlanStep,
    derive_intent,
    find_cycle,
    parse_plan,
    transitive_dependencies,
    validate_plan,
    waves,
)

ALL = {"transit", "weather", "calendar"}


def S(
    sid: str, agent: str = "transit", deps: list[str] | None = None, task: str = "do the thing"
) -> PlanStep:
    return PlanStep(id=sid, agent=agent, task=task, depends_on=deps or [])  # type: ignore[arg-type]


def plan(intent: str, steps: list[PlanStep] | None = None, q: str | None = None) -> Plan:
    return Plan(intent=intent, steps=steps or [], clarification_question=q)  # type: ignore[arg-type]


# --- parsing ------------------------------------------------------------------------------


def test_parse_valid_and_invalid_json() -> None:
    raw = json.dumps(
        {
            "intent": "transit",
            "steps": [{"id": "s1", "agent": "transit", "task": "next bus", "depends_on": []}],
            "clarification_question": None,
        }
    )
    assert parse_plan(raw).steps[0].agent == "transit"
    for bad in [
        "not json",
        "[]",
        '{"intent": "dance"}',
        '{"intent": "transit", "steps": [{"id": "s1",'
        '"agent": "teleport", "task": "x y z", "depends_on": []}]}',
    ]:
        with pytest.raises(PlanError):
            parse_plan(bad)


def test_numeric_ids_from_the_model_are_accepted_as_strings() -> None:
    p = parse_plan(
        {
            "intent": "transit",
            "steps": [{"id": 1, "agent": "transit", "task": "next bus", "depends_on": []}],
        }
    )
    assert p.steps[0].id == "1"


def test_schema_enumerates_the_allowed_values() -> None:
    props = PLAN_SCHEMA["properties"]
    assert set(props["intent"]["enum"]) == {
        "multi_agent",
        "transit",
        "weather",
        "calendar",
        "clarify",
        "out_of_scope",
    }
    assert set(props["steps"]["items"]["properties"]["agent"]["enum"]) == ALL


# --- validation and repair ----------------------------------------------------------------


def test_intent_is_derived_from_the_steps() -> None:
    assert derive_intent([S("s1", "transit")]) == "transit"
    assert derive_intent([S("s1", "calendar"), S("s2", "transit", ["s1"])]) == "multi_agent"
    fixed = validate_plan(plan("calendar", [S("s1", "calendar"), S("s2", "transit", ["s1"])]), ALL)
    assert fixed.intent == "multi_agent" and any(
        "replaced by 'multi_agent'" in r for r in fixed.repairs
    )


def test_ids_are_normalised_and_dependencies_follow() -> None:
    fixed = validate_plan(
        plan("multi_agent", [S("1", "calendar"), S("two", "transit", ["1"])]), ALL
    )
    assert [(s.id, s.depends_on) for s in fixed.steps] == [("s1", []), ("s2", ["s1"])]
    assert "renamed step ids to s1..sn" in fixed.repairs


def test_a_clarification_next_to_real_steps_is_dropped() -> None:
    # observed live: the model added 'share your location' next to a complete plan
    p = validate_plan(plan("calendar", [S("s1", "calendar")], "Where are you?"), ALL)
    assert p.clarification_question is None and p.intent == "calendar"
    assert any("dropped a clarification" in r for r in p.repairs)


def test_clarify_and_out_of_scope() -> None:
    c = validate_plan(plan("clarify", [S("s1")], " Where to? "), ALL)
    assert c.intent == "clarify" and c.clarification_question == "Where to?" and c.steps == []
    assert (
        validate_plan(plan("transit", [], "Where to?"), ALL).intent == "clarify"
    )  # no steps + question
    with pytest.raises(PlanError, match="needs a clarification_question"):
        validate_plan(plan("clarify"), ALL)
    o = validate_plan(plan("out_of_scope", [S("s1")]), ALL)
    assert o.intent == "out_of_scope" and o.steps == [] and o.repairs


@pytest.mark.parametrize(
    ("steps", "match"),
    [
        ([], "no steps"),
        ([S("s1"), S("s1")], "duplicate step ids"),
        ([S("s1", deps=["s9"])], "unknown step"),
        ([S("s1", deps=["s1"])], "depends on itself"),
        ([S("s1", deps=["s2"]), S("s2", deps=["s1"])], "dependency cycle"),
        ([S("s1", deps=["s3"]), S("s2", deps=["s1"]), S("s3", deps=["s2"])], "dependency cycle"),
        ([S(f"s{i}") for i in range(1, 6)], "too many steps"),
        ([S("bad id!")], "bad step id"),
    ],
)
def test_invalid_plans_are_rejected(steps: list[PlanStep], match: str) -> None:
    with pytest.raises(PlanError, match=match):
        validate_plan(plan("transit", steps), ALL)


def test_steps_for_an_unavailable_agent_are_an_error_not_silently_dropped() -> None:
    with pytest.raises(PlanError, match="'transit' is not available"):
        validate_plan(plan("transit", [S("s1", "transit")]), {"weather", "calendar"})


# --- graph helpers ------------------------------------------------------------------------


def test_waves_group_independent_steps() -> None:
    steps = [
        S("s1", "calendar"),
        S("s2", "weather"),
        S("s3", "transit", ["s1", "s2"]),
        S("s4", "weather", ["s3"]),
    ]
    assert [[s.id for s in w] for w in waves(steps)] == [["s1", "s2"], ["s3"], ["s4"]]


def test_find_cycle_and_transitive_dependencies() -> None:
    assert find_cycle([S("a"), S("b", deps=["a"])]) is None
    assert find_cycle([S("a", deps=["b"]), S("b", deps=["a"])]) is not None
    chain = [S("s1"), S("s2", deps=["s1"]), S("s3", deps=["s2"]), S("s4")]
    assert (
        transitive_dependencies("s3", chain) == ["s1", "s2"]
        and transitive_dependencies("s4", chain) == []
    )
    with pytest.raises(PlanError):
        waves([S("a", deps=["b"]), S("b", deps=["a"])])
