"""The orchestrator: route -> run specialist agents (dependency-aware, concurrent) -> synthesise.

Rules enforced here (the agents themselves cannot do any of this):
  * Agents never call each other. Only this module composes them, passing earlier results to later
    steps as STRUCTURED DATA (tool outputs), not prose.
  * Everything shares one RequestBudget (tokens + wall time); a blown budget degrades the answer
    gracefully instead of hanging.
  * Proposals created by the calendar agent are collected here and surfaced as ``approval_required``
    events; approval status in the final text comes from the real queue, never from model prose.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime
from typing import Any

from app.agents.calendar import prompts as calendar_prompts
from app.agents.transit import prompts as transit_prompts
from app.agents.transit.geo import build_trip_artifact
from app.agents.transit.service import TransitService
from app.agents.weather import prompts as weather_prompts
from app.agents.weather.advice import RoutePlan
from app.config import ROLES, Settings
from app.core.agent import Agent, AgentResult, RequestBudget
from app.core.approvals import ApprovalGate, PendingAction
from app.core.livestream import live_sink, reasoning_override, scope
from app.core.llm import LLM
from app.core.memory import UserProfile
from app.core.timeutil import TZ
from app.core.tools import ToolRegistry
from app.core.tracing import SpanKind, Tracer, redact
from app.orchestrator.context import plans_var, proposals_var
from app.orchestrator.events import (
    ApprovalRequired,
    ErrorEvent,
    Event,
    Final,
    PlanEvent,
    StepOutcome,
    StepResult,
    StepStarted,
    ToolOutput,
    TripEvent,
    live_event,
)
from app.orchestrator.placeholders import PlaceholderError, resolve
from app.orchestrator.planner import Plan, PlanStep
from app.orchestrator.router import Router, RouterError
from app.orchestrator.sessions import SessionStore
from app.orchestrator.synthesiser import Synthesiser, approval_footer

OUT_OF_SCOPE_TEXT = (
    "I can only help with getting around (buses and trains), the weather for your trips, and your "
    "calendar. I can't help with that one."
)
MAX_DEP_OUTPUT_CHARS = 2500

SPECIALISTS: dict[str, tuple[str, str]] = {
    "transit": (transit_prompts.SYSTEM_PROMPT, transit_prompts.PROMPT_VERSION),
    "weather": (weather_prompts.SYSTEM_PROMPT, weather_prompts.PROMPT_VERSION),
    "calendar": (calendar_prompts.SYSTEM_PROMPT, calendar_prompts.PROMPT_VERSION),
}


def parse_tool_output(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def plans_from_outcomes(
    outcomes: Sequence[StepOutcome], transit: TransitService | None
) -> list[RoutePlan]:
    """Journeys from transit steps' plan_trip outputs, with the start coordinates the weather
    advice needs. Built in code: the model never copies or supplies this data."""
    plans: list[RoutePlan] = []
    for o in outcomes:
        for t in o.tool_outputs:
            if t.tool != "plan_trip" or t.is_error or not isinstance(t.output, dict):
                continue
            lat = lon = None
            if transit is not None:
                stops = [transit.static.get_stop(s["stop_id"]) for s in t.output.get("origin", [])]
                found = [s for s in stops if s is not None]
                if found:
                    lat = sum(s.lat for s in found) / len(found)
                    lon = sum(s.lon for s in found) / len(found)
            for journey in t.output.get("journeys", []):
                plans.append(
                    RoutePlan.model_validate({**journey, "latitude": lat, "longitude": lon})
                )
    return plans


def _dep_data(outcomes: Sequence[StepOutcome]) -> list[dict[str, Any]]:
    out = []
    for o in outcomes:
        data = []
        for t in o.tool_outputs:
            raw = (
                t.output if isinstance(t.output, str) else json.dumps(t.output, ensure_ascii=False)
            )
            data.append({"tool": t.tool, "result": raw[:MAX_DEP_OUTPUT_CHARS]})
        out.append({"step": o.id, "agent": o.agent, "task": o.task, "data": data})
    return out


class Orchestrator:
    def __init__(
        self,
        *,
        llm: LLM,
        tracer: Tracer,
        registry: ToolRegistry,
        profile: UserProfile,
        settings: Settings,
        router: Router,
        synthesiser: Synthesiser,
        sessions: SessionStore,
        available: set[str],
        gate: ApprovalGate | None = None,
        transit: TransitService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.llm, self.tracer, self.registry, self.profile = llm, tracer, registry, profile
        self.settings, self.router, self.synth = settings, router, synthesiser
        self.sessions, self.available, self.gate, self.transit = sessions, available, gate, transit
        self.clock = clock or (lambda: datetime.now(TZ))

    # ------------------------------------------------------------------ public

    async def run(
        self,
        session_id: str,
        message: str,
        agent: str | None = None,
        reasoning: bool = False,
        detail: bool = False,
    ) -> AsyncIterator[Event]:
        """Stream events for one request. The work runs in its own task; closing the iterator
        (client disconnect) cancels it. ``agent`` talks to one specialist directly: the router is
        skipped, everything else (budget, tracing, approval gate, synthesis) is unchanged."""
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        task = asyncio.create_task(self._run(session_id, message, queue, agent, reasoning, detail))
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------------ internals

    def _agent(self, name: str) -> Agent:
        system, version = SPECIALISTS[name]
        role = ROLES["agent"]
        return Agent(
            name=name, system_prompt=system, tools=self.registry.toolset_for(name),
            model=role.model, llm=self.llm, tracer=self.tracer, think=role.think,
            max_steps=self.settings.max_agent_steps,
            tool_result_max_chars=self.settings.tool_result_max_chars, prompt_version=version,
        )  # fmt: skip

    def _rejections(self) -> list[str]:
        if self.gate is None:
            return []
        return [
            f"{a.summary}" + (f" (reason: {a.reject_reason})" if a.reject_reason else "")
            for a in self.gate.recent_rejections(3)
        ]

    async def _run(
        self, session_id: str, message: str, queue: asyncio.Queue[Event | None],
        agent: str | None = None, reasoning: bool = False, detail: bool = False,
    ) -> None:  # fmt: skip
        emit = queue.put_nowait

        def sink(raw: dict[str, Any]) -> None:  # the live processing stream for this request
            if (
                ev := live_event(raw, lambda t: redact(t, self.settings.secret_values()))
            ) is not None:
                emit(ev)

        if detail:  # opt-in: older clients and tests see exactly the events they always did
            live_sink.set(sink)
        reasoning_override.set(True if reasoning else None)
        started = time.monotonic()
        rid: str | None = None
        proposals: list[PendingAction] = []
        proposals_var.set(proposals)
        try:
            budget = RequestBudget(
                self.settings.max_request_tokens, self.settings.max_request_seconds
            )
            now = self.clock()
            with self.tracer.request() as rid, self.tracer.span(
                SpanKind.ORCHESTRATOR, "orchestrator", input=message,
                attributes={"session_id": session_id},
            ) as root:  # fmt: skip
                if agent:
                    root.set_attr("direct_agent", agent)
                final = await self._handle(
                    session_id, message, now, budget, proposals, emit, rid, agent
                )
                root.set_attr("status", final.status)
                root.set_output(final.text)
                tokens_in = tokens_out = 0
                for s in self.tracer.store.get_spans(rid):
                    if s.kind is SpanKind.LLM:
                        tokens_in += s.tokens_in
                        tokens_out += s.tokens_out
                final = final.model_copy(update={
                    "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                })  # fmt: skip
            emit(final)
            self.sessions.add(session_id, message, final.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # last resort: never leave the client hanging
            emit(ErrorEvent(request_id=rid, message=f"{type(exc).__name__}: {exc}"))
            emit(
                Final(
                    request_id=rid or "",
                    status="failed",
                    text="Something went wrong on my side, so I could not finish that.",
                )
            )
        finally:
            queue.put_nowait(None)

    async def _handle(
        self, session_id: str, message: str, now: datetime, budget: RequestBudget,
        proposals: list[PendingAction], emit: Callable[[Event], None], rid: str,
        agent: str | None = None,
    ) -> Final:  # fmt: skip
        if agent is not None:
            if agent not in SPECIALISTS or agent not in self.available:
                text = f"The {agent} agent is not available right now."
                return Final(request_id=rid, status="failed", text=text)
            task = message if len(message.strip()) >= 3 else f"{message.strip()} (user request)"
            plan = Plan(intent=agent, steps=[PlanStep(id="s1", agent=agent, task=task)])
        else:
            try:
                with scope(step="router", agent="router", phase="router"):
                    plan = await self.router.route(
                        message, self.sessions.history(session_id), now,
                        rejections=self._rejections(), budget=budget,
                    )  # fmt: skip
            except RouterError as exc:
                text = f"Sorry, I couldn't work out how to handle that ({exc})."
                return Final(request_id=rid, status="failed", text=text)
        emit(PlanEvent(request_id=rid, plan=plan))
        if plan.intent == "clarify":
            return Final(request_id=rid, status="clarify", text=plan.clarification_question or "")
        if plan.intent == "out_of_scope":
            return Final(request_id=rid, status="out_of_scope", text=OUT_OF_SCOPE_TEXT)

        outcomes = await self._execute(plan, message, now, budget, proposals, emit)
        if live_sink.get() is not None:  # detail clients also get map-ready trips
            for ev in await self._trip_events(outcomes):
                emit(ev)
        ordered = [outcomes[s.id] for s in plan.steps]
        with scope(step="synth", agent="synthesiser", phase="synth"):
            text = await self.synth.synthesise(message, plan, ordered, proposals, now, budget)
        ok = [o for o in ordered if o.status == "ok"]
        status = "ok" if len(ok) == len(ordered) else ("partial" if ok else "failed")
        return Final(
            request_id=rid, status=status, text=text, pending_approvals=[a.id for a in proposals]
        )

    async def _trip_events(self, outcomes: dict[str, StepOutcome]) -> list[Event]:
        """One ``trip`` event per planned journey set: geometry, distances and stops, built in code
        (the model never sees coordinates). Sources, in order of preference: a plan_trip result;
        connecting options attached to next_departures; and, when someone asked for departures
        TOWARDS a place, a journey computed here so the answer always has a map."""
        if self.transit is None:
            return []
        out: list[Event] = []
        for o in outcomes.values():
            plans: list[dict[str, Any]] = []
            outputs = [t for t in o.tool_outputs if not t.is_error and isinstance(t.output, dict)]
            for t in outputs:
                if t.tool == "plan_trip":
                    plans.append(t.output)
                elif t.tool == "next_departures" and t.output.get("connections"):
                    plans.append(
                        {"origin": t.output.get("stops"), "journeys": t.output["connections"]}
                    )
            if not plans:
                for t in outputs:
                    if (
                        t.tool == "next_departures"
                        and t.input.get("towards")
                        and t.input.get("stop_id")
                    ):
                        try:
                            res = await self.transit.plan_trip(
                                str(t.input["stop_id"]), str(t.input["towards"])
                            )
                            plans.append(json.loads(res.model_dump_json()))
                        except Exception:  # decoration only: never fail an answer over a map
                            continue
                        break
            for plan_like in plans:
                try:
                    art = await asyncio.to_thread(
                        build_trip_artifact, self.transit.static, plan_like
                    )
                except Exception:
                    continue
                if art is not None:
                    out.append(TripEvent(step=o.id, agent=o.agent, artifact=art))
        return out

    async def _execute(
        self, plan: Plan, message: str, now: datetime, budget: RequestBudget,
        proposals: list[PendingAction], emit: Callable[[Event], None],
    ) -> dict[str, StepOutcome]:  # fmt: skip
        loop = asyncio.get_running_loop()
        futures: dict[str, asyncio.Future[StepOutcome]] = {
            s.id: loop.create_future() for s in plan.steps
        }
        announced = 0

        async def go(step: PlanStep) -> None:
            nonlocal announced
            deps = [await futures[d] for d in step.depends_on]  # each step waits only for ITS deps
            outcome = await self._execute_step(step, deps, message, now, budget, emit)
            while announced < len(proposals):  # surface new approval cards as soon as they exist
                emit(ApprovalRequired(action=proposals[announced]))
                announced += 1
            futures[step.id].set_result(outcome)

        await asyncio.gather(*(go(s) for s in plan.steps))
        return {sid: f.result() for sid, f in futures.items()}

    async def _execute_step(
        self, step: PlanStep, deps: list[StepOutcome], message: str, now: datetime,
        budget: RequestBudget, emit: Callable[[Event], None],
    ) -> StepOutcome:  # fmt: skip
        """Runs one specialist. NEVER raises: every failure becomes a failed/skipped outcome."""
        t0 = time.monotonic()
        bad = [d.id for d in deps if d.status != "ok"]
        if bad:
            why = f"depends on step(s) that did not succeed: {', '.join(bad)}"
            outcome = StepOutcome(
                id=step.id, agent=step.agent, task=step.task, status="skipped", error=why
            )
            emit(self._result_event(outcome))
            return outcome
        try:  # fill in {s1.start-15m}-style placeholders from the earlier steps' structured data
            task = resolve(step.task, {d.id: d for d in deps})
        except PlaceholderError as exc:
            why = f"could not work out the time for this step: {exc}"
            outcome = StepOutcome(
                id=step.id, agent=step.agent, task=step.task, status="failed", error=why
            )
            emit(StepStarted(id=step.id, agent=step.agent, task=step.task))
            emit(self._result_event(outcome))
            return outcome
        emit(StepStarted(id=step.id, agent=step.agent, task=task))
        try:
            if step.agent == "weather":
                plans_var.set(plans_from_outcomes(deps, self.transit))
            parts = [f"Current local time: {now:%A %d %B %Y %H:%M}.", f"Your task: {task}",
                     f"The user's original request: {message}"]  # fmt: skip
            if deps:
                parts.append(
                    "Results from earlier steps (structured data; use them, do not ask the user "
                    "for them):\n" + json.dumps(_dep_data(deps), ensure_ascii=False)
                )
            with self.tracer.span(
                SpanKind.ORCHESTRATOR, f"step:{step.id}", input=step.task,
                attributes={"agent": step.agent, "depends_on": step.depends_on},
            ):  # fmt: skip
                with scope(step=step.id, agent=step.agent, phase="agent"):
                    result = await self._agent(step.agent).run("\n\n".join(parts), budget=budget)
            outcome = self._outcome(step, result, t0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = StepOutcome(id=step.id, agent=step.agent, task=step.task, status="failed",
                                  error=f"{type(exc).__name__}: {exc}",
                                  duration_ms=round((time.monotonic() - t0) * 1000))  # fmt: skip
        emit(self._result_event(outcome))
        return outcome

    @staticmethod
    def _outcome(step: PlanStep, r: AgentResult, t0: float) -> StepOutcome:
        return StepOutcome(
            id=step.id, agent=step.agent, task=step.task,
            status="ok" if r.ok else "failed", text=r.text,
            error=None if r.ok else (r.error or r.status),
            tool_calls=r.tool_calls,
            tool_outputs=[
                ToolOutput(tool=t["tool"], input=t["input"], output=parse_tool_output(t["output"]),
                           is_error=t["is_error"])
                for t in r.tool_log
            ],
            tokens_in=r.tokens_in, tokens_out=r.tokens_out,
            duration_ms=round((time.monotonic() - t0) * 1000),
        )  # fmt: skip

    @staticmethod
    def _result_event(o: StepOutcome) -> StepResult:
        return StepResult(id=o.id, agent=o.agent, status=o.status, text=o.text, error=o.error,
                          tool_calls=o.tool_calls, duration_ms=o.duration_ms)  # fmt: skip


__all__ = ["Orchestrator", "approval_footer", "plans_from_outcomes"]
