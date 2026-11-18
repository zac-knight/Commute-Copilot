from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.core.approvals import ActionStatus
from app.core.llm import LLMError
from app.core.testing import text_response, tool_use_response
from app.core.tracing import SpanKind
from app.orchestrator.engine import OUT_OF_SCOPE_TEXT, plans_from_outcomes
from app.orchestrator.events import (
    ApprovalRequired,
    ErrorEvent,
    Final,
    PlanEvent,
    StepOutcome,
    StepResult,
    StepStarted,
    ToolOutput,
)
from app.orchestrator.synthesiser import NO_PENDING_NOTE, reconcile_approval_claims
from tests.orchestrator_fixtures import RoleLLM, build_test_state, collect, plan_response, step


def types(events: list[Any]) -> list[str]:
    return [e.type for e in events]


def final_of(events: list[Any]) -> Final:
    return next(e for e in reversed(events) if isinstance(e, Final))


def synth_input(llm: RoleLLM) -> dict[str, Any]:
    return json.loads(llm.requests["synth"][-1]["messages"][0]["content"])


# --- single agent -------------------------------------------------------------------------


async def test_single_agent_request_streams_the_documented_events(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router", plan_response("transit", [step("s1", "transit", "next departures from A")])
    )
    llm.script(
        "transit",
        tool_use_response([("next_departures", {"stop_id": "A", "limit": 2})]),
        text_response("Next bus 08:05 (delayed)."),
    )
    llm.script("synth", text_response("Your next bus leaves at 08:05 (5 min late)."))
    events = await collect(state, "next buses from A?")
    assert types(events) == ["plan", "step_started", "step_result", "final"]
    plan = events[0]
    assert isinstance(plan, PlanEvent) and plan.plan.intent == "transit" and plan.request_id
    res = events[2]
    assert (
        isinstance(res, StepResult) and res.status == "ok" and res.tool_calls == ["next_departures"]
    )
    final = final_of(events)
    assert final.status == "ok" and final.text == "Your next bus leaves at 08:05 (5 min late)."
    assert final.request_id == plan.request_id and final.tokens_in > 0 and final.duration_ms >= 0
    # the synthesiser got STRUCTURED tool output, not just the specialist's prose
    data = synth_input(llm)["results"][0]["data"][0]
    assert data["tool"] == "next_departures" and '"status":"delayed"' in data["result"].replace(
        " ", ""
    )


async def test_the_specialist_is_told_the_time_task_and_original_request(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router", plan_response("weather", [step("s1", "weather", "forecast for the next hours")])
    )
    llm.script("weather", text_response("Dry."))
    llm.script("synth", text_response("Dry day."))
    await collect(state, "how's the weather?")
    first = llm.requests["weather"][0]["messages"][0]["content"]
    assert "Monday 09 November 2026 07:50" in first
    assert "Your task: forecast for the next hours" in first
    assert "The user's original request: how's the weather?" in first
    assert "Results from earlier steps" not in first  # no dependencies


# --- multi agent --------------------------------------------------------------------------


async def test_dependent_step_receives_the_earlier_results_as_structured_data(
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
                    "Plan a trip from A to Dee Station arriving 15 minutes before the slot in s1",
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
        text_response("Free: 13:10-17:00."),
    )
    seen: dict[str, str] = {}

    def transit_turn(messages: list[dict[str, Any]]) -> Any:
        seen["first"] = messages[0]["content"]
        return tool_use_response(
            [("plan_trip", {"origin": "A", "destination": "DSTN", "depart_after": "today 07:50"})]
        )

    llm.script("transit", transit_turn, text_response("Take the 100 then the train."))
    llm.script("synth", text_response("Free slot 13:10; leave around 07:53."))
    events = await collect(state, "free hour this afternoon and how do I get to Dee Station")
    assert types(events) == [
        "plan",
        "step_started",
        "step_result",
        "step_started",
        "step_result",
        "final",
    ]
    assert [e.id for e in events if isinstance(e, StepStarted)] == ["s1", "s2"]  # s2 only after s1
    dep = seen["first"].split(
        "Results from earlier steps (structured data; use them, do not ask the user for them):\n"
    )[1]
    (step_data,) = json.loads(dep)
    assert step_data["step"] == "s1" and step_data["agent"] == "calendar"
    slots = json.loads(step_data["data"][0]["result"])["slots"]
    assert slots[0]["start_iso"] == "2026-11-09T13:10+10:00"  # machine-readable, ready to use
    assert final_of(events).status == "ok"


async def test_independent_steps_really_run_in_parallel(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response(
            "multi_agent",
            [
                step("s1", "weather", "weather now please"),
                step("s2", "transit", "next departures from A"),
            ],
        ),
    )
    arrived = 0
    both = asyncio.Event()

    async def barrier() -> None:  # a sequential executor would deadlock here
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=3)

    llm.hooks["weather"] = llm.hooks["transit"] = barrier
    llm.script("weather", text_response("Dry."))
    llm.script("transit", text_response("Bus at 08:00."))
    llm.script("synth", text_response("Dry, and a bus at 08:00."))
    events = await collect(state, "weather and next bus")
    assert final_of(events).status == "ok" and arrived == 2
    started = [i for i, e in enumerate(events) if isinstance(e, StepStarted)]
    results = [i for i, e in enumerate(events) if isinstance(e, StepResult)]
    assert max(started) < min(results)  # both started before either finished


async def test_weather_advice_uses_the_plan_from_the_transit_step_without_the_model_copying_it(
    tmp_path: Path,
) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response(
            "multi_agent",
            [
                step("s1", "transit", "Plan a trip from A to Dee Station leaving now"),
                step(
                    "s2",
                    "weather",
                    "Should I leave now or wait? Advise on the planned trip",
                    ["s1"],
                ),
            ],
        ),
    )
    llm.script(
        "transit",
        tool_use_response(
            [("plan_trip", {"origin": "A", "destination": "DSTN", "depart_after": "today 07:50"})]
        ),
        text_response("Bus 100 then rail."),
    )
    llm.script("weather", tool_use_response([("commute_advice", {})]), text_response("Go now."))
    llm.script("synth", text_response("Leave about 07:53; no rain expected."))
    events = await collect(state, "should I leave now for Dee Station?")
    assert final_of(events).status == "ok"
    advice = next(
        t for t in synth_input(llm)["results"][1]["data"] if t["tool"] == "commute_advice"
    )
    result = json.loads(advice["result"])
    assert "leave_now" in result and result["hazards"]["slots_used"] > 0
    # forecast fetched at the journey's own start (stop A), not at coordinates the model invented
    lat, lon = state.weather.client.calls[-1]  # type: ignore[attr-defined]
    assert (round(lat, 2), round(lon, 2)) == (-27.23, 153.02)


def test_plans_from_outcomes_builds_route_plans_with_start_coordinates(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path)
    journey = {
        "legs": [
            {
                "kind": "transit",
                "depart_iso": "2026-11-09T08:00:00+10:00",
                "arrive_iso": "2026-11-09T08:20:00+10:00",
                "route": "100",
                "mode": "bus",
            }
        ]
    }
    out = StepOutcome(
        id="s1",
        agent="transit",
        task="t",
        status="ok",
        tool_outputs=[
            ToolOutput(
                tool="plan_trip",
                input={},
                output={"origin": [{"stop_id": "A"}], "journeys": [journey]},
            ),
            ToolOutput(tool="plan_trip", input={}, output={"journeys": []}, is_error=True),
            ToolOutput(tool="next_departures", input={}, output={"departures": []}),
        ],
    )
    (plan,) = plans_from_outcomes([out], state.transit)
    assert (plan.latitude, plan.longitude) == (-27.23, 153.02) and plan.legs[0].route == "100"
    assert (
        plans_from_outcomes([out], None)[0].latitude is None
    )  # no transit service: falls back to home


# --- clarify / out of scope / router failure ----------------------------------------------


async def test_clarify_asks_and_runs_no_agent(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script("router", plan_response("clarify", question="Where are you heading?"))
    events = await collect(state, "should I leave now or wait?")
    assert types(events) == ["plan", "final"]
    f = final_of(events)
    assert f.status == "clarify" and f.text == "Where are you heading?"
    assert not llm.requests["synth"] and not llm.requests["transit"]


async def test_out_of_scope_is_polite_and_deterministic(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script("router", plan_response("out_of_scope"))
    f = final_of(await collect(state, "book me a flight to Sydney"))
    assert f.status == "out_of_scope" and f.text == OUT_OF_SCOPE_TEXT and not llm.requests["synth"]


async def test_router_failure_is_reported_not_raised(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script("router", text_response("garbage"), text_response("more garbage"))
    events = await collect(state, "hello")
    assert types(events) == ["final"]
    assert final_of(events).status == "failed" and "couldn't work out" in final_of(events).text


# --- failures and degradation -------------------------------------------------------------


def boom(_: list[dict[str, Any]]) -> Any:
    raise LLMError("connection", "model server down")


async def test_failed_step_skips_its_dependents_and_the_answer_says_so(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response(
            "multi_agent",
            [
                step("s1", "transit", "plan a trip from A to Dee"),
                step("s2", "weather", "advise on the trip", ["s1"]),
            ],
        ),
    )
    llm.script("transit", boom)
    events = await collect(state, "trip + weather")
    results = [e for e in events if isinstance(e, StepResult)]
    assert [(r.id, r.status) for r in results] == [("s1", "failed"), ("s2", "skipped")]
    assert "did not succeed: s1" in (results[1].error or "")
    f = final_of(events)
    assert (
        f.status == "failed" and "model server down" in f.text and "did not succeed: s1" in f.text
    )
    assert not llm.requests["synth"]  # nothing to synthesise: deterministic reply, no model call


async def test_partial_success_keeps_the_good_part_and_names_the_failure(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response(
            "multi_agent",
            [
                step("s1", "weather", "weather now please"),
                step("s2", "transit", "next departures from A"),
            ],
        ),
    )
    llm.script("weather", text_response("Dry."))
    llm.script("transit", boom)
    llm.script("synth", text_response("It will be dry."))  # forgets to mention the failure
    f = final_of(await collect(state, "weather and bus"))
    assert f.status == "partial" and f.text.startswith("It will be dry.")
    assert "transit" in f.text and "model server down" in f.text  # the failure can never be dropped
    assert synth_input(llm)["results"][1]["status"] == "failed"


async def test_synthesiser_failure_falls_back_to_the_specialists_text(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script("router", plan_response("weather", [step("s1", "weather", "weather now please")]))
    llm.script("weather", text_response("It is dry and 22 degrees."))
    llm.script("synth", boom)
    f = final_of(await collect(state, "weather?"))
    assert (
        f.status == "ok" and "It is dry and 22 degrees." in f.text and "couldn't polish" in f.text
    )


async def test_exhausted_budget_degrades_gracefully(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path, max_request_tokens=100)  # the router alone uses 120
    llm.script(
        "router", plan_response("transit", [step("s1", "transit", "next departures from A")])
    )
    events = await collect(state, "next bus")
    f = final_of(events)
    assert f.status == "failed" and "token budget" in f.text
    assert not llm.requests["transit"]  # the agent never even called the model


async def test_unexpected_error_still_ends_the_stream(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script("router", plan_response("weather", [step("s1", "weather", "weather now please")]))
    llm.script("weather", text_response("Dry."))  # no synth script: RoleLLM raises AssertionError
    events = await collect(state, "weather?")
    assert isinstance(events[-2], ErrorEvent) and isinstance(events[-1], Final)
    assert events[-1].status == "failed"


# --- approvals ----------------------------------------------------------------------------


async def test_proposal_streams_an_approval_card_and_the_footer_comes_from_the_real_queue(
    tmp_path: Path,
) -> None:
    state, llm = build_test_state(tmp_path)
    before = state.calendar_writer.path.read_bytes()
    llm.script(
        "router",
        plan_response(
            "calendar", [step("s1", "calendar", "Propose adding 'Coffee' today at 15:00")]
        ),
    )
    llm.script(
        "calendar",
        tool_use_response([("propose_create_event", {"title": "Coffee", "start": "today 15:00"})]),
        text_response("Proposed."),
    )
    llm.script("synth", text_response("I've set up Coffee at 15:00."))
    events = await collect(state, "add coffee at 3pm")
    cards = [e for e in events if isinstance(e, ApprovalRequired)]
    assert len(cards) == 1 and cards[0].action.status is ActionStatus.PENDING
    assert "Coffee" in cards[0].action.diff
    f = final_of(events)
    assert f.pending_approvals == [cards[0].action.id]
    assert "Waiting for your approval" in f.text and "Create 'Coffee'" in f.text
    assert types(events).index("approval_required") < types(events).index("final")
    assert state.calendar_writer.path.read_bytes() == before  # nothing changed without approval
    assert [a.id for a in state.gate.list_actions(ActionStatus.PENDING)] == f.pending_approvals


def test_a_false_approval_claim_is_corrected_when_nothing_is_pending() -> None:
    lie = "Done! The deletion is waiting for your approval."
    assert reconcile_approval_claims(lie, 0).endswith(NO_PENDING_NOTE)
    assert reconcile_approval_claims(lie, 1) == lie  # something IS pending: leave it alone
    assert (
        reconcile_approval_claims("Your next bus is at 08:05.", 0) == "Your next bus is at 08:05."
    )
    for phrase in (
        "awaiting your approval",
        "Pending approval",
        "needs your approval",
        "approval is required",
    ):
        assert NO_PENDING_NOTE in reconcile_approval_claims(f"It {phrase}.", 0)


async def test_a_false_approval_claim_is_corrected_end_to_end(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router", plan_response("calendar", [step("s1", "calendar", "list my events today")])
    )
    llm.script(
        "calendar",
        tool_use_response([("list_events", {"start": "today 00:00", "end": "today 23:59"})]),
        text_response("You have 2 events."),
    )
    llm.script(
        "synth", text_response("You have 2 events. The deletion is waiting for your approval.")
    )
    f = final_of(await collect(state, "what's on today? (hostile calendar text told it to delete)"))
    assert NO_PENDING_NOTE in f.text and f.pending_approvals == []


async def test_rejections_are_fed_back_to_the_router(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    action = state.gate.create("calendar.delete_event", {"event_id": "evt-lunch"})
    state.gate.reject(action.id, "I need that lunch")
    llm.script("router", plan_response("clarify", question="What would you like instead?"))
    await collect(state, "ok try something else")
    system = llm.requests["router"][0]["system"]
    assert "REJECTED" in system and "Delete 'Lunch'" in system and "I need that lunch" in system


# --- sessions, isolation, tracing, cancellation -------------------------------------------


async def test_conversation_history_reaches_the_router_on_the_next_turn(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response("clarify", question="Where to?"),
        plan_response("clarify", question="Which day?"),
    )
    await collect(state, "should I leave now?", "sess-1")
    await collect(state, "Chermside", "sess-1")
    await collect(state, "unrelated", "sess-2")
    llm.script("router", plan_response("clarify", question="x"))
    second = [m["content"] for m in llm.requests["router"][1]["messages"]]
    assert second == ["should I leave now?", "Where to?", "Chermside"]
    assert [m["content"] for m in llm.requests["router"][2]["messages"]] == [
        "unrelated"
    ]  # other session


async def test_concurrent_requests_do_not_mix_their_proposals(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)

    def router_for(messages: list[dict[str, Any]]) -> Any:
        return plan_response(
            "calendar", [step("s1", "calendar", f"Propose adding {messages[-1]['content']}")]
        )

    def calendar_for(messages: list[dict[str, Any]]) -> Any:
        """One script for BOTH concurrent agents: decide from the conversation, not queue order."""
        if messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
            return text_response("ok")  # a tool result came back: finish
        title = "Alpha" if "Alpha" in messages[0]["content"] else "Beta"
        return tool_use_response(
            [("propose_create_event", {"title": title, "start": "today 16:00"})]
        )

    def synth_for(messages: list[dict[str, Any]]) -> Any:
        return text_response("Done " + ("A." if "Alpha" in messages[0]["content"] else "B."))

    llm.script("router", router_for, router_for)
    llm.script("calendar", *[calendar_for] * 4)
    llm.script("synth", synth_for, synth_for)
    a, b = await asyncio.gather(
        collect(state, "event Alpha", "sa"), collect(state, "event Beta", "sb")
    )
    fa, fb = final_of(a), final_of(b)
    assert len(fa.pending_approvals) == len(fb.pending_approvals) == 1
    assert set(fa.pending_approvals).isdisjoint(fb.pending_approvals)
    titles = {
        state.gate.get(i).payload["title"] for i in fa.pending_approvals + fb.pending_approvals
    }
    assert titles == {"Alpha", "Beta"}
    assert state.gate.get(fa.pending_approvals[0]).payload["title"] == "Alpha"  # each got its own
    assert state.gate.get(fb.pending_approvals[0]).payload["title"] == "Beta"
    assert fa.text.startswith("Done A.") and fb.text.startswith("Done B.")


async def test_the_whole_request_is_one_trace_with_prompt_versions(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router", plan_response("transit", [step("s1", "transit", "next departures from A")])
    )
    llm.script(
        "transit", tool_use_response([("next_departures", {"stop_id": "A"})]), text_response("ok")
    )
    llm.script("synth", text_response("Bus at 08:05."))
    f = final_of(await collect(state, "next bus"))
    spans = state.store.get_spans(f.request_id)
    by_name = {s.name: s for s in spans}
    root = by_name["orchestrator"]
    assert root.kind is SpanKind.ORCHESTRATOR and root.parent_id is None
    assert root.attributes["status"] == "ok" and root.output_preview.startswith("Bus at 08:05")
    assert {"router", "step:s1", "transit", "synthesiser", "next_departures"} <= set(by_name)
    assert by_name["router"].attributes["prompt_version"] == "router-v3"
    assert by_name["transit"].attributes["prompt_version"] == "transit-v1"
    assert by_name["synthesiser"].attributes["prompt_version"] == "synth-v2"
    assert (
        by_name["step:s1"].parent_id == root.span_id
        and by_name["transit"].parent_id == by_name["step:s1"].span_id
    )
    assert sum(s.tokens_in for s in spans if s.kind is SpanKind.LLM) == f.tokens_in


async def test_closing_the_stream_cancels_the_work(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script("router", plan_response("weather", [step("s1", "weather", "weather now please")]))
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.sleep(60)

    llm.hooks["weather"] = hang
    before = len(asyncio.all_tasks())
    gen = state.orchestrator.run("s1", "weather?")
    first = await gen.__anext__()
    assert first.type == "plan"
    await asyncio.wait_for(started.wait(), 3)
    await gen.aclose()  # the client disconnected
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) <= before  # no leaked request task


@pytest.mark.parametrize("agents", [{"weather", "calendar"}])
async def test_agents_that_are_not_available_are_not_offered(
    tmp_path: Path, agents: set[str]
) -> None:
    state, llm = build_test_state(tmp_path, with_transit=False)
    assert state.available == agents
    llm.script("router", plan_response("clarify", question="?"))
    await collect(state, "hi")
    assert "Agents NOT available right now: ['transit']" in llm.requests["router"][0]["system"]


# --- wiring: refresh loop, shutdown, sessions ---------------------------------------------


async def test_the_daily_gtfs_refresh_loop_reloads_the_planner_and_survives_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agents.transit.gtfs_static import RefreshResult
    from app.orchestrator import wiring

    state, _ = build_test_state(tmp_path, gtfs_refresh_hours=0.0)
    assert state.transit is not None
    reloads: list[int] = []
    monkeypatch.setattr(state.transit, "reload", lambda: reloads.append(1))
    results = iter(
        [
            RefreshResult("updated", "5 trips"),
            RuntimeError("boom"),
            RefreshResult("up_to_date", "304"),
        ]
    )
    calls = 0

    async def fake_refresh(*_: Any, **__: Any) -> RefreshResult:
        nonlocal calls
        calls += 1
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(wiring, "refresh_feed", fake_refresh)
    task = asyncio.create_task(wiring.gtfs_refresh_loop(state))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if calls >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 3 and reloads == [
        1
    ]  # reloaded after the 'updated' result only; the error did not kill the loop
    assert state.last_refresh["status"] == "up_to_date"


async def test_shutdown_closes_every_resource(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path)
    closed: list[str] = []

    async def aclose() -> None:
        closed.append("async")

    state.closers.extend([lambda: closed.append("sync"), aclose])
    await state.aclose()
    assert closed == ["sync", "async"]


def test_session_store_is_bounded() -> None:
    from app.orchestrator.sessions import SessionStore

    s = SessionStore(max_sessions=2, max_turns=2)
    for i in range(3):
        s.add(f"s{i}", f"q{i}", f"a{i}")
    assert s.history("s0") == [] and s.history("s2") == [
        ("q2", "a2")
    ]  # the oldest session was evicted
    for i in range(5):
        s.add("s2", f"q{i}", f"a{i}")
    assert len(s.history("s2")) == 2  # only the last turns are kept


# --- the decision comes from code, never from model prose (found while recording the demo) -------


ADVICE_WAIT = {
    "leave_now": False,
    "suggested_leave_time": "09:00",
    "reasons": [
        "Rain is likely while you are outside (walking to the first stop): 80% chance.",
        "Waiting about 75 min should avoid the worst of it.",
    ],
    "bring": ["umbrella"],
    "confidence": 0.8,
}


def test_decision_text_is_written_by_code_from_the_advice() -> None:
    from app.orchestrator.synthesiser import decision_text

    wait = decision_text(ADVICE_WAIT)
    assert wait.startswith("**Wait: leave around 09:00.**") and "- Bring: umbrella." in wait
    assert "Rain is likely" in wait and "uncertain" not in wait
    assert decision_text({**ADVICE_WAIT, "leave_now": True}).startswith("**Leave now.**")
    cancelled = decision_text(
        {
            "leave_now": False,
            "suggested_leave_time": None,
            "reasons": ["service cancelled"],
            "bring": [],
            "confidence": 0.4,
        }
    )
    assert (
        cancelled.startswith("**Don't head out yet.**")
        and "uncertain (confidence 40%)" in cancelled
    )
    assert (
        len(decision_text({**ADVICE_WAIT, "reasons": ["a", "b", "c", "d", "e"]}).splitlines()) <= 6
    )


async def test_the_answer_leads_with_the_rule_based_decision_even_if_the_model_contradicts_it(
    tmp_path: Path,
) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response(
            "multi_agent",
            [
                step("s1", "transit", "Plan a trip from A to Dee Station leaving now"),
                step("s2", "weather", "Should I leave now or wait? Advise on the trip", ["s1"]),
            ],
        ),
    )
    llm.script(
        "transit",
        tool_use_response([("plan_trip", {"origin": "A", "destination": "DSTN"})]),
        text_response("Bus 100 then rail. Since it looks like rain, leaving now is advisable."),
    )
    llm.script("weather", tool_use_response([("commute_advice", {})]), text_response("ok"))
    wet = {i: {"rain_prob_pct": 80.0, "rain_mm": 2.0, "weather_code": 63} for i in range(7, 10)}
    state.weather.client.fc = __import__("tests.weather_fixtures", fromlist=["forecast"]).forecast(
        wet
    )  # type: ignore[attr-defined]
    llm.script(
        "synth", text_response("Leave now for Dee Station; the bus is at 08:00.")
    )  # the model flips it
    f = final_of(await collect(state, "leave now for Dee Station or wait? looks like rain"))
    assert f.text.startswith("**Wait: leave around ")  # the rules decide, and it is FIRST
    assert "umbrella" in f.text.splitlines()[1:6].__str__()
    assert f.text.index("**Wait") < f.text.index(
        "Leave now for Dee Station"
    )  # model text follows, never leads
    payload = synth_input(llm)
    assert payload["decision_shown_to_user_first"][0].startswith("**Wait")
    # and the transit agent's improvised rain comment never reached the synthesiser
    assert not any("agent_summary" in r for r in payload["results"][:2] if r["data"])
    assert "leaving now is advisable" not in json.dumps(payload)


async def test_prose_is_only_forwarded_for_steps_with_no_structured_results(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router",
        plan_response(
            "multi_agent",
            [
                step("s1", "weather", "weather now please"),
                step("s2", "transit", "next departures from A"),
            ],
        ),
    )
    llm.script("weather", text_response("No tools needed: it is dry."))  # no tool calls -> no data
    llm.script(
        "transit",
        tool_use_response([("next_departures", {"stop_id": "A"})]),
        text_response("INVENTED PROSE"),
    )
    llm.script("synth", text_response("ok"))
    await collect(state, "weather and bus")
    results = {r["agent"]: r for r in synth_input(llm)["results"]}
    assert results["weather"]["agent_summary"] == "No tools needed: it is dry."
    assert "agent_summary" not in results["transit"] and results["transit"]["data"]


async def test_no_decision_block_when_advice_failed(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "router", plan_response("weather", [step("s1", "weather", "Should I leave now or wait?")])
    )
    llm.script(
        "weather", tool_use_response([("commute_advice", {})]), text_response("No trip to assess.")
    )
    llm.script("synth", text_response("I need a planned trip first."))
    f = final_of(await collect(state, "leave now?"))  # commute_advice errors: no planned journey
    assert (
        "**Leave now" not in f.text
        and "**Wait" not in f.text
        and "decision_shown_to_user_first" not in synth_input(llm)
    )


def test_drop_decision_repeats_removes_only_restated_lines() -> None:
    from app.orchestrator.synthesiser import decision_text, drop_decision_repeats

    d = decision_text(
        {"leave_now": False, "suggested_leave_time": "09:00",
         "reasons": ["Rain is likely while you are outside: 80% chance."], "bring": ["umbrella"]}
    )  # fmt: skip
    model = (
        "Wait: leave around 09:00.  \n"
        "- Rain is likely while you are outside: 80% chance.  \n"
        "- Bring: umbrella.\n"
        "Trip departs 07:46 from North Lakes station, arrives 08:43."
    )
    assert (
        drop_decision_repeats(model, [d])
        == "Trip departs 07:46 from North Lakes station, arrives 08:43."
    )
    # unrelated text and an empty decision list are untouched
    assert drop_decision_repeats("Bus 687 at 07:49.", [d]) == "Bus 687 at 07:49."
    assert drop_decision_repeats(model, []) == model.strip()


# --- direct agent mode (the user picks the specialist; the router is skipped) --------------


async def test_direct_agent_mode_skips_the_router_and_still_synthesises(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script(
        "transit",
        tool_use_response([("next_departures", {"stop_id": "A", "limit": 1})]),
        text_response("Next bus 08:05."),
    )
    llm.script("synth", text_response("Next bus at 08:05."))
    events = [e async for e in state.orchestrator.run("s", "next bus from A?", "transit")]
    assert types(events) == ["plan", "step_started", "step_result", "final"]
    assert not llm.requests["router"]  # the router was never called
    plan = events[0]
    assert isinstance(plan, PlanEvent) and plan.plan.intent == "transit"
    assert [(s.agent, s.task) for s in plan.plan.steps] == [("transit", "next bus from A?")]
    assert final_of(events).status == "ok"


async def test_direct_mode_cannot_bypass_the_approval_gate(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    before = state.calendar_writer.path.read_bytes()
    llm.script(
        "calendar",
        tool_use_response([("propose_delete_event", {"event_id": "evt-sync"})]),
        text_response("Proposed."),
    )
    llm.script("synth", text_response("Waiting for approval."))
    events = [e async for e in state.orchestrator.run("s", "delete team sync", "calendar")]
    assert any(isinstance(e, ApprovalRequired) for e in events)
    assert (
        state.calendar_writer.path.read_bytes() == before
    )  # nothing changed until a human approves
    assert [a.status for a in state.gate.list_actions()] == [ActionStatus.PENDING]


async def test_direct_mode_to_an_unavailable_agent_fails_politely(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    state.orchestrator.available.discard("weather")
    events = [e async for e in state.orchestrator.run("s", "will it rain?", "weather")]
    f = final_of(events)
    assert f.status == "failed" and "not available" in f.text and not llm.requests["weather"]
