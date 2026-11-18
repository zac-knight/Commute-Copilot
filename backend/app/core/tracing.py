"""Span model, secret redaction and a SQLite trace store.

Spans nest through ``contextvars`` so parent/child links survive ``asyncio.gather``
(each task copies the current context). Every write goes through :func:`redact`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class SpanKind(StrEnum):
    LLM = "llm"
    TOOL = "tool"
    AGENT = "agent"
    ORCHESTRATOR = "orchestrator"


class Span(BaseModel):
    request_id: str
    span_id: str
    parent_id: str | None = None
    kind: SpanKind
    name: str
    start: float
    end: float | None = None
    status: str = "ok"  # "ok" | "error"
    input_preview: str = ""
    output_preview: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None
    cost_estimate: float | None = None
    error: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)  # e.g. prompt_version


# --- redaction -------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),  # bearer tokens
    re.compile(r"ya29\.[A-Za-z0-9._\-]{8,}"),  # Google OAuth access tokens
    re.compile(r"1//[A-Za-z0-9._\-]{20,}"),  # Google OAuth refresh tokens
]
REDACTED = "[REDACTED]"


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove known secret values and common token shapes from ``text``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def preview(value: Any, limit: int = 500, secrets: Iterable[str] = ()) -> str:
    """Compact, redacted, truncated string form of ``value`` for storing in a span."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(value)
    text = redact(text, secrets)
    if len(text) > limit:
        omitted = len(text) - limit
        return f"{text[:limit]}…[+{omitted} chars]"
    return text


# --- store -----------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    start REAL NOT NULL,
    "end" REAL,
    status TEXT NOT NULL,
    input_preview TEXT NOT NULL,
    output_preview TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    model TEXT,
    cost_estimate REAL,
    error TEXT,
    attributes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spans_request ON spans(request_id, start);
"""


class RequestSummary(BaseModel):
    """Per-request totals. Tokens count LLM spans ONLY: agent/orchestrator spans carry a rollup of
    their children's tokens for display, and summing both would double count."""

    request_id: str
    start: float
    end: float | None
    span_count: int
    error_count: int
    tokens_in: int
    tokens_out: int
    cost_estimate: float


class TraceStore:
    """Thread-safe SQLite writer/reader for spans. ``":memory:"`` is allowed for tests."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def write(self, span: Span) -> None:
        row = span.model_dump(mode="json")
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO spans VALUES
                (:span_id,:request_id,:parent_id,:kind,:name,:start,:end,:status,
                 :input_preview,:output_preview,:tokens_in,:tokens_out,:model,
                 :cost_estimate,:error,:attributes)""",
                {**row, "attributes": json.dumps(row["attributes"], default=str)},
            )

    def usage_since(self, since: float) -> dict[str, dict[str, float]]:
        """Per agent and per tool call counts, error counts and mean duration (s) since ``since``
        (epoch s). Keys: ``agent:<name>`` and ``tool:<name>``. For the Agents page."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT kind, name, COUNT(*), SUM(status='error'), AVG("end" - start),
                          SUM(tokens_in), SUM(tokens_out)
                   FROM spans WHERE kind IN ('agent','tool') AND start >= ? AND "end" IS NOT NULL
                   GROUP BY kind, name""",
                (since,),
            ).fetchall()
        return {
            f"{kind}:{name}": {
                "calls": n, "errors": errs or 0, "mean_s": mean or 0.0,
                "tokens_in": tin or 0, "tokens_out": tout or 0,
            }
            for kind, name, n, errs, mean, tin, tout in rows
        }  # fmt: skip

    def get_spans(self, request_id: str) -> list[Span]:
        with self._lock:
            cur = self._conn.execute(
                'SELECT span_id,request_id,parent_id,kind,name,start,"end",status,'
                "input_preview,output_preview,tokens_in,tokens_out,model,cost_estimate,"
                "error,attributes FROM spans WHERE request_id=? ORDER BY start, rowid",
                (request_id,),
            )
            rows = cur.fetchall()
        return [
            Span(
                span_id=r[0],
                request_id=r[1],
                parent_id=r[2],
                kind=SpanKind(r[3]),
                name=r[4],
                start=r[5],
                end=r[6],
                status=r[7],
                input_preview=r[8],
                output_preview=r[9],
                tokens_in=r[10],
                tokens_out=r[11],
                model=r[12],
                cost_estimate=r[13],
                error=r[14],
                attributes=json.loads(r[15]),
            )
            for r in rows
        ]

    def request_summary(self, request_id: str) -> RequestSummary | None:
        with self._lock:
            r = self._conn.execute(
                """SELECT request_id, MIN(start), MAX("end"), COUNT(*), SUM(status='error'),
                          SUM(CASE WHEN kind='llm' THEN tokens_in ELSE 0 END),
                          SUM(CASE WHEN kind='llm' THEN tokens_out ELSE 0 END),
                          COALESCE(SUM(cost_estimate), 0)
                   FROM spans WHERE request_id=? GROUP BY request_id""",
                (request_id,),
            ).fetchone()
        if r is None:
            return None
        return RequestSummary(
            request_id=r[0], start=r[1], end=r[2], span_count=r[3], error_count=r[4] or 0,
            tokens_in=r[5] or 0, tokens_out=r[6] or 0, cost_estimate=r[7],
        )  # fmt: skip

    def recent_requests(self, limit: int = 50) -> list[RequestSummary]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT request_id, MIN(start), MAX("end"), COUNT(*),
                          SUM(status='error'), SUM(CASE WHEN kind='llm' THEN tokens_in ELSE 0 END),
                          SUM(CASE WHEN kind='llm' THEN tokens_out ELSE 0 END),
                          COALESCE(SUM(cost_estimate), 0)
                   FROM spans GROUP BY request_id ORDER BY MIN(start) DESC LIMIT ?""",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            RequestSummary(
                request_id=r[0],
                start=r[1],
                end=r[2],
                span_count=r[3],
                error_count=r[4] or 0,
                tokens_in=r[5] or 0,
                tokens_out=r[6] or 0,
                cost_estimate=r[7],
            )
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def span_tree(spans: list[Span]) -> list[dict[str, Any]]:
    """Nest spans by parent_id. Roots are spans whose parent is absent from the list. Children keep
    start-time order. Used by the trace viewer API."""
    nodes = {s.span_id: {**s.model_dump(mode="json"), "children": []} for s in spans}
    roots: list[dict[str, Any]] = []
    for s in sorted(spans, key=lambda x: x.start):
        node = nodes[s.span_id]
        parent = nodes.get(s.parent_id) if s.parent_id else None
        (parent["children"] if parent is not None else roots).append(node)
    return roots


# --- tracer ----------------------------------------------------------------------------

_current_request: ContextVar[str | None] = ContextVar("trace_request_id", default=None)
_current_span: ContextVar[str | None] = ContextVar("trace_span_id", default=None)


class SpanHandle:
    """Mutable view of an in-flight span; the tracer writes it when the block exits."""

    def __init__(self, span: Span, limit: int, secrets: Sequence[str]) -> None:
        self.span = span
        self._limit = limit
        self._secrets = secrets

    def set_input(self, value: Any) -> None:
        self.span.input_preview = preview(value, self._limit, self._secrets)

    def set_output(self, value: Any) -> None:
        self.span.output_preview = preview(value, self._limit, self._secrets)

    def set_usage(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
        cost: float | None = None,
        model: str | None = None,
    ) -> None:
        self.span.tokens_in = tokens_in
        self.span.tokens_out = tokens_out
        self.span.cost_estimate = cost
        if model:
            self.span.model = model

    def set_attr(self, key: str, value: Any) -> None:
        self.span.attributes[key] = value

    def set_error(self, message: str) -> None:
        self.span.status = "error"
        self.span.error = redact(message, self._secrets)[: self._limit * 2]


class Tracer:
    def __init__(
        self, store: TraceStore, *, preview_chars: int = 500, secrets: Sequence[str] = ()
    ) -> None:
        self.store = store
        self._limit = preview_chars
        self._secrets = list(secrets)

    @staticmethod
    def current_request_id() -> str | None:
        return _current_request.get()

    @contextmanager
    def request(self, request_id: str | None = None) -> Iterator[str]:
        """Start a request scope; spans opened inside share this request id."""
        rid = request_id or uuid.uuid4().hex
        req_token = _current_request.set(rid)
        span_token = _current_span.set(None)
        try:
            yield rid
        finally:
            _current_span.reset(span_token)
            _current_request.reset(req_token)

    @contextmanager
    def span(
        self,
        kind: SpanKind,
        name: str,
        *,
        input: Any = None,
        model: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[SpanHandle]:
        """Open a span. Exceptions are recorded on the span and re-raised."""
        rid = _current_request.get()
        owns_request = rid is None
        if owns_request:
            rid = uuid.uuid4().hex
            req_token = _current_request.set(rid)
        assert rid is not None
        span = Span(
            request_id=rid,
            span_id=uuid.uuid4().hex[:16],
            parent_id=_current_span.get(),
            kind=kind,
            name=name,
            start=time.time(),
            model=model,
            attributes=dict(attributes or {}),
        )
        handle = SpanHandle(span, self._limit, self._secrets)
        if input is not None:
            handle.set_input(input)
        span_token = _current_span.set(span.span_id)
        try:
            yield handle
        except BaseException as exc:
            handle.set_error(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            _current_span.reset(span_token)
            span.end = time.time()
            self.store.write(span)
            if owns_request:
                _current_request.reset(req_token)
