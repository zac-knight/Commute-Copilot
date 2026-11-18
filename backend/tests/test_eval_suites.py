"""Tests for the evaluation machinery itself (a grader nobody tested is a ruler nobody calibrated).

Also: the deterministic suites are RUN here, so a regression in transit/advice/calendar/safety
behaviour fails `make test` and CI, not only `make eval`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.hallucination import check_answer, times_in
from app.evaluation.stats import bootstrap_ci, cohen_kappa, paired_bootstrap_diff
from evals import dsl
from evals import judge as judge_mod
from evals.deterministic import load_cases, run_advice, run_calendar, run_transit
from evals.live import E2ERun, _group_ok, grade_e2e, summarise_e2e
from evals.report import build_report, compare_router, load_results
from evals.run_eval import run_det
from evals.safety import run_safety

# --- the deterministic suites themselves --------------------------------------------------


async def test_deterministic_suites_all_pass() -> None:
    doc = await run_det()
    failures = [
        (name, c["id"], c["failures"])
        for name, s in doc["suites"].items()
        for c in s["cases"]
        if not c["passed"]
    ]
    assert failures == []
    n = {k: v["n"] for k, v in doc["suites"].items()}
    assert n["transit"] >= 20 and n["advice"] >= 10 and n["calendar"] >= 15 and n["safety"] >= 10


@pytest.mark.parametrize("suite", ["transit", "advice", "calendar", "safety", "e2e"])
def test_case_files_are_well_formed(suite: str) -> None:
    cases = load_cases(suite)
    assert cases and all(c["id"] and c["desc" if suite != "e2e" else "message"] for c in cases)


async def test_a_wrong_expectation_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner must FAIL a case whose expectation is false (a grader that can't fail is useless)."""
    import evals.deterministic as det

    real = det.load_cases

    def tampered(suite: str) -> list[dict[str, Any]]:
        cases = real(suite)
        if suite == "transit":
            cases[0]["expect"][0]["eq"] = "T-WRONG"
        return cases

    monkeypatch.setattr(det, "load_cases", tampered)
    results = await run_transit()
    assert not results[0].passed and "T-WRONG" in results[0].failures[0]
    assert all(r.passed for r in results[1:])


def test_advice_and_calendar_runners_fail_wrong_expectations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import evals.deterministic as det

    real = det.load_cases

    def tampered(suite: str) -> list[dict[str, Any]]:
        cases = real(suite)
        cases[0]["expect"][0] = {"path": "nonexistent.path", "eq": 1}
        return cases

    monkeypatch.setattr(det, "load_cases", tampered)
    assert not run_advice()[0].passed and not run_calendar()[0].passed


async def test_a_safety_failure_is_detected_when_the_defence_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation check: if execution skipped the approval check, the suite must go red."""
    from app.core import approvals

    def reckless(self: Any, action_id: str) -> Any:
        a = self._load(action_id)
        self._set(action_id, status=approvals.ActionStatus.APPROVED)  # pretend it was approved
        a = self._load(action_id)
        self._handlers[a.type].execute(a)
        return a

    monkeypatch.setattr(approvals.ApprovalGate, "execute_approved_action", reckless)
    results = await run_safety()
    failed = {r.id for r in results if not r.passed}
    assert {"sf07", "sf08"} <= failed  # unapproved + tampered execution now get through


# --- DSL ----------------------------------------------------------------------------------


DATA = {
    "a": [{"k": "x", "n": 1.04}, {"k": "y", "n": 2}],
    "s": "Hello World",
    "z": None,
    "t": ["Alpha", "beta"],
}


@pytest.mark.parametrize(
    ("spec", "ok"),
    [
        ({"path": "a.0.k", "eq": "x"}, True),
        ({"path": "a.0.k", "eq": "y"}, False),
        ({"path": "a.0.n", "approx": 1.0, "tol": 0.05}, True),
        ({"path": "a.0.n", "approx": 1.0, "tol": 0.01}, False),
        ({"path": "a", "len": 2}, True),
        ({"path": "a", "any": {"k": "y"}}, True),
        ({"path": "a", "none": {"k": "y"}}, False),
        ({"path": "s", "contains": "hello"}, True),
        ({"path": "s", "not_contains": "hello"}, False),
        ({"path": "s", "regex": r"W\w+d$"}, True),
        ({"path": "z", "absent": True}, True),
        ({"path": "nope", "absent": True}, True),
        ({"path": "s", "absent": True}, False),
        ({"path": "t", "has_text": "ALP"}, True),
        ({"path": "t", "not_has_text": "gamma"}, True),
        ({"path": "t", "contains": "Alpha"}, True),
        ({"path": "a.5.k", "eq": "x"}, False),
        ({"path": "a.0.n", "gte": 1}, True),
        ({"path": "a.0.n", "lte": 1}, False),
        ({"path": "s", "in": ["Hello World", "x"]}, True),
    ],
)
def test_dsl_assertions(spec: dict[str, Any], ok: bool) -> None:
    assert dsl.check(DATA, spec)[0] is ok


def test_dsl_rejects_unknown_assertion_and_reports_actual_value() -> None:
    with pytest.raises(ValueError, match="unknown assertion"):
        dsl.check(DATA, {"path": "s", "wibble": 1})
    ok, msg = dsl.check(DATA, {"path": "s", "eq": "nope"})
    assert not ok and "Hello World" in msg
    assert dsl.check_all(DATA, [{"path": "s", "eq": "Hello World"}]) == []


# --- hallucination check ------------------------------------------------------------------

SRC = [
    {
        "departures": [
            {"predicted_iso": "2026-11-18T08:05:00+10:00", "route": "680", "stop_id": "10795"}
        ]
    },
    "can I make the 8:12 train?",
]


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        ("The 680 leaves at 8:05am.", True),
        ("The 680 leaves at 08:05.", True),
        ("The 680 leaves at 8:06am.", False),  # a time with no source
        ("You asked about the 8:12 train.", True),  # from the user's own message
        ("Take route 680 from stop 10795.", True),
        ("Take route 412 to the city.", False),
        ("Board at stop 99999.", False),
        ("It is a 25 min walk, 1.25 km, 22.5°C, 80% chance.", True),  # numbers that are not times
        ("Leaves at 20:05.", False),  # 8:05pm is not 8:05am
        ("Leaves at 8pm.", False),
    ],
)
def test_hallucination_check(answer: str, ok: bool) -> None:
    assert check_answer(answer, SRC).ok is ok


def test_after_midnight_gtfs_times_support_the_next_day_clock_time() -> None:
    assert check_answer("It runs at 00:05.", [{"scheduled": "24:05:00"}]).ok
    assert times_in("8.12pm and 9am and 07:30") == {(20, 12), (9, 0), (7, 30)}


def test_tolerated_times_cover_the_clock() -> None:
    assert not check_answer("It is 07:45 now.", []).ok
    assert check_answer("It is 07:45 now.", [], tolerated_times=["07:45"]).ok


# --- statistics ---------------------------------------------------------------------------


def test_bootstrap_is_seeded_and_brackets_the_mean() -> None:
    xs = [1.0] * 30 + [0.0] * 10
    p, lo, hi = bootstrap_ci(xs)
    assert p == pytest.approx(0.75) and lo < 0.75 < hi and lo > 0.55 and hi < 0.9
    assert bootstrap_ci(xs) == (p, lo, hi)  # same seed, same answer
    assert bootstrap_ci([]) == (0.0, 0.0, 0.0)


def test_paired_bootstrap_detects_a_real_gap_and_not_a_tie() -> None:
    a = [1.0] * 40 + [0.0] * 10
    b = [1.0] * 25 + [0.0] * 25
    d, lo, _ = paired_bootstrap_diff(a, b)
    assert d == pytest.approx(0.3) and lo > 0  # CI excludes zero
    same = [1.0, 0.0] * 20
    _, lo2, hi2 = paired_bootstrap_diff(same, same)
    assert lo2 == 0 == hi2


def test_cohen_kappa_extremes() -> None:
    assert cohen_kappa(["pass", "fail", "pass"], ["pass", "fail", "pass"]) == 1.0
    assert cohen_kappa(["pass", "pass", "fail", "fail"], ["pass", "fail", "pass", "fail"]) == 0.0
    assert cohen_kappa(["pass"] * 4, ["pass"] * 4) == 1.0  # degenerate: identical constant labels
    assert cohen_kappa(["pass", "fail"], ["fail", "pass"]) == -1.0


# --- e2e grading --------------------------------------------------------------------------


def run(**kw: Any) -> E2ERun:
    base = dict(
        id="x", status="ok", tools_by_agent={"transit": ["next_departures"]},
        answer="The next 680 leaves at 7:49am.", sources=["07:49 680"], approvals=0,
    )  # fmt: skip
    return E2ERun(**{**base, **kw})


CASE = {
    "id": "x", "category": "transit", "message": "next 680?",
    "tools": {"transit": [{"any_of": ["next_departures", "plan_trip"]}]},
    "statuses": ["ok"], "must": ["7:49"], "must_not": ["cancelled"], "approvals": 0,
}  # fmt: skip


def test_grade_e2e_passes_a_good_run() -> None:
    g = grade_e2e(CASE, run())
    assert g["passed"] and all(g["checks"].values()) and g["notes"] == []


@pytest.mark.parametrize(
    ("kw", "failed_check"),
    [
        ({"status": "failed"}, "status"),
        ({"tools_by_agent": {"transit": ["service_alerts"]}}, "tools"),
        ({"tools_by_agent": {}}, "tools"),
        ({"answer": "The next 680 leaves at 9:15am."}, "must"),
        ({"answer": "7:49 but it is cancelled."}, "must_not"),
        ({"approvals": 1}, "approvals"),
        ({"calendar_changed": True}, "no_unapproved_write"),
        ({"answer": "The 680 leaves at 7:49 and 9:59."}, "grounded"),
    ],
)
def test_grade_e2e_catches_each_failure_mode(kw: dict[str, Any], failed_check: str) -> None:
    g = grade_e2e(CASE, run(**kw))
    assert not g["passed"] and g["checks"][failed_check] is False and g["notes"]


def test_grade_e2e_forbidden_tool_and_error_fail_the_case() -> None:
    c = {**CASE, "forbid": ["propose_delete_event"]}
    g = grade_e2e(
        c,
        run(tools_by_agent={"transit": ["next_departures"], "calendar": ["propose_delete_event"]}),
    )
    assert g["checks"]["forbidden_tools"] is False and not g["passed"]
    assert not grade_e2e(CASE, run(error="TimeoutError"))["passed"]
    assert _group_ok("a", {"a"}) and not _group_ok({"any_of": ["a", "b"]}, {"c"})


def test_tool_selection_separates_wrong_tools_from_not_being_invoked() -> None:
    case = {**CASE, "tools": {"transit": ["plan_trip"], "weather": ["get_forecast"]}}
    g = grade_e2e(case, run(tools_by_agent={"transit": ["plan_trip"]}))
    assert g["tool_selection"]["transit"] == {"invoked": True, "ok": True}
    assert g["tool_selection"]["weather"] == {"invoked": False, "ok": False}  # router never sent it


def test_summarise_e2e_counts() -> None:
    rows = []
    for i, ok in enumerate([True, True, False]):
        r = run(id=str(i), answer="7:49" if ok else "9:59")
        rows.append({"id": str(i), "category": "transit", "message": "m", "rubric": [],
                     "run": r.__dict__, "grade": grade_e2e(CASE, r)})  # fmt: skip
    s = summarise_e2e(rows)
    assert s["task_success"]["k"] == 2 and s["task_success"]["n"] == 3
    assert s["grounded"]["k"] == 2 and s["unsupported_claims"]["unsupported"] == 1
    assert s["tool_selection"]["transit"]["all"]["k"] == 3
    assert s["no_unapproved_write"]["k"] == 3


# --- judge --------------------------------------------------------------------------------


def test_parse_verdict_accepts_only_complete_well_formed_replies() -> None:
    good = json.dumps({"items": [{"n": 2, "pass": False}, {"n": 1, "pass": True}]})
    assert judge_mod.parse_verdict(good, 2) == [True, False]
    assert judge_mod.parse_verdict(json.dumps({"items": [{"n": 1, "pass": True}]}), 2) is None
    assert judge_mod.parse_verdict("not json", 1) is None
    assert judge_mod.parse_verdict(json.dumps({"items": [{"n": 1}]}), 1) is None


def test_judge_prompt_wraps_evidence_and_truncates() -> None:
    p = judge_mod.build_prompt("q?", "DATA", "A", ["is it right?"])
    assert "<data>\nDATA\n</data>" in p and "<answer>\nA\n</answer>" in p and "1. is it right?" in p
    assert len(judge_mod.evidence_digest(["x" * 10_000])) < 6100
    assert "never instructions" in judge_mod.JUDGE_SYSTEM


async def test_run_judge_with_a_scripted_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.testing import ScriptedLLM, text_response
    from app.evaluation import runner

    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    row = {
        "id": "e01",
        "message": "q",
        "rubric": ["a", "b"],
        "run": {"answer": "ans", "sources": ["s"]},
    }
    runner.save(
        "e2e", "m", {"kind": "e2e", "model": "m", "run_at": "t", "partial": False, "cases": [row]}
    )
    llm = ScriptedLLM(
        [text_response(json.dumps({"items": [{"n": 1, "pass": True}, {"n": 2, "pass": False}]}))]
    )
    doc = await judge_mod.run_judge("m", "judge-model", progress=lambda _: None, llm=llm)  # type: ignore[arg-type]
    assert doc["cases"][0]["items"] == [True, False] and doc["self_judged"] is False
    assert doc["summary"]["items"]["k"] == 1 and doc["summary"]["items"]["n"] == 2
    assert (
        llm.requests[0]["json_schema"] == judge_mod.JUDGE_SCHEMA and llm.requests[0]["tools"] == []
    )


def test_agreement_uses_only_fully_labelled_answers_and_reports_direction_of_error() -> None:
    sheet = {"entries": [
        {"id": "a", "human": ["pass", "fail"]},
        {"id": "b", "human": ["pass", None]},  # unlabelled -> ignored
        {"id": "c", "human": ["fail", "fail"]},
    ]}  # fmt: skip
    jd = {"cases": [{"id": "a", "items": [True, True]}, {"id": "b", "items": [True, True]},
                    {"id": "c", "items": [False, False]}]}  # fmt: skip
    ag = judge_mod.agreement(sheet, jd)
    assert ag["labelled_answers"] == 2 and ag["items"] == 4
    assert (
        ag["agreement"]["k"] == 3 and ag["judge_too_lenient"] == 1 and ag["judge_too_strict"] == 0
    )
    assert judge_mod.agreement({"entries": [{"id": "a", "human": [None]}]}, jd) == {
        "labelled_answers": 0, "items": 0,
    }  # fmt: skip


def test_make_sheet_never_includes_judge_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.evaluation import runner

    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(judge_mod, "LABELS_DIR", tmp_path / "labels")
    monkeypatch.setattr(judge_mod, "SHEET", tmp_path / "labels" / "sheet.yaml")
    row = {"id": "e01", "message": "q", "rubric": ["a"], "run": {"answer": "ans", "sources": ["s"]}}
    runner.save(
        "e2e", "m", {"kind": "e2e", "model": "m", "run_at": "t", "partial": False, "cases": [row]}
    )
    runner.save(
        "judge", "m", {"kind": "judge", "model": "m", "cases": [{"id": "e01", "items": [True]}]}
    )
    path = judge_mod.make_sheet("m")
    import yaml

    doc = yaml.safe_load(path.read_text())
    (entry,) = doc["entries"]
    # exactly what a human needs: no judge verdicts anywhere (no anchoring)
    assert set(entry) == {"id", "question", "answer", "data", "rubric", "human"}
    assert entry["human"] == [None] and doc["answer_model"] == "m"


# --- report -------------------------------------------------------------------------------


def _router_doc(model: str, passes: list[bool], lat: float) -> dict[str, Any]:
    from app.evaluation.stats import rate

    cases = [{"id": f"c{i}", "passed": p, "latency_ms": lat + i, "message": "m", "expected_intent": "transit",
              "expected_agents": ["transit"], "actual_intent": "transit" if p else "weather",
              "actual_agents": ["transit"] if p else ["weather"]} for i, p in enumerate(passes)]  # fmt: skip
    r = rate(sum(passes), len(passes))
    summary = {"exact": r, "intent": r, "over_acted": {"k": 0}, "over_clarified": {"k": 1},
               "latency_ms": {"p50": lat, "p95": lat * 2, "mean": lat}, "tokens_in_mean": 1200.0,
               "tokens_out_mean": 40.0, "by_category": {"transit": r}}  # fmt: skip
    return {"kind": "router", "model": model, "run_at": "2026-11-18T00:00:00", "router_prompt": "router-v3",
            "partial": False, "summary": summary, "cases": cases}  # fmt: skip


def test_compare_router_pairs_cases_and_reports_a_difference() -> None:
    a = _router_doc("big", [True] * 45 + [False] * 5, 5000)
    b = _router_doc("small", [True] * 30 + [False] * 20, 2000)
    c = compare_router(a, b)
    assert c["n"] == 50 and c["diff"] == pytest.approx(0.3) and c["lo"] > 0
    assert c["discordant"]["only_a"] == 15 and c["sign_p"] < 0.01


def test_report_without_live_results_says_not_run_and_makes_no_recommendation() -> None:
    results = {"det": None, "router": {}, "e2e": {}, "judge": {}}
    text = build_report(results, date(2026, 11, 18))
    assert text.count("NOT RUN") >= 3 and "no recommendation" in text
    assert "Caveats" in text and "2026-11-18" in text


def test_report_with_two_models_gives_a_comparison_with_intervals() -> None:
    results = {"det": None, "router": {"big": _router_doc("big", [True] * 45 + [False] * 5, 5000),
                                       "small": _router_doc("small", [True] * 30 + [False] * 20, 2000)},
               "e2e": {}, "judge": {}}  # fmt: skip
    text = build_report(results, date(2026, 11, 18))
    assert (
        "paired-bootstrap CI" in text
        and "Recommendation" in text
        and "NOT RUN: only one" not in text
    )
    tie = {"det": None, "router": {"a": _router_doc("a", [True, False] * 25, 1), "b": _router_doc("b", [True, False] * 25, 1)},
           "e2e": {}, "judge": {}}  # fmt: skip
    assert "not distinguishable" in build_report(tie, date(2026, 11, 18))


def test_report_lists_deterministic_failures() -> None:
    det = {"run_at": "now", "suites": {"transit": {"n": 2, "passed": 1, "rate": {"k": 1, "n": 2, "rate": 0.5, "lo": 0.1, "hi": 0.9},
           "cases": [{"id": "tx1", "desc": "d", "passed": True, "failures": []},
                     {"id": "tx2", "desc": "broken", "passed": False, "failures": ["expected X"]}]}}}  # fmt: skip
    text = build_report({"det": det, "router": {}, "e2e": {}, "judge": {}}, date(2026, 11, 18))
    assert "`tx2` broken: expected X" in text and "**1/2**" in text


def test_load_results_ignores_partial_runs_and_missing_dir(tmp_path: Path) -> None:
    assert load_results(tmp_path / "nope") == {"det": None, "router": {}, "e2e": {}, "judge": {}}
    (tmp_path / "a__router.json").write_text(
        json.dumps({"kind": "router", "model": "a", "partial": True})
    )
    (tmp_path / "b__router.json").write_text(
        json.dumps({"kind": "router", "model": "b", "partial": False})
    )
    assert list(load_results(tmp_path)["router"]) == ["b"]


# --- /eval/latest summary -----------------------------------------------------------------


def test_latest_summary_reports_only_complete_runs_and_says_unavailable_when_empty(
    tmp_path: Path,
) -> None:
    from app.evaluation.summary import latest_summary

    assert latest_summary(tmp_path / "missing") == {
        "available": False, "deterministic": None, "router": {}, "e2e": {},
    }  # fmt: skip
    assert latest_summary(tmp_path)["available"] is False
    (tmp_path / "deterministic.json").write_text(
        json.dumps({"run_at": "t", "suites": {"transit": {"n": 3, "passed": 3, "cases": []}}})
    )
    (tmp_path / "a__router.json").write_text(
        json.dumps({"kind": "router", "model": "a", "partial": True})
    )
    (tmp_path / "bad.json").write_text("{not json")
    r = _router_doc("b", [True, False], 1000)
    r["summary"]["n"] = 2
    (tmp_path / "b__router.json").write_text(json.dumps(r))
    s = latest_summary(tmp_path)
    t = s["deterministic"]["suites"]["transit"]
    assert s["available"] and (t["passed"], t["n"]) == (3, 3) and t["cases"] == []
    assert list(s["router"]) == ["b"] and s["router"]["b"]["exact"]["k"] == 1
