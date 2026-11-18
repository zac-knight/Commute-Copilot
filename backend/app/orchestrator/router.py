"""Router: user message (+ history + profile) -> validated Plan.

The model proposes (schema-constrained JSON); ``planner.validate_plan`` disposes. One retry with the
validation errors fed back; after that the router gives up with a PlanError the orchestrator turns
into an honest message (it never guesses a plan).
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection, Sequence
from datetime import datetime

from app.config import RoleConfig
from app.core.agent import RequestBudget
from app.core.livestream import emit
from app.core.llm import LLM, LLMError
from app.core.memory import UserProfile
from app.core.tracing import SpanKind, Tracer
from app.orchestrator.planner import PLAN_SCHEMA, Plan, PlanError, parse_plan, validate_plan
from app.orchestrator.prompts import Prompt, load_prompt

ROUTER_PROMPT = load_prompt("router", 3)
MAX_HISTORY_TURNS = 3
Turn = tuple[str, str]  # (user message, assistant reply)
GENERIC_PLACE_QUESTION = "Where would you like to go? I wasn't sure which place you meant."


def ungrounded_places(
    plan: Plan, message: str, history: Sequence[Turn], profile: UserProfile
) -> list[str]:
    """Saved places (nicknames or their targets) that the PLAN uses but the user never said.

    Observed live: the 8B model reads 'usual destinations' in its context and plans a trip to
    Chermside for 'should I leave now or wait?' even when told never to assume a destination.
    Home is exempt. Only the user's own recent words count as grounding (assistant text does not).
    """
    said = " ".join([message, *(user for user, _ in history[-MAX_HISTORY_TURNS:])]).lower()
    tasks = " ".join(s.task for s in plan.steps).lower()
    found: list[str] = []
    for nickname, target in profile.destinations.items():
        names = {nickname.lower(), target.lower()}
        if any(n in tasks for n in names) and not any(n in said for n in names):
            found.append(nickname)
    return found


class RouterError(Exception):
    pass


class Router:
    def __init__(
        self,
        llm: LLM,
        tracer: Tracer,
        role: RoleConfig,
        available: Collection[str],
        profile: UserProfile,
        *,
        prompt: Prompt = ROUTER_PROMPT,
        max_tokens: int = 700,
    ) -> None:
        self.llm, self.tracer, self.role = llm, tracer, role
        self.available = set(available)
        self.profile = profile
        self.prompt = prompt
        self.max_tokens = max_tokens

    def system_prompt(self, now: datetime, rejections: Sequence[str] = ()) -> str:
        dests = ", ".join(f"{k} ({v})" for k, v in self.profile.destinations.items())
        profile = f"home stop: {self.profile.home}; usual destinations: {dests}"
        context = f"\nCurrent local time: {now:%A %d %B %Y %H:%M}."
        if len(self.available) < 3:
            missing = sorted({"transit", "weather", "calendar"} - self.available)
            context += f" Agents NOT available right now: {missing}."
        if rejections:
            context += (
                " The user recently REJECTED these proposed changes (do not re-propose the same "
                "thing; offer an alternative if they ask): " + "; ".join(rejections) + "."
            )
        return self.prompt.text.replace("{{PROFILE}}", profile).replace(
            "{{CONTEXT}}", context + "\n"
        )

    async def route(
        self,
        message: str,
        history: Sequence[Turn],
        now: datetime,
        *,
        rejections: Sequence[str] = (),
        budget: RequestBudget | None = None,
    ) -> Plan:
        messages: list[dict[str, object]] = []
        for user, assistant in history[-MAX_HISTORY_TURNS:]:
            messages += [{"role": "user", "content": user},
                         {"role": "assistant", "content": assistant[:300]}]  # fmt: skip
        messages.append({"role": "user", "content": message})
        system = self.system_prompt(now, rejections)

        with self.tracer.span(
            SpanKind.ORCHESTRATOR, "router", input=message, model=self.role.model,
            attributes={"prompt_version": self.prompt.version},
        ) as span:  # fmt: skip
            problems: list[str] = []
            for attempt in (1, 2):
                if budget and (reason := budget.exceeded()):
                    raise RouterError(reason)
                try:
                    plan = await self._call(system, messages, attempt, budget)
                    plan = validate_plan(plan, self.available)
                    if assumed := ungrounded_places(plan, message, history, self.profile):
                        raise PlanError([
                            f"the plan uses {assumed} but the user never mentioned it; "
                            "do not assume a destination. If the destination is unknown, "
                            "ask a clarification_question"
                        ])  # fmt: skip
                except PlanError as exc:
                    problems = exc.problems
                    span.set_attr(f"attempt{attempt}_problems", problems)
                    messages = [
                        *messages,
                        {"role": "assistant", "content": "(invalid plan)"},
                        {"role": "user", "content": (
                            "That plan was rejected: " + "; ".join(problems)
                            + ". Output a corrected JSON plan for my previous request only."
                        )},
                    ]  # fmt: skip
                    continue
                span.set_output(plan.model_dump_json())
                span.set_attr("plan", json.loads(plan.model_dump_json()))
                span.set_attr("attempts", attempt)
                return plan
            if any("never mentioned" in p for p in problems):
                # The model keeps assuming a saved place: ask instead of guessing.
                fallback = Plan(
                    intent="clarify", clarification_question=GENERIC_PLACE_QUESTION,
                    repairs=["replaced a plan that assumed a destination with a question"],
                )  # fmt: skip
                span.set_attr("plan", json.loads(fallback.model_dump_json()))
                return fallback
            raise RouterError("could not produce a valid plan: " + "; ".join(problems))

    async def _call(
        self,
        system: str,
        messages: list[dict[str, object]],
        attempt: int,
        budget: RequestBudget | None,
    ) -> Plan:
        import asyncio

        with self.tracer.span(SpanKind.LLM, f"router.llm#{attempt}", model=self.role.model) as span:
            span.set_input(messages[-1]["content"])
            try:
                emit("llm_started", n=attempt, model=self.role.model, think=self.role.think,
                     prompt_version=ROUTER_PROMPT.version, messages=len(messages), tools=[])  # fmt: skip
                t0 = time.monotonic()
                async with asyncio.timeout(budget.remaining_seconds() if budget else None):
                    resp = await self.llm.complete(
                        model=self.role.model, system=system, messages=messages,
                        tools=[], max_tokens=self.max_tokens, think=self.role.think,
                        json_schema=PLAN_SCHEMA,
                    )  # fmt: skip
            except TimeoutError as exc:
                raise RouterError("time budget exhausted while routing") from exc
            except LLMError as exc:
                raise RouterError(f"{exc.kind}: {exc}") from exc
            emit("llm_usage", n=attempt, model=resp.model, tokens_in=resp.usage.input_tokens,
                 tokens_out=resp.usage.output_tokens, thinking_chars=0,
                 duration_ms=round((time.monotonic() - t0) * 1000))  # fmt: skip
            span.set_usage(tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
                           model=resp.model)  # fmt: skip
            span.set_output(resp.text)
            if budget:
                budget.add_tokens(resp.usage.total)
            return parse_plan(resp.text)
