"""docs/safety.md must describe the permissions that the code actually enforces."""

from __future__ import annotations

import re
from pathlib import Path

from app.agents.calendar.service import CalendarReader
from app.agents.calendar.store import LocalCalendarReader
from app.agents.calendar.tools import register_calendar_tools
from app.agents.transit.tools import register_transit_tools
from app.agents.weather.tools import register_weather_tools
from app.core.approvals import ApprovalGate
from app.core.memory import UserProfile
from app.core.tools import ToolRegistry

DOC = Path(__file__).resolve().parents[2] / "docs" / "safety.md"


def full_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_transit_tools(reg, None)  # type: ignore[arg-type]  # closures are never executed here
    register_weather_tools(reg, None, lambda: [])  # type: ignore[arg-type]
    reader = CalendarReader(LocalCalendarReader(Path("/nonexistent/c.json")), None, UserProfile())
    register_calendar_tools(reg, reader, ApprovalGate(":memory:").proposal_sink())
    return reg


def doc_rows() -> set[tuple[str, str, str]]:
    pattern = r"^\| (\w+) \| `(\w+)` \| (read|propose|execute) \|"
    return {(m[1], m[2], m[3]) for m in re.finditer(pattern, DOC.read_text(), re.M)}


def test_permissions_table_matches_the_registry_exactly() -> None:
    actual = {
        (agent, tool, access.value)
        for agent, tools in full_registry().permissions().items()
        for tool, access in tools
    }
    assert doc_rows() == actual


def test_no_agent_holds_an_execute_tool() -> None:
    assert doc_rows() and all(a != "execute" for _, _, a in doc_rows())
