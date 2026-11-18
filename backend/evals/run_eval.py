"""Evaluation CLI.

uv run python -m evals.run_eval det                     # deterministic suites (CI); exit 1 on any failure
uv run python -m evals.run_eval report                  # regenerate docs/evaluation.md
uv run python -m evals.run_eval router --model M        # live: routing accuracy
uv run python -m evals.run_eval e2e --model M [--only e01,m03]
uv run python -m evals.run_eval judge --model M [--judge-model J]
uv run python -m evals.run_eval sheet --model M         # write the hand-labelling sheet
uv run python -m evals.run_eval agreement               # judge vs human (after labelling)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from app.config import ROLES
from app.evaluation.runner import RESULTS_DIR, run_router
from app.evaluation.stats import rate
from evals import judge as judge_mod
from evals.deterministic import CaseResult, load_cases, run_advice, run_calendar, run_transit
from evals.report import write_report
from evals.safety import run_safety


async def run_det() -> dict[str, Any]:
    suites: dict[str, list[CaseResult]] = {
        "transit": await run_transit(),
        "advice": run_advice(),
        "calendar": run_calendar(),
        "safety": await run_safety(),
    }
    return {
        "schema": 1, "kind": "deterministic", "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "suites": {
            name: {
                "n": len(rs), "passed": sum(r.passed for r in rs),
                "rate": rate(sum(r.passed for r in rs), len(rs)),
                "cases": [r.to_dict() for r in rs],
            }
            for name, rs in suites.items()
        },
    }  # fmt: skip


def cmd_det() -> int:
    doc = asyncio.run(run_det())
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "deterministic.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    bad = 0
    for name, s in doc["suites"].items():
        print(f"{name:9} {s['passed']:3}/{s['n']:<3}")
        for c in s["cases"]:
            if not c["passed"]:
                bad += 1
                print(f"   FAIL {c['id']}: {c['desc']}\n        {'; '.join(c['failures'])}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument(
        "cmd", choices=["det", "report", "router", "e2e", "judge", "sheet", "agreement"]
    )
    ap.add_argument("--model", default=ROLES["router"].model, help="the model under test (answers)")
    ap.add_argument("--judge-model", default=None, help="judge model (default: same as --model)")
    ap.add_argument("--only", default="", help="comma-separated case ids (e2e only)")
    ap.add_argument("--limit", type=int, default=0, help="first N cases (router smoke test)")
    ns = ap.parse_args(argv)

    if ns.cmd == "det":
        return cmd_det()
    if ns.cmd == "report":
        print(f"wrote {write_report()}")
        return 0
    if ns.cmd == "router":
        from app.evaluation.router_cases import ROUTER_CASES

        cases = ROUTER_CASES[: ns.limit] if ns.limit else ROUTER_CASES
        doc = asyncio.run(run_router(ns.model, cases))
        print(f"exact {doc['summary']['exact']['rate']:.0%}")
        return 0
    if ns.cmd == "e2e":
        from evals.live import run_e2e

        cases = load_cases("e2e")
        if ns.only:
            keep = set(ns.only.split(","))
            cases = [c for c in cases if c["id"] in keep]
        doc = asyncio.run(run_e2e(ns.model, cases))
        print(f"task success {doc['summary']['task_success']['rate']:.0%}")
        return 0
    if ns.cmd == "judge":
        doc = asyncio.run(judge_mod.run_judge(ns.model, ns.judge_model or ns.model))
        print(f"judge pass rate {doc['summary']['items']['rate']:.0%}")
        return 0
    if ns.cmd == "sheet":
        print(f"wrote {judge_mod.make_sheet(ns.model)}; fill in the `human:` fields by hand")
        return 0
    sheet = judge_mod.load_sheet()
    if sheet is None:
        print("no sheet: run `sheet` first and label it")
        return 2
    from app.evaluation.runner import load

    jd = load("judge", sheet["answer_model"])
    if jd is None:
        print("no judge results for that model: run `judge` first")
        return 2
    print(json.dumps(judge_mod.agreement(sheet, jd), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
