"""Hallucination check for final answers: every clock time, stop id and route number an answer
states must appear in the tool outputs (or the user's own message / the clock) that fed it.

Deliberately mechanical and conservative: it cannot judge whether a sentence is *true*, only whether
a concrete, checkable token has a source. Free-text claims ("it will be sunny") go to the judge.
Date-time ISO strings in tool outputs are expanded to the clock times they contain, so
"08:05" is supported by ``2026-11-18T08:05:00+10:00``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# 8:12, 08:12, 8:12am, 8.12pm, 14:10. Bare "8am" is also a time.
_TIME = re.compile(
    r"(?<![\d:])(\d{1,2})[:.](\d{2})\s*([ap]m)?(?![\d:])|(?<![\d:.])(\d{1,2})\s*([ap]m)\b", re.I
)
_ISO_TIME = re.compile(r"[T ](\d{2}):(\d{2})")
_HMS = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2}):\d{2}(?![\d:])")  # GTFS "24:05:00"
_STOP_ID = re.compile(r"\b(?:stop|stop id|stop_id)\s*#?\s*[:=]?\s*(\d{4,})\b", re.I)
_ROUTE = re.compile(r"\b(?:route|bus|service)\s+([0-9]{1,3}[A-Z]?)\b")
_UNIT_AFTER = re.compile(
    r"\s*(?:km|kms|mm|min|mins|minutes|%|°|m\b|c\b)", re.I
)  # "1.25 km" is not a time


def _norm_time(h: int, m: int, ampm: str | None) -> tuple[int, int]:
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    return (h, m)


def times_in(text: str) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for m in _TIME.finditer(text):
        if m.group(1) is not None:
            h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        else:
            h, mi, ap = int(m.group(4)), 0, m.group(5)
        if h <= 24 and mi < 60:
            out.add(_norm_time(h, mi, ap))
    return out


def _times_in_sources(blob: str) -> set[tuple[int, int]]:
    found = {(int(h), int(mi)) for h, mi in _ISO_TIME.findall(blob)}
    found |= {(int(h), int(mi)) for h, mi in _HMS.findall(blob)}
    # plain "08:12" and "8:12am" in already-formatted fields
    found |= times_in(blob)
    # 24:05 style (after midnight) is also 00:05
    found |= {(h % 24, mi) for h, mi in found if h >= 24}
    return found


@dataclass
class HallucinationReport:
    unsupported_times: list[str] = field(default_factory=list)
    unsupported_stops: list[str] = field(default_factory=list)
    unsupported_routes: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not (self.unsupported_times or self.unsupported_stops or self.unsupported_routes)

    @property
    def unsupported(self) -> list[str]:
        return self.unsupported_times + self.unsupported_stops + self.unsupported_routes


def _blob(sources: Iterable[Any]) -> str:
    return "\n".join(
        s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str) for s in sources
    )


def check_answer(
    answer: str, sources: Iterable[Any], *, tolerated_times: Iterable[str] = ()
) -> HallucinationReport:
    """``sources``: tool outputs, the user message and the frozen clock. ``tolerated_times``: times
    the system itself stated (e.g. 'now')."""
    blob = _blob(sources)
    allowed_times = _times_in_sources(blob) | {t for s in tolerated_times for t in times_in(s)}
    rep = HallucinationReport()
    for m in _TIME.finditer(answer):
        if m.group(3) is None and m.group(5) is None and _UNIT_AFTER.match(answer, m.end()):
            continue
        t = times_in(m.group(0))
        if not t:
            continue
        rep.checked += 1
        if not (t & allowed_times):
            rep.unsupported_times.append(m.group(0).strip())
    for m in _STOP_ID.finditer(answer):
        rep.checked += 1
        if m.group(1) not in blob:
            rep.unsupported_stops.append(m.group(1))
    low = blob.lower()
    for m in _ROUTE.finditer(answer):
        rep.checked += 1
        r = m.group(1)
        if not re.search(rf"(?<![0-9A-Za-z]){re.escape(r.lower())}(?![0-9A-Za-z])", low):
            rep.unsupported_routes.append(r)
    return rep
