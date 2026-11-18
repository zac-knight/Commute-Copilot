from __future__ import annotations

import asyncio

import pytest

from app.core.tracing import REDACTED, SpanKind, Tracer, TraceStore, preview, redact


def test_redact_known_secret_and_token_shapes() -> None:
    text = "key sk-ant-api03-ABCDEFGH12345 and Bearer abcdefgh12345 and s3cret-value-123"
    out = redact(text, ["s3cret-value-123"])
    assert "sk-ant" not in out and "abcdefgh12345" not in out and "s3cret" not in out
    assert out.count(REDACTED) == 3


def test_preview_truncates_and_redacts() -> None:
    out = preview({"k": "x" * 1000}, limit=50)
    assert len(out) < 100 and "+" in out and "chars" in out
    assert REDACTED in preview("sk-ant-abcdefghijk", 100)


def test_nested_spans_and_roundtrip(tracer: Tracer, store: TraceStore) -> None:
    with tracer.request() as rid, tracer.span(SpanKind.AGENT, "a", input={"q": 1}) as a:
        a.set_attr("prompt_version", "v1")
        with tracer.span(SpanKind.LLM, "l", model="m") as s:
            s.set_usage(tokens_in=10, tokens_out=5, cost=0.01)
            s.set_output("hi")
    spans = {s.name: s for s in store.get_spans(rid)}
    assert spans["l"].parent_id == spans["a"].span_id
    assert spans["a"].parent_id is None
    assert spans["a"].attributes == {"prompt_version": "v1"}
    assert spans["l"].tokens_in == 10 and spans["l"].cost_estimate == 0.01
    assert spans["l"].end is not None and spans["l"].end >= spans["l"].start


def test_exception_recorded_and_reraised(tracer: Tracer, store: TraceStore) -> None:
    with pytest.raises(ValueError), tracer.request() as rid, tracer.span(SpanKind.TOOL, "t"):
        raise ValueError("bad s3cret-value-123")
    (span,) = store.get_spans(rid)
    assert span.status == "error" and "ValueError" in (span.error or "")
    assert "s3cret" not in (span.error or "")


async def test_parallel_children_share_parent(tracer: Tracer, store: TraceStore) -> None:
    async def child(n: int) -> None:
        with tracer.span(SpanKind.TOOL, f"c{n}"):
            await asyncio.sleep(0.01)

    with tracer.request() as rid, tracer.span(SpanKind.AGENT, "parent"):
        await asyncio.gather(child(1), child(2))
    spans = store.get_spans(rid)
    parent = next(s for s in spans if s.name == "parent")
    kids = [s for s in spans if s.name.startswith("c")]
    assert len(kids) == 2 and all(k.parent_id == parent.span_id for k in kids)


def test_span_without_request_gets_its_own(tracer: Tracer, store: TraceStore) -> None:
    with tracer.span(SpanKind.ORCHESTRATOR, "solo"):
        pass
    (summary,) = store.recent_requests()
    assert summary.span_count == 1 and Tracer.current_request_id() is None


def test_recent_requests_aggregates(tracer: Tracer, store: TraceStore) -> None:
    with tracer.request() as rid:
        with tracer.span(SpanKind.LLM, "a") as s:
            s.set_usage(tokens_in=3, tokens_out=2, cost=0.5)
        with pytest.raises(RuntimeError), tracer.span(SpanKind.TOOL, "b"):
            raise RuntimeError("x")
    (summary,) = store.recent_requests()
    assert summary.request_id == rid
    assert (summary.span_count, summary.error_count) == (2, 1)
    assert (summary.tokens_in, summary.tokens_out, summary.cost_estimate) == (3, 2, 0.5)


def test_summary_counts_tokens_from_llm_spans_only_not_the_agent_rollup(
    tracer: Tracer, store: TraceStore
) -> None:
    """Regression: an agent span carries its LLM calls' tokens as a rollup; summing every span
    double counted (live: 7,730 reported vs 4,804 real)."""
    with tracer.request() as rid, tracer.span(SpanKind.AGENT, "agent") as agent:
        for _ in range(2):
            with tracer.span(SpanKind.LLM, "llm") as s:
                s.set_usage(tokens_in=100, tokens_out=10)
        agent.set_usage(tokens_in=200, tokens_out=20)  # the rollup of the two calls above
    for summary in (store.request_summary(rid), store.recent_requests()[0]):
        assert summary is not None
        assert (summary.tokens_in, summary.tokens_out) == (200, 20)
        assert summary.span_count == 3
