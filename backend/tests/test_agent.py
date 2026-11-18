from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel

from app.core.agent import Agent, RequestBudget
from app.core.llm import LLMError, LLMResponse, TokenUsage
from app.core.testing import ScriptedLLM, text_response, tool_use_response
from app.core.tools import Tool, ToolSet
from app.core.tracing import SpanKind, Tracer, TraceStore


class EchoIn(BaseModel):
    msg: str


class Counter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def echo(self, args: EchoIn) -> str:
        self.calls.append(args.msg)
        if args.msg == "fail":
            raise RuntimeError("tool broke")
        return f"echo:{args.msg}"


def make_agent(
    llm: Any, tracer: Tracer, counter: Counter | None = None, **kw: Any
) -> tuple[Agent, Counter]:
    counter = counter or Counter()
    tools = ToolSet([Tool("echo", "Echo.", EchoIn, counter.echo)])
    agent = Agent(
        name="t",
        system_prompt="sys",
        tools=tools,
        model="test-model",
        llm=llm,
        tracer=tracer,
        prompt_version="t-v1",
        **kw,
    )
    return agent, counter


async def test_final_answer_without_tools(tracer: Tracer) -> None:
    agent, _ = make_agent(ScriptedLLM([text_response("hello")]), tracer)
    res = await agent.run("hi")
    assert res.ok and res.text == "hello" and res.steps == 1 and res.tool_calls == []


async def test_tool_loop_and_message_shapes(tracer: Tracer) -> None:
    llm = ScriptedLLM([tool_use_response([("echo", {"msg": "a"})]), text_response("done")])
    agent, counter = make_agent(llm, tracer)
    res = await agent.run("go")
    assert res.ok and res.text == "done" and counter.calls == ["a"]
    second = llm.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_scripted_0", "content": "echo:a"}
    ]
    assert llm.requests[0]["tools"][0]["name"] == "echo"


async def test_parallel_tool_calls_run_concurrently_and_return_in_one_message(
    tracer: Tracer,
) -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def slow(args: EchoIn) -> str:
        started.append(args.msg)
        if len(started) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1)  # deadlocks if run sequentially
        return args.msg

    tools = ToolSet([Tool("echo", "d", EchoIn, slow)])
    llm = ScriptedLLM(
        [tool_use_response([("echo", {"msg": "x"}), ("echo", {"msg": "y"})]), text_response("ok")]
    )
    agent = Agent(name="t", system_prompt="s", tools=tools, model="m", llm=llm, tracer=tracer)
    res = await agent.run("go")
    assert res.ok
    results = llm.requests[1]["messages"][-1]["content"]
    assert [r["content"] for r in results] == ["x", "y"]  # one user message, call order kept


async def test_tool_error_is_returned_to_model_and_loop_recovers(
    tracer: Tracer, store: TraceStore
) -> None:
    llm = ScriptedLLM(
        [
            tool_use_response([("echo", {"msg": "fail"})]),
            tool_use_response([("echo", {"msg": "ok"})], id_prefix="toolu_b"),
            text_response("recovered"),
        ]
    )
    agent, _ = make_agent(llm, tracer)
    with tracer.request() as rid:
        res = await agent.run("go")
    assert res.ok and res.text == "recovered"
    first_result = llm.requests[1]["messages"][-1]["content"][0]
    assert first_result["is_error"] is True
    assert json.loads(first_result["content"])["error"]["type"] == "tool_failed"
    tool_spans = [s for s in store.get_spans(rid) if s.kind is SpanKind.TOOL]
    assert [s.status for s in tool_spans] == ["error", "ok"]


async def test_max_steps_returns_clear_result_and_does_not_run_last_tools(
    tracer: Tracer,
) -> None:
    llm = ScriptedLLM([tool_use_response([("echo", {"msg": str(i)})]) for i in range(3)])
    agent, counter = make_agent(llm, tracer, max_steps=3)
    res = await agent.run("loop forever")
    assert res.status == "max_steps" and "3 steps" in (res.error or "")
    assert res.steps == 3 and len(llm.requests) == 3
    assert counter.calls == ["0", "1"]  # the 3rd turn's call was never executed


async def test_token_budget_stops_loop(tracer: Tracer) -> None:
    llm = ScriptedLLM(
        [
            tool_use_response([("echo", {"msg": "a"})], tokens_in=900, tokens_out=200),
            text_response("never reached"),
        ]
    )
    agent, _ = make_agent(llm, tracer)
    res = await agent.run("go", budget=RequestBudget(max_tokens=1000, max_seconds=60))
    assert res.status == "budget_exceeded" and "token" in (res.error or "")
    assert len(llm.requests) == 1


async def test_time_budget_aborts_slow_llm(tracer: Tracer) -> None:
    class Slow:
        async def complete(self, **_: Any) -> LLMResponse:
            await asyncio.sleep(5)
            return text_response("late")

    agent, _ = make_agent(Slow(), tracer)
    res = await agent.run("go", budget=RequestBudget(max_tokens=10_000, max_seconds=0.05))
    assert res.status == "budget_exceeded"


async def test_llm_error_is_captured_not_raised(tracer: Tracer, store: TraceStore) -> None:
    class Failing:
        async def complete(self, **_: Any) -> LLMResponse:
            raise LLMError("rate_limit", "slow down", retryable=True)

    agent, _ = make_agent(Failing(), tracer)
    with tracer.request() as rid:
        res = await agent.run("go")
    assert res.status == "error" and "rate_limit" in (res.error or "")
    spans = store.get_spans(rid)
    assert any(s.kind is SpanKind.LLM and s.status == "error" for s in spans)
    assert next(s for s in spans if s.kind is SpanKind.AGENT).status == "error"


async def test_truncation_is_an_error(tracer: Tracer) -> None:
    trunc = LLMResponse([{"type": "text", "text": "par"}], "max_tokens", TokenUsage(1, 1), "m")
    agent, _ = make_agent(ScriptedLLM([trunc]), tracer)
    res = await agent.run("x")
    assert res.status == "error" and "max_tokens" in (res.error or "")


async def test_assistant_blocks_are_replayed_unchanged(tracer: Tracer) -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    first = LLMResponse(
        [thinking, {"type": "tool_use", "id": "t1", "name": "echo", "input": {"msg": "a"}}],
        "tool_use",
        TokenUsage(1, 1),
        "m",
    )
    llm = ScriptedLLM([first, text_response("ok")])
    agent, _ = make_agent(llm, tracer)
    await agent.run("go")
    assistant_turn = llm.requests[1]["messages"][1]
    assert assistant_turn["content"][0] == thinking


async def test_tool_output_truncated(tracer: Tracer) -> None:
    class Big:
        async def echo(self, args: EchoIn) -> str:
            return "z" * 500

    llm = ScriptedLLM([tool_use_response([("echo", {"msg": "a"})]), text_response("ok")])
    tools = ToolSet([Tool("echo", "d", EchoIn, Big().echo)])
    agent = Agent(
        name="t",
        system_prompt="s",
        tools=tools,
        model="m",
        llm=llm,
        tracer=tracer,
        tool_result_max_chars=100,
    )
    await agent.run("go")
    content = llm.requests[1]["messages"][-1]["content"][0]["content"]
    assert content.startswith("z" * 100) and "truncated: 400" in content


async def test_trace_tree_tokens_prompt_version_and_redaction(
    tracer: Tracer, store: TraceStore
) -> None:
    llm = ScriptedLLM(
        [tool_use_response([("echo", {"msg": "s3cret-value-123"})]), text_response("done")]
    )
    agent, _ = make_agent(llm, tracer)
    with tracer.request() as rid:
        res = await agent.run("my key is s3cret-value-123")
    spans = store.get_spans(rid)
    agent_span = next(s for s in spans if s.kind is SpanKind.AGENT)
    assert agent_span.attributes["prompt_version"] == "t-v1"
    assert agent_span.attributes["status"] == "completed"
    children = [s for s in spans if s.parent_id == agent_span.span_id]
    assert sorted(s.kind.value for s in children) == ["llm", "llm", "tool"]
    assert sum(s.tokens_in for s in spans if s.kind is SpanKind.LLM) == res.tokens_in == 200
    assert all("s3cret" not in (s.input_preview + s.output_preview) for s in spans)
