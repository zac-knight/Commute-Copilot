"""Synthesiser: step results -> one concise, friendly answer, using only facts in the results.

The model writes the wording; code owns everything that must be true:
  * if every step failed, the reply is deterministic (no model call);
  * approval status ('waiting for your approval') is appended from the REAL approval queue, and any
    such claim the model makes when nothing is pending is corrected (observed live in Phase 4).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.config import RoleConfig
from app.core.agent import RequestBudget
from app.core.approvals import PendingAction
from app.core.livestream import effective_think, emit
from app.core.llm import LLM, LLMError
from app.core.tracing import SpanKind, Tracer
from app.orchestrator.events import StepOutcome
from app.orchestrator.planner import Plan
from app.orchestrator.prompts import Prompt, load_prompt

SYNTH_PROMPT = load_prompt("synth", 2)
MAX_OUTPUT_CHARS = 2500  # per tool output handed to the model (the context window is small)
MAX_STEP_TEXT = 600

_APPROVAL_CLAIM = re.compile(
    r"(waiting (for|on) (your )?(approval|confirmation)|awaiting (your )?approval|"
    r"pending (your )?approval|needs? your approval|approval (is )?(pending|required))",
    re.IGNORECASE,
)
NO_PENDING_NOTE = "Note: nothing has been proposed, so nothing is waiting for your approval."


def reconcile_approval_claims(text: str, pending_count: int) -> str:
    """Correct a claim of pending approval when the queue is empty."""
    if pending_count == 0 and _APPROVAL_CLAIM.search(text):
        return f"{text.rstrip()}\n{NO_PENDING_NOTE}"
    return text


def approval_footer(pending: Sequence[PendingAction]) -> str:
    if not pending:
        return ""
    lines = [f"- {a.summary}" for a in pending]
    return "Waiting for your approval (open the approvals panel):\n" + "\n".join(lines)


def advice_blocks(outcomes: Sequence[StepOutcome]) -> list[dict[str, Any]]:
    """Successful ``commute_advice`` results (the deterministic leave-now/wait decisions)."""
    out = []
    for o in outcomes:
        for t in o.tool_outputs:
            if t.tool == "commute_advice" and not t.is_error and isinstance(t.output, dict):
                out.append(t.output)
    return out


def decision_text(advice: dict[str, Any]) -> str:
    """The authoritative leave-now / wait decision, written by CODE from commute_advice's output.

    Observed live: the model wrote 'Leave now' while the rules said 'wait until 09:00', copying a
    transit agent's improvised rain comment. The decision must never come from model prose."""
    when = advice.get("suggested_leave_time")
    if advice.get("leave_now"):
        head = "**Leave now.**"
    elif when:
        head = f"**Wait: leave around {when}.**"
    else:
        head = "**Don't head out yet.**"
    lines = [head]
    lines += [f"- {r}" for r in (advice.get("reasons") or [])[:3]]
    if advice.get("bring"):
        lines.append("- Bring: " + ", ".join(advice["bring"]) + ".")
    conf = advice.get("confidence")
    if isinstance(conf, (int, float)) and conf < 0.6:
        lines.append(f"- This is uncertain (confidence {conf:.0%}).")
    return "\n".join(lines)


def _norm(line: str) -> str:
    return re.sub(r"[^a-z0-9: ]+", "", re.sub(r"\s+", " ", line.lower())).strip()


def drop_decision_repeats(text: str, decisions: Sequence[str]) -> str:
    """Remove model lines that merely restate the code-written decision block.

    Observed live (8B model): the 'do not repeat the decision' prompt rule was ignored and the
    headline, reasons and packing list appeared twice. Prompts are not a guarantee, so this is
    enforced in code. It only deletes repeats; it never adds or changes a fact."""
    keys = [k for d in decisions for line in d.splitlines() if len(k := _norm(line)) >= 8]
    kept = []
    for line in text.splitlines():
        n = _norm(line)
        if n and len(n) >= 8 and any(n == k or n in k or k in n for k in keys):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _compact(outcome: StepOutcome) -> dict[str, Any]:
    data = []
    for t in outcome.tool_outputs:
        raw = t.output if isinstance(t.output, str) else json.dumps(t.output, ensure_ascii=False)
        if len(raw) > MAX_OUTPUT_CHARS:
            raw = raw[:MAX_OUTPUT_CHARS] + "...[truncated]"
        data.append({"tool": t.tool, "is_error": t.is_error, "result": raw})
    row: dict[str, Any] = {
        "step": outcome.id, "agent": outcome.agent, "task": outcome.task, "status": outcome.status,
        "error": outcome.error, "data": data,
    }  # fmt: skip
    if not data:  # a specialist's own prose is only used when it has no structured results at all
        row["agent_summary"] = outcome.text[:MAX_STEP_TEXT]
    return row


def failure_text(outcomes: Sequence[StepOutcome]) -> str:
    lines = ["I couldn't complete that:"]
    for o in outcomes:
        why = o.error or (
            "skipped because an earlier step failed" if o.status == "skipped" else "no result"
        )
        lines.append(f"- {o.agent}: {why}")
    return "\n".join(lines)


class Synthesiser:
    def __init__(
        self, llm: LLM, tracer: Tracer, role: RoleConfig, *, prompt: Prompt = SYNTH_PROMPT,
        max_tokens: int = 700,
    ) -> None:  # fmt: skip
        self.llm, self.tracer, self.role, self.prompt = llm, tracer, role, prompt
        self.max_tokens = max_tokens

    async def synthesise(
        self,
        question: str,
        plan: Plan,
        outcomes: Sequence[StepOutcome],
        pending: Sequence[PendingAction],
        now: datetime,
        budget: RequestBudget | None = None,
    ) -> str:
        footer = approval_footer(pending)
        if outcomes and not any(o.status == "ok" for o in outcomes):
            return "\n".join(x for x in (failure_text(outcomes), footer) if x)
        decisions = [decision_text(a) for a in advice_blocks(outcomes)]
        payload: dict[str, Any] = {
            "user_request": question,
            "now": f"{now:%A %d %B %Y %H:%M} (Brisbane)",
            "results": [_compact(o) for o in outcomes],
        }
        if decisions:
            payload["decision_shown_to_user_first"] = decisions
        decision_block = "\n\n".join(decisions)
        with self.tracer.span(
            SpanKind.LLM, "synthesiser", model=self.role.model,
            attributes={"prompt_version": self.prompt.version},
        ) as span:  # fmt: skip
            span.set_input(question)
            try:
                think = effective_think(self.role.think)
                emit("llm_started", n=1, model=self.role.model, think=think,
                     prompt_version=self.prompt.version, messages=1, tools=[])  # fmt: skip
                t0 = time.monotonic()
                async with asyncio.timeout(budget.remaining_seconds() if budget else None):
                    content = json.dumps(payload, ensure_ascii=False)
                    resp = await self.llm.complete(
                        model=self.role.model, system=self.prompt.text,
                        messages=[{"role": "user", "content": content}],
                        tools=[], max_tokens=self.max_tokens, think=think,
                    )  # fmt: skip
                emit("llm_usage", n=1, model=resp.model, tokens_in=resp.usage.input_tokens,
                     tokens_out=resp.usage.output_tokens,
                     thinking_chars=len(resp.extra.get("thinking", "")),
                     duration_ms=round((time.monotonic() - t0) * 1000))  # fmt: skip
            except (TimeoutError, LLMError) as exc:
                # Don't lose the work already done: fall back to the specialists' own summaries.
                span.set_error(f"{type(exc).__name__}: {exc}")
                summary = "\n".join(o.text for o in outcomes if o.status == "ok" and o.text)
                text = (summary or failure_text(outcomes)) + "\n(I couldn't polish this answer.)"
                return "\n".join(x for x in (decision_block, text, footer) if x)
            span.set_usage(tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
                           model=resp.model)  # fmt: skip
            span.set_output(resp.text)
            if budget:
                budget.add_tokens(resp.usage.total)
        text = reconcile_approval_claims(resp.text.strip(), len(pending))
        text = drop_decision_repeats(text, decisions)
        failed = [o for o in outcomes if o.status != "ok"]
        if failed:  # make sure a failure is never silently dropped from the answer
            missing = [o for o in failed if o.agent not in text.lower()]
            if missing:
                text += "\n" + failure_text(missing)
        return "\n".join(x for x in (decision_block, text, footer) if x)
