"""Live processing stream: lets the UI watch what the model is doing while it does it.

A request sets a *sink* (a function taking a dict) in a ContextVar; the LLM client, the agent loop,
the router and the synthesiser ``emit`` events (reasoning and answer tokens, tool calls with their
JSON arguments, tool results, token usage). With no sink every call is a no-op, so tests, scripts
and the offline paths behave exactly as before. ContextVars propagate into the asyncio tasks of one
request and are isolated between concurrent requests (same pattern as ``orchestrator/context.py``).

Nothing here may raise into the caller: a broken observer must never break an answer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

log = logging.getLogger(__name__)

Sink = Callable[[dict[str, Any]], None]

live_sink: ContextVar[Sink | None] = ContextVar("live_sink", default=None)
# Who is speaking right now: {"step": "s1", "agent": "transit", "phase": "agent"}.
live_scope: ContextVar[dict[str, str]] = ContextVar("live_scope", default={})  # noqa: B039
# Per-request switch: True = let thinking-capable models reason (slower, visible reasoning tokens).
reasoning_override: ContextVar[bool | None] = ContextVar("reasoning_override", default=None)


def streaming() -> bool:
    return live_sink.get() is not None


def emit(kind: str, **data: Any) -> None:
    sink = live_sink.get()
    if sink is None:
        return
    try:
        sink({"kind": kind, **live_scope.get(), **data})
    except Exception:  # pragma: no cover - an observer bug must not break the request
        log.exception("live sink failed for %s", kind)


@contextmanager
def scope(**fields: str) -> Iterator[None]:
    token = live_scope.set({**live_scope.get(), **fields})
    try:
        yield
    finally:
        live_scope.reset(token)


def effective_think(default: bool | None) -> bool | None:
    """The think flag for an AGENT or SYNTHESIS call (the router is never overridden: latency)."""
    return True if reasoning_override.get() else default
