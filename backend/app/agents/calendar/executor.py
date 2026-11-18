"""The ONLY module that can modify a calendar. It is invoked solely through
``ApprovalGate.execute_approved_action``, i.e. after a human approved the exact payload.

It is wired up once at app start-up (``register_calendar_handlers``) and is never imported by any
agent module (a test scans the agents' source to enforce that).
"""

from __future__ import annotations

from typing import Any

from app.agents.calendar import proposals as P
from app.agents.calendar.models import CalEvent
from app.agents.calendar.service import CalendarReader
from app.agents.calendar.store import LocalCalendarWriter
from app.core.approvals import ActionHandler, ApprovalGate, PendingAction


def register_calendar_handlers(
    gate: ApprovalGate, reader: CalendarReader, writer: LocalCalendarWriter
) -> None:
    """Prepare-side handlers use the read-only reader; execute-side ones use the writer."""

    def execute_create(action: PendingAction) -> dict[str, Any]:
        p = P.CreatePayload.model_validate(action.payload)  # re-validated at execution time
        event = CalEvent(
            id=f"evt-{action.id}",  # deterministic: a retry can never create a second event
            title=p.title, start=p.start, end=p.end, description=p.description,
            location=p.location, source="local", writable=True,
        )  # fmt: skip
        stored, created = writer.add(event)
        return {"event_id": stored.id, "created": created, "title": stored.title}

    def execute_update(action: PendingAction) -> dict[str, Any]:
        p = P.UpdatePayload.model_validate(action.payload)
        changes = {k: v for k, v in p.changes.model_dump().items() if v is not None}
        new = writer.update(p.event_id, changes, p.before_etag)
        return {"event_id": new.id, "title": new.title, "changed": sorted(changes)}

    def execute_delete(action: PendingAction) -> dict[str, Any]:
        p = P.DeletePayload.model_validate(action.payload)
        deleted = writer.delete(p.event_id, p.before_etag)
        return {"event_id": p.event_id, "deleted": deleted}

    gate.register(ActionHandler(P.CREATE, P.prepare_create, execute_create))
    gate.register(
        ActionHandler(P.UPDATE, lambda raw: P.prepare_update(reader, raw), execute_update)
    )
    gate.register(
        ActionHandler(P.DELETE, lambda raw: P.prepare_delete(reader, raw), execute_delete)
    )
