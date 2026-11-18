"""The live processing stream: what the UI watches while a model works."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.livestream import emit, live_sink, scope
from app.core.llm import LLMError, OllamaLLM
from app.core.testing import text_response, tool_use_response
from app.orchestrator.events import MAX_WIRE_OUTPUT, live_event
from tests.orchestrator_fixtures import build_test_state, plan_response, step

# --- sink basics --------------------------------------------------------------------------


def test_emit_without_a_sink_is_a_silent_no_op() -> None:
    assert live_sink.get() is None
    emit("token", delta="x")  # must not raise


def test_emit_merges_scope_and_survives_a_broken_sink() -> None:
    seen: list[dict[str, Any]] = []
    t = live_sink.set(seen.append)
    try:
        with scope(step="s1", agent="transit"):
            emit("token", delta="hi")
        assert seen == [{"kind": "token", "step": "s1", "agent": "transit", "delta": "hi"}]

        def boom(_: dict[str, Any]) -> None:
            raise RuntimeError("observer bug")

        live_sink.set(boom)
        emit("token", delta="still fine")  # the request must not break
    finally:
        live_sink.reset(t)


def test_live_event_redacts_truncates_and_drops_junk() -> None:
    red = lambda s: s.replace("SECRET", "[REDACTED]")  # noqa: E731
    ev = live_event({"kind": "token", "step": "s1", "delta": "the SECRET is"}, red)
    assert ev is not None and ev.type == "token" and ev.delta == "the [REDACTED] is"
    big = live_event({"kind": "tool_result", "name": "t", "output": "x" * (MAX_WIRE_OUTPUT + 500)})
    assert big is not None and big.output.endswith("[+500 chars]")  # type: ignore[union-attr]
    call = live_event({"kind": "tool_call", "name": "t", "input": {"k": "SECRET"}}, red)
    assert call is not None and call.input == {"k": "[REDACTED]"}  # type: ignore[union-attr]
    assert live_event({"kind": "nonsense"}) is None
    assert live_event({"kind": "tool_call"}) is None  # missing required field


# --- streaming Ollama client --------------------------------------------------------------


def ndjson(*chunks: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(c) for c in chunks) + "\n").encode()


def streaming_llm(body: bytes, status: int = 200) -> tuple[OllamaLLM, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(status, content=body)

    return OllamaLLM(
        "http://o.test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ), seen


CHUNKS = (
    {"message": {"role": "assistant", "thinking": "The user wants "}, "done": False},
    {"message": {"role": "assistant", "thinking": "the weather."}, "done": False},
    {"message": {"role": "assistant", "content": "It will "}, "done": False},
    {"message": {"role": "assistant", "content": "rain."}, "done": False},
    {
        "message": {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "get_forecast", "arguments": {"hours": 6}}}],
        },
        "done": False,
    },
    {
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 42,
        "eval_count": 7,
        "model": "m",
    },
)


async def test_streaming_emits_deltas_and_returns_the_same_shape_as_non_streaming() -> None:
    llm, seen = streaming_llm(ndjson(*CHUNKS))
    events: list[dict[str, Any]] = []
    t = live_sink.set(events.append)
    try:
        r = await llm.complete(
            model="m",
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            max_tokens=50,
            think=True,
        )
    finally:
        live_sink.reset(t)
    assert seen[0]["stream"] is True and seen[0]["think"] is True
    assert [e["kind"] for e in events] == ["thought", "thought", "token", "token"]
    assert (
        "".join(e["delta"] for e in events if e["kind"] == "thought")
        == "The user wants the weather."
    )
    assert r.text == "It will rain." and [c.name for c in r.tool_calls] == ["get_forecast"]
    assert (r.usage.input_tokens, r.usage.output_tokens) == (42, 7)
    assert r.extra["thinking"] == "The user wants the weather." and r.stop_reason == "tool_use"


async def test_without_a_sink_the_client_stays_non_streaming() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "ok"}, "done": True, "model": "m"})

    llm = OllamaLLM(
        "http://o.test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    r = await llm.complete(
        model="m", system="s", messages=[{"role": "user", "content": "q"}], tools=[], max_tokens=5
    )
    assert seen[0]["stream"] is False and r.text == "ok"


async def test_a_malformed_line_does_not_end_the_answer() -> None:
    body = ndjson(CHUNKS[2]) + b"{not json\n" + ndjson(CHUNKS[3], CHUNKS[-1])
    llm, _ = streaming_llm(body)
    t = live_sink.set(lambda _: None)
    try:
        r = await llm.complete(
            model="m",
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            max_tokens=5,
        )
    finally:
        live_sink.reset(t)
    assert r.text == "It will rain."


@pytest.mark.parametrize(("status", "kind"), [(404, "model_missing"), (500, "api_error")])
async def test_streaming_errors_map_to_the_same_llm_errors(status: int, kind: str) -> None:
    llm, _ = streaming_llm(b'{"error":"x"}', status)
    t = live_sink.set(lambda _: None)
    try:
        with pytest.raises(LLMError) as ei:
            await llm.complete(
                model="m",
                system="s",
                messages=[{"role": "user", "content": "q"}],
                tools=[],
                max_tokens=5,
            )
    finally:
        live_sink.reset(t)
    assert ei.value.kind == kind


# --- through the orchestrator -------------------------------------------------------------


async def run_events(
    tmp_path: Path, *, detail: bool, reasoning: bool = False, agent: str | None = None
) -> tuple[list[Any], Any]:
    state, llm = build_test_state(tmp_path)
    if agent is None:
        llm.script(
            "router", plan_response("transit", [step("s1", "transit", "next departures from A")])
        )
    llm.script(
        "transit",
        tool_use_response([("next_departures", {"stop_id": "A", "limit": 1})]),
        text_response("Bus at 08:05."),
    )
    llm.script("synth", text_response("Your bus is at 08:05."))
    events = [
        e async for e in state.orchestrator.run("s", "next bus from A?", agent, reasoning, detail)
    ]
    return events, llm


async def test_detail_off_means_exactly_the_classic_events(tmp_path: Path) -> None:
    events, _ = await run_events(tmp_path, detail=False)
    assert [e.type for e in events] == ["plan", "step_started", "step_result", "final"]


async def test_detail_on_adds_tool_calls_results_and_usage_in_a_sensible_order(
    tmp_path: Path,
) -> None:
    events, _ = await run_events(tmp_path, detail=True)
    kinds = [e.type for e in events]
    for needed in ("llm_started", "tool_call", "tool_result", "llm_usage"):
        assert needed in kinds
    call = next(e for e in events if e.type == "tool_call")
    result = next(e for e in events if e.type == "tool_result")
    assert (
        call.name == "next_departures"
        and call.input["stop_id"] == "A"
        and call.agent == "transit"
        and call.step == "s1"
    )
    assert result.call_id == call.call_id and not result.is_error and "departures" in result.output
    assert kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("step_result")
    # the router and the synthesiser announce themselves too
    started = [(e.step, e.agent) for e in events if e.type == "llm_started"]
    assert (
        ("router", "router") in started
        and ("s1", "transit") in started
        and ("synth", "synthesiser") in started
    )
    assert kinds[-1] == "final"  # the classic ending is unchanged


async def test_direct_mode_has_no_router_events(tmp_path: Path) -> None:
    events, _ = await run_events(tmp_path, detail=True, agent="transit")
    assert not any(getattr(e, "step", "") == "router" for e in events)


async def test_reasoning_switch_turns_thinking_on_for_agents_and_synthesis_but_not_the_router(
    tmp_path: Path,
) -> None:
    _, llm = await run_events(tmp_path, detail=False, reasoning=True)
    assert llm.requests["transit"][0]["think"] is True and llm.requests["synth"][0]["think"] is True
    assert llm.requests["router"][0]["think"] is not True  # latency: never overridden
    _, llm2 = await run_events(tmp_path, detail=False, reasoning=False)
    assert llm2.requests["transit"][0]["think"] is not True


async def test_secrets_never_reach_the_stream(tmp_path: Path) -> None:
    state, llm = build_test_state(tmp_path)
    llm.script("router", plan_response("transit", [step("s1", "transit", "departures")]))
    llm.script(
        "transit",
        tool_use_response(
            [("next_departures", {"stop_id": "A", "note": "Bearer abcdefghijklmnop"})]
        ),
        text_response("ok"),
    )
    llm.script("synth", text_response("done"))
    events = [e async for e in state.orchestrator.run("s", "x", None, False, True)]
    wire = " ".join(e.model_dump_json() for e in events)
    assert "abcdefghijklmnop" not in wire
