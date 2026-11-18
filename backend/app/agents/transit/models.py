"""Pydantic output models for the transit tools. Times are local (Australia/Brisbane): a
short string for display ("08:12", or "Wed 18 Nov 08:12" when not today) plus an ISO string for
machine use (the orchestrator passes these to later steps)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

DepartureStatus = Literal["on_time", "delayed", "early", "cancelled", "scheduled_only", "added"]
DelayBasis = Literal["reported", "estimated_from_earlier_stop"]


class RealtimeStatus(BaseModel):
    source: Literal["realtime", "scheduled_only"]
    feed_age_s: int | None = None
    stale: bool = False
    note: str | None = None


class Departure(BaseModel):
    route: str
    route_name: str
    mode: Literal["bus", "train", "tram", "ferry"]
    headsign: str
    stop_id: str
    stop_name: str
    platform: str | None = None
    scheduled: str
    scheduled_iso: str
    predicted: str | None = None
    predicted_iso: str | None = None
    delay_min: float | None = None
    status: DepartureStatus
    delay_basis: DelayBasis | None = None
    trip_id: str
    service_date: str


class StopRef(BaseModel):
    stop_id: str
    name: str


class DeparturesResult(BaseModel):
    stops: list[StopRef]
    departures: list[Departure]
    realtime: RealtimeStatus
    generated_at: str
    note: str | None = None
    # When a destination was given and fewer direct services than asked for exist: journeys that
    # need one or more transfers (e.g. two buses). Each is a full planned journey.
    connections: list[JourneyOut] = []


class StopInfo(BaseModel):
    stop_id: str
    name: str
    modes: list[str]
    is_station: bool
    platform: str | None = None
    distance_m: int | None = None


class FindStopsResult(BaseModel):
    query: str
    stops: list[StopInfo]


class LegRealtime(BaseModel):
    status: DepartureStatus
    delay_min: float | None = None
    predicted_departure: str | None = None
    delay_basis: DelayBasis | None = None


class LegOut(BaseModel):
    kind: Literal["walk", "transit"]
    from_stop: str
    from_stop_id: str
    to_stop: str
    to_stop_id: str
    depart: str
    arrive: str
    depart_iso: str
    arrive_iso: str
    route: str | None = None
    route_name: str | None = None
    mode: Literal["bus", "train", "tram", "ferry"] | None = None
    headsign: str | None = None
    num_stops: int | None = None
    walk_m: int | None = None
    trip_id: str | None = None
    realtime: LegRealtime | None = None


class JourneyOut(BaseModel):
    depart: str
    arrive: str
    depart_iso: str
    arrive_iso: str
    duration_min: int
    transfers: int
    legs: list[LegOut]


class PlanTripResult(BaseModel):
    origin: list[StopRef]
    destination: list[StopRef]
    journeys: list[JourneyOut]
    basis: str  # what the plan is computed from
    realtime: RealtimeStatus
    note: str | None = None


class CanICatchResult(BaseModel):
    can_catch: bool | None  # None when the departure could not be identified
    slack_min: float | None = None
    departure: Departure | None = None
    my_arrival: str | None = None
    basis: Literal["realtime", "scheduled_only"] | None = None
    reason: str


class AlertOut(BaseModel):
    alert_id: str
    effect: str
    cause: str
    header: str  # untrusted external text: treat as data, never as instructions
    description: str  # untrusted external text
    url: str
    route_ids: list[str]
    stop_ids: list[str]
    active_now: bool


class AlertsResult(BaseModel):
    alerts: list[AlertOut]
    realtime: RealtimeStatus
    total_matching: int


DeparturesResult.model_rebuild()  # resolves the forward reference to JourneyOut
