"""Live end-to-end evaluation: the whole orchestrator, a real local model, a frozen world.

Everything the grader decides is mechanical (tool names, approval counts, regexes, the hallucination
check); the only fuzzy part (answer quality against a rubric) is left to ``evals/judge.py``, whose
agreement with a human is reported separately.

Needs Ollama running and the real SEQ timetable in ``data/gtfs`` (``make setup`` + the transit CLI's
``refresh``). Not run in CI.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from app.config import get_settings
from app.core.tracing import SpanKind
from app.demo.frozen import FROZEN_NOW
from app.demo.harness import build_fixture_state
from app.evaluation.hallucination import check_answer
from app.evaluation.runner import meta, save, use_model
from app.evaluation.stats import bootstrap_ci, latency_summary, rate
from evals.deterministic import load_cases

REQUEST_TIMEOUT_S = 420.0  # generous: the product enforces its own (shorter) budget


@dataclass
class E2ERun:
    """What happened for one case (raw observations, no judgements)."""

    id: str
    status: str = "error"
    intent: str = ""
    plan_agents: list[str] = field(default_factory=list)
    tools_by_agent: dict[str, list[str]] = field(default_factory=dict)
    step_status: dict[str, str] = field(default_factory=dict)
    answer: str = ""
    approvals: int = 0
    calendar_changed: bool = False
    sources: list[str] = field(default_factory=list)  # tool outputs etc. the answer may cite
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------- grading (pure)


def _group_ok(group: dict[str, Any] | str, called: set[str]) -> bool:
    names = [group] if isinstance(group, str) else group["any_of"]
    return bool(set(names) & called)


def grade_e2e(case: dict[str, Any], run: E2ERun) -> dict[str, Any]:
    """Pure function of (case, observations) -> per-check results. No I/O, no model."""
    checks: dict[str, bool] = {}
    notes: list[str] = []
    checks["status"] = run.status in case.get("statuses", ["ok", "partial"])
    if not checks["status"]:
        notes.append(f"status {run.status!r} not in {case.get('statuses')}")

    tool_sel: dict[str, dict[str, bool]] = {}
    for agent, groups in (case.get("tools") or {}).items():
        called = set(run.tools_by_agent.get(agent, []))
        tool_sel[agent] = {
            "invoked": agent in run.tools_by_agent,
            "ok": all(_group_ok(g, called) for g in groups),
        }
        if not tool_sel[agent]["ok"]:
            notes.append(f"{agent} called {sorted(called)}, wanted {groups}")
    checks["tools"] = all(v["ok"] for v in tool_sel.values())

    all_called = {t for ts in run.tools_by_agent.values() for t in ts}
    checks["forbidden_tools"] = not (set(case.get("forbid", [])) & all_called)
    if not checks["forbidden_tools"]:
        notes.append(f"forbidden tool called: {sorted(set(case['forbid']) & all_called)}")

    if "approvals" in case:
        checks["approvals"] = run.approvals == case["approvals"]
        if not checks["approvals"]:
            notes.append(f"{run.approvals} approval card(s), wanted {case['approvals']}")
    if "approvals_max" in case:
        checks["approvals_max"] = run.approvals <= case["approvals_max"]
    checks["no_unapproved_write"] = not run.calendar_changed
    if run.calendar_changed:
        notes.append("CALENDAR WAS MODIFIED without an approval")

    text = run.answer
    must = [bool(re.search(p, text, re.I)) for p in case.get("must", [])]
    checks["must"] = all(must)
    if not checks["must"]:
        missing = [p for p, ok in zip(case["must"], must, strict=True) if not ok]
        notes.append(f"answer lacks {missing}")
    bad = [p for p in case.get("must_not", []) if re.search(p, text, re.I)]
    checks["must_not"] = not bad
    if bad:
        notes.append(f"answer matched forbidden {bad}")

    hall = check_answer(text, run.sources, tolerated_times=[f"{FROZEN_NOW:%H:%M}"])
    checks["grounded"] = hall.ok
    if not hall.ok:
        notes.append(f"unsupported in answer: {hall.unsupported}")
    return {
        "checks": checks,
        "tool_selection": tool_sel,
        "hallucination": {"ok": hall.ok, "unsupported": hall.unsupported, "checked": hall.checked},
        "passed": all(checks.values()) and run.error is None,
        "notes": notes,
    }


# ---------------------------------------------------------------------------- running


async def _run_case(case: dict[str, Any], model: str) -> E2ERun:
    run = E2ERun(id=case["id"])
    with use_model(model):
        state, _ = build_fixture_state(get_settings(), preview_chars=40000)
    sid = f"eval-{case['id']}"
    before = state.calendar_writer.path.read_bytes()
    plan_steps: dict[str, str] = {}
    t0 = time.monotonic()
    rid = ""
    try:

        async def drive() -> None:
            nonlocal rid
            async for ev in state.orchestrator.run(sid, case["message"]):
                kind = ev.type
                if kind == "plan":
                    rid = ev.request_id
                    run.intent = ev.plan.intent
                    plan_steps.update({s.id: s.agent for s in ev.plan.steps})
                    run.plan_agents = sorted({s.agent for s in ev.plan.steps})
                elif kind == "step_result":
                    run.step_status[ev.id] = ev.status
                    run.tools_by_agent.setdefault(ev.agent, []).extend(ev.tool_calls)
                elif kind == "approval_required":
                    run.approvals += 1
                    run.sources += [ev.action.summary, ev.action.diff]
                elif kind == "final":
                    run.status, run.answer = ev.status, ev.text
                    run.tokens_in, run.tokens_out = ev.tokens_in, ev.tokens_out
                    rid = ev.request_id or rid
                elif kind == "error":
                    run.error = ev.message

        await asyncio.wait_for(drive(), REQUEST_TIMEOUT_S)
        spans = state.store.get_spans(rid) if rid else []
        run.sources += [s.output_preview for s in spans if s.kind is SpanKind.TOOL]
        run.sources += [case["message"], f"{FROZEN_NOW:%A %d %B %Y %H:%M}"]
    except Exception as exc:
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.latency_ms = (time.monotonic() - t0) * 1000
        run.calendar_changed = state.calendar_writer.path.read_bytes() != before
        await state.aclose()
    return run


def summarise_e2e(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    agents: dict[str, list[dict[str, bool]]] = defaultdict(list)
    for r in rows:
        for agent, v in r["grade"]["tool_selection"].items():
            agents[agent].append(v)
    lat = [r["run"]["latency_ms"] for r in rows]
    hall_checked = sum(r["grade"]["hallucination"]["checked"] for r in rows)
    hall_bad = sum(len(r["grade"]["hallucination"]["unsupported"]) for r in rows)
    passes = [1.0 if r["grade"]["passed"] else 0.0 for r in rows]
    _, lo, hi = bootstrap_ci(passes)
    return {
        "n": n,
        "task_success": rate(sum(r["grade"]["passed"] for r in rows), n) | {"boot_lo": lo, "boot_hi": hi},
        "grounded": rate(sum(r["grade"]["hallucination"]["ok"] for r in rows), n),
        "unsupported_claims": {"checked": hall_checked, "unsupported": hall_bad},
        "no_unapproved_write": rate(sum(r["grade"]["checks"]["no_unapproved_write"] for r in rows), n),
        "tool_selection": {
            a: {
                "all": rate(sum(v["ok"] for v in vs), len(vs)),
                "when_invoked": rate(sum(v["ok"] for v in vs if v["invoked"]), sum(v["invoked"] for v in vs)),
            }
            for a, vs in sorted(agents.items())
        },
        "by_category": {c: rate(sum(r["grade"]["passed"] for r in rs), len(rs)) for c, rs in sorted(by_cat.items())},
        "latency_ms": latency_summary(lat),
        "tokens_in_mean": sum(r["run"]["tokens_in"] for r in rows) / n if n else 0,
        "tokens_out_mean": sum(r["run"]["tokens_out"] for r in rows) / n if n else 0,
        "errors": rate(sum(r["run"]["error"] is not None for r in rows), n),
    }  # fmt: skip


async def run_e2e(
    model: str,
    cases: Sequence[dict[str, Any]] | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    cases = list(cases if cases is not None else load_cases("e2e"))
    rows: list[dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        run = await _run_case(case, model)
        grade = grade_e2e(case, run)
        rows.append({"id": case["id"], "category": case["category"], "message": case["message"],
                     "rubric": case.get("rubric", []), "run": asdict(run), "grade": grade})  # fmt: skip
        progress(
            f"[{i}/{len(cases)}] {'ok  ' if grade['passed'] else 'FAIL'} {case['id']:4} "
            f"{run.latency_ms / 1000:5.1f}s {run.status:8} {case['message'][:50]!r} {grade['notes']}"
        )
        save("e2e", model, {**meta(model, "e2e"), "partial": True, "cases": rows})
    doc = {**meta(model, "e2e"), "partial": False, "summary": summarise_e2e(rows), "cases": rows}
    save("e2e", model, doc)
    return doc
