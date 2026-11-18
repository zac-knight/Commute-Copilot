from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.approvals import ActionStatus
from app.demo.frozen import FIXTURES, FROZEN_NOW, FixtureWeatherClient, frozen_clock
from app.demo.replay import (
    DemoRunner,
    Transcript,
    load_transcripts,
    match_transcript,
)
from app.demo.state import NoLLM
from app.main import create_app
from app.orchestrator.events import ApprovalRequired, Final, PlanEvent
from tests.orchestrator_fixtures import build_test_state


def make_transcript(name: str = "dentist", prompts: list[str] | None = None) -> Transcript:
    action = {
        "id": "old-id",
        "type": "calendar.create_event",
        "diff": "x",
        "summary": "Create 'Dentist'",
        "payload_hash": "0" * 64,
        "status": "pending",
        "version": 1,
        "created_at": "2026-11-18T07:45:00+10:00",
        "expires_at": "2026-11-18T08:15:00+10:00",
        "decided_at": None,
        "executed_at": None,
        "reject_reason": None,
        "result": None,
        "error": None,
        "payload": {
            "title": "Dentist",
            "start": "2026-11-19T14:00:00+10:00",
            "end": "2026-11-19T15:00:00+10:00",
            "description": "",
            "location": "Chermside",
        },
    }
    plan = {
        "intent": "calendar",
        "steps": [
            {"id": "s1", "agent": "calendar", "task": "Propose adding Dentist", "depends_on": []}
        ],
        "clarification_question": None,
        "repairs": [],
    }
    events = [
        {"t": 0.0, "event": {"type": "plan", "request_id": "OLD", "plan": plan}},
        {
            "t": 0.5,
            "event": {
                "type": "step_started",
                "id": "s1",
                "agent": "calendar",
                "task": "Propose adding Dentist",
            },
        },
        {
            "t": 2.0,
            "event": {
                "type": "step_result",
                "id": "s1",
                "agent": "calendar",
                "status": "ok",
                "text": "ok",
                "error": None,
                "tool_calls": ["propose_create_event"],
                "duration_ms": 1500,
            },
        },
        {"t": 2.1, "event": {"type": "approval_required", "action": action}},
        {
            "t": 3.0,
            "event": {
                "type": "final",
                "request_id": "OLD",
                "status": "ok",
                "text": "Proposed.",
                "pending_approvals": ["old-id"],
                "tokens_in": 100,
                "tokens_out": 10,
                "duration_ms": 3000,
            },
        },
    ]
    spans = [
        {
            "request_id": "OLD",
            "span_id": "a",
            "parent_id": None,
            "kind": "orchestrator",
            "name": "orchestrator",
            "start": 0.0,
            "end": 3.0,
            "status": "ok",
            "input_preview": "q",
            "output_preview": "a",
            "tokens_in": 0,
            "tokens_out": 0,
            "model": None,
            "cost_estimate": None,
            "error": None,
            "attributes": {},
        },
        {
            "request_id": "OLD",
            "span_id": "b",
            "parent_id": "a",
            "kind": "llm",
            "name": "router.llm#1",
            "start": 0.1,
            "end": 0.9,
            "status": "ok",
            "input_preview": "",
            "output_preview": "",
            "tokens_in": 100,
            "tokens_out": 10,
            "model": "qwen3:8b",
            "cost_estimate": None,
            "error": None,
            "attributes": {},
        },
    ]
    return Transcript(name, prompts or ["Add 'Dentist' Thursday 2pm at Chermside"], events, spans)


# --- matching -----------------------------------------------------------------------------


def transcripts() -> list[Transcript]:
    return [
        make_transcript(
            "dentist",
            ["Add 'Dentist' Thursday 2pm at Chermside and warn me if I can't get there on time."],
        ),
        make_transcript(
            "buses",
            [
                "Next 3 buses from home to Chermside",
                "When are the next buses from North Lakes to Chermside?",
            ],
        ),
        make_transcript("rain", ["Should I leave now for Chermside or wait? It looks like rain."]),
    ]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Next 3 buses from home to Chermside", "buses"),  # exact
        ("next 3 buses from home to chermside!!", "buses"),  # case and punctuation
        ("what are the next buses from north lakes to chermside", "buses"),  # paraphrase
        ("Add Dentist Thursday 2pm at Chermside, warn me if I can't make it in time", "dentist"),
        ("leave now for chermside or wait for the rain", "rain"),
        ("What is the meaning of life", None),
        ("", None),
        ("the a an to of", None),  # only stop words
    ],
)
def test_matching(message: str, expected: str | None) -> None:
    t = match_transcript(message, transcripts())
    assert (t.name if t else None) == expected


# --- replay -------------------------------------------------------------------------------


async def run_replay(tmp_path: Path, message: str) -> tuple[list[Any], Any]:
    state, _ = build_test_state(tmp_path)
    runner = DemoRunner(transcripts(), state.gate, state.store, state.orchestrator.router, speed=0)
    return [e async for e in runner.run("s", message)], state


async def test_replay_streams_the_recorded_events_with_a_fresh_request_id(tmp_path: Path) -> None:
    events, _ = await run_replay(tmp_path, "Add 'Dentist' Thursday 2pm at Chermside")
    assert [e.type for e in events] == [
        "plan",
        "step_started",
        "step_result",
        "approval_required",
        "final",
    ]
    plan, final = events[0], events[-1]
    assert isinstance(plan, PlanEvent) and isinstance(final, Final)
    assert plan.request_id == final.request_id and plan.request_id not in ("OLD", "")
    assert final.tokens_in == 100  # the RECORDED numbers are replayed, not invented


async def test_a_recorded_approval_card_becomes_a_real_pending_action(tmp_path: Path) -> None:
    events, state = await run_replay(tmp_path, "Add 'Dentist' Thursday 2pm at Chermside")
    card = next(e for e in events if isinstance(e, ApprovalRequired)).action
    assert card.id != "old-id" and card.status is ActionStatus.PENDING
    assert card.payload_hash != "0" * 64  # recomputed by the real gate
    assert next(e for e in events if isinstance(e, Final)).pending_approvals == [card.id]
    # approving it really executes against the (fake) calendar
    state.gate.approve(card.id, card.payload_hash)
    done = state.gate.execute_approved_action(card.id)
    assert done.status is ActionStatus.EXECUTED
    assert state.calendar_writer.get(f"evt-{card.id}").title == "Dentist"  # type: ignore[union-attr]


async def test_replaying_twice_gives_two_independent_cards(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path)
    runner = DemoRunner(transcripts(), state.gate, state.store, state.orchestrator.router, speed=0)
    ids = []
    for _ in range(2):
        async for e in runner.run("s", "Add 'Dentist' Thursday 2pm at Chermside"):
            if isinstance(e, ApprovalRequired):
                ids.append(e.action.id)
    assert len(set(ids)) == 2 and len(state.gate.list_actions(ActionStatus.PENDING)) == 2


async def test_the_recorded_trace_is_inserted_under_the_new_request_id(tmp_path: Path) -> None:
    events, state = await run_replay(tmp_path, "Add 'Dentist' Thursday 2pm at Chermside")
    rid = next(e for e in events if isinstance(e, Final)).request_id
    spans = state.store.get_spans(rid)
    by = {s.name: s for s in spans}
    assert by["router.llm#1"].parent_id == by["orchestrator"].span_id  # the tree structure survives
    assert by["router.llm#1"].tokens_in == 100 and by["router.llm#1"].model == "qwen3:8b"
    assert (by["orchestrator"].end or 0) - by["orchestrator"].start == pytest.approx(
        3.0
    )  # recorded latency
    assert state.store.request_summary(rid) is not None


async def test_unmatched_questions_get_a_friendly_menu_not_a_made_up_answer(tmp_path: Path) -> None:
    events, state = await run_replay(tmp_path, "What is the meaning of life")
    (final,) = events
    assert isinstance(final, Final) and final.status == "clarify"
    assert "demo mode" in final.text.lower() and "Next 3 buses" in final.text
    assert state.gate.list_actions() == []


async def test_a_full_gate_skips_the_card_instead_of_crashing(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path, max_pending_actions=0)
    runner = DemoRunner(transcripts(), state.gate, state.store, state.orchestrator.router, speed=0)
    events = [e async for e in runner.run("s", "Add 'Dentist' Thursday 2pm at Chermside")]
    assert not any(isinstance(e, ApprovalRequired) for e in events)
    assert isinstance(events[-1], Final) and events[-1].pending_approvals == []


async def test_replay_delays_are_capped_and_scaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.demo.replay as replay

    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(replay.asyncio, "sleep", fake_sleep)
    state, _ = build_test_state(tmp_path)
    runner = DemoRunner(
        transcripts(), state.gate, state.store, state.orchestrator.router, speed=1.0, max_gap_s=1.2
    )
    _ = [e async for e in runner.run("s", "Add 'Dentist' Thursday 2pm at Chermside")]
    assert slept and max(slept) <= 1.2 + 1e-9  # the 1.5 s recorded gap is capped at 1.2 s
    slept.clear()
    fast = DemoRunner(transcripts(), state.gate, state.store, state.orchestrator.router, speed=0)
    _ = [e async for e in fast.run("s", "Add 'Dentist' Thursday 2pm at Chermside")]
    assert slept == []


# --- demo state and API -------------------------------------------------------------------


def demo_settings(tmp_path: Path) -> Settings:
    return Settings(
        demo=True,
        demo_speed=0,
        db_path=tmp_path / "REAL.sqlite",
        calendar_dir=tmp_path / "REALCAL",
        profile_path=tmp_path / "REALPROFILE.json",
        ui_settings_path=tmp_path / "REALUI.json",
        gtfs_auto_refresh=False,
    )


def test_demo_mode_never_touches_the_real_profile_calendar_or_database(tmp_path: Path) -> None:
    with TestClient(create_app(settings=demo_settings(tmp_path))) as c:
        assert c.get("/health").json()["demo"] is True
        c.put(
            "/settings",
            json={
                "home": "North Lakes station",
                "destinations": {},
                "working_hours_start": "08:00",
                "working_hours_end": "16:00",
                "buffer_minutes": 30,
            },
        )
        c.get("/approvals")
    assert not any(
        p.name.startswith("REAL") for p in tmp_path.iterdir()
    )  # all writes went to a temp dir


def test_demo_health_lists_examples_and_agents_and_the_model_is_never_called(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(settings=demo_settings(tmp_path))) as c:
        h = c.get("/health").json()
        assert h["agents_available"] == ["calendar", "transit", "weather"]
        assert h["llm"]["reachable"] is None or isinstance(h["llm"]["reachable"], bool)
        if load_transcripts():
            assert h["demo_examples"] and all(isinstance(x, str) for x in h["demo_examples"])


async def test_nollm_fails_loudly() -> None:
    from app.core.llm import LLMError

    with pytest.raises(LLMError, match="demo mode has no model"):
        await NoLLM().complete()


def test_the_demo_chat_endpoint_streams_sse(tmp_path: Path) -> None:
    with TestClient(create_app(settings=demo_settings(tmp_path))) as c:
        r = c.post("/chat", json={"session_id": "s", "message": "What is the meaning of life"})
        assert r.status_code == 200 and "event: final" in r.text


# --- the frozen fixtures and the SHIPPED recordings -----------------------------------------


async def test_frozen_weather_is_deterministic_and_has_the_synthetic_shower() -> None:
    res = await FixtureWeatherClient().forecast(-27.2, 153.0)
    assert res.data is not None and res.age_s == 0
    by_hour = {h.time.strftime("%d %H"): h for h in res.data.hours}
    assert by_hour["18 08"].rain_prob_pct == 75 and by_hour["18 09"].rain_prob_pct == 80
    assert frozen_clock() == FROZEN_NOW and FROZEN_NOW.isoformat() == "2026-11-18T07:45:00+10:00"
    meta = json.loads((FIXTURES / "meta.json").read_text())
    assert "SYNTHETIC" in meta["weather"]  # the edit is disclosed, not hidden


def test_shipped_recordings_are_valid_and_honest() -> None:
    recorded = load_transcripts()
    if not recorded:
        pytest.skip("no recordings yet (run scripts/record_demo.py)")
    names = {t.name for t in recorded}
    assert {"dentist", "next_buses", "leave_now_rain"} <= names
    from app.agents.calendar.proposals import prepare_create
    from app.demo.replay import _EVENT

    for t in recorded:
        assert t.prompts and t.spans
        events = [_EVENT.validate_python(r["event"]) for r in t.events]  # every event parses
        assert isinstance(events[-1], Final), t.name
        times = [r["t"] for r in t.events]
        assert times == sorted(times)
        for e in events:
            if isinstance(e, ApprovalRequired):
                prepare_create(e.action.payload)  # the proposal replays into a valid pending action
        text = events[-1].text if isinstance(events[-1], Final) else ""
        assert "api_key" not in text.lower() and "/Users/" not in json.dumps(
            t.spans
        )  # nothing personal
    for (
        t
    ) in recorded:  # no answer may repeat itself (the 8B model once restated the decision block)
        text = next(r["event"]["text"] for r in t.events if r["event"]["type"] == "final")
        lines = [ln.strip(" -*").lower() for ln in text.splitlines() if len(ln.strip()) > 12]
        assert len(lines) == len(set(lines)), f"{t.name} repeats a line:\n{text}"
    wait = next(t for t in recorded if t.name == "leave_now_rain")
    final = next(r["event"] for r in wait.events if r["event"]["type"] == "final")
    assert final["text"].startswith("**Wait"), (
        "the recorded answer must agree with the rule-based advice"
    )
