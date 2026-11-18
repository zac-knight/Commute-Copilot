"""GTFS-Realtime: parsing, caching and delay semantics.

How realtime is interpreted (checked against the live TransLink SEQ feed, 2026-11-17):

* A realtime trip is keyed by ``(trip_id, start_date)``. A static ``trip_id`` runs on many
  dates, so trip_id alone is ambiguous.
* Trip ``schedule_relationship`` seen live: SCHEDULED, CANCELED, REPLACEMENT (matches a static
  trip; treated like SCHEDULED with authoritative updates) and ADDED (no static counterpart).
* ``StopTimeUpdate`` relationships seen live: SCHEDULED and SKIPPED. NO_DATA is handled per the
  spec (no prediction for that stop).
* Delays are in **seconds** (positive = late). When an event has both ``time`` and ``delay`` we
  trust ``time`` (spec: time takes precedence) and derive the delay from the schedule.
* Updates cover only some stops on most trips (132 of 228 live). Per the spec, a stop with no
  explicit update inherits the delay of the closest *preceding* update that has one. That is an
  estimate and is reported as ``propagated`` so callers can say so. A stop before any update
  gets no prediction (scheduled-only).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import httpx
from google.transit import gtfs_realtime_pb2 as pb

from app.agents.transit.gtfs_static import parse_yyyymmdd

RT_BASE = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ"
# NB: the brief assumed ".../ServiceAlerts"; the real endpoint is ".../alerts" (the former is 400).
TRIP_UPDATES_URL = f"{RT_BASE}/TripUpdates"
ALERTS_URL = f"{RT_BASE}/alerts"
VEHICLES_URL = f"{RT_BASE}/VehiclePositions"
VEHICLES_TTL_S = 8.0  # positions go stale fast; the other feeds can be cached longer

_TRIP_REL = pb.TripDescriptor.ScheduleRelationship
_STOP_REL = pb.TripUpdate.StopTimeUpdate.ScheduleRelationship


@dataclass(frozen=True)
class StopTimeUpdate:
    stop_sequence: int | None
    stop_id: str
    relationship: str
    arrival_delay: int | None = None
    arrival_time: int | None = None
    departure_delay: int | None = None
    departure_time: int | None = None


@dataclass(frozen=True)
class TripRT:
    trip_id: str
    start_date: date | None
    route_id: str | None
    relationship: str
    updates: tuple[StopTimeUpdate, ...] = ()


@dataclass(frozen=True)
class RealtimeSnapshot:
    feed_timestamp: int  # epoch seconds, from the feed header
    fetched_at: float
    trips: dict[tuple[str, date | None], TripRT] = field(default_factory=dict)

    def find(self, trip_id: str, service_date: date) -> TripRT | None:
        return self.trips.get((trip_id, service_date)) or self.trips.get((trip_id, None))

    def added_trips(self) -> list[TripRT]:
        return [t for t in self.trips.values() if t.relationship == "ADDED"]


def _event(ev: pb.TripUpdate.StopTimeEvent | None) -> tuple[int | None, int | None]:
    """(delay_s, time_epoch) of a StopTimeEvent; either may be absent."""
    if ev is None:
        return None, None
    return (ev.delay if ev.HasField("delay") else None, ev.time if ev.HasField("time") else None)


def parse_trip_updates(data: bytes, fetched_at: float) -> RealtimeSnapshot:
    feed = pb.FeedMessage()
    feed.ParseFromString(data)
    trips: dict[tuple[str, date | None], TripRT] = {}
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        updates = []
        for s in tu.stop_time_update:
            arr = s.arrival if s.HasField("arrival") else None
            dep = s.departure if s.HasField("departure") else None
            arr_delay, arr_time = _event(arr)
            dep_delay, dep_time = _event(dep)
            updates.append(
                StopTimeUpdate(
                    stop_sequence=s.stop_sequence if s.HasField("stop_sequence") else None,
                    stop_id=s.stop_id,
                    relationship=_STOP_REL.Name(s.schedule_relationship),
                    arrival_delay=arr_delay,
                    arrival_time=arr_time,
                    departure_delay=dep_delay,
                    departure_time=dep_time,
                )
            )
        updates.sort(key=lambda u: (u.stop_sequence is None, u.stop_sequence or 0))
        start = parse_yyyymmdd(tu.trip.start_date) if tu.trip.start_date else None
        rel = _TRIP_REL.Name(tu.trip.schedule_relationship)
        trips[(tu.trip.trip_id, start)] = TripRT(
            tu.trip.trip_id, start, tu.trip.route_id or None, rel, tuple(updates)
        )
    return RealtimeSnapshot(feed.header.timestamp, fetched_at, trips)


# --- delay resolution ----------------------------------------------------------------------

StopRTKind = Literal["explicit", "propagated", "skipped", "no_data", "cancelled"]


@dataclass(frozen=True)
class StopRT:
    kind: StopRTKind
    delay_s: int | None = None
    predicted_epoch: int | None = None


def resolve_stop_departure(
    trip: TripRT, stop_sequence: int, stop_id: str, scheduled_epoch: int
) -> StopRT:
    """Realtime state of the *departure* from one stop of a trip."""
    if trip.relationship in ("CANCELED", "DELETED"):
        return StopRT("cancelled")

    explicit = next((u for u in trip.updates if u.stop_sequence == stop_sequence), None)
    if explicit is None:
        # No update with this sequence: match on stop_id when it identifies the stop uniquely
        # (replacement/added runs can number stops differently from the static trip).
        by_stop = [u for u in trip.updates if u.stop_id == stop_id]
        if len(by_stop) == 1:
            explicit = by_stop[0]
    if explicit is not None:
        if explicit.relationship == "SKIPPED":
            return StopRT("skipped")
        if explicit.relationship == "NO_DATA":
            return StopRT("no_data")
        time_ = (
            explicit.departure_time
            if explicit.departure_time is not None
            else explicit.arrival_time
        )
        if time_ is not None:  # time takes precedence over delay
            return StopRT("explicit", time_ - scheduled_epoch, time_)
        delay = (
            explicit.departure_delay
            if explicit.departure_delay is not None
            else explicit.arrival_delay
        )
        if delay is not None:
            return StopRT("explicit", delay, scheduled_epoch + delay)
        return StopRT("no_data")

    # propagate from the closest preceding update that carries a delay
    for u in reversed(trip.updates):
        if u.stop_sequence is None or u.stop_sequence >= stop_sequence:
            continue
        if u.relationship != "SCHEDULED":
            continue
        delay = u.departure_delay if u.departure_delay is not None else u.arrival_delay
        if delay is not None:
            return StopRT("propagated", delay, scheduled_epoch + delay)
    return StopRT("no_data")


# --- alerts --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    alert_id: str
    header: str
    description: str
    cause: str
    effect: str
    url: str
    periods: tuple[tuple[int, int | None], ...]
    route_ids: frozenset[str]
    stop_ids: frozenset[str]

    def active_at(self, epoch: float) -> bool:
        if not self.periods:
            return True
        return any(s <= epoch and (e is None or epoch <= e) for s, e in self.periods)


@dataclass(frozen=True)
class AlertSnapshot:
    feed_timestamp: int
    fetched_at: float
    alerts: tuple[Alert, ...]


def _translated(ts: pb.TranslatedString) -> str:
    if not ts.translation:
        return ""
    for t in ts.translation:
        if t.language.lower().startswith("en"):
            return str(t.text)
    return str(ts.translation[0].text)


def parse_alerts(data: bytes, fetched_at: float) -> AlertSnapshot:
    feed = pb.FeedMessage()
    feed.ParseFromString(data)
    out = []
    for ent in feed.entity:
        if not ent.HasField("alert"):
            continue
        a = ent.alert
        periods = tuple((p.start, p.end if p.HasField("end") else None) for p in a.active_period)
        out.append(
            Alert(
                alert_id=ent.id,
                header=_translated(a.header_text),
                description=_translated(a.description_text),
                cause=pb.Alert.Cause.Name(a.cause),
                effect=pb.Alert.Effect.Name(a.effect),
                url=_translated(a.url),
                periods=periods,
                route_ids=frozenset(e.route_id for e in a.informed_entity if e.route_id),
                stop_ids=frozenset(e.stop_id for e in a.informed_entity if e.stop_id),
            )
        )
    return AlertSnapshot(feed.header.timestamp, fetched_at, tuple(out))


# --- vehicle positions ---------------------------------------------------------------------


@dataclass(frozen=True)
class VehiclePos:
    vehicle_id: str
    label: str
    trip_id: str
    route_id: str
    lat: float
    lon: float
    bearing: float | None
    speed_ms: float | None
    status: str  # INCOMING_AT | STOPPED_AT | IN_TRANSIT_TO | ""
    stop_id: str
    timestamp: int


@dataclass(frozen=True)
class VehicleSnapshot:
    feed_timestamp: int
    fetched_at: float
    vehicles: tuple[VehiclePos, ...]


def parse_vehicle_positions(data: bytes, fetched_at: float) -> VehicleSnapshot:
    feed = pb.FeedMessage()
    feed.ParseFromString(data)
    out = []
    for ent in feed.entity:
        if not ent.HasField("vehicle") or not ent.vehicle.HasField("position"):
            continue
        v = ent.vehicle
        pos = v.position
        if not (-90 <= pos.latitude <= 90 and -180 <= pos.longitude <= 180) or (
            pos.latitude == 0 and pos.longitude == 0
        ):
            continue
        status = (
            pb.VehiclePosition.VehicleStopStatus.Name(v.current_status)
            if v.HasField("current_status")
            else ""
        )
        out.append(
            VehiclePos(
                vehicle_id=v.vehicle.id or ent.id,
                label=v.vehicle.label,
                trip_id=v.trip.trip_id,
                route_id=v.trip.route_id,
                lat=pos.latitude,
                lon=pos.longitude,
                bearing=float(pos.bearing) if pos.HasField("bearing") else None,
                speed_ms=float(pos.speed) if pos.HasField("speed") else None,
                status=status,
                stop_id=v.stop_id,
                timestamp=int(v.timestamp),
            )
        )
    return VehicleSnapshot(int(feed.header.timestamp), fetched_at, tuple(out))


# --- client with TTL cache -----------------------------------------------------------------


@dataclass(frozen=True)
class FeedResult[T]:
    data: T | None
    age_s: float | None  # now - feed header timestamp (how old the *data* is)
    stale: bool = False  # True when serving a cached copy because the refresh failed
    error: str | None = None


class RealtimeClient:
    """Fetches the realtime feeds on demand with a short TTL cache.

    On a failed refresh the last good copy is served (``stale=True``) only while it is younger
    than ``max_stale_s``; beyond that the feed is reported unavailable, never silently old.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        ttl_s: float = 25.0,
        max_stale_s: float = 180.0,
        trip_updates_url: str = TRIP_UPDATES_URL,
        alerts_url: str = ALERTS_URL,
        vehicles_url: str = VEHICLES_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = client or httpx.AsyncClient(timeout=15.0)
        self._ttl = ttl_s
        self._max_stale = max_stale_s
        self._urls = {"trips": trip_updates_url, "alerts": alerts_url, "vehicles": vehicles_url}
        self._ttls = {"vehicles": min(ttl_s, VEHICLES_TTL_S)}
        self._clock = clock
        self._cache: dict[str, RealtimeSnapshot | AlertSnapshot | VehicleSnapshot] = {}
        self._locks = {
            "trips": asyncio.Lock(),
            "alerts": asyncio.Lock(),
            "vehicles": asyncio.Lock(),
        }

    async def trip_updates(self) -> FeedResult[RealtimeSnapshot]:
        res = await self._get("trips", parse_trip_updates)
        return res  # type: ignore[return-value]

    async def alerts(self) -> FeedResult[AlertSnapshot]:
        res = await self._get("alerts", parse_alerts)
        return res  # type: ignore[return-value]

    async def vehicles(self) -> FeedResult[VehicleSnapshot]:
        res = await self._get("vehicles", parse_vehicle_positions)
        return res  # type: ignore[return-value]

    async def _get(
        self,
        name: str,
        parse: Callable[[bytes, float], RealtimeSnapshot | AlertSnapshot | VehicleSnapshot],
    ) -> FeedResult[RealtimeSnapshot | AlertSnapshot | VehicleSnapshot]:
        async with self._locks[name]:  # concurrent callers share one fetch
            now = self._clock()
            cached = self._cache.get(name)
            if cached is not None and now - cached.fetched_at < self._ttls.get(name, self._ttl):
                return FeedResult(cached, now - cached.feed_timestamp)
            try:
                resp = await self._http.get(self._urls[name])
                resp.raise_for_status()
                snap = parse(resp.content, now)
            except Exception as exc:  # network, HTTP status or protobuf decode error
                err = f"{type(exc).__name__}: {exc}"
                if cached is not None and now - cached.fetched_at < self._max_stale:
                    return FeedResult(cached, now - cached.feed_timestamp, stale=True, error=err)
                return FeedResult(None, None, error=err)
            self._cache[name] = snap
            return FeedResult(snap, now - snap.feed_timestamp)

    def cache_ages(self) -> dict[str, float]:
        """Seconds since each cached feed's own timestamp (how old the DATA is), for /health."""
        now = self._clock()
        return {k: round(now - v.feed_timestamp, 1) for k, v in self._cache.items()}

    async def aclose(self) -> None:
        await self._http.aclose()
