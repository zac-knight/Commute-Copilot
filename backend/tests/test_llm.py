from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.llm import (
    LLMError,
    OllamaLLM,
    from_ollama_response,
    to_ollama_messages,
    to_ollama_tools,
)

TOOL_DEF = {
    "name": "echo",
    "description": "Echo.",
    "input_schema": {"type": "object", "properties": {"msg": {"type": "string"}}},
}


def make_llm(handler: Any) -> tuple[OllamaLLM, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return handler(request)  # type: ignore[no-any-return]

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return OllamaLLM("http://ollama.test", num_ctx=4096, client=client), seen


def ok(data: dict[str, Any]) -> Any:
    return lambda _req: httpx.Response(200, json=data)


def test_messages_translated_with_tool_roundtrip() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t1", "name": "echo", "input": {"msg": "a"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "A"}]},
    ]
    assert to_ollama_messages("sys", messages) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [{"function": {"name": "echo", "arguments": {"msg": "a"}}}],
        },
        {"role": "tool", "tool_name": "echo", "content": "A"},
    ]


def test_tool_definitions_translated() -> None:
    (t,) = to_ollama_tools([TOOL_DEF])
    assert t["type"] == "function" and t["function"]["parameters"] == TOOL_DEF["input_schema"]


def test_response_with_tool_calls_and_usage() -> None:
    r = from_ollama_response(
        {
            "model": "m",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "echo", "arguments": {"msg": "a"}}}],
            },
            "done_reason": "stop",
            "prompt_eval_count": 11,
            "eval_count": 7,
        },
        "m",
    )
    assert r.stop_reason == "tool_use" and r.tool_calls[0].name == "echo"
    assert r.tool_calls[0].id.startswith("call_")
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.total) == (11, 7, 18)


def test_response_length_means_max_tokens() -> None:
    r = from_ollama_response({"message": {"content": "par"}, "done_reason": "length"}, "m")
    assert r.stop_reason == "max_tokens" and r.text == "par"


async def test_complete_request_shape() -> None:
    llm, seen = make_llm(ok({"message": {"content": "hi"}, "done_reason": "stop"}))
    resp = await llm.complete(
        model="qwen3:8b",
        system="s",
        messages=[{"role": "user", "content": "x"}],
        tools=[TOOL_DEF],
        max_tokens=100,
        think=False,
    )
    assert resp.text == "hi" and resp.stop_reason == "end_turn"
    body = seen[0]
    assert body["stream"] is False and body["think"] is False
    assert body["options"] == {"num_ctx": 4096, "num_predict": 100}
    assert body["tools"][0]["function"]["name"] == "echo"


async def test_no_tools_omits_tools_and_think() -> None:
    llm, seen = make_llm(ok({"message": {"content": "hi"}}))
    await llm.complete(model="m", system="s", messages=[], tools=[], max_tokens=10)
    assert "tools" not in seen[0] and "think" not in seen[0]


async def test_model_missing_gives_pull_hint() -> None:
    llm, _ = make_llm(lambda _r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(LLMError, match="ollama pull qwen3:8b") as info:
        await llm.complete(model="qwen3:8b", system="s", messages=[], tools=[], max_tokens=10)
    assert info.value.kind == "model_missing"


async def test_server_down_gives_clear_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    llm, _ = make_llm(refuse)
    with pytest.raises(LLMError, match="ollama serve") as info:
        await llm.complete(model="m", system="s", messages=[], tools=[], max_tokens=10)
    assert info.value.kind == "connection" and not info.value.retryable


async def test_timeout_and_5xx_are_retryable() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    llm, _ = make_llm(slow)
    with pytest.raises(LLMError) as info:
        await llm.complete(model="m", system="s", messages=[], tools=[], max_tokens=10)
    assert info.value.kind == "timeout" and info.value.retryable

    llm, _ = make_llm(lambda _r: httpx.Response(500, text="oops"))
    with pytest.raises(LLMError) as info2:
        await llm.complete(model="m", system="s", messages=[], tools=[], max_tokens=10)
    assert info2.value.kind == "api_error" and info2.value.retryable
