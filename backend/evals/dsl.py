"""A tiny assertion language for YAML eval cases, so expectations are data, not code.

expect:
  - {path: departures.0.status, eq: cancelled}
  - {path: departures.0.delay_min, approx: 5.0, tol: 0.1}
  - {path: departures, len: 3}
  - {path: note, contains: Cancelled}
  - {path: departures.1.predicted, absent: true}
  - {path: legs, any: {kind: walk}}          # some list item has these field values
  - {path: reasons, has_text: "12 min late"} # some string in a list contains this text
"""

from __future__ import annotations

import re
from typing import Any

MISSING = object()


def lookup(data: Any, path: str) -> Any:
    cur = data
    for part in path.split(".") if path else []:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return MISSING
        elif isinstance(cur, dict):
            if part not in cur:
                return MISSING
            cur = cur[part]
        else:
            return MISSING
    return cur


def check(data: Any, spec: dict[str, Any]) -> tuple[bool, str]:
    """(passed, message). ``message`` explains a failure with the actual value."""
    path = spec.get("path", "")
    got = lookup(data, path)
    shown = "<missing>" if got is MISSING else repr(got)
    if spec.get("absent"):
        ok = got is MISSING or got is None
        return ok, f"{path}: expected absent, got {shown}"
    if got is MISSING:
        return False, f"{path}: missing"
    if "eq" in spec:
        return got == spec["eq"], f"{path}: expected {spec['eq']!r}, got {shown}"
    if "approx" in spec:
        tol = float(spec.get("tol", 0.01))
        ok = isinstance(got, int | float) and abs(got - float(spec["approx"])) <= tol
        return ok, f"{path}: expected {spec['approx']}±{tol}, got {shown}"
    if "in" in spec:
        return got in spec["in"], f"{path}: expected one of {spec['in']}, got {shown}"
    if "contains" in spec:
        want = spec["contains"]
        ok = (want.lower() in got.lower()) if isinstance(got, str) else want in got
        return ok, f"{path}: expected to contain {want!r}, got {shown}"
    if "has_text" in spec:  # some string item of a list contains this substring
        want = spec["has_text"].lower()
        ok = any(isinstance(i, str) and want in i.lower() for i in got)
        return ok, f"{path}: no item containing {spec['has_text']!r}; got {shown}"
    if "not_has_text" in spec:
        want = spec["not_has_text"].lower()
        bad = any(isinstance(i, str) and want in i.lower() for i in got)
        return not bad, f"{path}: unexpected item containing {spec['not_has_text']!r}"
    if "not_contains" in spec:
        want = spec["not_contains"]
        bad = (want.lower() in got.lower()) if isinstance(got, str) else want in got
        return not bad, f"{path}: expected NOT to contain {want!r}, got {shown}"
    if "regex" in spec:
        return bool(re.search(spec["regex"], str(got))), f"{path}: {spec['regex']!r} vs {shown}"
    if "len" in spec:
        return len(got) == spec["len"], f"{path}: expected length {spec['len']}, got {len(got)}"
    if "gte" in spec:
        return got >= spec["gte"], f"{path}: expected >= {spec['gte']}, got {shown}"
    if "lte" in spec:
        return got <= spec["lte"], f"{path}: expected <= {spec['lte']}, got {shown}"
    if "any" in spec:
        want = spec["any"]
        ok = any(isinstance(i, dict) and all(i.get(k) == v for k, v in want.items()) for i in got)
        return ok, f"{path}: no item matching {want}"
    if "none" in spec:
        want = spec["none"]
        bad = any(isinstance(i, dict) and all(i.get(k) == v for k, v in want.items()) for i in got)
        return not bad, f"{path}: unexpected item matching {want}"
    raise ValueError(f"unknown assertion in {spec}")


def check_all(data: Any, specs: list[dict[str, Any]]) -> list[str]:
    """Failure messages (empty list = all passed)."""
    return [msg for ok, msg in (check(data, s) for s in specs) if not ok]
