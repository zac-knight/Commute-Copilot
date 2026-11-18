from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ROLES
from app.core.agent import RequestBudget
from app.core.memory import UserProfile
from app.core.testing import ScriptedLLM, text_response
from app.core.tracing import SpanKind, Tracer, TraceStore
from app.orchestrator.planner import PLAN_SCHEMA
from app.orchestrator.prompts import load_prompt
from app.orchestrator.router import ROUTER_PROMPT, Router, RouterError
from tests.calendar_fixtures import NOW
from tests.orchestrator_fixtures import plan_response, step

ALL = {"transit", "weather", "calendar"}


def make_router(llm: ScriptedLLM, store: TraceStore, available: set[str] = ALL) -> Router:
    return Router(llm, Tracer(store), ROLES["router"], available, UserProfile())


async def test_valid_plan_first_try(store: TraceStore) -> None:
    llm = ScriptedLLM([plan_response("transit", [step("s1", "transit", "next buses from home")])])
    plan = await make_router(llm, store).route("next buses?", [], NOW)
    assert plan.intent == "transit" and plan.steps[0].task == "next buses from home"
    req = llm.requests[0]
    assert req["json_schema"] == PLAN_SCHEMA and req["tools"] == [] and req["think"] is False
    assert req["model"] == ROLES["router"].model


async def test_invalid_plan_gets_one_retry_with_the_errors_fed_back(store: TraceStore) -> None:
    llm = ScriptedLLM(
        [
            text_response("this is not json"),
            plan_response("weather", [step("s1", "weather", "forecast for the next hours")]),
        ]
    )
    plan = await make_router(llm, store).route("weather?", [], NOW)
    assert plan.intent == "weather"
    retry = llm.requests[1]["messages"]
    assert retry[-1]["role"] == "user" and "That plan was rejected" in retry[-1]["content"]


async def test_two_invalid_plans_raise_router_error(store: TraceStore) -> None:
    llm = ScriptedLLM([text_response("nope"), plan_response("transit", [])])
    with pytest.raises(RouterError, match="could not produce a valid plan"):
        await make_router(llm, store).route("hello", [], NOW)
    assert len(llm.requests) == 2  # exactly one retry, then give up


async def test_plan_for_unavailable_agent_is_retried_then_refused(store: TraceStore) -> None:
    bad = plan_response("transit", [step("s1", "transit", "next buses")])
    llm = ScriptedLLM([bad, bad])
    router = make_router(llm, store, available={"weather", "calendar"})
    with pytest.raises(RouterError, match="not available"):
        await router.route("next buses", [], NOW)
    assert "NOT available" in llm.requests[0]["system"] and "transit" in llm.requests[0]["system"]


async def test_history_profile_time_and_rejections_reach_the_prompt(store: TraceStore) -> None:
    llm = ScriptedLLM(
        [plan_response("calendar", [step("s1", "calendar", "list my events tomorrow")])]
    )
    history = [("What's on tomorrow?", "Team sync at 10:00."), ("Thanks", "Anytime.")]
    await make_router(llm, store).route(
        "and the day after?", history, NOW, rejections=["Create 'Gym' on Tue (reason: too early)"]
    )
    req = llm.requests[0]
    assert [m["content"] for m in req["messages"]] == [
        "What's on tomorrow?",
        "Team sync at 10:00.",
        "Thanks",
        "Anytime.",
        "and the day after?",
    ]
    system = req["system"]
    assert "North Lakes station" in system and "chermside" in system.lower()  # profile
    assert "Monday 09 November 2026 07:50" in system
    assert "REJECTED" in system and "too early" in system
    assert "{{" not in system  # every placeholder was filled


async def test_history_is_limited_to_the_last_turns(store: TraceStore) -> None:
    llm = ScriptedLLM([plan_response("weather", [step("s1", "weather", "weather now please")])])
    history = [(f"q{i}", f"a{i}") for i in range(10)]
    await make_router(llm, store).route("now?", history, NOW)
    assert [m["content"] for m in llm.requests[0]["messages"]][:2] == ["q7", "a7"]


async def test_repairs_trace_and_prompt_version_are_recorded(store: TraceStore) -> None:
    llm = ScriptedLLM(
        [
            plan_response(
                "calendar",
                [
                    step("1", "calendar", "find free time"),
                    step("2", "transit", "route to southbank", ["1"]),
                ],
            )
        ]
    )
    tracer = Tracer(store)
    router = Router(llm, tracer, ROLES["router"], ALL, UserProfile())
    with tracer.request() as rid:
        plan = await router.route("free slot then route to southbank", [], NOW)
    assert plan.intent == "multi_agent" and plan.repairs
    spans = {s.name: s for s in store.get_spans(rid)}
    assert spans["router"].kind is SpanKind.ORCHESTRATOR
    assert spans["router"].attributes["prompt_version"] == "router-v3"
    assert spans["router"].attributes["plan"]["intent"] == "multi_agent"
    assert spans["router.llm#1"].kind is SpanKind.LLM and spans["router.llm#1"].tokens_in > 0


async def test_budget_is_consumed_and_respected(store: TraceStore) -> None:
    llm = ScriptedLLM([plan_response("weather", [step("s1", "weather", "weather now please")])])
    budget = RequestBudget(max_tokens=10_000, max_seconds=30)
    await make_router(llm, store).route("weather", [], NOW, budget=budget)
    assert budget.tokens_used == 120  # the scripted response's tokens
    spent = RequestBudget(max_tokens=10, max_seconds=30)
    spent.add_tokens(10)
    with pytest.raises(RouterError, match="token budget"):
        await make_router(ScriptedLLM([]), store).route("x", [], NOW, budget=spent)


async def test_llm_errors_become_router_errors(store: TraceStore, tmp_path: Path) -> None:
    from app.core.llm import LLMError

    class Down:
        async def complete(self, **_: object) -> object:
            raise LLMError("connection", "Cannot reach Ollama")

    with pytest.raises(RouterError, match="Cannot reach Ollama"):
        await make_router(Down(), store).route("hi", [], NOW)  # type: ignore[arg-type]


def test_prompt_is_versioned_and_small() -> None:
    p = load_prompt("router", 3)
    assert p.version == "router-v3" and ROUTER_PROMPT.text == p.text
    assert len(p.text) < 6000  # few-shot but small (it is sent on every request)
    for example in ("clarify", "out_of_scope", "multi_agent"):
        assert example in p.text


def test_previous_prompt_versions_are_kept_on_file() -> None:
    for n in (1, 2):
        old = load_prompt("router", n)
        assert old.version == f"router-v{n}" and old.text != ROUTER_PROMPT.text


def test_v2_forbids_assumed_destinations_and_includes_the_clarify_dentist_example() -> None:
    text = ROUTER_PROMPT.text
    assert "NEVER assume a destination" in text
    assert '"clarification_question":"Where is the dentist?"' in text
    # examples must not reuse the user's saved places as destinations they never named
    assert "at Chermside" not in text


# --- grounding: never plan to a saved place the user did not mention -----------------------


async def test_a_plan_that_assumes_a_saved_place_is_retried_then_becomes_a_question(
    store: TraceStore,
) -> None:
    assumed = plan_response(
        "multi_agent",
        [
            step("s1", "transit", "Plan a trip from home to Chermside leaving now"),
            step("s2", "weather", "Should I leave now or wait?", ["s1"]),
        ],
    )
    llm = ScriptedLLM([assumed, assumed])
    plan = await make_router(llm, store).route(
        "Should I leave now or wait? It looks like rain.", [], NOW
    )
    assert plan.intent == "clarify" and "Where would you like to go" in (
        plan.clarification_question or ""
    )
    assert plan.repairs and len(llm.requests) == 2  # one retry, with the reason fed back
    assert "never mentioned" in llm.requests[1]["messages"][-1]["content"]


async def test_the_retry_can_fix_it_by_asking(store: TraceStore) -> None:
    llm = ScriptedLLM(
        [
            plan_response(
                "transit", [step("s1", "transit", "Plan a trip from home to Southbank now")]
            ),
            plan_response("clarify", question="Where are you heading?"),
        ]
    )
    plan = await make_router(llm, store).route("Should I leave now?", [], NOW)
    assert plan.intent == "clarify" and plan.clarification_question == "Where are you heading?"


async def test_naming_the_place_in_this_message_or_recent_history_is_grounding(
    store: TraceStore,
) -> None:
    ok = plan_response(
        "transit", [step("s1", "transit", "Plan a trip from home to Chermside leaving now")]
    )
    said_now = await make_router(ScriptedLLM([ok]), store).route(
        "Leave now for Chermside?", [], NOW
    )
    assert said_now.intent == "transit"
    earlier = [("I'm going to Chermside later", "OK."), ("Thanks", "Anytime.")]
    said_before = await make_router(ScriptedLLM([ok]), store).route("Leave now?", earlier, NOW)
    assert said_before.intent == "transit"
    # the nickname's target counts too ('southbank' -> 'South Bank station')
    sb = plan_response(
        "transit", [step("s1", "transit", "Plan a trip from home to South Bank station")]
    )
    assert (
        await make_router(ScriptedLLM([sb]), store).route("Take me to south bank station", [], NOW)
    ).intent == "transit"


async def test_assistant_text_alone_is_not_grounding(store: TraceStore) -> None:
    # only the USER's words ground a destination; the assistant may have been the one to suggest it
    ok = plan_response(
        "transit", [step("s1", "transit", "Plan a trip from home to Chermside leaving now")]
    )
    history = [("What's near me?", "You could go to Chermside.")]
    plan = await make_router(ScriptedLLM([ok, ok]), store).route("Leave now?", history, NOW)
    assert plan.intent == "clarify"


async def test_home_and_unsaved_places_are_not_policed(store: TraceStore) -> None:
    p = plan_response(
        "transit", [step("s1", "transit", "Plan a trip from home to Mount Gravatt leaving now")]
    )
    assert (
        await make_router(ScriptedLLM([p]), store).route("Mount Gravatt now", [], NOW)
    ).intent == "transit"


def test_v3_says_a_location_is_not_needed_just_to_add_an_event() -> None:
    assert "A location is NOT needed to add or change a calendar event" in ROUTER_PROMPT.text
    assert "Add 'Coffee with Sam' tomorrow at 3pm." in ROUTER_PROMPT.text
