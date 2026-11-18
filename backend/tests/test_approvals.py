from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.approvals import (
    ActionHandler,
    ActionStatus,
    ApprovalGate,
    HashMismatchError,
    InvalidTransitionError,
    NotFoundError,
    PendingAction,
    TooManyPendingError,
    compute_hash,
)
from app.core.timeutil import TZ

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=TZ)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Echo:
    """A test action: records every execution so we can prove 'exactly once'."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.fail_times = 0

    def prepare(self, raw: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        if not str(raw.get("title", "")).strip():
            raise ValueError("title is required")
        title = str(raw["title"]).strip()
        return {"title": title}, f"CREATE: {title}", f"Create {title}"

    def execute(self, action: PendingAction) -> dict[str, Any]:
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("disk full")
        self.runs.append(action.id)
        return {"created": action.payload["title"], "run": len(self.runs)}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def echo() -> Echo:
    return Echo()


@pytest.fixture
def gate(clock: Clock, echo: Echo) -> ApprovalGate:
    g = ApprovalGate(":memory:", clock=clock, ttl=timedelta(minutes=30), max_pending=3)
    g.register(ActionHandler("echo", echo.prepare, echo.execute))
    return g


def propose(gate: ApprovalGate, title: str = "Dentist") -> PendingAction:
    return gate.create("echo", {"title": title})


# --- creation -----------------------------------------------------------------------------


def test_create_is_pending_hashed_and_expires(gate: ApprovalGate) -> None:
    a = propose(gate, "  Dentist  ")
    assert a.status is ActionStatus.PENDING and a.payload == {"title": "Dentist"}  # normalised
    assert a.diff == "CREATE: Dentist" and a.summary == "Create Dentist" and a.version == 1
    assert a.payload_hash == compute_hash("echo", a.payload, a.diff)
    assert a.expires_at == T0 + timedelta(minutes=30) and a.created_at == T0
    assert gate.get(a.id) == a


def test_invalid_payload_and_unknown_type_create_nothing(gate: ApprovalGate) -> None:
    with pytest.raises(ValueError, match="title is required"):
        gate.create("echo", {"title": "  "})
    with pytest.raises(ValueError, match="unknown action type"):
        gate.create("launch_missiles", {})
    assert gate.list_actions() == []


def test_duplicate_handler_registration_is_refused(gate: ApprovalGate, echo: Echo) -> None:
    with pytest.raises(ValueError, match="already registered"):
        gate.register(ActionHandler("echo", echo.prepare, echo.execute))


def test_hash_is_order_independent_and_sensitive_to_every_part() -> None:
    h = compute_hash("t", {"a": 1, "b": 2}, "d")
    assert h == compute_hash("t", {"b": 2, "a": 1}, "d")
    assert (
        len(
            {
                h,
                compute_hash("u", {"a": 1, "b": 2}, "d"),
                compute_hash("t", {"a": 1, "b": 3}, "d"),
                compute_hash("t", {"a": 1, "b": 2}, "d2"),
            }
        )
        == 4
    )


# --- approve / execute --------------------------------------------------------------------


def test_happy_path_approve_then_execute(gate: ApprovalGate, echo: Echo, clock: Clock) -> None:
    a = propose(gate)
    clock.advance(minutes=1)
    ap = gate.approve(a.id, a.payload_hash)
    assert ap.status is ActionStatus.APPROVED and ap.decided_at == T0 + timedelta(minutes=1)
    assert echo.runs == []  # approving alone executes nothing
    done = gate.execute_approved_action(a.id)
    assert done.status is ActionStatus.EXECUTED and done.result == {"created": "Dentist", "run": 1}
    assert done.executed_at == T0 + timedelta(minutes=1) and echo.runs == [a.id]


def test_cannot_execute_without_approval(gate: ApprovalGate, echo: Echo) -> None:
    a = propose(gate)
    with pytest.raises(InvalidTransitionError, match="pending"):
        gate.execute_approved_action(a.id)
    gate.reject(a.id)
    with pytest.raises(InvalidTransitionError, match="rejected"):
        gate.execute_approved_action(a.id)
    assert echo.runs == []


def test_approval_needs_the_hash_the_user_saw(gate: ApprovalGate, echo: Echo) -> None:
    a = propose(gate)
    with pytest.raises(HashMismatchError, match="review it again"):
        gate.approve(a.id, "0" * 64)
    with pytest.raises(HashMismatchError):
        gate.approve(a.id, "")
    assert gate.get(a.id).status is ActionStatus.PENDING
    # both refused attempts are in the audit log
    assert [e["event"] for e in gate.audit_log(a.id)] == [
        "proposed",
        "approve_refused",
        "approve_refused",
    ]


def test_double_approve_is_a_harmless_noop(gate: ApprovalGate, echo: Echo) -> None:
    a = propose(gate)
    first = gate.approve(a.id, a.payload_hash)
    second = gate.approve(a.id, a.payload_hash)
    assert second == first and second.status is ActionStatus.APPROVED
    gate.execute_approved_action(a.id)
    assert gate.approve(a.id, a.payload_hash).status is ActionStatus.EXECUTED  # even after running
    assert echo.runs == [a.id]
    assert [e["event"] for e in gate.audit_log(a.id)].count("approved") == 1


def test_execute_is_idempotent_and_never_duplicates(gate: ApprovalGate, echo: Echo) -> None:
    a = propose(gate)
    gate.approve(a.id, a.payload_hash)
    r1, r2, r3 = (gate.execute_approved_action(a.id) for _ in range(3))
    assert r1 == r2 == r3 and echo.runs == [a.id]  # executor ran exactly once


def test_executor_failure_is_recorded_and_retry_is_possible(gate: ApprovalGate, echo: Echo) -> None:
    echo.fail_times = 1
    a = propose(gate)
    gate.approve(a.id, a.payload_hash)
    failed = gate.execute_approved_action(a.id)
    assert failed.status is ActionStatus.FAILED and "disk full" in (failed.error or "")
    assert echo.runs == []
    ok = gate.execute_approved_action(a.id)  # retry (the executor itself must be idempotent)
    assert ok.status is ActionStatus.EXECUTED and ok.error is None and echo.runs == [a.id]


def test_unknown_action_id(gate: ApprovalGate) -> None:
    for call in (gate.get, gate.reject, gate.execute_approved_action):
        with pytest.raises(NotFoundError):
            call("nope")  # type: ignore[operator]
    with pytest.raises(NotFoundError):
        gate.approve("nope", "x")


# --- expiry -------------------------------------------------------------------------------


def test_pending_action_expires_and_cannot_be_approved(gate: ApprovalGate, clock: Clock) -> None:
    a = propose(gate)
    clock.advance(minutes=29, seconds=59)
    assert gate.get(a.id).status is ActionStatus.PENDING
    clock.advance(seconds=1)  # exactly at expires_at
    assert gate.get(a.id).status is ActionStatus.EXPIRED
    with pytest.raises(InvalidTransitionError, match="expired"):
        gate.approve(a.id, a.payload_hash)
    assert "expired" in [e["event"] for e in gate.audit_log(a.id)]


def test_approved_but_unexecuted_action_expires_too(
    gate: ApprovalGate, echo: Echo, clock: Clock
) -> None:
    a = propose(gate)
    gate.approve(a.id, a.payload_hash)
    clock.advance(minutes=31)
    with pytest.raises(InvalidTransitionError, match="expired"):
        gate.execute_approved_action(a.id)
    assert echo.runs == []


def test_executed_actions_never_expire(gate: ApprovalGate, clock: Clock) -> None:
    a = propose(gate)
    gate.approve(a.id, a.payload_hash)
    gate.execute_approved_action(a.id)
    clock.advance(days=30)
    assert gate.get(a.id).status is ActionStatus.EXECUTED


# --- reject / edit ------------------------------------------------------------------------


def test_reject_records_the_reason_for_the_orchestrator(gate: ApprovalGate) -> None:
    a = propose(gate)
    r = gate.reject(a.id, "wrong day")
    assert r.status is ActionStatus.REJECTED and r.reject_reason == "wrong day" and r.decided_at
    assert gate.reject(a.id).status is ActionStatus.REJECTED  # idempotent
    assert [x.reject_reason for x in gate.recent_rejections()] == ["wrong day"]
    with pytest.raises(InvalidTransitionError, match="rejected"):
        gate.approve(a.id, a.payload_hash)


def test_reject_after_approval_but_before_execution(gate: ApprovalGate, echo: Echo) -> None:
    a = propose(gate)
    gate.approve(a.id, a.payload_hash)
    assert gate.reject(a.id, "changed my mind").status is ActionStatus.REJECTED
    with pytest.raises(InvalidTransitionError):
        gate.execute_approved_action(a.id)
    assert echo.runs == []


def test_cannot_reject_what_already_ran(gate: ApprovalGate) -> None:
    a = propose(gate)
    gate.approve(a.id, a.payload_hash)
    gate.execute_approved_action(a.id)
    with pytest.raises(InvalidTransitionError, match="executed"):
        gate.reject(a.id)


def test_edit_changes_the_hash_so_stale_approvals_cannot_apply(
    gate: ApprovalGate, clock: Clock
) -> None:
    a = propose(gate, "Dentist")
    clock.advance(minutes=20)
    e = gate.edit(a.id, {"title": "Dentist (moved)"})
    assert (
        e.version == 2
        and e.payload == {"title": "Dentist (moved)"}
        and e.diff == "CREATE: Dentist (moved)"
    )
    assert e.payload_hash != a.payload_hash
    assert e.expires_at == T0 + timedelta(minutes=50)  # the clock restarts on edit
    with pytest.raises(HashMismatchError):  # the user approved the OLD card: refused
        gate.approve(a.id, a.payload_hash)
    assert gate.approve(a.id, e.payload_hash).status is ActionStatus.APPROVED


def test_edit_with_bad_payload_keeps_the_original(gate: ApprovalGate) -> None:
    a = propose(gate)
    with pytest.raises(ValueError, match="title is required"):
        gate.edit(a.id, {"title": ""})
    assert gate.get(a.id).payload == a.payload and gate.get(a.id).version == 1


def test_edit_only_while_pending(gate: ApprovalGate) -> None:
    a = propose(gate)
    gate.approve(a.id, a.payload_hash)
    with pytest.raises(InvalidTransitionError, match="pending"):
        gate.edit(a.id, {"title": "Sneaky change after approval"})


# --- tampering ----------------------------------------------------------------------------


def tamper(db: Path, column: str, value: str, action_id: str) -> None:
    c = sqlite3.connect(db)
    c.execute(f"UPDATE pending_actions SET {column}=? WHERE id=?", (value, action_id))
    c.commit()
    c.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [("payload", '{"title": "DELETE EVERYTHING"}'), ("diff", "CREATE: something else")],
)
def test_payload_changed_after_approval_is_refused(
    tmp_path: Path, echo: Echo, clock: Clock, column: str, value: str
) -> None:
    db = tmp_path / "gate.sqlite"
    g = ApprovalGate(db, clock=clock)
    g.register(ActionHandler("echo", echo.prepare, echo.execute))
    a = propose(g)
    g.approve(a.id, a.payload_hash)
    tamper(db, column, value, a.id)  # someone edits the database between approval and execution
    with pytest.raises(HashMismatchError, match="refusing"):
        g.execute_approved_action(a.id)
    assert echo.runs == [] and g.get(a.id).status is ActionStatus.FAILED
    assert "execute_refused" in [e["event"] for e in g.audit_log(a.id)]
    with pytest.raises(HashMismatchError):  # a retry re-checks the hash: refused every time
        g.execute_approved_action(a.id)
    assert echo.runs == []


# --- limits -------------------------------------------------------------------------------


def test_too_many_pending_actions_are_refused(gate: ApprovalGate, clock: Clock) -> None:
    first = propose(gate, "a")
    propose(gate, "b")
    propose(gate, "c")
    with pytest.raises(TooManyPendingError, match="3 actions"):
        propose(gate, "d")
    gate.reject(first.id)  # resolving one makes room
    propose(gate, "d")
    clock.advance(minutes=31)  # expired ones do not count either
    propose(gate, "e")
    assert len(gate.list_actions(ActionStatus.PENDING)) == 1


# --- the narrow capability given to agents ------------------------------------------------


def test_agents_only_get_a_propose_capability(gate: ApprovalGate, echo: Echo) -> None:
    sink = gate.proposal_sink()
    public = {n for n in dir(sink) if not n.startswith("_")}
    assert public == {"propose"}
    action = sink.propose("echo", {"title": "From agent"})
    assert action.status is ActionStatus.PENDING and echo.runs == []
    for forbidden in ("approve", "execute_approved_action", "reject", "edit", "register"):
        assert not hasattr(sink, forbidden)
    # nothing reachable from the sink leads back to the gate's decision methods
    assert not any(isinstance(v, ApprovalGate) for v in vars(sink).values())


# --- concurrency and persistence ----------------------------------------------------------


def test_concurrent_approve_and_execute_runs_the_action_once(
    gate: ApprovalGate, echo: Echo
) -> None:
    a = propose(gate)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            gate.approve(a.id, a.payload_hash)
            gate.execute_approved_action(a.id)
        except Exception as exc:  # pragma: no cover - would fail the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and echo.runs == [a.id]
    assert gate.get(a.id).status is ActionStatus.EXECUTED


def test_state_survives_a_restart(tmp_path: Path, echo: Echo, clock: Clock) -> None:
    db = tmp_path / "gate.sqlite"
    g1 = ApprovalGate(db, clock=clock)
    g1.register(ActionHandler("echo", echo.prepare, echo.execute))
    a = propose(g1)
    g1.close()
    g2 = ApprovalGate(db, clock=clock)
    g2.register(ActionHandler("echo", echo.prepare, echo.execute))
    assert g2.get(a.id) == a
    g2.approve(a.id, a.payload_hash)
    assert g2.execute_approved_action(a.id).status is ActionStatus.EXECUTED


def test_audit_log_is_ordered_and_timestamped(gate: ApprovalGate, clock: Clock) -> None:
    a = propose(gate)
    clock.advance(minutes=2)
    gate.approve(a.id, a.payload_hash)
    clock.advance(minutes=1)
    gate.execute_approved_action(a.id)
    log = gate.audit_log(a.id)
    assert [e["event"] for e in log] == ["proposed", "approved", "executed"]
    assert [e["at"][:16] for e in log] == [
        "2026-11-09T09:00",
        "2026-11-09T09:02",
        "2026-11-09T09:03",
    ]
    assert gate.audit_log() == log


def test_list_filters_and_orders_newest_first(gate: ApprovalGate, clock: Clock) -> None:
    a = propose(gate, "first")
    clock.advance(minutes=1)
    b = propose(gate, "second")
    gate.reject(a.id)
    assert [x.id for x in gate.list_actions()] == [b.id, a.id]
    assert [x.id for x in gate.list_actions(ActionStatus.PENDING)] == [b.id]
    assert [x.id for x in gate.list_actions(ActionStatus.REJECTED)] == [a.id]
