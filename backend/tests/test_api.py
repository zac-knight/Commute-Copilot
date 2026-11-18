from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.approvals import ActionStatus
from app.core.testing import text_response, tool_use_response
from app.main import create_app
from tests.orchestrator_fixtures import RoleLLM, build_test_state, plan_response, step


@pytest.fixture
def api(tmp_path: Path) -> Any:
    state, llm = build_test_state(tmp_path)
    return TestClient(create_app(state)), state, llm


def sse(
    client: TestClient, message: str = "hi", session: str = "s1"
) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    with client.stream("POST", "/chat", json={"session_id": session, "message": message}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        name = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                out.append((name, json.loads(line[6:])))
    return out


def propose(state: Any, title: str = "Coffee") -> Any:
    return state.gate.create(
        "calendar.create_event",
        {"title": title, "start": "2026-11-09T15:00:00+10:00", "end": "2026-11-09T16:00:00+10:00"},
    )


# --- /chat --------------------------------------------------------------------------------


def test_chat_streams_server_sent_events(api: Any) -> None:
    client, _, llm = api
    llm.script(
        "router", plan_response("transit", [step("s1", "transit", "next departures from A")])
    )
    llm.script(
        "transit", tool_use_response([("next_departures", {"stop_id": "A"})]), text_response("ok")
    )
    llm.script("synth", text_response("Bus at 08:05."))
    events = sse(client, "next bus from A?")
    assert [n for n, _ in events] == ["plan", "step_started", "step_result", "final"]
    plan, started, result, final = (d for _, d in events)
    assert (
        plan["plan"]["steps"][0]["agent"] == "transit" and plan["request_id"] == final["request_id"]
    )
    assert started["agent"] == "transit" and result["status"] == "ok"
    assert final["status"] == "ok" and final["text"] == "Bus at 08:05."


def test_chat_stream_carries_the_approval_card(api: Any) -> None:
    client, _state, llm = api
    llm.script(
        "router",
        plan_response("calendar", [step("s1", "calendar", "Propose adding Coffee today 15:00")]),
    )
    llm.script(
        "calendar",
        tool_use_response([("propose_create_event", {"title": "Coffee", "start": "today 15:00"})]),
        text_response("Proposed."),
    )
    llm.script("synth", text_response("Set up."))
    events = sse(client, "add coffee")
    card = next(d for n, d in events if n == "approval_required")["action"]
    assert (
        card["status"] == "pending" and len(card["payload_hash"]) == 64 and "Coffee" in card["diff"]
    )
    assert next(d for n, d in events if n == "final")["pending_approvals"] == [card["id"]]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"session_id": "s1", "message": ""}, "message"),
        ({"session_id": "s1", "message": "x" * 2001}, "message"),
        ({"session_id": "", "message": "hi"}, "session_id"),
        ({"session_id": "bad id!", "message": "hi"}, "session_id"),
        ({"session_id": "s" * 65, "message": "hi"}, "session_id"),
        ({"message": "hi"}, "session_id"),
        ({"session_id": "s1"}, "message"),
    ],
)
def test_chat_input_is_validated_with_typed_errors(
    api: Any, body: dict[str, Any], field: str
) -> None:
    client, _, _ = api
    r = client.post("/chat", json=body)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["type"] == "validation_error" and field in [d["field"] for d in err["details"]]


# --- /approvals ---------------------------------------------------------------------------


def test_list_filter_and_validation(api: Any) -> None:
    client, state, _ = api
    a, b = propose(state, "A"), propose(state, "B")
    state.gate.reject(b.id, "no")
    assert [x["id"] for x in client.get("/approvals", params={"status": "pending"}).json()] == [
        a.id
    ]
    assert {x["id"] for x in client.get("/approvals").json()} == {a.id, b.id}
    assert [
        x["reject_reason"] for x in client.get("/approvals", params={"status": "rejected"}).json()
    ] == ["no"]
    assert client.get("/approvals", params={"status": "bogus"}).status_code == 422
    assert client.get("/approvals", params={"limit": 0}).status_code == 422


def test_approve_executes_exactly_what_was_shown(api: Any) -> None:
    client, state, _ = api
    a = propose(state)
    assert state.calendar_writer.get(f"evt-{a.id}") is None
    r = client.post(f"/approvals/{a.id}/approve", json={"payload_hash": a.payload_hash})
    assert r.status_code == 200
    action = r.json()["action"]
    assert action["status"] == "executed" and action["result"]["event_id"] == f"evt-{a.id}"
    assert state.calendar_writer.get(f"evt-{a.id}").title == "Coffee"  # type: ignore[union-attr]
    again = client.post(f"/approvals/{a.id}/approve", json={"payload_hash": a.payload_hash})
    assert (
        again.status_code == 200 and again.json()["action"]["status"] == "executed"
    )  # double click
    assert len([e for e in state.calendar_writer.events() if e.title == "Coffee"]) == 1


def test_approving_with_the_wrong_hash_is_refused_and_changes_nothing(api: Any) -> None:
    client, state, _ = api
    a = propose(state)
    r = client.post(f"/approvals/{a.id}/approve", json={"payload_hash": "0" * 64})
    assert r.status_code == 409 and r.json()["error"]["type"] == "payload_changed"
    assert state.gate.get(a.id).status is ActionStatus.PENDING
    assert state.calendar_writer.get(f"evt-{a.id}") is None
    malformed = client.post(f"/approvals/{a.id}/approve", json={"payload_hash": "xyz"})
    assert malformed.status_code == 422


def test_unknown_and_wrong_state_actions_have_typed_errors(api: Any) -> None:
    client, state, _ = api
    r = client.post("/approvals/nope/approve", json={"payload_hash": "0" * 64})
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"
    a = propose(state)
    client.post(f"/approvals/{a.id}/reject", json={"reason": "nah"})
    r2 = client.post(f"/approvals/{a.id}/approve", json={"payload_hash": a.payload_hash})
    assert r2.status_code == 409 and r2.json()["error"]["type"] == "invalid_state"
    e = client.post(f"/approvals/{a.id}/edit", json={"payload": {"title": "x"}})
    assert e.status_code == 409


def test_reject_records_the_reason_and_changes_nothing(api: Any) -> None:
    client, state, _ = api
    a = propose(state)
    r = client.post(f"/approvals/{a.id}/reject", json={"reason": "wrong day"})
    assert r.status_code == 200 and r.json()["action"]["reject_reason"] == "wrong day"
    assert state.calendar_writer.get(f"evt-{a.id}") is None
    assert client.post(f"/approvals/{a.id}/reject", json={"reason": "x" * 501}).status_code == 422


def test_edit_changes_the_hash_and_the_old_card_can_no_longer_be_approved(api: Any) -> None:
    client, state, _ = api
    a = propose(state)
    new = {**a.payload, "title": "Coffee with Sam"}
    r = client.post(f"/approvals/{a.id}/edit", json={"payload": new})
    assert r.status_code == 200
    edited = r.json()["action"]
    assert (
        edited["version"] == 2
        and edited["payload_hash"] != a.payload_hash
        and "Sam" in edited["diff"]
    )
    stale = client.post(f"/approvals/{a.id}/approve", json={"payload_hash": a.payload_hash})
    assert stale.status_code == 409 and stale.json()["error"]["type"] == "payload_changed"
    ok = client.post(f"/approvals/{a.id}/approve", json={"payload_hash": edited["payload_hash"]})
    assert ok.json()["action"]["status"] == "executed"
    assert state.calendar_writer.get(f"evt-{a.id}").title == "Coffee with Sam"  # type: ignore[union-attr]


def test_edit_with_an_invalid_payload_is_a_validation_error(api: Any) -> None:
    client, state, _ = api
    a = propose(state)
    r = client.post(f"/approvals/{a.id}/edit", json={"payload": {**a.payload, "title": "  "}})
    assert r.status_code == 422 and "title" in r.json()["error"]["message"]
    assert client.post(f"/approvals/{a.id}/edit", json={"payload": "nope"}).status_code == 422
    assert state.gate.get(a.id).version == 1


def test_the_pending_cap_comes_from_settings(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path, max_pending_actions=1)
    propose(state, "one")
    with pytest.raises(Exception, match="already waiting"):
        propose(state, "two")


# --- /traces ------------------------------------------------------------------------------


def test_trace_list_and_span_tree(api: Any) -> None:
    client, _, llm = api
    llm.script("router", plan_response("weather", [step("s1", "weather", "weather now please")]))
    llm.script("weather", text_response("Dry."))
    llm.script("synth", text_response("It's dry."))
    rid = next(d["request_id"] for n, d in sse(client, "weather?") if n == "final")
    listing = client.get("/traces", params={"limit": 5}).json()
    assert (
        listing[0]["request_id"] == rid
        and listing[0]["tokens_in"] > 0
        and listing[0]["span_count"] >= 5
    )
    detail = client.get(f"/traces/{rid}").json()
    assert detail["summary"]["request_id"] == rid
    (root,) = detail["tree"]
    assert root["name"] == "orchestrator" and root["kind"] == "orchestrator"
    names = {c["name"] for c in root["children"]}
    assert {"router", "step:s1", "synthesiser"} <= names
    step_node = next(c for c in root["children"] if c["name"] == "step:s1")
    assert (
        step_node["children"][0]["name"] == "weather"
        and step_node["children"][0]["kind"] == "agent"
    )
    assert root["input_preview"] == "weather?" and root["attributes"]["status"] == "ok"
    missing = client.get("/traces/doesnotexist")
    assert missing.status_code == 404 and missing.json()["error"]["type"] == "not_found"
    assert client.get("/traces", params={"limit": 0}).status_code == 422


# --- /health, /eval, errors, lifespan -----------------------------------------------------


def test_health_reports_models_agents_feeds_and_pending(api: Any) -> None:
    client, state, _ = api
    propose(state)
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["agents_available"] == ["calendar", "transit", "weather"]
    assert h["llm"]["models"]["router"]["model"] and "ollama" in h["llm"]["backend"]
    assert h["transit"]["loaded"] is True and h["transit"]["feed_start"] == "20261102"
    assert h["transit"]["realtime_age_s"] == {"trips": 12.0}
    assert h["approvals_pending"] == 1


def test_health_without_transit_data(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path, with_transit=False)
    h = TestClient(create_app(state)).get("/health").json()
    assert h["transit"] == {"loaded": False, "last_refresh_check": None}
    assert h["agents_available"] == ["calendar", "weather"]


def test_eval_endpoint_serves_the_latest_summary(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    client, _, _ = api
    monkeypatch.setattr(main_mod, "latest_summary", lambda: {"available": True, "router": {}})
    assert client.get("/eval/latest").json() == {"available": True, "router": {}}


def test_unexpected_errors_are_typed_and_do_not_leak_details(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, state, _ = api

    def explode(*_: Any, **__: Any) -> Any:
        raise RuntimeError("secret internal path /Users/someone/x")

    monkeypatch.setattr(state.gate, "list_actions", explode)
    c = TestClient(create_app(state), raise_server_exceptions=False)
    r = c.get("/approvals")
    assert r.status_code == 500 and r.json() == {
        "error": {"type": "internal_error", "message": "unexpected error"}
    }


def test_not_ready_before_startup(tmp_path: Path) -> None:
    r = TestClient(create_app(settings=Settings(db_path=tmp_path / "x.sqlite"))).get("/health")
    assert r.status_code == 503 and r.json()["error"]["type"] == "unavailable"


def test_lifespan_builds_the_app_from_settings_and_shuts_down_cleanly(tmp_path: Path) -> None:
    cfg = Settings(
        db_path=tmp_path / "app.sqlite",
        calendar_dir=tmp_path / "cal",
        gtfs_dir=tmp_path / "gtfs",
        profile_path=tmp_path / "p.json",
        gtfs_auto_refresh=False,
    )
    with TestClient(create_app(settings=cfg)) as c:
        h = c.get("/health").json()
        assert h["status"] == "ok" and h["transit"]["loaded"] is False  # no GTFS database yet
        assert h["agents_available"] == ["calendar", "weather"]
        assert c.get("/approvals").json() == []
    assert (tmp_path / "app.sqlite").exists()


# --- /settings ----------------------------------------------------------------------------

GOOD = {
    "home": "Alpha Street",
    "destinations": {"Dee": "Dee Station"},
    "working_hours_start": "08:30",
    "working_hours_end": "16:30",
    "buffer_minutes": 15,
}


class ModelListingLLM(RoleLLM):
    async def list_models(self) -> list[str]:
        return ["qwen3:8b", "llama3.1:8b"]  # unsorted on purpose


def test_get_settings_reports_profile_model_and_what_home_resolves_to(api: Any) -> None:
    client, state, _ = api
    s = client.get("/settings").json()
    assert s["home"] == "North Lakes station" and s["buffer_minutes"] == 10
    assert s["working_hours_start"] == "09:00" and s["router_model"] == state.router_model()
    assert s["available_models"] == []  # the fake LLM cannot list models
    assert s["home_resolves_to"] is None  # 'North Lakes station' is not in the tiny test feed


def test_put_settings_updates_every_service_immediately_and_persists(
    api: Any, tmp_path: Path
) -> None:
    client, state, _ = api
    r = client.put("/settings", json=GOOD)
    assert r.status_code == 200
    out = r.json()
    assert out["home"] == "Alpha Street" and out["home_resolves_to"] == "Alpha Street"
    assert out["destinations"] == {"dee": "Dee Station"}  # keys are normalised
    # the SAME profile object is shared, so the services see it without a restart
    assert state.transit.profile.home == "Alpha Street"  # type: ignore[union-attr]
    assert state.calendar_reader.profile.working_hours_start == "08:30"
    assert state.calendar_reader.profile.buffer_minutes == 15
    saved = json.loads((tmp_path / "profile.json").read_text())
    assert saved["home"] == "Alpha Street" and saved["buffer_minutes"] == 15
    assert not list(tmp_path.glob("*.tmp"))  # atomic write left nothing behind


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"working_hours_start": "9am"}, "working_hours_start"),
        ({"working_hours_end": "25:00"}, "working_hours_end"),
        ({"buffer_minutes": -1}, "buffer_minutes"),
        ({"buffer_minutes": 121}, "buffer_minutes"),
        ({"home": ""}, "home"),
        ({"home": "x" * 101}, "home"),
        ({"destinations": {str(i): "p" for i in range(21)}}, "destinations"),
    ],
)
def test_put_settings_validates_input(api: Any, patch: dict[str, Any], field: str) -> None:
    client, _, _ = api
    r = client.put("/settings", json={**GOOD, **patch})
    assert r.status_code == 422 and r.json()["error"]["type"] == "validation_error"
    assert field in [d["field"] for d in r.json()["error"]["details"]]


def test_put_settings_business_rules(api: Any) -> None:
    client, state, _ = api
    r = client.put(
        "/settings", json={**GOOD, "working_hours_start": "17:00", "working_hours_end": "09:00"}
    )
    assert r.status_code == 422 and "end after they start" in r.json()["error"]["message"]
    r = client.put("/settings", json={**GOOD, "home": "Atlantis"})  # not a stop in the timetable
    assert r.status_code == 422 and "no stop found" in r.json()["error"]["message"]
    r = client.put("/settings", json={**GOOD, "destinations": {" ": "x"}})
    assert r.status_code == 422
    assert (
        state.profile.home == "North Lakes station"
    )  # nothing was applied by the rejected requests


def test_router_model_choice_is_checked_applied_and_remembered(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path, llm=ModelListingLLM())
    client = TestClient(create_app(state))
    assert client.get("/settings").json()["available_models"] == ["llama3.1:8b", "qwen3:8b"]
    bad = client.put("/settings", json={**GOOD, "router_model": "gpt-9"})
    assert bad.status_code == 422 and "not installed" in bad.json()["error"]["message"]
    ok = client.put("/settings", json={**GOOD, "router_model": "llama3.1:8b"})
    assert ok.status_code == 200 and ok.json()["router_model"] == "llama3.1:8b"
    assert state.orchestrator.router.role.model == "llama3.1:8b"
    assert json.loads((tmp_path / "ui_settings.json").read_text()) == {
        "router_model": "llama3.1:8b"
    }
    # a restart (same settings) picks the saved choice up
    from app.orchestrator.wiring import build_state

    again = build_state(state.settings, llm=ModelListingLLM(), weather=state.weather)
    assert again.router_model() == "llama3.1:8b"


def test_omitting_router_model_keeps_the_current_one(api: Any) -> None:
    client, state, _ = api
    before = state.router_model()
    client.put("/settings", json=GOOD)
    assert state.router_model() == before


def test_agents_endpoint_describes_tools_permissions_and_usage(api: Any) -> None:
    client, _, _ = api
    cards = {c["name"]: c for c in client.get("/agents").json()}
    assert set(cards) == {"transit", "weather", "calendar"}
    cal = {t["name"]: t["access"] for t in cards["calendar"]["tools"]}
    assert cal["list_events"] == "read" and cal["propose_create_event"] == "propose"
    assert all(a != "execute" for c in cards.values() for a in (t["access"] for t in c["tools"]))
    assert cards["calendar"]["source"] and cards["transit"]["calls_24h"] == 0


def test_chat_accepts_an_agent_and_rejects_an_unknown_one(api: Any) -> None:
    client, _, _ = api
    bad = client.post("/chat", json={"session_id": "s", "message": "hi there", "agent": "banking"})
    assert bad.status_code == 422


def test_chat_forwards_detail_and_reasoning_to_the_orchestrator(api: Any) -> None:
    client, state, _ = api
    seen: dict[str, Any] = {}

    async def fake_run(
        session_id: str,
        message: str,
        agent: Any = None,
        reasoning: bool = False,
        detail: bool = False,
    ) -> Any:
        seen.update(agent=agent, reasoning=reasoning, detail=detail)
        return
        yield  # pragma: no cover - makes this an async generator

    state.orchestrator.run = fake_run
    client.post(
        "/chat",
        json={
            "session_id": "s",
            "message": "hello there",
            "detail": True,
            "reasoning": True,
            "agent": "weather",
        },
    )
    assert seen == {"agent": "weather", "reasoning": True, "detail": True}
    client.post("/chat", json={"session_id": "s", "message": "hello there"})
    assert seen == {"agent": None, "reasoning": False, "detail": False}
