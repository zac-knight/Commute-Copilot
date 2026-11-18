"""Validation and human-readable diffs for calendar change proposals.

This module only needs READ access (to look up the event being changed). It deliberately does not
import the calendar writer: nothing here can modify a calendar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agents.calendar.models import CalEvent
from app.agents.calendar.service import CalendarReader
from app.core.timeutil import TZ
from app.core.untrusted import sanitise

CREATE = "calendar.create_event"
UPDATE = "calendar.update_event"
DELETE = "calendar.delete_event"
MAX_DURATION = timedelta(days=7)


def _clean(text: str) -> str:
    return sanitise(text).strip()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are errors, never silently applied


class _Timed(_Strict):
    @field_validator("start", "end", check_fields=False)
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("times must include a timezone offset")
        return v.astimezone(TZ) if v is not None else None


class CreatePayload(_Timed):
    title: str = Field(min_length=1, max_length=200)
    start: datetime
    end: datetime
    description: str = Field(default="", max_length=2000)
    location: str = Field(default="", max_length=200)

    @field_validator("title", "description", "location")
    @classmethod
    def _sanitise(cls, v: str) -> str:
        return _clean(v)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        # checked AFTER cleaning: ' ' or only invisible characters must not become an empty title
        if not v:
            raise ValueError("title cannot be empty")
        return v


class Changes(_Timed):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    start: datetime | None = None
    end: datetime | None = None
    description: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, max_length=200)

    @field_validator("title", "description", "location")
    @classmethod
    def _sanitise(cls, v: str | None) -> str | None:
        return None if v is None else _clean(v)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("title cannot be empty")
        return v


class UpdatePayload(_Strict):
    event_id: str
    before_etag: str
    before: dict[str, str]  # what the user is shown as the current state
    changes: Changes


class DeletePayload(_Strict):
    event_id: str
    before_etag: str
    before: dict[str, str]


def _when(start: datetime, end: datetime) -> str:
    s, e = start.astimezone(TZ), end.astimezone(TZ)
    if s.date() == e.date():
        return f"{s:%a %d %b %Y %H:%M}-{e:%H:%M}"
    return f"{s:%a %d %b %Y %H:%M} to {e:%a %d %b %Y %H:%M}"


def _fmt_value(v: Any) -> str:
    return f"{v:%a %d %b %Y %H:%M}" if isinstance(v, datetime) else str(v)


def _snapshot(e: CalEvent) -> dict[str, str]:
    return {
        "title": e.title, "when": _when(e.start, e.end), "start": e.start.isoformat(),
        "end": e.end.isoformat(), "location": e.location, "description": e.description,
    }  # fmt: skip


def _check_range(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ValueError("the event must end after it starts")
    if end - start > MAX_DURATION:
        raise ValueError("events longer than 7 days are not supported")


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())


def prepare_create(raw: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    try:
        p = CreatePayload.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(_validation_message(exc)) from exc
    _check_range(p.start, p.end)
    lines = ["CREATE EVENT", f"  Title:    {p.title}", f"  When:     {_when(p.start, p.end)}"]
    if p.location:
        lines.append(f"  Where:    {p.location}")
    if p.description:
        lines.append(f"  Notes:    {p.description[:200]}")
    return (
        p.model_dump(mode="json"),
        "\n".join(lines),
        f"Create '{p.title}' on {_when(p.start, p.end)}",
    )


def _lookup(reader: CalendarReader, event_id: str) -> CalEvent:
    event = reader.find(str(event_id))
    if event is None:
        raise ValueError(f"no event with id {event_id!r}; list the events first to get its id")
    if not event.writable:
        raise ValueError(
            f"'{event.title}' comes from an imported calendar file ({event.source}) and is "
            "read-only; only events in the local calendar can be changed"
        )
    return event


def prepare_update(reader: CalendarReader, raw: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """``raw`` = {event_id, [before_etag, before], changes{...}}. Re-derives the 'before' state from
    the calendar, so a stale or forged snapshot in ``raw`` is ignored (edit keeps the same etag)."""
    event = _lookup(reader, raw.get("event_id", ""))
    try:
        changes = Changes.model_validate(raw.get("changes") or {})
    except ValidationError as exc:
        raise ValueError(_validation_message(exc)) from exc
    effective = {
        k: v for k, v in changes.model_dump().items()
        if v is not None and v != getattr(event, k)
    }  # fmt: skip
    if not effective:
        raise ValueError("those changes would not alter the event")
    new_start = effective.get("start", event.start)
    new_end = effective.get("end", event.end)
    _check_range(new_start, new_end)
    lines = [f"UPDATE EVENT '{event.title}' ({_when(event.start, event.end)})"]
    for key, new in effective.items():
        old = getattr(event, key)
        show = _fmt_value
        lines.append(f"  {key.capitalize():<12}{show(old) or '(empty)'}  ->  {show(new)}")
    payload = UpdatePayload(
        event_id=event.id,
        before_etag=event.etag,
        before=_snapshot(event),
        changes=Changes.model_validate(effective),
    )
    return (
        payload.model_dump(mode="json", exclude_none=False),
        "\n".join(lines),
        f"Update '{event.title}' ({', '.join(effective)})",
    )


def prepare_delete(reader: CalendarReader, raw: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    event = _lookup(reader, raw.get("event_id", ""))
    payload = DeletePayload(event_id=event.id, before_etag=event.etag, before=_snapshot(event))
    diff = "\n".join(
        [
            "DELETE EVENT",
            f"  Title:    {event.title}",
            f"  When:     {_when(event.start, event.end)}",
            *([f"  Where:    {event.location}"] if event.location else []),
        ]
    )
    return (
        payload.model_dump(mode="json"),
        diff,
        f"Delete '{event.title}' on {_when(event.start, event.end)}",
    )
