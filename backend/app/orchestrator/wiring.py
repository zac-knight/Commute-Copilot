"""Assemble the application: services, tool registry, approval gate and orchestrator.

This is the one place that holds both sides of the safety boundary: it builds the human-side gate
and calendar writer (given only to the executor) and the agent-side read-only reader and
propose-only sink (given to the tools).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.agents.calendar.executor import register_calendar_handlers
from app.agents.calendar.service import CalendarReader
from app.agents.calendar.store import LocalCalendarReader, LocalCalendarWriter
from app.agents.calendar.tools import register_calendar_tools
from app.agents.transit.gtfs_realtime import RealtimeClient
from app.agents.transit.gtfs_static import GtfsStatic, refresh_feed
from app.agents.transit.planner import JourneyPlanner
from app.agents.transit.service import TransitService
from app.agents.transit.tools import register_transit_tools
from app.agents.weather.client import WeatherClient
from app.agents.weather.service import WeatherService
from app.agents.weather.tools import register_weather_tools
from app.config import ROLES, RoleConfig, Settings
from app.core.approvals import ApprovalGate, PendingAction, ProposalSink
from app.core.llm import LLM, OllamaLLM
from app.core.memory import UserProfile, load_profile, save_profile
from app.core.tools import ToolRegistry
from app.core.tracing import Tracer, TraceStore
from app.orchestrator.context import plans_var, proposals_var
from app.orchestrator.engine import Orchestrator
from app.orchestrator.router import Router
from app.orchestrator.sessions import SessionStore
from app.orchestrator.synthesiser import Synthesiser

log = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    profile: UserProfile
    store: TraceStore
    tracer: Tracer
    llm: LLM
    registry: ToolRegistry
    gate: ApprovalGate
    orchestrator: Orchestrator
    sessions: SessionStore
    calendar_reader: CalendarReader
    calendar_writer: LocalCalendarWriter  # human-side only (the executor); never given to agents
    weather: WeatherService
    transit: TransitService | None = (
        None  # South East Queensland: the assistant's agents use this one
    )
    available: set[str] = field(default_factory=set)
    closers: list[Callable[[], Any]] = field(default_factory=list)
    last_refresh: dict[str, str] = field(default_factory=dict)

    async def available_models(self) -> list[str]:
        lister = getattr(self.llm, "list_models", None)
        return sorted(await lister()) if lister else []

    def router_model(self) -> str:
        return self.orchestrator.router.role.model

    def set_router_model(self, model: str) -> None:
        """Switch the model the router uses (takes effect on the next request) and remember it."""
        self.orchestrator.router.role = RoleConfig(model=model, think=ROLES["router"].think)
        path = self.settings.ui_settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"router_model": model}), encoding="utf-8")

    def apply_profile(self, new: UserProfile) -> None:
        """Update the SHARED profile object in place (transit, weather, calendar and the router all
        hold a reference to it, so they see the change immediately) and persist it."""
        for name in type(new).model_fields:
            setattr(self.profile, name, getattr(new, name))
        save_profile(self.settings.profile_path, self.profile)

    async def aclose(self) -> None:
        for close in self.closers:
            res = close()
            if asyncio.iscoroutine(res):
                await res


def _centroid(service: TransitService, query: str) -> tuple[float, float] | None:
    try:
        stops = service.resolve_place(query).stops
    except ValueError:
        return None
    return sum(s.lat for s in stops) / len(stops), sum(s.lon for s in stops) / len(stops)


def build_state(
    settings: Settings,
    *,
    llm: LLM | None = None,
    transit: TransitService | None = None,
    weather: WeatherService | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AppState:
    """Build everything from settings. Pass ``llm``/``transit``/``weather`` to override (tests)."""
    profile = load_profile(settings.profile_path)
    store = TraceStore(settings.db_path)
    tracer = Tracer(store, preview_chars=settings.trace_preview_chars,
                    secrets=settings.secret_values())  # fmt: skip
    closers: list[Callable[[], Any]] = [store.close]

    if llm is None:
        ollama = OllamaLLM(settings.ollama_base_url, timeout=settings.llm_timeout_seconds,
                           num_ctx=settings.num_ctx)  # fmt: skip
        closers.append(ollama.aclose)
        llm = ollama

    if transit is None and (settings.gtfs_dir / "gtfs.sqlite").exists():
        static = GtfsStatic(settings.gtfs_dir / "gtfs.sqlite")
        rt = RealtimeClient(ttl_s=settings.rt_ttl_seconds)
        closers.append(rt.aclose)
        transit = TransitService(
            static, rt,
            JourneyPlanner(static, walk_speed_m_per_min=settings.walk_speed_m_per_min,
                           transfer_radius_m=settings.transfer_radius_m,
                           min_transfer_s=settings.min_transfer_s),
            profile, clock=clock, on_time_s=settings.on_time_threshold_s,
        )  # fmt: skip
        from app.agents.transit.shapes import ShapeStore

        transit.shapes = ShapeStore(
            settings.gtfs_dir / "SEQ_GTFS.zip", settings.gtfs_dir / "shapes.sqlite"
        )
        transit.shapes.start()  # background: road geometry for the map (no-op when up to date)

    if weather is None:
        wc = WeatherClient(ttl_s=settings.weather_ttl_seconds,
                           max_stale_s=settings.weather_max_stale_seconds)  # fmt: skip
        closers.append(wc.aclose)
        weather = WeatherService(
            wc, profile, clock=clock,
            resolve_place=(lambda name: _centroid(transit, name)) if transit else None,
        )  # fmt: skip

    cal_dir = settings.calendar_dir
    writer = LocalCalendarWriter(cal_dir / "calendar.json")
    reader = CalendarReader(
        LocalCalendarReader(writer.path), cal_dir / "import", profile, clock=clock
    )
    gate = ApprovalGate(
        settings.db_path,
        clock=clock,
        ttl=timedelta(minutes=settings.approval_ttl_minutes),
        max_pending=settings.max_pending_actions,
    )
    register_calendar_handlers(gate, reader, writer)
    closers.append(gate.close)

    def record(action_type: str, payload: dict[str, Any]) -> PendingAction:
        action = gate.create(action_type, payload)
        bucket = proposals_var.get()
        if bucket is not None:  # tell the orchestrator what this request proposed
            bucket.append(action)
        return action

    registry = ToolRegistry()
    available = {"weather", "calendar"}
    if transit is not None:
        register_transit_tools(registry, transit)
        available.add("transit")
    register_weather_tools(registry, weather, lambda: plans_var.get())
    register_calendar_tools(registry, reader, ProposalSink(record))

    sessions = SessionStore()
    router = Router(llm, tracer, ROLES["router"], available, profile)
    try:  # a router model chosen in the UI survives restarts
        saved = json.loads(settings.ui_settings_path.read_text(encoding="utf-8"))
        if isinstance(saved.get("router_model"), str) and saved["router_model"]:
            router.role = RoleConfig(model=saved["router_model"], think=ROLES["router"].think)
    except (OSError, ValueError):
        pass
    orch = Orchestrator(
        llm=llm, tracer=tracer, registry=registry, profile=profile, settings=settings,
        router=router,
        synthesiser=Synthesiser(llm, tracer, ROLES["synthesis"]),
        sessions=sessions, available=available, gate=gate, transit=transit, clock=clock,
    )  # fmt: skip
    return AppState(
        settings=settings, profile=profile, store=store, tracer=tracer, llm=llm, registry=registry,
        gate=gate, orchestrator=orch, sessions=sessions, calendar_reader=reader,
        calendar_writer=writer, weather=weather, transit=transit,
        available=available, closers=closers,
    )  # fmt: skip


async def gtfs_refresh_loop(state: AppState) -> None:
    """Check for a new static feed at start-up and then every ``gtfs_refresh_hours`` (conditional
    GET, so an unchanged feed costs one tiny request). Failures are logged, never raised."""
    s = state.settings
    db = s.gtfs_dir / "gtfs.sqlite"
    while True:
        try:
            res = await refresh_feed(s.gtfs_dir, db)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            state.last_refresh = {"status": res.status, "detail": res.detail, "at": now}
            log.info("gtfs refresh: %s %s", res.status, res.detail)
            if res.status == "updated" and state.transit is not None:
                await asyncio.to_thread(state.transit.reload)
        except Exception:  # pragma: no cover - defensive; refresh_feed already catches
            log.exception("gtfs refresh loop error")
        await asyncio.sleep(s.gtfs_refresh_hours * 3600)
