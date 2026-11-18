"""Generate ``docs/evaluation.md`` from the JSON results in ``eval_results/``.

Pure functions over loaded dicts (so the report is unit-tested), plus a thin file loader. Whatever has
not been run is reported as NOT RUN rather than omitted: an absent number is information.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from app.evaluation.router_cases import ROUTER_CASES
from app.evaluation.stats import (
    bootstrap_ci,
    paired_bootstrap_diff,
    paired_differences,
    pct,
    sign_test_p,
)
from evals import judge as judge_mod

BACKEND = Path(__file__).resolve().parents[1]
RESULTS_DIR = BACKEND / "eval_results"
REPORT_PATH = BACKEND.parent / "docs" / "evaluation.md"

# group -> (target from the brief, how it runs)
TARGETS = {
    "routing": (35, "live model"), "transit": (20, "deterministic"), "advice": (10, "deterministic"),
    "calendar": (15, "deterministic"), "safety": (10, "deterministic + live"), "e2e": (10, "live model"),
}  # fmt: skip


def load_results(results_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    """{'det': ..., 'router': {model: doc}, 'e2e': {...}, 'judge': {...}} (only complete runs)."""
    out: dict[str, Any] = {"det": None, "router": {}, "e2e": {}, "judge": {}}
    if not results_dir.exists():
        return out
    for path in sorted(results_dir.glob("*.json")):
        doc = json.loads(path.read_text())
        if path.name == "deterministic.json":
            out["det"] = doc
        elif doc.get("kind") in ("router", "e2e", "judge") and not doc.get("partial"):
            out[doc["kind"]][doc["model"]] = doc
    return out


# ---------------------------------------------------------------------------- sections


def _case_counts(det: dict[str, Any] | None, e2e_cases: int) -> list[str]:
    n = {k: v["n"] for k, v in (det or {}).get("suites", {}).items()}
    n["routing"] = len(ROUTER_CASES)
    n["e2e"] = e2e_cases
    rows = ["| Group | Brief target | Cases written | Runs |", "|---|---|---|---|"]
    for g, (target, how) in TARGETS.items():
        rows.append(f"| {g} | about {target} | {n.get(g, 'n/a')} | {how} |")
    total = sum(v for v in n.values() if isinstance(v, int))
    rows.append(f"| **total** | at least 80 | **{total}** | |")
    return rows


def _det_section(det: dict[str, Any] | None) -> list[str]:
    if det is None:
        return [
            "## Deterministic suites",
            "",
            "**NOT RUN.** `uv run python -m evals.run_eval det`.",
            "",
        ]
    rows = ["## Deterministic suites (no model, no network; run in CI on every push)", "",
            f"Run at {det['run_at']}.", "", "| Suite | Cases | Passed | Pass rate (95% Wilson CI) |",
            "|---|---|---|---|"]  # fmt: skip
    for name, s in det["suites"].items():
        rows.append(f"| {name} | {s['n']} | {s['passed']} | {pct(s['rate'])} |")
    failed = [c for s in det["suites"].values() for c in s["cases"] if not c["passed"]]
    rows += ["", "**Failures:** " + ("none." if not failed else "")]
    for c in failed:
        rows.append(f"- `{c['id']}` {c['desc']}: {'; '.join(c['failures'])}")
    rows += [
        "",
        "These suites check hand-derived expectations for code written by the same person, so they are "
        "regression guards more than bug finders. Writing the cases did surface two mistakes, both in "
        "the *expectations* (a 24 h look-ahead boundary, and a 'minor hazard' level miscounted as "
        "blocking); the code was right both times.",
        "",
    ]
    return rows


def _router_row(model: str, d: dict[str, Any]) -> str:
    s = d["summary"]
    lat = s["latency_ms"]
    return (
        f"| `{model}` | {pct(s['exact'])} | {pct(s['intent'])} | {s['over_acted']['k']} | "
        f"{s['over_clarified']['k']} | {lat['p50'] / 1000:.1f} s / {lat['p95'] / 1000:.1f} s | "
        f"{s['tokens_in_mean']:.0f} in / {s['tokens_out_mean']:.0f} out |"
    )


def compare_router(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Paired comparison of two complete router runs on the SAME cases."""
    ia = {c["id"]: c for c in a["cases"]}
    ib = {c["id"]: c for c in b["cases"]}
    ids = sorted(set(ia) & set(ib))
    pa = [1.0 if ia[i]["passed"] else 0.0 for i in ids]
    pb = [1.0 if ib[i]["passed"] else 0.0 for i in ids]
    diff, lo, hi = paired_bootstrap_diff(pa, pb)
    disc = paired_differences([bool(x) for x in pa], [bool(x) for x in pb])
    _, la_lo, la_hi = bootstrap_ci([ia[i]["latency_ms"] for i in ids])
    _, lb_lo, lb_hi = bootstrap_ci([ib[i]["latency_ms"] for i in ids])
    return {
        "n": len(ids), "diff": diff, "lo": lo, "hi": hi, "discordant": disc,
        "sign_p": sign_test_p(disc["only_a"], disc["only_b"]),
        "lat_a": (la_lo, la_hi), "lat_b": (lb_lo, lb_hi),
    }  # fmt: skip


def _router_section(router: dict[str, Any]) -> list[str]:
    if not router:
        return [
            "## Routing",
            "",
            "**NOT RUN.** `uv run python -m evals.run_eval router --model qwen3:8b`.",
            "",
        ]
    out = [
        "## Routing (live model, 'does the plan have the right shape?')", "",
        f"{len(ROUTER_CASES)} labelled messages, graded mechanically: intent, set of agents and the "
        "dependency edges must all match. *Over-acted* = ran agents when a clarification or refusal was "
        "right (the risky error); *over-clarified* = asked a question when it should have acted.", "",
        "| Model | Exact plan | Intent | Over-acted | Over-clarified | Latency p50 / p95 | Tokens/request |",
        "|---|---|---|---|---|---|---|",
    ]  # fmt: skip
    for m, d in router.items():
        out.append(_router_row(m, d))
    for m, d in router.items():
        out += ["", f"**`{m}` by category** (run {d['run_at']}, prompt `{d['router_prompt']}`)", "",
                "| Category | Passed |", "|---|---|"]  # fmt: skip
        for c, r in d["summary"]["by_category"].items():
            out.append(f"| {c} | {pct(r)} |")
        wrong = [c for c in d["cases"] if not c["passed"]][:15]
        if wrong:
            out += ["", "Misrouted (first 15):", ""]
            for c in wrong:
                out.append(
                    f"- `{c['id']}` {c['message']!r}: expected {c['expected_intent']} "
                    f"{c['expected_agents']}, got {c['actual_intent']} {c['actual_agents']}"
                    + (f" ({c['error']})" if c.get("error") else "")
                )
    out.append("")
    return out


def _comparison_section(router: dict[str, Any]) -> list[str]:
    out = ["## Model comparison (router)", ""]
    models = list(router)
    if len(models) < 2:
        out += [
            "**NOT RUN: only one model has complete router results"
            + (f" (`{models[0]}`)" if models else "")
            + ".** The brief asks for a Haiku-vs-Sonnet comparison; this project is local-only (see "
            "`docs/decisions.md`), so the comparison is between two local models. To run it: "
            "`ollama pull qwen3:14b` then `make eval-live MODELS='qwen3:8b qwen3:14b'`. "
            "Until then there is **no recommendation**: one model's numbers cannot say whether a bigger "
            "one would help.",
            "",
        ]
        return out
    a, b = models[0], models[1]
    c = compare_router(router[a], router[b])
    sig = c["lo"] > 0 or c["hi"] < 0
    out += [
        f"`{a}` vs `{b}` on the same {c['n']} cases (paired).", "",
        f"- Accuracy difference ({a} - {b}): **{100 * c['diff']:+.1f} points**, 95% paired-bootstrap CI "
        f"[{100 * c['lo']:+.1f}, {100 * c['hi']:+.1f}].",
        f"- Cases only {a} got right: {c['discordant']['only_a']}; only {b}: {c['discordant']['only_b']}; "
        f"exact sign test p = {c['sign_p']:.3f}.",
        f"- Latency per request, 95% bootstrap CI of the mean: {a} {c['lat_a'][0] / 1000:.1f}-{c['lat_a'][1] / 1000:.1f} s; "
        f"{b} {c['lat_b'][0] / 1000:.1f}-{c['lat_b'][1] / 1000:.1f} s.",
        "- Cost: $0 for both (local). Tokens are in the table above.",
        "",
        ("**Recommendation:** the accuracy gap is unlikely to be case-sampling noise on this set; prefer the "
         "more accurate model for routing unless its latency is unacceptable."
         if sig else
         "**Recommendation:** the interval includes 0, so these models are **not distinguishable on this "
         "case set**; prefer the faster one."),
        "A 'meaningful' difference here means the CI excludes 0 *and* is large enough to matter to a user; "
        "it says nothing about messages unlike these cases.",
        "",
    ]  # fmt: skip
    return out


def _e2e_section(e2e: dict[str, Any], judge: dict[str, Any]) -> list[str]:
    if not e2e:
        return ["## End to end (live model, frozen world)", "",
                "**NOT RUN.** `uv run python -m evals.run_eval e2e --model qwen3:8b`.", ""]  # fmt: skip
    out = ["## End to end (live model, frozen world)", "",
           "Each case runs the whole orchestrator. A case passes only if ALL of: acceptable final status, "
           "expected tools called by the expected agents, no forbidden tool, exact approval-card count, "
           "calendar bytes unchanged, answer regexes, **and the hallucination check** (every clock time, "
           "route number and stop id in the answer appears in tool output, the question, or the clock).", ""]  # fmt: skip
    for m, d in e2e.items():
        s = d["summary"]
        ts = s["tool_selection"]
        lat = s["latency_ms"]
        out += [
            f"### `{m}` (run {d['run_at']})", "",
            f"- Task success: {pct(s['task_success'])}; bootstrap CI "
            f"[{100 * s['task_success']['boot_lo']:.0f}, {100 * s['task_success']['boot_hi']:.0f}]%.",
            f"- Answers with no unsupported time/route/stop: {pct(s['grounded'])} "
            f"({s['unsupported_claims']['unsupported']} unsupported of {s['unsupported_claims']['checked']} checkable claims).",
            f"- No calendar write without approval: {pct(s['no_unapproved_write'])}.",
            f"- Latency p50 / p95: {lat['p50'] / 1000:.0f} s / {lat['p95'] / 1000:.0f} s; "
            f"{s['tokens_in_mean']:.0f} tokens in, {s['tokens_out_mean']:.0f} out per request; cost $0 (local).",
            f"- Errors (timeouts, crashes): {pct(s['errors'])}.", "",
            "| Agent | Tool selection (all cases that need it) | Tool selection (when the router invoked it) |",
            "|---|---|---|",
        ]  # fmt: skip
        for a, v in ts.items():
            out.append(f"| {a} | {pct(v['all'])} | {pct(v['when_invoked'])} |")
        out += ["", "| Category | Passed |", "|---|---|"]
        for c, r in s["by_category"].items():
            out.append(f"| {c} | {pct(r)} |")
        out += ["", "| Case | Result | Latency | Why it failed |", "|---|---|---|---|"]
        for r in d["cases"]:
            g = r["grade"]
            out.append(
                f"| `{r['id']}` {r['message'][:55]} | {'pass' if g['passed'] else 'FAIL'} | "
                f"{r['run']['latency_ms'] / 1000:.0f} s | {'; '.join(g['notes'])[:160]} |"
            )
        out.append("")
    out += _judge_section(judge)
    return out


def _judge_section(judge: dict[str, Any]) -> list[str]:
    out = ["### LLM judge (free-text quality) and its validation", ""]
    if not judge:
        out += ["**NOT RUN.** `uv run python -m evals.run_eval judge --model qwen3:8b`.", ""]
    for m, d in judge.items():
        s = d["summary"]
        out.append(
            f"- Answers by `{m}`, judged by `{d['judge_model']}`: {pct(s['items'])} of rubric items passed "
            f"over {s['answers']} answers ({s['unparseable']} unparseable)."
            + (
                "  **Self-judged** (same model wrote and judged): expect optimism."
                if d["self_judged"]
                else ""
            )
        )
    sheet = judge_mod.load_sheet()
    out.append("")
    if sheet is None:
        out += ["**Judge validation: NOT DONE.** No hand-labelled sheet exists. Generate one with "
                "`uv run python -m evals.run_eval sheet --model qwen3:8b`, label it, then run `agreement`. "
                "**Until then the judge's scores are unvalidated and are not quality numbers.**", ""]  # fmt: skip
        return out
    done = sum(all(x in ("pass", "fail") for x in e["human"]) for e in sheet["entries"])
    if not judge or done < 20:
        out += [f"**Judge validation: INCOMPLETE.** {done} of {len(sheet['entries'])} sheet entries are labelled; "
                "at least 20 answers are required before the agreement is reported.", ""]  # fmt: skip
        return out
    for d in judge.values():
        ag = judge_mod.agreement(sheet, d)
        if not ag["items"]:
            continue
        out += [
            f"**Judge vs human on {ag['labelled_answers']} hand-labelled answers ({ag['items']} rubric items):** "
            f"agreement {pct(ag['agreement'])}, Cohen's kappa {ag['kappa']:.2f}. Judge pass rate "
            f"{100 * ag['judge_pass_rate']:.0f}% vs human {100 * ag['human_pass_rate']:.0f}% "
            f"(judge too lenient on {ag['judge_too_lenient']} items, too strict on {ag['judge_too_strict']}).", ""]  # fmt: skip
    return out


CAVEATS = """## Caveats (read before quoting any number)

- **Small, synthetic, self-authored.** The cases were written by the person who wrote the system, from
  the same mental model of how it works. Several suites expect what the author already knows is true, so
  they guard against regressions more than they discover unknown failures. Near-duplicates of the router
  few-shot examples are excluded by a test, but the author's phrasing style is still in every case.
- **Intervals are narrower than the truth.** Wilson and bootstrap intervals only reflect sampling over
  these cases. They assume cases are exchangeable, ignore author bias, and say nothing about messages
  unlike these.
- **Frozen, partly hand-made world.** Timetable = the real TransLink SEQ feed (static) with a hand-made
  realtime snapshot (one delayed and one cancelled 680); weather = a real Open-Meteo capture with a
  *synthetic* shower added; calendar = a fake one. The deterministic transit cases use a separate
  hand-made mini feed so CI can build it from nothing. Real-world realtime messiness (stale vehicles,
  replacement trips) is covered by Phase 2's live checks, not by these scores.
- **One local 8B model, thinking off.** Results are for that model on this hardware at this prompt
  version; latency is a laptop number. They are not evidence about any hosted model.
- **The hallucination check is mechanical.** It catches an invented time, route number or stop id; it
  cannot catch a wrong sentence, a wrong inference or a wrong *combination* of true facts.
- **Safety is measured against a model that always obeys.** That isolates the approval gate (the real
  defence) from model behaviour; it does not measure how often a real model resists an injection (5 live
  runs in Phase 4 were an anecdote). Injection text variety is small (6 forms).
- **Single run per model, temperature as served by Ollama.** No repeated runs, so run-to-run variance
  is not measured; a rerun can differ.
- **No real calendar.** Google Calendar was not built (no OAuth client); calendar evals use the local
  calendar and crafted data.
"""

REPRODUCE = """## Reproduce

```bash
make eval                      # deterministic suites + this report (no model; what CI runs)
make eval-live                 # router + end-to-end + judge against Ollama (slow: tens of minutes)
cd backend
uv run python -m evals.run_eval sheet --model qwen3:8b      # writes evals/labels/judge_sheet.yaml
uv run python -m evals.run_eval agreement                    # after you fill the sheet by hand
```
"""


def build_report(results: dict[str, Any], today: date, e2e_cases: int = 0) -> str:
    models = sorted({*results["router"], *results["e2e"], *results["judge"]})
    lines = [
        "# Evaluation", "",
        f"*Generated {today.isoformat()} by `make eval` from `backend/eval_results/`. Do not edit by hand.*", "",
        "## Summary", "",
    ]  # fmt: skip
    det = results["det"]
    if det:
        total = sum(s["n"] for s in det["suites"].values())
        ok = sum(s["passed"] for s in det["suites"].values())
        lines.append(
            f"- Deterministic suites: **{ok}/{total}** passed (transit, advice, calendar, safety)."
        )
    for m, d in results["router"].items():
        lines.append(f"- Routing, `{m}`: **{pct(d['summary']['exact'])}** exact plans.")
    for m, d in results["e2e"].items():
        lines.append(f"- End to end, `{m}`: task success **{pct(d['summary']['task_success'])}**.")
    if not (results["router"] or results["e2e"]):
        lines.append("- Live-model evaluations: **NOT RUN** in this report.")
    lines.append(f"- Models evaluated live: {', '.join(f'`{m}`' for m in models) or 'none'}.")
    lines += ["", "## What exists", "", *_case_counts(det, e2e_cases), ""]
    lines += _det_section(det)
    lines += _router_section(results["router"])
    lines += _comparison_section(results["router"])
    lines += _e2e_section(results["e2e"], results["judge"])
    lines += [CAVEATS, REPRODUCE]
    return "\n".join(lines).rstrip() + "\n"


def write_report(results_dir: Path = RESULTS_DIR, path: Path = REPORT_PATH) -> Path:
    from evals.deterministic import load_cases

    text = build_report(load_results(results_dir), date.today(), len(load_cases("e2e")))
    path.write_text(text)
    return path
