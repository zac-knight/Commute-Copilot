"""FastAPI app: chat (SSE), approvals, traces, health, eval.

Single user, no authentication: bind to localhost only. CORS allows only the local frontend origin.
Errors are typed: ``{"error": {"type": ..., "message": ...}}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.analytics.analysis import summarise
from app.analytics.store import DelayStore
from app.config import ROLES, Settings, get_settings
from app.core.approvals import (
    ActionStatus,
    ApprovalError,
    HashMismatchError,
    InvalidTransitionError,
    NotFoundError,
    PendingAction,
    TooManyPendingError,
)
from app.core.tracing import RequestSummary, span_tree
from app.demo.state import build_demo_state
from app.evaluation.runner import RESULTS_DIR
from app.evaluation.summary import latest_summary
from app.orchestrator.events import to_sse
from app.orchestrator.wiring import AppState, build_state, gtfs_refresh_loop
from app.planner_api import make_router as planner_router

SESSION_ID = r"^[A-Za-z0-9_.:-]+$"
StatusFilter = Literal["pending", "approved", "rejected", "expired", "executed", "failed"]


AGENT_META: dict[str, dict[str, str]] = {
    "transit": {
        "title": "Transit",
        "prompt_version": "transit-v1",
        "source": "TransLink SEQ GTFS (static, SQLite) + GTFS-Realtime (TripUpdates, alerts)",
        "summary": "Departures, delays, cancellations, journey planning and service alerts.",
    },
    "weather": {
        "title": "Weather",
        "prompt_version": "weather-v1",
        "source": "Open-Meteo hourly forecast (free, no key), cached 10 min",
        "summary": "Forecast and a deterministic leave-now-or-wait decision for a trip.",
    },
    "calendar": {
        "title": "Calendar",
        "prompt_version": "calendar-v1",
        "source": "Local calendar + read-only .ics import",
        "summary": "Finds free time and conflicts; proposes changes that you approve.",
    },
}


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=SESSION_ID)
    message: str = Field(min_length=1, max_length=2000)
    # Talk to one specialist directly (the router is skipped); None = Auto.
    agent: Literal["transit", "weather", "calendar"] | None = None
    # Let thinking-capable models reason first (slower; the reasoning streams to the Inspector).
    reasoning: bool = False
    # Also stream the processing detail (tokens, tool calls, usage). Off = the classic events only.
    detail: bool = False


class ApproveRequest(BaseModel):
    payload_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class EditRequest(BaseModel):
    payload: dict[str, Any]


HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class SettingsIn(BaseModel):
    home: str = Field(min_length=1, max_length=100)
    destinations: dict[str, str] = Field(default_factory=dict, max_length=20)
    working_hours_start: str = Field(pattern=HHMM)
    working_hours_end: str = Field(pattern=HHMM)
    buffer_minutes: int = Field(ge=0, le=120)
    router_model: str | None = Field(default=None, max_length=100)


class SettingsOut(BaseModel):
    home: str
    destinations: dict[str, str]
    working_hours_start: str
    working_hours_end: str
    buffer_minutes: int
    router_model: str
    available_models: list[str]
    home_resolves_to: str | None = None  # the stop/station name the home text matched


class ApprovalResult(BaseModel):
    action: PendingAction


class TraceDetail(BaseModel):
    request_id: str
    summary: RequestSummary
    tree: list[dict[str, Any]]


def _error(status: int, kind: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"type": kind, "message": message, **extra}}
    )


def create_app(state: AppState | None = None, *, settings: Settings | None = None) -> FastAPI:
    """``state`` given: used as is (tests). Otherwise built from settings at start-up."""
    cfg = settings or (state.settings if state else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = state is None
        st = state or (
            build_demo_state(cfg, speed=cfg.demo_speed) if cfg.demo else build_state(cfg)
        )
        app.state.app_state = st
        refresh: asyncio.Task[None] | None = None
        if owned and cfg.gtfs_auto_refresh and not cfg.demo:
            refresh = asyncio.create_task(gtfs_refresh_loop(st))
        try:
            yield
        finally:
            if refresh is not None:
                refresh.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresh
            if owned:
                await st.aclose()

    app = FastAPI(title="Commute Agents", lifespan=lifespan)
    if state is not None:  # usable without running the lifespan (plain TestClient)
        app.state.app_state = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[cfg.frontend_origin],  # the local frontend only
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    def st(request: Request) -> AppState:
        s: AppState | None = getattr(request.app.state, "app_state", None)
        if s is None:
            raise HTTPException(status_code=503, detail="application is starting")
        return s

    app.include_router(planner_router(lambda request: st(request)))

    # ------------------------------------------------------------ error handling

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
                   for e in exc.errors()]  # fmt: skip
        return _error(422, "validation_error", "the request is not valid", details=details)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        kind = {404: "not_found", 503: "unavailable"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, kind, str(exc.detail))

    @app.exception_handler(NotFoundError)
    async def _nf(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(InvalidTransitionError)
    async def _bad_transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return _error(409, "invalid_state", str(exc))

    @app.exception_handler(HashMismatchError)
    async def _hash(_: Request, exc: HashMismatchError) -> JSONResponse:
        return _error(409, "payload_changed", str(exc))

    @app.exception_handler(TooManyPendingError)
    async def _many(_: Request, exc: TooManyPendingError) -> JSONResponse:
        return _error(429, "too_many_pending", str(exc))

    @app.exception_handler(ApprovalError)
    async def _approval(_: Request, exc: ApprovalError) -> JSONResponse:
        return _error(400, "approval_error", str(exc))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        return _error(500, "internal_error", "unexpected error")  # details stay in the logs

    # ------------------------------------------------------------ chat (SSE)

    @app.post("/chat")
    async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
        s = st(request)

        async def frames() -> AsyncIterator[str]:
            async for event in s.orchestrator.run(
                req.session_id, req.message, req.agent, req.reasoning, req.detail
            ):
                yield to_sse(event)

        return StreamingResponse(
            frames(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )  # fmt: skip

    # ------------------------------------------------------------ approvals

    @app.get("/approvals")
    async def list_approvals(
        request: Request,
        status: StatusFilter | None = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> list[PendingAction]:
        gate = st(request).gate
        return await asyncio.to_thread(
            gate.list_actions, ActionStatus(status) if status else None, limit
        )

    @app.post("/approvals/{action_id}/approve")
    async def approve(action_id: str, body: ApproveRequest, request: Request) -> ApprovalResult:
        """Approve the exact payload the user was shown (its hash), then execute it."""
        gate = st(request).gate

        def work() -> PendingAction:
            gate.approve(action_id, body.payload_hash)
            return gate.execute_approved_action(action_id)

        return ApprovalResult(action=await asyncio.to_thread(work))

    @app.post("/approvals/{action_id}/reject")
    async def reject(action_id: str, body: RejectRequest, request: Request) -> ApprovalResult:
        gate = st(request).gate
        return ApprovalResult(action=await asyncio.to_thread(gate.reject, action_id, body.reason))

    @app.post("/approvals/{action_id}/edit")
    async def edit(action_id: str, body: EditRequest, request: Request) -> ApprovalResult:
        gate = st(request).gate
        try:
            return ApprovalResult(
                action=await asyncio.to_thread(gate.edit, action_id, body.payload)
            )
        except ValueError as exc:  # the edited payload failed validation
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ------------------------------------------------------------ traces

    @app.get("/traces")
    async def traces(
        request: Request, limit: int = Query(50, ge=1, le=500)
    ) -> list[RequestSummary]:
        return await asyncio.to_thread(st(request).store.recent_requests, limit)

    @app.get("/traces/{request_id}")
    async def trace(request_id: str, request: Request) -> TraceDetail:
        store = st(request).store
        spans = await asyncio.to_thread(store.get_spans, request_id)
        summary = await asyncio.to_thread(store.request_summary, request_id)
        if not spans or summary is None:
            raise HTTPException(status_code=404, detail=f"no trace {request_id!r}")
        return TraceDetail(request_id=request_id, summary=summary, tree=span_tree(spans))

    # ------------------------------------------------------------ agents

    @app.get("/agents")
    async def agents(request: Request) -> list[dict[str, Any]]:
        """Card data for each specialist: tools + permissions (from the registry, i.e. what code
        enforces), data source, prompt version, availability, 24 h usage from the trace store."""
        s = st(request)
        usage = await asyncio.to_thread(s.store.usage_since, time.time() - 86400)
        tools = s.registry.describe()
        out = []
        for name, meta in AGENT_META.items():
            u = usage.get(f"agent:{name}", {})
            out.append({
                "name": name, **meta, "available": name in s.available,
                "tools": [
                    {**t, "calls_24h": int(usage.get(f"tool:{t['name']}", {}).get("calls", 0)),
                     "errors_24h": int(usage.get(f"tool:{t['name']}", {}).get("errors", 0))}
                    for t in tools.get(name, [])
                ],
                "calls_24h": int(u.get("calls", 0)), "errors_24h": int(u.get("errors", 0)),
                "mean_s_24h": round(u.get("mean_s", 0.0), 2) if u else None,
                "tokens_in_24h": int(u.get("tokens_in", 0)),
                "tokens_out_24h": int(u.get("tokens_out", 0)),
            })  # fmt: skip
        return out

    # ------------------------------------------------------------ analytics (Phase 8)

    @app.get("/analytics/summary")
    async def analytics_summary(request: Request) -> dict[str, Any]:
        """Delay analysis over what `make collect` has recorded. Sections that do not have enough
        data come back as null with a reason; nothing here is simulated."""
        path = st(request).settings.analytics_db_path

        def work() -> dict[str, Any]:
            store = DelayStore(path)
            try:
                return summarise(
                    store.observations(), store.rain(), store.poll_stats(), time.time()
                )
            finally:
                store.close()

        return await asyncio.to_thread(work)

    # ------------------------------------------------------------ health / eval

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        s = st(request)
        ping = getattr(s.llm, "ping", None)
        transit: dict[str, Any] = {"loaded": s.transit is not None}
        if s.transit is not None:
            with contextlib.suppress(Exception):
                meta = s.transit.static.meta()
                transit.update(
                    feed_start=meta.get("feed_start"), feed_end=meta.get("feed_end"),
                    gtfs_downloaded_at=meta.get("downloaded_at"),
                    realtime_age_s=s.transit.realtime.cache_ages(),
                )  # fmt: skip
        transit["last_refresh_check"] = s.last_refresh or None
        pending = await asyncio.to_thread(s.gate.list_actions, ActionStatus.PENDING, 200)
        examples = getattr(s.orchestrator, "examples", None)
        return {
            "status": "ok",
            "demo": cfg.demo,
            "demo_examples": examples() if cfg.demo and examples else [],
            "demo_examples_by_agent": (
                {a: examples(a) for a in AGENT_META} if cfg.demo and examples else {}
            ),
            "llm": {
                "backend": f"ollama @ {cfg.ollama_base_url}",
                "reachable": await ping() if ping else None,
                "models": {role: c.model_dump() for role, c in ROLES.items()},
            },
            "agents_available": sorted(s.available),
            "transit": transit,
            "approvals_pending": len(pending),
        }

    # ------------------------------------------------------------ settings

    async def _settings_out(s: AppState) -> SettingsOut:
        p = s.profile
        resolved: str | None = None
        if s.transit is not None:
            with contextlib.suppress(Exception):
                resolved = s.transit.resolve_place(p.home).label
        return SettingsOut(
            home=p.home, destinations=dict(p.destinations),
            working_hours_start=p.working_hours_start, working_hours_end=p.working_hours_end,
            buffer_minutes=p.buffer_minutes, router_model=s.router_model(),
            available_models=await s.available_models(), home_resolves_to=resolved,
        )  # fmt: skip

    @app.get("/settings")
    async def get_settings_endpoint(request: Request) -> SettingsOut:
        return await _settings_out(st(request))

    @app.put("/settings")
    async def put_settings(body: SettingsIn, request: Request) -> SettingsOut:
        s = st(request)
        if body.working_hours_end <= body.working_hours_start:
            raise HTTPException(status_code=422, detail="working hours must end after they start")
        for key, value in body.destinations.items():
            if not key.strip() or len(key) > 40 or not value.strip() or len(value) > 100:
                raise HTTPException(
                    status_code=422, detail="destination names/places are 1-40/1-100 characters"
                )
        if s.transit is not None:  # the home stop must exist in the timetable data
            try:
                await asyncio.to_thread(s.transit.resolve_place, body.home)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"home: {exc}") from exc
        if body.router_model is not None:
            models = await s.available_models()
            if models and body.router_model not in models:
                raise HTTPException(
                    status_code=422,
                    detail=f"model {body.router_model!r} is not installed (have: {models})",
                )
        new = s.profile.model_copy(update={
            "home": body.home.strip(),
            "destinations": {k.strip().lower(): v.strip() for k, v in body.destinations.items()},
            "working_hours_start": body.working_hours_start,
            "working_hours_end": body.working_hours_end,
            "buffer_minutes": body.buffer_minutes,
        })  # fmt: skip
        await asyncio.to_thread(s.apply_profile, new)
        if body.router_model is not None and body.router_model != s.router_model():
            s.set_router_model(body.router_model)
        return await _settings_out(s)

    @app.get("/eval/latest")
    async def eval_latest() -> dict[str, Any]:
        # Headline numbers from backend/eval_results/ (full report: docs/evaluation.md).
        return await asyncio.to_thread(latest_summary)

    @app.post("/eval/run/deterministic")
    async def eval_run_deterministic() -> dict[str, Any]:
        """Run the offline suites (transit, advice, calendar, safety): no model, no network, a few
        seconds. The live-model suites are slow and run from the CLI (`make eval-live`)."""
        try:
            from evals.run_eval import run_det  # a dev-time package; absent in a slim install
        except ImportError:
            return JSONResponse(  # type: ignore[return-value]
                status_code=501,
                content={
                    "error": {"type": "not_available", "message": "evals package not installed"}
                },
            )
        doc = await run_det()
        RESULTS_DIR.mkdir(exist_ok=True)
        (RESULTS_DIR / "deterministic.json").write_text(json.dumps(doc, indent=1))
        return await asyncio.to_thread(latest_summary)

    return app


app = create_app()
