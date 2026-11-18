"""Calendar tools exposed to the LLM.

Capability boundary: these closures capture ONLY a read-only ``CalendarReader`` and a propose-only
``ProposalSink``. They hold no calendar writer and no reference to the approval gate, so nothing
here can change a calendar or approve anything. All text taken from calendars is delimited as
untrusted data. (Enforced by tests/test_calendar_safety.py.)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.calendar import proposals as P
from app.agents.calendar.models import CalEvent
from app.agents.calendar.service import CalendarReader, parse_hhmm
from app.agents.calendar.slots import Interval
from app.core.approvals import ProposalSink
from app.core.timeutil import fmt_local, parse_when
from app.core.tools import Access, Tool, ToolRegistry
from app.core.untrusted import wrap_untrusted

AGENT_NAME = "calendar"
MAX_EVENTS_RETURNED = 50
MAX_SLOTS_RETURNED = 20


# ------------------------------------------------------------------ inputs


class ListEventsInput(BaseModel):
    start: str = Field(
        description="Start of the range, e.g. 'tomorrow 00:00' or '2026-11-12T09:00'."
    )
    end: str = Field(description="End of the range, e.g. 'tomorrow 23:59'.")


class FindFreeSlotsInput(BaseModel):
    start: str = Field(description="Earliest time to consider, e.g. 'tomorrow 12:00'.")
    end: str = Field(description="Latest time to consider, e.g. 'tomorrow 17:00'.")
    duration_minutes: int = Field(
        ge=5, le=480, description="How long the slot must be, in minutes."
    )
    working_hours: str | None = Field(
        default=None,
        description="Only offer times within HH:MM-HH:MM. Omit for the user's usual hours.",
    )
    whole_day: bool = Field(
        default=False, description="Ignore working hours (offer any time of day)."
    )
    buffer_minutes: int | None = Field(
        default=None,
        ge=0,
        le=120,
        description="Clear time to keep around meetings. Omit for the user's usual.",
    )
    all_day_events_block: bool = Field(
        default=False, description="Treat all-day events (e.g. 'away') as blocking the whole day."
    )


class CheckConflictsInput(BaseModel):
    proposed_start: str = Field(description="When the new event would start.")
    proposed_end: str = Field(description="When the new event would end.")


class ProposeCreateInput(BaseModel):
    title: str = Field(description="Event title, as the user asked.")
    start: str = Field(description="Start, e.g. 'thursday 2pm' or ISO.")
    end: str | None = Field(default=None, description="End. Omit to use duration_minutes.")
    duration_minutes: int = Field(
        default=60, ge=5, le=1440, description="Used when end is omitted."
    )
    description: str = Field(default="", description="Optional notes.")
    location: str = Field(default="", description="Optional location.")


class ProposeUpdateInput(BaseModel):
    event_id: str = Field(description="The `id` from list_events.")
    title: str | None = Field(default=None, description="New title, if changing it.")
    start: str | None = Field(default=None, description="New start, if changing it.")
    end: str | None = Field(default=None, description="New end, if changing it.")
    description: str | None = Field(default=None, description="New notes, if changing them.")
    location: str | None = Field(default=None, description="New location, if changing it.")


class ProposeDeleteInput(BaseModel):
    event_id: str = Field(description="The `id` from list_events of the ONE event to delete.")


# ------------------------------------------------------------------ outputs


class EventOut(BaseModel):
    id: str
    title: str  # untrusted text, delimited
    start: str
    end: str
    start_iso: str
    end_iso: str
    all_day: bool
    busy: bool
    location: str  # untrusted text, delimited
    description: str  # untrusted text, delimited
    source: str
    writable: bool


class ListEventsResult(BaseModel):
    events: list[EventOut]
    total: int
    warnings: list[str] = []
    note: str | None = None


class SlotOut(BaseModel):
    start: str
    end: str
    start_iso: str
    end_iso: str
    minutes: int


class FreeSlotsResult(BaseModel):
    slots: list[SlotOut]
    working_hours: str
    buffer_minutes: int
    warnings: list[str] = []
    note: str | None = None


class ConflictsResult(BaseModel):
    has_conflict: bool
    overlapping: list[EventOut]
    too_close: list[EventOut]
    buffer_minutes: int
    warnings: list[str] = []


class ProposalResult(BaseModel):
    action_id: str
    type: str
    status: Literal["pending"]
    summary: str
    diff: str
    expires_at: str
    conflicts: list[EventOut] = []
    note: str


# ------------------------------------------------------------------ registration


def register_calendar_tools(
    registry: ToolRegistry, reader: CalendarReader, sink: ProposalSink
) -> None:
    def now() -> datetime:
        return reader.clock()

    def when(text: str) -> datetime:
        return parse_when(text, now())

    def event_out(e: CalEvent) -> EventOut:
        n = now()
        src = f"calendar:{e.source}"
        return EventOut(
            id=e.id, title=wrap_untrusted(e.title, src, max_chars=200),
            start=fmt_local(e.start, n), end=fmt_local(e.end, n),
            start_iso=e.start.isoformat(timespec="minutes"), end_iso=e.end.isoformat(timespec="minutes"),
            all_day=e.all_day, busy=e.busy,
            location=wrap_untrusted(e.location, src, max_chars=200),
            description=wrap_untrusted(e.description, src, max_chars=300),
            source=e.source, writable=e.writable,
        )  # fmt: skip

    def slot_out(s: Interval) -> SlotOut:
        n = now()
        return SlotOut(
            start=fmt_local(s.start, n), end=fmt_local(s.end, n),
            start_iso=s.start.isoformat(timespec="minutes"), end_iso=s.end.isoformat(timespec="minutes"),
            minutes=round(s.minutes),
        )  # fmt: skip

    def list_events(a: ListEventsInput) -> ListEventsResult:
        res = reader.events_between(when(a.start), when(a.end))
        shown = res.events[:MAX_EVENTS_RETURNED]
        return ListEventsResult(
            events=[event_out(e) for e in shown], total=len(res.events),
            warnings=[wrap_untrusted(w, "import_warning") for w in res.warnings],
            note=f"Showing the first {MAX_EVENTS_RETURNED} of {len(res.events)}."
            if len(res.events) > len(shown) else None,
        )  # fmt: skip

    def find_free_slots(a: FindFreeSlotsInput) -> FreeSlotsResult:
        hours = None
        if a.working_hours:
            lo, _, hi = a.working_hours.partition("-")
            hours = (parse_hhmm(lo), parse_hhmm(hi))
        slots, warnings = reader.free_slots(
            when(a.start), when(a.end), a.duration_minutes, working_hours=hours,
            whole_day=a.whole_day, buffer_minutes=a.buffer_minutes, all_day_blocks=a.all_day_events_block,
        )  # fmt: skip
        wh = (
            "any time"
            if a.whole_day
            else (
                a.working_hours
                or f"{reader.profile.working_hours_start}-{reader.profile.working_hours_end}"
            )
        )
        buf = reader.profile.buffer_minutes if a.buffer_minutes is None else a.buffer_minutes
        return FreeSlotsResult(
            slots=[slot_out(s) for s in slots[:MAX_SLOTS_RETURNED]], working_hours=wh,
            buffer_minutes=buf, warnings=[wrap_untrusted(w, "import_warning") for w in warnings],
            note=None if slots else "No free slot of that length in that range.",
        )  # fmt: skip

    def check_conflicts(a: CheckConflictsInput) -> ConflictsResult:
        overlapping, close, warnings = reader.conflicts(
            when(a.proposed_start), when(a.proposed_end)
        )
        return ConflictsResult(
            has_conflict=bool(overlapping), overlapping=[event_out(e) for e in overlapping],
            too_close=[event_out(e) for e in close], buffer_minutes=reader.profile.buffer_minutes,
            warnings=[wrap_untrusted(w, "import_warning") for w in warnings],
        )  # fmt: skip

    def propose(
        action_type: str, raw: dict[str, Any], check: tuple[datetime, datetime] | None
    ) -> ProposalResult:
        conflicts: list[EventOut] = []
        if check:
            overlapping, _, _ = reader.conflicts(*check, buffer_minutes=0)
            conflicts = [event_out(e) for e in overlapping if e.id != raw.get("event_id")]
        action = sink.propose(action_type, raw)
        return ProposalResult(
            action_id=action.id, type=action.type, status="pending", summary=action.summary,
            diff=action.diff, expires_at=fmt_local(action.expires_at, now()), conflicts=conflicts,
            note="NOT DONE YET: this is a proposal waiting for the user's approval. The calendar has "
                 "not been changed.",
        )  # fmt: skip

    def propose_create_event(a: ProposeCreateInput) -> ProposalResult:
        start = when(a.start)
        end = when(a.end) if a.end else start + timedelta(minutes=a.duration_minutes)
        raw = {"title": a.title, "start": start.isoformat(), "end": end.isoformat(),
               "description": a.description, "location": a.location}  # fmt: skip
        return propose(P.CREATE, raw, (start, end) if end > start else None)

    def propose_update_event(a: ProposeUpdateInput) -> ProposalResult:
        changes: dict[str, object] = {}
        for key in ("title", "description", "location"):
            if (v := getattr(a, key)) is not None:
                changes[key] = v
        for key in ("start", "end"):
            if (v := getattr(a, key)) is not None:
                changes[key] = when(v).isoformat()
        return propose(P.UPDATE, {"event_id": a.event_id, "changes": changes}, None)

    def propose_delete_event(a: ProposeDeleteInput) -> ProposalResult:
        return propose(P.DELETE, {"event_id": a.event_id}, None)

    for tool in (
        Tool(
            "list_events",
            "List calendar events in a time range (local calendar plus imported "
            "files). Returns each event's id, times and whether it is writable.",
            ListEventsInput,
            list_events,
            Access.READ,
        ),
        Tool(
            "find_free_slots",
            "Find free time slots of a given length in a range, applying the "
            "user's working hours and buffer around meetings.",
            FindFreeSlotsInput,
            find_free_slots,
            Access.READ,
        ),
        Tool(
            "check_conflicts",
            "Check whether a proposed time clashes with existing events.",
            CheckConflictsInput,
            check_conflicts,
            Access.READ,
        ),
        Tool(
            "propose_create_event",
            "PROPOSE adding an event. Creates an approval card only; the "
            "calendar is not changed until the user approves.",
            ProposeCreateInput,
            propose_create_event,
            Access.PROPOSE,
        ),
        Tool(
            "propose_update_event",
            "PROPOSE changing one event (use its id from list_events). "
            "Approval card only; nothing changes until the user approves.",
            ProposeUpdateInput,
            propose_update_event,
            Access.PROPOSE,
        ),
        Tool(
            "propose_delete_event",
            "PROPOSE deleting ONE event (use its id from list_events). "
            "Approval card only; nothing is deleted until the user approves.",
            ProposeDeleteInput,
            propose_delete_event,
            Access.PROPOSE,
        ),
    ):
        registry.register(tool)
    registry.grant(
        AGENT_NAME,
        ["list_events", "find_free_slots", "check_conflicts", "propose_create_event",
         "propose_update_event", "propose_delete_event"],
    )  # fmt: skip
