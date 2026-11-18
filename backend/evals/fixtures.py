"""Frozen, offline worlds for the deterministic evals. The timetable is the tiny hand-made GTFS
feed in ``tests/transit_fixtures.py`` (a 'frozen slice' that CI can build from nothing); realtime
snapshots are described in each YAML case."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agents.transit.gtfs_realtime import FeedResult, parse_alerts, parse_trip_updates
from app.agents.transit.planner import JourneyPlanner
from app.agents.transit.service import TransitService
from app.core.memory import UserProfile
from app.core.timeutil import TZ
from tests.transit_fixtures import alert_feed, build_static, trip_update_feed


class CaseRealtime:
    """Stands in for the realtime client with a fixed snapshot, or a failure."""

    def __init__(self, spec: dict[str, Any], now: datetime) -> None:
        self.error: str | None = spec.get("error")
        self.stale: bool = bool(spec.get("stale"))
        self.age: float = float(spec.get("age", 12.0))
        trips = [_trip(t) for t in spec.get("trips", [])]
        self.snap = parse_trip_updates(trip_update_feed(trips), 0.0)
        alerts = [_alert(a, int(now.timestamp())) for a in spec.get("alerts", [])]
        self.alert_snap = parse_alerts(alert_feed(alerts), 0.0)

    def cache_ages(self) -> dict[str, float]:
        return {"trips": self.age}

    async def trip_updates(self) -> FeedResult[Any]:
        if self.error and not self.stale:
            return FeedResult(None, None, error=self.error)
        return FeedResult(self.snap, self.age, stale=self.stale, error=self.error)

    async def alerts(self) -> FeedResult[Any]:
        if self.error and not self.stale:
            return FeedResult(None, None, error=self.error)
        return FeedResult(self.alert_snap, self.age, stale=self.stale, error=self.error)


def _trip(t: dict[str, Any]) -> dict[str, Any]:
    t = dict(t)
    t["start_date"] = str(t.get("start_date", ""))
    return t


def _alert(a: dict[str, Any], now_epoch: int) -> dict[str, Any]:
    """``periods_rel: [[-60, 60]]`` = active from 60 s ago to 60 s ahead (seconds from 'now')."""
    a = dict(a)
    rel = a.pop("periods_rel", None)
    if rel is not None:
        a["periods"] = [(now_epoch + s, None if e is None else now_epoch + e) for s, e in rel]
    return a


def parse_dt(text: str) -> datetime:
    """'2026-11-09T07:50' in Australia/Brisbane."""
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def build_transit(now: str, rt: dict[str, Any] | None = None) -> TransitService:
    tmp = Path(tempfile.mkdtemp(prefix="eval-transit-"))
    static = build_static(tmp)
    return TransitService(
        static,
        CaseRealtime(rt or {}, parse_dt(now)),  # type: ignore[arg-type]
        JourneyPlanner(static),  # type: ignore[arg-type]
        UserProfile(home="Alpha Street", destinations={"dee": "Dee Station"}),
        clock=lambda: parse_dt(now),
    )
