from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.testing import text_response, tool_use_response
from app.orchestrator.events import StepOutcome, StepResult, StepStarted, ToolOutput
from app.orchestrator.placeholders import PlaceholderError, referenced_steps, resolve
from app.orchestrator.planner import Plan, PlanError, PlanStep, validate_plan
from tests.orchestrator_fixtures import build_test_state, collect, plan_response, step
from tests.test_orchestrator_engine import final_of

ALL = {"transit", "weather", "calendar"}


def outcome(sid: str, tool: str, output: Any, agent: str = "calendar") -> StepOutcome:
    return StepOutcome(
        id=sid,
        agent=agent,
        task="t",
        status="ok",
        tool_outputs=[ToolOutput(tool=tool, input={}, output=output)],
    )


SLOTS = outcome(
    "s1",
    "find_free_slots",
    {
        "slots": [
            {"start_iso": "2026-11-19T14:10+10:00", "end_iso": "2026-11-19T14:50+10:00"},
            {"start_iso": "2026-11-19T16:10+10:00", "end_iso": "2026-11-19T17:00+10:00"},
        ]
    },
)


# --- resolve ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("arrive by {s1.start}", "arrive by 2026-11-19T14:10+10:00"),
        ("arrive by {s1.start-15m}", "arrive by 2026-11-19T13:55+10:00"),
        ("leave after {s1.end+10m}", "leave after 2026-11-19T15:00+10:00"),
        ("{s1.start-2h} and {s1.end}", "2026-11-19T12:10+10:00 and 2026-11-19T14:50+10:00"),
        ("midnight crossing {s1.start-14h}", "midnight crossing 2026-11-19T00:10+10:00"),
        ("no placeholders here", "no placeholders here"),
        ("braces {not a placeholder} stay", "braces {not a placeholder} stay"),
    ],
)
def test_resolve_slots_and_offsets(task: str, expected: str) -> None:
    assert resolve(task, {"s1": SLOTS}) == expected


def test_each_source_of_times() -> None:
    journey = outcome(
        "s2",
        "plan_trip",
        {
            "journeys": [
                {"depart_iso": "2026-11-19T08:00+10:00", "arrive_iso": "2026-11-19T09:05+10:00"}
            ]
        },
        "transit",
    )
    events = outcome(
        "s3",
        "list_events",
        {"events": [{"start_iso": "2026-11-19T10:00+10:00", "end_iso": "2026-11-19T11:00+10:00"}]},
    )
    deps = outcome(
        "s4",
        "next_departures",
        {
            "departures": [
                {
                    "scheduled_iso": "2026-11-19T08:00+10:00",
                    "predicted_iso": "2026-11-19T08:07+10:00",
                }
            ]
        },
        "transit",
    )
    scheduled_only = outcome(
        "s5",
        "next_departures",
        {"departures": [{"scheduled_iso": "2026-11-19T08:00+10:00", "predicted_iso": None}]},
        "transit",
    )
    res = {"s2": journey, "s3": events, "s4": deps, "s5": scheduled_only}
    assert resolve("{s2.depart} {s2.arrive} {s2.start} {s2.end}", res) == (
        "2026-11-19T08:00+10:00 2026-11-19T09:05+10:00 "
        "2026-11-19T08:00+10:00 2026-11-19T09:05+10:00"
    )
    assert resolve("{s3.start}-{s3.end}", res) == "2026-11-19T10:00+10:00-2026-11-19T11:00+10:00"
    assert resolve("{s4.depart}", res) == "2026-11-19T08:07+10:00"  # the realtime time wins
    assert resolve("{s5.start}", res) == "2026-11-19T08:00+10:00"


@pytest.mark.parametrize(
    ("task", "problem"),
    [
        ("{s9.start}", "has not run"),
        ("{s1.arrive}", "no arrive time"),  # a free slot has no 'arrive'
    ],
)
def test_unresolvable_placeholders_fail_loudly(task: str, problem: str) -> None:
    with pytest.raises(PlaceholderError, match=problem):
        resolve(task, {"s1": SLOTS})


def test_no_free_slot_is_a_clear_error_not_a_made_up_time() -> None:
    empty = outcome("s1", "find_free_slots", {"slots": [], "note": "No free slot"})
    errored = StepOutcome(
        id="s1",
        agent="calendar",
        task="t",
        status="ok",
        tool_outputs=[
            ToolOutput(tool="find_free_slots", input={}, output={"error": "x"}, is_error=True)
        ],
    )
    for o in (empty, errored):
        with pytest.raises(PlaceholderError, match="no free slot or no journey was found"):
            resolve("arrive by {s1.start-15m}", {"s1": o})


def test_referenced_steps_are_unique_and_ordered() -> None:
    assert referenced_steps("a {s2.start} b {s1.end-5m} c {s2.end}") == ["s2", "s1"]
    assert referenced_steps("nothing") == []


# --- plan validation adds the missing dependency ------------------------------------------


def S(sid: str, agent: str, task: str, deps: list[str] | None = None) -> PlanStep:
    return PlanStep(id=sid, agent=agent, task=task, depends_on=deps or [])  # type: ignore[arg-type]


def test_a_placeholder_adds_the_dependency_the_model_forgot() -> None:
    plan = Plan(
        intent="multi_agent",
        steps=[
            S("s1", "calendar", "find a free slot"),
            S("s2", "transit", "arrive by {s1.start-15m}"),
        ],
    )
    fixed = validate_plan(plan, ALL)
    assert fixed.steps[1].depends_on == ["s1"]
    assert any("added dependency s2 -> s1" in r for r in fixed.repairs)


def test_placeholders_for_unknown_or_own_step_are_rejected() -> None:
    with pytest.raises(PlanError, match="unknown step s7"):
        validate_plan(
            Plan(intent="transit", steps=[S("s1", "transit", "arrive by {s7.start}")]), ALL
        )
    with pytest.raises(PlanError, match="refers to itself"):
        validate_plan(
            Plan(intent="transit", steps=[S("s1", "transit", "arrive by {s1.start}")]), ALL
        )


def test_a_dependency_added_by_a_placeholder_can_still_form_a_cycle_error() -> None:
    steps = [S("s1", "calendar", "x {s2.end}"), S("s2", "transit", "y {s1.start}")]
    with pytest.raises(PlanError, match="dependency cycle"):
        validate_plan(Plan(intent="multi_agent", steps=steps), ALL)


# --- end to end ---------------------------------------------------------------------------


async def test_the_transit_step_receives_an_exact_time_not_a_sentence_to_interpret(
    tmp_path: Path,
) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response(
            "multi_agent",
            [
                step("s1", "calendar", "Find a free 60 minute slot today afternoon"),
                step(
                    "s2",
                    "transit",
                    "Plan a trip from A to Dee Station arriving by {s1.start-15m}",
                    ["s1"],
                ),
            ],
        ),
    )
    llm.script(
        "calendar",
        tool_use_response(
            [
                (
                    "find_free_slots",
                    {"start": "today 12:00", "end": "today 17:00", "duration_minutes": 60},
                )
            ]
        ),
        text_response("13:10"),
    )
    llm.script("transit", text_response("Planned."))
    llm.script("synth", text_response("ok"))
    events = await collect(state, "free slot and a trip")
    # default_events: sync 10-11, lunch 12-13, 10 min buffer -> first free slot starts 13:10
    started = next(e for e in events if isinstance(e, StepStarted) and e.id == "s2")
    assert started.task == "Plan a trip from A to Dee Station arriving by 2026-11-09T12:55+10:00"
    sent = llm.requests["transit"][0]["messages"][0]["content"]
    assert "arriving by 2026-11-09T12:55+10:00" in sent and "{s1" not in sent
    assert final_of(events).status == "ok"


async def test_no_free_slot_means_the_trip_step_fails_with_the_reason(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response(
            "multi_agent",
            [
                step("s1", "calendar", "Find a free 8 hour slot today"),
                step(
                    "s2",
                    "transit",
                    "Plan a trip from A to Dee Station arriving by {s1.start-15m}",
                    ["s1"],
                ),
            ],
        ),
    )
    llm.script(
        "calendar",
        tool_use_response(
            [
                (
                    "find_free_slots",
                    {"start": "today 09:00", "end": "today 17:00", "duration_minutes": 480},
                )
            ]
        ),
        text_response("No slot that long."),
    )
    llm.script("synth", text_response("There is no 8 hour free slot today."))
    events = await collect(state, "free 8 hours and a trip")
    results = {e.id: e for e in events if isinstance(e, StepResult)}
    assert results["s1"].status == "ok" and results["s2"].status == "failed"
    assert "no free slot or no journey was found" in (results["s2"].error or "")
    assert not llm.requests["transit"]  # the transit agent was never asked to guess a time
    f = final_of(events)
    assert f.status == "partial" and "no 8 hour free slot" in f.text and "transit" in f.text
