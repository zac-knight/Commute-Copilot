"""Compact summary of the latest evaluation results, for ``GET /eval/latest`` and the UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evaluation.runner import RESULTS_DIR


def _read(path: Path) -> dict[str, Any] | None:
    try:
        doc: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc


def latest_summary(results_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    """What has been evaluated, headline numbers only. Partial (interrupted) runs are skipped, and
    anything not run is simply absent: the UI should show 'not run', never a made-up number."""
    out: dict[str, Any] = {"available": False, "deterministic": None, "router": {}, "e2e": {}}
    if not results_dir.exists():
        return out
    for path in sorted(results_dir.glob("*.json")):
        doc = _read(path)
        if doc is None:
            continue
        if path.name == "deterministic.json":
            out["deterministic"] = {
                "run_at": doc.get("run_at"),
                "suites": {
                    k: {
                        "passed": v["passed"],
                        "n": v["n"],
                        "rate": v.get("rate"),
                        "cases": [
                            {"id": c["id"], "desc": c["desc"], "passed": c["passed"]}
                            for c in v.get("cases", [])
                        ],
                    }
                    for k, v in doc["suites"].items()
                },
            }
        elif doc.get("partial"):
            continue
        elif doc.get("kind") == "router":
            s = doc["summary"]
            out["router"][doc["model"]] = {
                "run_at": doc["run_at"], "n": s["n"], "exact": s["exact"], "intent": s["intent"],
                "over_acted": s["over_acted"]["k"], "over_clarified": s["over_clarified"]["k"],
                "latency_ms": s["latency_ms"], "by_category": s["by_category"],
                "tokens_in_mean": s["tokens_in_mean"], "tokens_out_mean": s["tokens_out_mean"],
            }  # fmt: skip
        elif doc.get("kind") == "e2e":
            s = doc["summary"]
            out["e2e"][doc["model"]] = {
                "run_at": doc["run_at"], "n": s["n"], "task_success": s["task_success"],
                "grounded": s["grounded"], "latency_ms": s["latency_ms"],
                "unsupported_claims": s["unsupported_claims"],
                "no_unapproved_write": s["no_unapproved_write"],
                "tool_selection": s["tool_selection"], "by_category": s["by_category"],
            }  # fmt: skip
    out["available"] = bool(out["deterministic"] or out["router"] or out["e2e"])
    return out
