"""Generic tool-using agent loop.

Contract: ``Agent.run`` always returns an ``AgentResult``; it never loops forever and
never raises for model/tool/budget problems. Stop conditions: final text answer,
``max_steps`` model calls, request budget (tokens or wall time), or LLM error.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.livestream import effective_think, emit
from app.core.llm import LLM, LLMError, LLMResponse, ToolCall
from app.core.tools import ToolOutcome, ToolSet
from app.core.tracing import SpanKind, Tracer

Status = Literal["completed", "max_steps", "budget_exceeded", "error"]


class RequestBudget:
    """Shared token + wall-time allowance for one user request (all agents draw on it)."""

    def __init__(self, max_tokens: int, max_seconds: float) -> None:
        self.max_tokens = max_tokens
        self.max_seconds = max_seconds
        self.tokens_used = 0
        self._started = time.monotonic()

    def add_tokens(self, n: int) -> None:
        self.tokens_used += n

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - (time.monotonic() - self._started))

    def exceeded(self) -> str | None:
        if self.tokens_used >= self.max_tokens:
            return f"token budget exhausted ({self.tokens_used}/{self.max_tokens})"
        if self.remaining_seconds() <= 0:
            return f"time budget exhausted ({self.max_seconds:.0f}s)"
        return None


@dataclass
class AgentResult:
    status: Status
    text: str
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    tool_calls: list[str] = field(default_factory=list)  # names, in call order
    # Every tool call with its input and raw output text, so callers can pass STRUCTURED results
    # to later steps and the synthesiser instead of relying on the model's prose.
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class Agent:
    def __init__(
        self,
        *,
        name: str,
        system_prompt: str,
        tools: ToolSet,
        model: str,
        llm: LLM,
        tracer: Tracer,
        think: bool | None = None,
        max_steps: int = 6,
        max_tokens: int = 4096,
        tool_result_max_chars: int = 20_000,
        prompt_version: str | None = None,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model
        self.llm = llm
        self.tracer = tracer
        self.think = think
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.tool_result_max_chars = tool_result_max_chars
        self.prompt_version = prompt_version

    async def run(self, user_message: str, *, budget: RequestBudget | None = None) -> AgentResult:
        attrs = {"prompt_version": self.prompt_version} if self.prompt_version else None
        with self.tracer.span(
            SpanKind.AGENT, self.name, input=user_message, model=self.model, attributes=attrs
        ) as agent_span:
            result = await self._loop(user_message, budget)
            agent_span.set_output(result.text or result.error or "")
            agent_span.set_attr("status", result.status)
            agent_span.set_attr("steps", result.steps)
            agent_span.set_usage(
                tokens_in=result.tokens_in, tokens_out=result.tokens_out, model=self.model
            )
            if result.status != "completed":
                agent_span.set_error(f"{result.status}: {result.error or result.text}")
            return result

    # ------------------------------------------------------------------------------------

    async def _loop(self, user_message: str, budget: RequestBudget | None) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        res = AgentResult(status="error", text="", messages=messages)
        tool_defs = self.tools.definitions()

        for step in range(1, self.max_steps + 1):
            if budget and (reason := budget.exceeded()):
                return self._finish(res, "budget_exceeded", error=reason)

            try:
                response = await self._call_llm(step, messages, tool_defs, budget)
            except TimeoutError:
                return self._finish(res, "budget_exceeded", error="time budget exhausted")
            except LLMError as exc:
                return self._finish(res, "error", error=f"{exc.kind}: {exc}")

            res.steps = step
            res.tokens_in += response.usage.input_tokens
            res.tokens_out += response.usage.output_tokens
            if budget:
                budget.add_tokens(response.usage.total)
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "max_tokens":
                return self._finish(
                    res, "error", error="response truncated at max_tokens", text=response.text
                )

            calls = response.tool_calls
            if not calls:
                return self._finish(res, "completed", text=response.text)

            if step == self.max_steps:
                break  # don't execute tools whose results could never be used

            try:
                results = await self._run_tools(calls, budget, res)
            except TimeoutError:
                return self._finish(res, "budget_exceeded", error="time budget exhausted")
            res.tool_calls.extend(c.name for c in calls)
            messages.append({"role": "user", "content": results})

        return self._finish(
            res,
            "max_steps",
            error=f"could not complete within {self.max_steps} steps",
        )

    @staticmethod
    def _finish(
        res: AgentResult, status: Status, *, text: str = "", error: str | None = None
    ) -> AgentResult:
        res.status = status
        res.error = error
        res.text = text
        if status == "max_steps":
            res.text = "I could not complete this request within the step limit."
        elif status == "budget_exceeded":
            res.text = "I stopped because this request exceeded its budget."
        return res

    async def _call_llm(
        self,
        step: int,
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]],
        budget: RequestBudget | None,
    ) -> LLMResponse:
        with self.tracer.span(SpanKind.LLM, f"{self.name}.llm#{step}", model=self.model) as span:
            span.set_input(messages[-1]["content"])
            timeout = budget.remaining_seconds() if budget else None
            think = effective_think(self.think)
            emit(
                "llm_started", n=step, model=self.model, think=think,
                prompt_version=self.prompt_version or "", messages=len(messages),
                tools=[d["name"] for d in tool_defs],
            )  # fmt: skip
            t0 = time.monotonic()
            async with asyncio.timeout(timeout):
                response = await self.llm.complete(
                    model=self.model,
                    system=self.system_prompt,
                    messages=messages,
                    tools=tool_defs,
                    max_tokens=self.max_tokens,
                    think=think,
                )
            emit(
                "llm_usage", n=step, model=response.model, tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                duration_ms=round((time.monotonic() - t0) * 1000),
                thinking_chars=len(response.extra.get("thinking", "")),
            )  # fmt: skip
            span.set_usage(
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                model=response.model,
            )
            span.set_attr("stop_reason", response.stop_reason)
            span.set_output(response.text or [c.name for c in response.tool_calls])
            return response

    async def _run_tools(
        self, calls: list[ToolCall], budget: RequestBudget | None, res: AgentResult
    ) -> list[dict[str, Any]]:
        """Run the calls from one assistant turn concurrently; results keep call order."""
        timeout = budget.remaining_seconds() if budget else None
        async with asyncio.timeout(timeout):
            outcomes = await asyncio.gather(*(self._run_tool(c) for c in calls))
        blocks: list[dict[str, Any]] = []
        for call, outcome in zip(calls, outcomes, strict=True):
            res.tool_log.append(
                {
                    "tool": call.name,
                    "input": call.input,
                    "output": outcome.content,
                    "is_error": outcome.is_error,
                }
            )
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": self._truncate(outcome.content),
            }
            if outcome.is_error:
                block["is_error"] = True
            blocks.append(block)
        return blocks

    async def _run_tool(self, call: ToolCall) -> ToolOutcome:
        emit("tool_call", call_id=call.id, name=call.name, input=call.input)
        t0 = time.monotonic()
        with self.tracer.span(SpanKind.TOOL, call.name, input=call.input) as span:
            outcome = await self.tools.call(call.name, call.input)
            emit(
                "tool_result", call_id=call.id, name=call.name, output=outcome.content,
                is_error=outcome.is_error, duration_ms=round((time.monotonic() - t0) * 1000),
            )  # fmt: skip
            span.set_output(outcome.content)
            if outcome.is_error:
                span.set_error(outcome.content)
            return outcome

    def _truncate(self, text: str) -> str:
        limit = self.tool_result_max_chars
        if len(text) <= limit:
            return text
        return f"{text[:limit]}\n[truncated: {len(text) - limit} more characters omitted]"
