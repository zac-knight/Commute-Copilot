"""Run evaluations against a live local model. Results are JSON files in ``eval_results/``."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import BACKEND_DIR, ROLES, RoleConfig, get_settings
from app.core.tracing import SpanKind
from app.demo.frozen import FROZEN_NOW
from app.demo.harness import build_fixture_state
from app.evaluation.grading import RouterGrade, grade_router
from app.evaluation.router_cases import ROUTER_CASES, RouterCase
from app.evaluation.stats import latency_summary, rate
from app.orchestrator.router import ROUTER_PROMPT
from app.orchestrator.wiring import AppState

RESULTS_DIR = BACKEND_DIR / "eval_results"


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


@contextmanager
def use_model(model: str):  # type: ignore[no-untyped-def]
    """Point every role at one model for the duration (restored afterwards)."""
    saved = dict(ROLES)
    for role, cfg in saved.items():
        ROLES[role] = RoleConfig(model=model, think=cfg.think)
    try:
        yield
    finally:
        ROLES.update(saved)


def save(kind: str, model: str, doc: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{slug(model)}__{kind}.json"
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    return path


def load(kind: str, model: str) -> dict[str, Any] | None:
    path = RESULTS_DIR / f"{slug(model)}__{kind}.json"
    return json.loads(path.read_text()) if path.exists() else None


def meta(model: str, kind: str) -> dict[str, Any]:
    return {
        "schema": 1, "kind": kind, "model": model,
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "frozen_now": FROZEN_NOW.isoformat(),
        "router_prompt": ROUTER_PROMPT.version,
    }  # fmt: skip


def _llm_tokens(state: AppState, rid: str) -> tuple[int, int]:
    spans = [s for s in state.store.get_spans(rid) if s.kind is SpanKind.LLM]
    return sum(s.tokens_in for s in spans), sum(s.tokens_out for s in spans)


# ----------------------------------------------------------------------------- router


def summarise_router(grades: Sequence[RouterGrade]) -> dict[str, Any]:
    n = len(grades)
    by_cat: dict[str, list[RouterGrade]] = defaultdict(list)
    for g in grades:
        by_cat[g.category].append(g)
    return {
        "n": n,
        "exact": rate(sum(g.passed for g in grades), n),
        "intent": rate(sum(g.intent_ok for g in grades), n),
        "agents": rate(sum(g.agents_ok for g in grades), n),
        "dependencies": rate(sum(g.deps_ok for g in grades), n),
        # The two ways to be wrong are not equally bad: acting when you should have asked
        # (or refused) can send the user somewhere they never named.
        "over_acted": rate(sum(g.over_acted for g in grades), n),
        "over_clarified": rate(sum(g.over_clarified for g in grades), n),
        "needed_repair": rate(sum(g.repaired for g in grades), n),
        "errors": rate(sum(g.error is not None for g in grades), n),
        "latency_ms": latency_summary(g.latency_ms for g in grades),
        "tokens_in_mean": sum(g.tokens_in for g in grades) / n if n else 0,
        "tokens_out_mean": sum(g.tokens_out for g in grades) / n if n else 0,
        "by_category": {
            c: rate(sum(g.passed for g in gs), len(gs)) for c, gs in sorted(by_cat.items())
        },
    }  # fmt: skip


async def run_router(
    model: str,
    cases: Sequence[RouterCase] = ROUTER_CASES,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    with use_model(model):
        state, _ = build_fixture_state(get_settings())
    router = state.orchestrator.router
    grades: list[RouterGrade] = []
    try:
        for i, case in enumerate(cases, 1):
            t0 = time.monotonic()
            plan, err = None, None
            with state.tracer.request() as rid:
                try:
                    plan = await router.route(case.message, list(case.history), FROZEN_NOW)
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
            g = grade_router(case, plan, err)
            g.latency_ms = (time.monotonic() - t0) * 1000
            g.tokens_in, g.tokens_out = _llm_tokens(state, rid)
            grades.append(g)
            mark = "ok  " if g.passed else "FAIL"
            progress(
                f"[{i}/{len(cases)}] {mark} {case.id:5} {case.message[:60]!r} -> {g.actual_intent}"
            )
            if i % 10 == 0:
                save("router", model, {**meta(model, "router"), "partial": True,
                                       "cases": [x.to_dict() for x in grades]})  # fmt: skip
    finally:
        await state.aclose()
    doc = {**meta(model, "router"), "partial": False, "summary": summarise_router(grades),
           "cases": [g.to_dict() for g in grades]}  # fmt: skip
    save("router", model, doc)
    return doc


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kind", choices=["router"])
    ap.add_argument("--model", default=ROLES["router"].model)
    ap.add_argument("--limit", type=int, default=0, help="only the first N cases (smoke test)")
    ns = ap.parse_args()
    chosen = ROUTER_CASES[: ns.limit] if ns.limit else ROUTER_CASES
    out = asyncio.run(run_router(ns.model, chosen))
    s = out["summary"]
    print(
        f"\nexact {s['exact']['rate']:.0%}  intent {s['intent']['rate']:.0%}  "
        f"over-acted {s['over_acted']['k']}"
    )
