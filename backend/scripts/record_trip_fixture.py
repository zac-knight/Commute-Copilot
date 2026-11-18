"""Record the demo trip (real SEQ timetable, frozen clock) used by demo mode's trip card.

uv run python -m scripts.record_trip_fixture
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from app.agents.transit.geo import build_trip_artifact
from app.config import get_settings
from app.core.timeutil import TZ
from app.demo.frozen import FIXTURES
from app.demo.harness import build_fixture_state


async def main() -> None:
    state, _ = build_fixture_state(get_settings())
    transit = state.orchestrator.transit
    assert transit is not None
    arrive = datetime(2026, 11, 19, 13, 55, tzinfo=TZ)  # the question the recorded answers used
    plan = await transit.plan_trip("home", "Chermside", arrive_by=arrive)
    art = build_trip_artifact(transit.static, json.loads(plan.model_dump_json()))
    assert art is not None
    out = FIXTURES / "trips" / "home_chermside.json"
    out.write_text(json.dumps(art, separators=(",", ":")))
    j = art["journeys"][0]
    print(f"wrote {out}: {j['depart']} -> {j['arrive']}, {j['summary']['total_m']} m")
    await state.aclose()


if __name__ == "__main__":
    asyncio.run(main())
