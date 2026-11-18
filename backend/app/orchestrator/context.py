"""Per-request context shared between the orchestrator and the tools it composes.

ContextVars propagate into the asyncio tasks (and ``to_thread`` workers) started for one request and
are isolated between concurrent requests. They let the orchestrator hand an agent's tools data the
model must not be trusted to copy (a planned journey) or collect what happened (proposals created).
"""

from __future__ import annotations

from contextvars import ContextVar

from app.agents.weather.advice import RoutePlan
from app.core.approvals import PendingAction

# Journeys the weather step may assess (built from a transit step's structured output).
plans_var: ContextVar[list[RoutePlan]] = ContextVar("plans", default=[])  # noqa: B039
# Where proposals created during this request are recorded (None outside a request).
proposals_var: ContextVar[list[PendingAction] | None] = ContextVar("proposals", default=None)
