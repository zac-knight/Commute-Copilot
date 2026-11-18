"""Calendar test builders. 2026-11-09 is a Monday."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.agents.calendar.models import CalEvent
from app.core.timeutil import TZ

DAY = datetime(2026, 11, 9, tzinfo=TZ)


def at(h: int, m: int = 0, day: int = 0) -> datetime:
    return (DAY + timedelta(days=day)).replace(hour=h, minute=m)


def ev(title: str, start: datetime, end: datetime, **kw: Any) -> CalEvent:
    return CalEvent(
        id=kw.pop("id", f"evt-{title}-{start:%d%H%M}"), title=title, start=start, end=end, **kw
    )


def all_day(title: str, day: int, days: int = 1, **kw: Any) -> CalEvent:
    return ev(title, at(0, 0, day), at(0, 0, day + days), all_day=True, busy=False, **kw)


# --- a complete calendar environment ------------------------------------------------------

from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

from app.agents.calendar.executor import register_calendar_handlers  # noqa: E402
from app.agents.calendar.service import CalendarReader  # noqa: E402
from app.agents.calendar.store import LocalCalendarReader, LocalCalendarWriter  # noqa: E402
from app.agents.calendar.tools import AGENT_NAME, register_calendar_tools  # noqa: E402
from app.core.approvals import ApprovalGate  # noqa: E402
from app.core.memory import UserProfile  # noqa: E402
from app.core.tools import ToolRegistry, ToolSet  # noqa: E402

NOW = at(7, 50)


@dataclass
class Env:
    reader: CalendarReader
    writer: LocalCalendarWriter  # held by the test (the 'human' side) and the executor ONLY
    gate: ApprovalGate
    registry: ToolRegistry
    tools: ToolSet  # what the agent gets
    import_dir: Path


def make_env(
    tmp_path: Path,
    events: list[CalEvent] | None = None,
    *,
    max_pending: int = 10,
    buffer_minutes: int = 10,
) -> Env:
    writer = LocalCalendarWriter(tmp_path / "calendar.json")
    for e in events or []:
        writer.add(e)
    import_dir = tmp_path / "import"
    import_dir.mkdir(exist_ok=True)
    reader = CalendarReader(
        LocalCalendarReader(writer.path),
        import_dir,
        UserProfile(buffer_minutes=buffer_minutes),
        clock=lambda: NOW,
    )
    gate = ApprovalGate(
        ":memory:", clock=lambda: NOW, ttl=timedelta(minutes=30), max_pending=max_pending
    )
    register_calendar_handlers(gate, reader, writer)
    registry = ToolRegistry()
    register_calendar_tools(registry, reader, gate.proposal_sink())
    return Env(reader, writer, gate, registry, registry.toolset_for(AGENT_NAME), import_dir)


def default_events() -> list[CalEvent]:
    return [
        ev("Team sync", at(10), at(11), id="evt-sync", location="Room 1", description="weekly"),
        ev("Lunch", at(12), at(13), id="evt-lunch"),
        ev("Dentist", at(14, day=3), at(15, day=3), id="evt-dentist"),  # Thursday 12 Nov
    ]
