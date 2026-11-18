from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.approvals import ActionStatus, TooManyPendingError
from app.core.testing import ScriptedLLM, text_response, tool_use_response
from app.core.tools import Access
from app.core.tracing import Tracer, TraceStore
from tests.calendar_fixtures import AGENT_NAME, Env, at, default_events, make_env
from tests.test_calendar_sources import ICS


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path, default_events())


async def call(env: Env, name: str, args: dict[str, Any]) -> dict[str, Any]:
    out = await env.tools.call(name, args)
    return json.loads(out.content)


def titles(env: Env) -> list[str]:
    return [e.title for e in env.writer.events()]


def approve_and_run(env: Env, action_id: str) -> Any:
    a = env.gate.get(action_id)
    env.gate.approve(action_id, a.payload_hash)
    return env.gate.execute_approved_action(action_id)


# --- registration -------------------------------------------------------------------------


def test_calendar_agent_tool_set_and_access_levels(env: Env) -> None:
    perms = dict(env.registry.permissions()[AGENT_NAME])
    assert perms == {
        "check_conflicts": Access.READ,
        "find_free_slots": Access.READ,
        "list_events": Access.READ,
        "propose_create_event": Access.PROPOSE,
        "propose_delete_event": Access.PROPOSE,
        "propose_update_event": Access.PROPOSE,
    }
    assert Access.EXECUTE not in perms.values()
    defs = env.tools.definitions()
    assert all(d["description"] for d in defs)
    assert all("description" in p for d in defs for p in d["input_schema"]["properties"].values())


# --- read tools ---------------------------------------------------------------------------


async def test_list_events_wraps_untrusted_text_and_reports_writability(env: Env) -> None:
    (env.import_dir / "work.ics").write_text(ICS)
    res = await call(env, "list_events", {"start": "today 00:00", "end": "today 23:59"})
    by = {e["id"]: e for e in res["events"]}
    sync = by["evt-sync"]
    assert sync["title"] == '<untrusted_data source="calendar:local">Team sync</untrusted_data>'
    assert 'source="calendar:local"' in sync["location"] and "Room 1" in sync["location"]
    assert sync["writable"] is True and sync["start"] == "10:00" and sync["end"] == "11:00"
    imported = next(e for e in res["events"] if e["source"] == "ics:work.ics")
    assert imported["writable"] is False and "calendar:ics:work.ics" in imported["title"]
    assert [e["start_iso"] for e in res["events"]] == sorted(e["start_iso"] for e in res["events"])


async def test_list_events_truncates_and_reports(tmp_path: Path) -> None:
    from tests.calendar_fixtures import ev

    many = [
        ev(
            f"E{i}",
            at(9) + __import__("datetime").timedelta(minutes=i),
            at(9, 1) + __import__("datetime").timedelta(minutes=i),
            id=f"evt-{i}",
        )
        for i in range(60)
    ]
    e = make_env(tmp_path, many)
    res = await call(e, "list_events", {"start": "today 00:00", "end": "today 23:59"})
    assert len(res["events"]) == 50 and res["total"] == 60 and "first 50 of 60" in res["note"]


async def test_free_slots_for_tomorrow_afternoon(env: Env) -> None:
    res = await call(
        env,
        "find_free_slots",
        {"start": "tomorrow 12:00", "end": "tomorrow 17:00", "duration_minutes": 60},
    )
    assert res["working_hours"] == "09:00-17:00" and res["buffer_minutes"] == 10
    assert [(s["start"][-5:], s["end"][-5:]) for s in res["slots"]] == [("12:00", "17:00")]
    assert res["slots"][0]["start"].startswith("Tue 10 Nov")  # the day is stated when not today


async def test_free_slots_respect_buffer_and_custom_hours(env: Env) -> None:
    # sync 10-11 and lunch 12-13, 10 min buffer: 09:00-09:50 fits 45 min, 11:10-11:50 does not
    res = await call(
        env,
        "find_free_slots",
        {"start": "today 09:00", "end": "today 17:00", "duration_minutes": 45},
    )
    assert [(s["start"], s["end"]) for s in res["slots"]] == [
        ("09:00", "09:50"),
        ("13:10", "17:00"),
    ]
    short = await call(
        env,
        "find_free_slots",
        {"start": "today 09:00", "end": "today 17:00", "duration_minutes": 30, "buffer_minutes": 0},
    )
    assert ("11:00", "12:00") in [(s["start"], s["end"]) for s in short["slots"]]
    custom = await call(
        env,
        "find_free_slots",
        {
            "start": "today 00:00",
            "end": "today 23:59",
            "duration_minutes": 30,
            "working_hours": "18:00-20:00",
        },
    )
    assert [(s["start"], s["end"]) for s in custom["slots"]] == [("18:00", "20:00")]
    nothing = await call(
        env,
        "find_free_slots",
        {"start": "today 10:00", "end": "today 11:00", "duration_minutes": 30},
    )
    assert nothing["slots"] == [] and "No free slot" in nothing["note"]


async def test_free_slot_tool_errors_are_actionable(env: Env) -> None:
    bad = await env.tools.call(
        "find_free_slots",
        {
            "start": "today 09:00",
            "end": "today 17:00",
            "duration_minutes": 30,
            "working_hours": "nine-five",
        },
    )
    assert bad.is_error and "HH:MM" in bad.content
    backwards = await env.tools.call(
        "find_free_slots", {"start": "today 17:00", "end": "today 09:00", "duration_minutes": 30}
    )
    assert backwards.is_error and "after start" in backwards.content
    assert (
        await env.tools.call(
            "find_free_slots", {"start": "today 09:00", "end": "today 17:00", "duration_minutes": 1}
        )
    ).error_type == "invalid_input"


async def test_check_conflicts(env: Env) -> None:
    res = await call(
        env, "check_conflicts", {"proposed_start": "today 10:30", "proposed_end": "today 11:30"}
    )
    assert res["has_conflict"] and "Team sync" in res["overlapping"][0]["title"]
    close = await call(
        env, "check_conflicts", {"proposed_start": "today 11:05", "proposed_end": "today 11:50"}
    )
    assert (
        not close["has_conflict"] and "Team sync" in close["too_close"][0]["title"]
    )  # within the buffer


# --- propose tools: nothing changes until approval ----------------------------------------


async def test_create_proposal_changes_nothing_until_approved(env: Env) -> None:
    before = env.writer.path.read_bytes()
    res = await call(env, "propose_create_event", {"title": "Coffee", "start": "today 15:00"})
    assert res["status"] == "pending" and res["type"] == "calendar.create_event"
    assert (
        "NOT DONE YET" in res["note"] and "Coffee" in res["diff"] and "15:00-16:00" in res["diff"]
    )
    assert env.writer.path.read_bytes() == before and "Coffee" not in titles(env)
    done = approve_and_run(env, res["action_id"])
    assert done.status is ActionStatus.EXECUTED and "Coffee" in titles(env)
    assert done.result and done.result["event_id"] == f"evt-{res['action_id']}"


async def test_create_with_explicit_end_default_duration_and_conflicts(env: Env) -> None:
    res = await call(
        env,
        "propose_create_event",
        {"title": "Workshop", "start": "today 10:30", "end": "today 12:30", "location": "Hall"},
    )
    assert "10:30-12:30" in res["diff"] and "Where:    Hall" in res["diff"]
    assert {c["id"] for c in res["conflicts"]} == {
        "evt-sync",
        "evt-lunch",
    }  # the user can be warned
    default = await call(env, "propose_create_event", {"title": "Call", "start": "today 16:00"})
    assert "16:00-17:00" in default["diff"] and default["conflicts"] == []


async def test_edit_before_approval_changes_what_runs(env: Env) -> None:
    res = await call(env, "propose_create_event", {"title": "Coffee", "start": "today 15:00"})
    old = env.gate.get(res["action_id"])
    edited = env.gate.edit(res["action_id"], {**old.payload, "title": "Coffee with Sam"})
    env.gate.approve(res["action_id"], edited.payload_hash)
    env.gate.execute_approved_action(res["action_id"])
    assert "Coffee with Sam" in titles(env) and "Coffee" not in titles(env)


async def test_rejection_changes_nothing_and_is_remembered(env: Env) -> None:
    before = env.writer.path.read_bytes()
    res = await call(env, "propose_create_event", {"title": "Coffee", "start": "today 15:00"})
    env.gate.reject(res["action_id"], "I'd rather not")
    assert env.writer.path.read_bytes() == before
    assert [a.reject_reason for a in env.gate.recent_rejections()] == ["I'd rather not"]


async def test_update_proposal_diff_and_execution(env: Env) -> None:
    res = await call(
        env,
        "propose_update_event",
        {
            "event_id": "evt-sync",
            "start": "today 11:00",
            "end": "today 12:00",
            "title": "Team sync (moved)",
        },
    )
    assert "UPDATE EVENT 'Team sync'" in res["diff"] and "->" in res["diff"]
    assert "Mon 09 Nov 2026 10:00  ->  Mon 09 Nov 2026 11:00" in res["diff"]
    assert "Team sync (moved)" not in titles(env)
    approve_and_run(env, res["action_id"])
    sync = env.writer.get("evt-sync")
    assert (
        sync and sync.title == "Team sync (moved)" and sync.start == at(11) and sync.end == at(12)
    )
    assert sync.location == "Room 1"  # untouched fields are preserved


async def test_update_is_refused_if_the_event_changed_after_review(env: Env) -> None:
    res = await call(env, "propose_update_event", {"event_id": "evt-sync", "title": "Renamed"})
    env.writer.update("evt-sync", {"title": "Someone else renamed it"}, None)  # changed meanwhile
    done = approve_and_run(env, res["action_id"])
    assert done.status is ActionStatus.FAILED and "changed since you reviewed" in (done.error or "")
    assert env.writer.get("evt-sync").title == "Someone else renamed it"  # type: ignore[union-attr]


async def test_delete_proposal_and_idempotent_execution(env: Env) -> None:
    res = await call(env, "propose_delete_event", {"event_id": "evt-lunch"})
    assert res["diff"].startswith("DELETE EVENT") and "Lunch" in res["diff"]
    assert "Lunch" in titles(env)
    done = approve_and_run(env, res["action_id"])
    assert done.status is ActionStatus.EXECUTED and "Lunch" not in titles(env)
    again = env.gate.execute_approved_action(res["action_id"])
    assert again == done and len(env.writer.events()) == 2


async def test_proposal_errors_are_actionable_and_create_no_cards(env: Env) -> None:
    (env.import_dir / "work.ics").write_text(ICS)
    imported = next(
        e for e in env.reader.events_between(at(0), at(0, day=1)).events if e.source != "local"
    )
    cases = [
        ("propose_delete_event", {"event_id": "evt-nope"}, "no event with id"),
        ("propose_delete_event", {"event_id": imported.id}, "read-only"),
        ("propose_update_event", {"event_id": imported.id, "title": "x"}, "imported calendar file"),
        ("propose_update_event", {"event_id": "evt-sync"}, "would not alter"),
        ("propose_update_event", {"event_id": "evt-sync", "title": "Team sync"}, "would not alter"),
        (
            "propose_update_event",
            {"event_id": "evt-sync", "end": "today 09:00"},
            "end after it starts",
        ),
        ("propose_create_event", {"title": " ", "start": "today 15:00"}, "title cannot be empty"),
        (
            "propose_create_event",
            {"title": "\u200b\u202e", "start": "today 15:00"},
            "title cannot be empty",
        ),
        ("propose_update_event", {"event_id": "evt-sync", "title": "  "}, "title cannot be empty"),
        (
            "propose_create_event",
            {"title": "x", "start": "today 15:00", "end": "today 14:00"},
            "end after",
        ),
        (
            "propose_create_event",
            {"title": "x", "start": "today 09:00", "end": "2026-12-01T09:00"},
            "7 days",
        ),
        ("propose_create_event", {"title": "x", "start": "someday"}, "cannot understand time"),
    ]
    for name, args, expected in cases:
        out = await env.tools.call(name, args)
        assert out.is_error and expected in out.content, (name, args, out.content)
    assert env.gate.list_actions() == []  # none of them created an approval card


async def test_hostile_proposal_text_is_cleaned(env: Env) -> None:
    res = await call(env, "propose_create_event", {"title": "Meet‮\x00evil​", "start": "today 15:00"})
    assert "‮" not in res["diff"] and "\x00" not in res["diff"] and "Meetevil" in res["diff"]


async def test_pending_cap_stops_a_flood_of_proposals(tmp_path: Path) -> None:
    e = make_env(tmp_path, default_events(), max_pending=2)
    for i in range(2):
        await call(e, "propose_create_event", {"title": f"E{i}", "start": f"today {13 + i}:00"})
    out = await e.tools.call("propose_create_event", {"title": "E3", "start": "today 16:00"})
    assert out.is_error and "already waiting for approval" in out.content
    assert len(e.gate.list_actions(ActionStatus.PENDING)) == 2
    with pytest.raises(TooManyPendingError):
        e.gate.proposal_sink().propose("calendar.delete_event", {"event_id": "evt-sync"})


# --- agent loop ---------------------------------------------------------------------------


async def test_agent_proposes_and_says_it_is_waiting(env: Env, store: TraceStore) -> None:
    from app.agents.calendar.prompts import PROMPT_VERSION, SYSTEM_PROMPT, task_message
    from app.core.agent import Agent

    llm = ScriptedLLM(
        [
            tool_use_response(
                [("propose_create_event", {"title": "Dentist", "start": "thursday 2pm"})]
            ),
            text_response(
                "I've proposed adding Dentist on Thursday 14:00. It's waiting for your approval."
            ),
        ]
    )
    tracer = Tracer(store)
    agent = Agent(
        name=AGENT_NAME,
        system_prompt=SYSTEM_PROMPT,
        tools=env.tools,
        model="t",
        llm=llm,
        tracer=tracer,
        prompt_version=PROMPT_VERSION,
    )
    with tracer.request():
        res = await agent.run(task_message("Add Dentist Thursday 2pm", at(7, 50)))
    assert res.ok and res.tool_calls == ["propose_create_event"]
    pending = env.gate.list_actions(ActionStatus.PENDING)
    assert len(pending) == 1 and "Thu 12 Nov 2026 14:00-15:00" in pending[0].diff
    assert titles(env).count("Dentist") == 1  # only the seeded one: the proposal changed nothing
    tool_result = json.loads(llm.requests[1]["messages"][-1]["content"][0]["content"])
    assert "NOT DONE YET" in tool_result["note"]
