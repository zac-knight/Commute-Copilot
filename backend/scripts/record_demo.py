"""Record the demo transcripts from the REAL system (local Ollama model, real GTFS timetable) with a
frozen clock, frozen weather and frozen realtime. Run once; commit the resulting JSON.

    uv run python -m scripts.record_demo            # all scenarios
    uv run python -m scripts.record_demo 4 5        # only scenarios 4 and 5 (1-based)
Takes ~10 minutes (the local model is slow).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from app.config import ROLES, get_settings
from app.demo.frozen import (
    FROZEN_NOW,
)
from app.demo.harness import build_fixture_state
from app.orchestrator.events import Final

OUT = Path(__file__).resolve().parents[1] / "app" / "demo" / "fixtures" / "transcripts"

SCENARIOS: list[dict[str, object]] = [
    {
        "name": "next_buses",
        "prompts": [
            "Next 3 buses from home to Chermside",
            "When are the next buses from North Lakes to Chermside?",
            "What buses go to Chermside soon?",
        ],
    },
    {
        "name": "slot_and_trip",
        "prompts": [
            "Find me a free 30 minute slot tomorrow afternoon and tell me how to get to Chermside "
            "for it.",
            "Find a free half hour tomorrow afternoon and how do I get to Chermside for it?",
        ],
    },
    {
        "name": "leave_now_rain",
        "prompts": [
            "Should I leave now for Chermside or wait? It looks like rain.",
            "Leave now for Chermside or wait for the rain?",
        ],
    },
    {
        "name": "dentist",
        "prompts": [
            "Add 'Dentist' Thursday 2pm at Chermside and warn me if I can't get there on time.",
            "Add Dentist on Thursday at 2pm at Chermside and tell me if I can't make it in time",
        ],
    },
    {
        "name": "clarify",
        "prompts": [
            "Should I leave now or wait? It looks like rain.",
            "Should I leave now or wait?",
        ],
    },
    {"name": "out_of_scope", "prompts": ["Book me a flight to Sydney.", "Order me a pizza"]},
    {
        "name": "hostile_calendar",
        "prompts": [
            "What's on my calendar tomorrow?",
            "What do I have on tomorrow?",
            "Summarise tomorrow's schedule.",
        ],
    },
]


async def main(only: set[int]) -> None:
    state, _tmp = build_fixture_state(get_settings())
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        for i, sc in enumerate(SCENARIOS, 1):
            if only and i not in only:
                continue
            prompt = str(sc["prompts"][0])  # type: ignore[index]
            print(f"[{i}/{len(SCENARIOS)}] {sc['name']}: {prompt}", flush=True)
            t0 = time.monotonic()
            events: list[dict[str, object]] = []
            rid = ""
            async for ev in state.orchestrator.run(f"rec-{i}", prompt):
                events.append(
                    {"t": round(time.monotonic() - t0, 2), "event": ev.model_dump(mode="json")}
                )
                if isinstance(ev, Final):
                    rid = ev.request_id
                    print(
                        f"      -> {ev.status} ({time.monotonic() - t0:.0f}s): {ev.text[:160]!r}",
                        flush=True,
                    )
            spans = state.store.get_spans(rid)
            root_start = min(s.start for s in spans)
            rows = []
            for s in spans:
                d = s.model_dump(mode="json")
                d["start"] = round(s.start - root_start, 4)
                d["end"] = round((s.end or s.start) - root_start, 4)
                rows.append(d)
            doc = {
                "name": sc["name"],
                "prompts": sc["prompts"],
                "model": ROLES["agent"].model,
                "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "frozen_now": FROZEN_NOW.isoformat(),
                "events": events,
                "spans": rows,
            }
            (OUT / f"{i:02d}_{sc['name']}.json").write_text(
                json.dumps(doc, indent=1, ensure_ascii=False)
            )
    finally:
        await state.aclose()


if __name__ == "__main__":
    asyncio.run(main({int(a) for a in sys.argv[1:]}))
