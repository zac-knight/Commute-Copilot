"""Thin wrapper over a local Ollama server (https://ollama.com), the only LLM backend.

Internally the app uses ONE message format (Anthropic-style content blocks: ``text``,
``tool_use``, ``tool_result``) because it is simple and explicit. This module translates it
to Ollama's native ``/api/chat`` format and back, so the agent loop knows nothing about
Ollama. Tracing is done by the caller (the agent loop), so a scripted fake is traced
identically to the real client.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.livestream import emit, streaming

log = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    content: list[dict[str, Any]]  # text / tool_use blocks; appended as the assistant turn
    stop_reason: str | None  # "end_turn" | "tool_use" | "max_tokens"
    usage: TokenUsage
    model: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(id=b["id"], name=b["name"], input=b.get("input") or {})
            for b in self.content
            if b.get("type") == "tool_use"
        ]


class LLM(Protocol):
    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        think: bool | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse: ...


# --- format translation (pure functions, unit-tested) ------------------------------------


def to_ollama_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    tool_names: dict[str, str] = {}  # tool_use id -> name, for matching results
    for msg in messages:
        content = msg["content"]
        if msg["role"] == "assistant":
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            calls = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    tool_names[b["id"]] = b["name"]
                    calls.append({"function": {"name": b["name"], "arguments": b["input"]}})
            turn: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
            }
            if calls:
                turn["tool_calls"] = calls
            out.append(turn)
        elif isinstance(content, str):
            out.append({"role": "user", "content": content})
        else:
            for b in content:
                if b.get("type") == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_name": tool_names.get(b["tool_use_id"], ""),
                            "content": b["content"],
                        }
                    )
                elif b.get("type") == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


def to_ollama_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def from_ollama_response(data: dict[str, Any], model: str) -> LLMResponse:
    msg = data.get("message") or {}
    blocks: list[dict[str, Any]] = []
    if text := (msg.get("content") or ""):
        blocks.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = call["function"]
        args = fn.get("arguments") or {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "name": fn["name"],
                "input": args if isinstance(args, dict) else {},
            }
        )
    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    elif data.get("done_reason") == "length":
        stop = "max_tokens"
    else:
        stop = "end_turn"
    usage = TokenUsage(data.get("prompt_eval_count") or 0, data.get("eval_count") or 0)
    thinking = msg.get("thinking") or data.get("_thinking") or ""
    extra = {"thinking": thinking} if thinking else {}
    return LLMResponse(blocks, stop, usage, data.get("model") or model, extra)


# --- client ------------------------------------------------------------------------------


class OllamaLLM:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        timeout: float = 120.0,
        num_ctx: int = 8192,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._num_ctx = num_ctx
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        think: bool | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        # Ollama silently truncates prompts beyond num_ctx (default 4096), which would
        # corrupt tool-heavy conversations, so it is always set explicitly.
        body: dict[str, Any] = {
            "model": model,
            "messages": to_ollama_messages(system, messages),
            "stream": False,
            "options": {"num_ctx": self._num_ctx, "num_predict": max_tokens},
        }
        if tools:
            body["tools"] = to_ollama_tools(tools)
        if think is not None:
            body["think"] = think
        if json_schema is not None:
            body["format"] = json_schema  # schema-constrained decoding (verified live)
        if streaming():
            data = await self._stream(body, model)
        else:
            data = await self._post(body, model)
        result = from_ollama_response(data, model)
        log.debug(
            "llm call model=%s in=%d out=%d",
            model,
            result.usage.input_tokens,
            result.usage.output_tokens,
        )
        return result

    def _raise_for(self, status: int, text: str, model: str) -> None:
        if status == 404:
            raise LLMError("model_missing", f"Model {model!r} not found. Run: ollama pull {model}")
        if status >= 400:
            raise LLMError("api_error", f"{status}: {text[:300]}", retryable=status >= 500)

    async def _post(self, body: dict[str, Any], model: str) -> dict[str, Any]:
        try:
            resp = await self._client.post(f"{self._base_url}/api/chat", json=body)
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", f"Ollama timed out: {exc!r}", retryable=True) from exc
        except httpx.ConnectError as exc:
            raise LLMError(
                "connection",
                f"Cannot reach Ollama at {self._base_url}. Is it running? (`ollama serve`)",
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError("connection", f"Ollama request failed: {exc!r}", retryable=True) from exc
        self._raise_for(resp.status_code, resp.text, model)
        data: dict[str, Any] = resp.json()
        return data

    async def _stream(self, body: dict[str, Any], model: str) -> dict[str, Any]:
        """Same request with ``stream: true``: reasoning and answer deltas are emitted to the live
        sink as they arrive; the aggregate is returned in the non-streaming response shape."""
        body = {**body, "stream": True}
        content: list[str] = []
        thinking: list[str] = []
        calls: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        try:
            async with self._client.stream("POST", f"{self._base_url}/api/chat", json=body) as resp:
                if resp.status_code >= 400:
                    self._raise_for(resp.status_code, (await resp.aread()).decode(), model)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue  # a malformed line must not end the answer
                    msg = chunk.get("message") or {}
                    if t := msg.get("thinking"):
                        thinking.append(t)
                        emit("thought", delta=t)
                    if c := msg.get("content"):
                        content.append(c)
                        emit("token", delta=c)
                    calls += msg.get("tool_calls") or []
                    if chunk.get("done"):
                        final = chunk
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", f"Ollama timed out: {exc!r}", retryable=True) from exc
        except httpx.ConnectError as exc:
            raise LLMError(
                "connection",
                f"Cannot reach Ollama at {self._base_url}. Is it running? (`ollama serve`)",
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError("connection", f"Ollama request failed: {exc!r}", retryable=True) from exc
        return {
            "message": {"content": "".join(content), "tool_calls": calls},
            "done_reason": final.get("done_reason"),
            "prompt_eval_count": final.get("prompt_eval_count"),
            "eval_count": final.get("eval_count"),
            "model": final.get("model") or model,
            "_thinking": "".join(thinking),
        }

    async def list_models(self) -> list[str]:
        """Names of the models installed in Ollama (empty if the server is unreachable)."""
        try:
            r = await self._client.get(f"{self._base_url}/api/tags", timeout=3.0)
            r.raise_for_status()
            return sorted(m["name"] for m in r.json().get("models", []))
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def ping(self) -> bool:
        """Is the Ollama server reachable? (used by /health)"""
        try:
            r = await self._client.get(f"{self._base_url}/api/version", timeout=1.5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
