"""Scripted LLM for tests and the offline demo. Makes NO network calls."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.config import ROLES
from app.core.llm import LLMResponse, TokenUsage

_DEFAULT_MODEL = ROLES["agent"].model

Script = LLMResponse | Callable[[list[dict[str, Any]]], LLMResponse]


def text_response(
    text: str, *, tokens_in: int = 100, tokens_out: int = 20, model: str = _DEFAULT_MODEL
) -> LLMResponse:
    return LLMResponse(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=TokenUsage(tokens_in, tokens_out),
        model=model,
    )


def tool_use_response(
    calls: Sequence[tuple[str, dict[str, Any]]],
    *,
    tokens_in: int = 100,
    tokens_out: int = 20,
    model: str = _DEFAULT_MODEL,
    id_prefix: str = "toolu_scripted",
) -> LLMResponse:
    return LLMResponse(
        content=[
            {"type": "tool_use", "id": f"{id_prefix}_{i}", "name": n, "input": inp}
            for i, (n, inp) in enumerate(calls)
        ],
        stop_reason="tool_use",
        usage=TokenUsage(tokens_in, tokens_out),
        model=model,
    )


class ScriptedLLM:
    """Returns the scripted responses in order; records every request it receives."""

    def __init__(self, script: Sequence[Script]) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self._script:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        item = self._script.pop(0)
        return item(kwargs["messages"]) if callable(item) else item
