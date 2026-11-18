"""Approval gate: the ONLY way an agent's proposed change can ever reach the outside world.

Design (see docs/safety.md)
---------------------------
* Agents get a :class:`ProposalSink` that can do exactly one thing: create a ``PENDING`` action. It
  has no approve/execute method. The gate itself is never passed to an agent.
* A human (the UI/API) calls :meth:`ApprovalGate.approve` with the payload hash they were shown,
  then :meth:`ApprovalGate.execute_approved_action` runs the registered executor. That method is
  the single place executors are invoked.
* ``execute_approved_action`` runs only if: status is APPROVED, the action has not expired, and the
  hash recomputed from the stored payload still equals the hash approved (tamper check).
  Execution is idempotent: a second call returns the stored result and never re-runs the executor.

State machine::

    PENDING --approve--> APPROVED --execute--> EXECUTED
       |  \\                 |  \\-- (executor error) --> FAILED --execute (retry)--> EXECUTED
       |   \\--reject-->  REJECTED <--reject--/
       \\--(time)--> EXPIRED  (also from APPROVED if not executed in time)
    edit: PENDING -> PENDING (payload/diff replaced, hash changes, version+1, expiry restarts)

Approving an already APPROVED/EXECUTED action is a harmless no-op (double click); every other
illegal transition raises :class:`InvalidTransitionError`. Every transition is appended to an audit
log with a timestamp.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.timeutil import TZ


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class ApprovalError(Exception):
    pass


class NotFoundError(ApprovalError):
    pass


class InvalidTransitionError(ApprovalError):
    pass


class HashMismatchError(ApprovalError):
    """The payload the user approved is not the payload now stored."""


class TooManyPendingError(ApprovalError):
    pass


class PendingAction(BaseModel):
    id: str
    type: str
    payload: dict[str, Any]
    diff: str  # human-readable, shown to the user
    summary: str  # one line for lists
    payload_hash: str
    status: ActionStatus
    version: int = 1
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    reject_reason: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class ActionHandler:
    """What one action type needs: validate+describe a payload, and execute it once approved."""

    type: str
    # raw payload -> (normalised payload, human-readable diff, one-line summary); raises ValueError
    prepare: Callable[[dict[str, Any]], tuple[dict[str, Any], str, str]]
    execute: Callable[[PendingAction], dict[str, Any]]  # must be idempotent


def compute_hash(action_type: str, payload: dict[str, Any], diff: str) -> str:
    """Hash of exactly what the user is shown and what will run."""
    canon = json.dumps(
        {"type": action_type, "payload": payload, "diff": diff},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )  # fmt: skip
    return hashlib.sha256(canon.encode()).hexdigest()


class ProposalSink:
    """The ONLY approval-gate capability given to agents: propose. No approve, no execute."""

    def __init__(self, propose: Callable[[str, dict[str, Any]], PendingAction]) -> None:
        self.propose = propose


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, payload TEXT NOT NULL, diff TEXT NOT NULL,
    summary TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
    version INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    decided_at TEXT, executed_at TEXT, reject_reason TEXT, result TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS approval_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL, at TEXT NOT NULL,
    event TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_actions_status ON pending_actions(status);
"""
_TERMINAL_FOR_EXPIRY = {ActionStatus.PENDING, ActionStatus.APPROVED}


class ApprovalGate:
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=30),
        max_pending: int = 10,
    ) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(TZ))
        self._ttl = ttl
        self._max_pending = max_pending
        self._handlers: dict[str, ActionHandler] = {}
        with self._lock:
            self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ wiring

    def register(self, handler: ActionHandler) -> None:
        if handler.type in self._handlers:
            raise ValueError(f"handler for {handler.type!r} already registered")
        self._handlers[handler.type] = handler

    def proposal_sink(self) -> ProposalSink:
        """What agents receive. A closure over ``create``; nothing else is reachable from it."""
        return ProposalSink(self.create)

    # ------------------------------------------------------------------ storage helpers

    def _now(self) -> datetime:
        return self._clock().astimezone(TZ)

    def _log(self, action_id: str, event: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO approval_log (action_id, at, event, detail) VALUES (?,?,?,?)",
            (action_id, self._now().isoformat(timespec="seconds"), event, detail[:500]),
        )

    @staticmethod
    def _row_to_action(r: sqlite3.Row) -> PendingAction:
        return PendingAction(
            id=r["id"], type=r["type"], payload=json.loads(r["payload"]), diff=r["diff"],
            summary=r["summary"], payload_hash=r["payload_hash"], status=ActionStatus(r["status"]),
            version=r["version"], created_at=datetime.fromisoformat(r["created_at"]),
            expires_at=datetime.fromisoformat(r["expires_at"]),
            decided_at=datetime.fromisoformat(r["decided_at"]) if r["decided_at"] else None,
            executed_at=datetime.fromisoformat(r["executed_at"]) if r["executed_at"] else None,
            reject_reason=r["reject_reason"],
            result=json.loads(r["result"]) if r["result"] else None, error=r["error"],
        )  # fmt: skip

    def _load(self, action_id: str) -> PendingAction:
        """Fetch one action, lazily expiring it if its time is up. Caller holds the lock."""
        r = self._conn.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone()
        if r is None:
            raise NotFoundError(f"no such action {action_id!r}")
        action = self._row_to_action(r)
        if action.status in _TERMINAL_FOR_EXPIRY and self._now() >= action.expires_at:
            self._conn.execute(
                "UPDATE pending_actions SET status=? WHERE id=?", (ActionStatus.EXPIRED, action_id)
            )
            self._log(action_id, "expired", f"was {action.status.value}")
            action = action.model_copy(update={"status": ActionStatus.EXPIRED})
        return action

    @staticmethod
    def _encode(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat(timespec="seconds")
        if isinstance(v, dict):
            return json.dumps(v)
        if isinstance(v, ActionStatus):
            return v.value
        return v

    def _set(self, action_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [self._encode(v) for v in fields.values()]
        self._conn.execute(f"UPDATE pending_actions SET {cols} WHERE id=?", [*vals, action_id])

    # ------------------------------------------------------------------ propose

    def create(self, action_type: str, raw_payload: dict[str, Any]) -> PendingAction:
        handler = self._handlers.get(action_type)
        if handler is None:
            raise ValueError(f"unknown action type {action_type!r}")
        payload, diff, summary = handler.prepare(raw_payload)  # validates; may raise ValueError
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._expire_all()
                n = self._conn.execute(
                    "SELECT COUNT(*) FROM pending_actions WHERE status=?", (ActionStatus.PENDING,)
                ).fetchone()[0]
                if n >= self._max_pending:
                    raise TooManyPendingError(
                        f"{n} actions are already waiting for approval; resolve them first"
                    )
                now = self._now()
                action = PendingAction(
                    id=uuid.uuid4().hex[:12], type=action_type, payload=payload, diff=diff,
                    summary=summary, payload_hash=compute_hash(action_type, payload, diff),
                    status=ActionStatus.PENDING, created_at=now, expires_at=now + self._ttl,
                )  # fmt: skip
                self._conn.execute(
                    "INSERT INTO pending_actions (id,type,payload,diff,summary,payload_hash,status,"
                    "version,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (action.id, action.type, json.dumps(payload), diff, summary,
                     action.payload_hash, ActionStatus.PENDING.value, 1,
                     now.isoformat(timespec="seconds"),
                     action.expires_at.isoformat(timespec="seconds")),
                )  # fmt: skip
                self._log(action.id, "proposed", summary)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return action

    def _expire_all(self) -> None:
        now = self._now().isoformat(timespec="seconds")
        for r in self._conn.execute(
            "SELECT id, status FROM pending_actions WHERE status IN (?,?) AND expires_at<=?",
            (ActionStatus.PENDING, ActionStatus.APPROVED, now),
        ).fetchall():
            self._conn.execute(
                "UPDATE pending_actions SET status=? WHERE id=?", (ActionStatus.EXPIRED, r["id"])
            )
            self._log(r["id"], "expired", f"was {r['status']}")

    # ------------------------------------------------------------------ read

    def get(self, action_id: str) -> PendingAction:
        with self._lock:
            return self._load(action_id)

    def list_actions(
        self, status: ActionStatus | None = None, limit: int = 50
    ) -> list[PendingAction]:
        with self._lock:
            self._expire_all()
            q = "SELECT id FROM pending_actions"
            args: list[Any] = []
            if status is not None:
                q += " WHERE status=?"
                args.append(status.value)
            q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
            return [self._load(r["id"]) for r in self._conn.execute(q, [*args, limit]).fetchall()]

    def audit_log(self, action_id: str | None = None) -> list[dict[str, str]]:
        with self._lock:
            q, args = "SELECT action_id, at, event, detail FROM approval_log", []
            if action_id:
                q, args = q + " WHERE action_id=?", [action_id]
            return [dict(r) for r in self._conn.execute(q + " ORDER BY seq", args).fetchall()]

    def recent_rejections(self, limit: int = 10) -> list[PendingAction]:
        """For the orchestrator: what the user turned down and why, to offer alternatives."""
        return self.list_actions(ActionStatus.REJECTED, limit)

    # ------------------------------------------------------------------ human decisions

    def approve(self, action_id: str, payload_hash: str) -> PendingAction:
        """The caller must present the hash of what the user was shown."""
        with self._lock:
            a = self._load(action_id)
            if a.status in (ActionStatus.APPROVED, ActionStatus.EXECUTED):
                return a  # double click: harmless no-op
            if a.status is not ActionStatus.PENDING:
                raise InvalidTransitionError(f"cannot approve an action that is {a.status.value}")
            if payload_hash != a.payload_hash:
                self._log(action_id, "approve_refused", "hash mismatch")
                raise HashMismatchError(
                    "the action changed since you reviewed it; review it again before approving"
                )
            self._set(action_id, status=ActionStatus.APPROVED, decided_at=self._now())
            self._log(action_id, "approved", f"hash {payload_hash[:12]}")
            return self._load(action_id)

    def reject(self, action_id: str, reason: str | None = None) -> PendingAction:
        with self._lock:
            a = self._load(action_id)
            if a.status is ActionStatus.REJECTED:
                return a
            if a.status not in (ActionStatus.PENDING, ActionStatus.APPROVED):
                raise InvalidTransitionError(f"cannot reject an action that is {a.status.value}")
            self._set(action_id, status=ActionStatus.REJECTED, decided_at=self._now(),
                      reject_reason=(reason or "")[:500] or None)  # fmt: skip
            self._log(action_id, "rejected", reason or "")
            return self._load(action_id)

    def edit(self, action_id: str, new_payload: dict[str, Any]) -> PendingAction:
        """Replace a PENDING action's payload (the user changed their mind about the details). The
        hash changes, so anything the user approved earlier can never apply to the new version."""
        with self._lock:
            a = self._load(action_id)
            if a.status is not ActionStatus.PENDING:
                raise InvalidTransitionError(
                    f"can only edit a pending action, this one is {a.status.value}"
                )
            payload, diff, summary = self._handlers[a.type].prepare(new_payload)
            now = self._now()
            self._set(action_id, payload=payload, diff=diff, summary=summary,
                      payload_hash=compute_hash(a.type, payload, diff), version=a.version + 1,
                      expires_at=now + self._ttl)  # fmt: skip
            self._log(action_id, "edited", f"v{a.version + 1}")
            return self._load(action_id)

    # ------------------------------------------------------------------ the single execution path

    def execute_approved_action(self, action_id: str) -> PendingAction:
        """Run the executor for an APPROVED action. The only call site of any executor."""
        with self._lock:
            a = self._load(action_id)
            if a.status is ActionStatus.EXECUTED:
                return a  # idempotent: never run twice
            if a.status not in (ActionStatus.APPROVED, ActionStatus.FAILED):
                raise InvalidTransitionError(
                    f"only an approved action can be executed; this one is {a.status.value}"
                )
            if compute_hash(a.type, a.payload, a.diff) != a.payload_hash:
                self._set(
                    action_id, status=ActionStatus.FAILED, error="payload hash mismatch (tampered)"
                )
                self._log(action_id, "execute_refused", "stored payload no longer matches its hash")
                raise HashMismatchError("stored payload does not match the approved hash; refusing")
            handler = self._handlers.get(a.type)
            if handler is None:
                raise ApprovalError(f"no executor registered for {a.type!r}")
            try:
                result = handler.execute(a)
            except Exception as exc:
                self._set(
                    action_id,
                    status=ActionStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
                self._log(action_id, "failed", f"{type(exc).__name__}: {exc}")
                return self._load(action_id)
            self._set(
                action_id,
                status=ActionStatus.EXECUTED,
                executed_at=self._now(),
                result=result,
                error=None,
            )
            self._log(action_id, "executed", json.dumps(result, default=str)[:300])
            return self._load(action_id)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
