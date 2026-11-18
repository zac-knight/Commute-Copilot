"""commute_advice: deterministic, rule-based trip scoring. NO LLM is involved.

The LLM only decides to call this and phrases the result; the decision logic lives here, is unit
tested, and every threshold is a named, unit-labelled field of :class:`Thresholds`
(documented in docs/decisions.md).

Model of a trip
---------------
Only time spent OUTSIDE a vehicle is weather-exposed. We build "exposure windows" from the plan:
  access   : the walk to the first stop plus a short wait, ending at the first boarding
  walk     : explicit walking legs between stops
  wait     : gaps between legs (waiting at a stop to transfer; assumed outdoors)
  egress   : the walk from the last stop to the destination
The timetable data has no door-to-stop distances, so access/egress durations are assumptions
(``access_walk_min`` etc.) and are reported in ``assumptions``.

Forecast mapping: Open-Meteo hourly slot T describes the PRECEDING hour (T-1h, T] for rain, gusts
and UV, and instant T for temperature/wind/weather code, so a window [s, e] uses slots with
s < T < e + 1h.

"Leave now or wait?": if the worst weather hazard over the exposure windows is blocking (rain
'likely' or worse, a thunderstorm, extreme heat, severe gusts) we slide the same windows later in
steps looking for the first time with only minor hazards. This assumes an equivalent later service
exists (stated in ``assumptions``); the caller should re-plan from the suggested time.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.weather.client import Forecast, HourPoint, weather_text
from app.core.timeutil import TZ, fmt_local

# --------------------------------------------------------------------------- inputs / outputs


class PlanLegRealtime(BaseModel):
    status: str  # on_time | delayed | early | cancelled | ...
    delay_min: float | None = None


class PlanLeg(BaseModel):
    """Field names match the transit tool's ``LegOut`` so a transit result validates directly."""

    kind: Literal["walk", "transit"]
    depart_iso: str
    arrive_iso: str
    walk_m: int | None = None
    mode: str | None = None
    route: str | None = None
    realtime: PlanLegRealtime | None = None


class RoutePlan(BaseModel):
    legs: list[PlanLeg] = Field(min_length=1)
    # Where the trip starts. Weather is sampled here (one point per trip). Filled in by whoever
    # builds the plan from stop coordinates; the LLM never supplies it. None -> the user's home.
    latitude: float | None = None
    longitude: float | None = None


class WeatherStatus(BaseModel):
    source: Literal["live", "unavailable"]
    data_age_s: int | None = None
    stale: bool = False
    note: str | None = None


class HazardSummary(BaseModel):
    """Worst conditions over the exposure windows. Levels: 0 none, 1 minor (bring something, no
    need to change plans), 2 significant, 3 severe. ``blocking_level`` >= 2 means it is worth
    delaying the trip if conditions improve soon."""

    rain_level: int = 0
    rain_prob_pct: float | None = None
    rain_mm_per_h: float | None = None
    thunderstorm: bool = False
    heat_level: int = 0
    max_apparent_c: float | None = None
    min_apparent_c: float | None = None
    uv_index: float | None = None
    wind_level: int = 0
    max_gust_kmh: float | None = None
    blocking_level: int = 0
    exposed_minutes: float = 0.0
    slots_used: int = 0


class CommuteAdvice(BaseModel):
    leave_now: bool
    suggested_leave_time: str | None = None  # local, display form
    suggested_leave_time_iso: str | None = None
    reasons: list[str]
    bring: list[str]
    confidence: float  # 0..1, see _confidence for exactly what moves it
    hazards: HazardSummary
    assumptions: list[str]
    weather: WeatherStatus


@dataclass(frozen=True)
class Thresholds:
    """All numbers behind the advice. Units in the field names."""

    # rain, evaluated per forecast hour
    rain_possible_prob_pct: float = 30.0
    rain_possible_mm_h: float = 0.5
    rain_likely_prob_pct: float = 60.0
    rain_likely_mm_h: float = 1.5
    rain_heavy_mm_h: float = 5.0
    min_rain_exposure_min: float = 3.0  # a shorter dash outdoors is ignored for rain
    thunderstorm_codes: frozenset[int] = frozenset({95, 96, 99})
    mid_probability_low_pct: float = 30.0  # 30-70 % = genuinely uncertain (lowers confidence)
    mid_probability_high_pct: float = 70.0
    # heat / cold on apparent ("feels like") temperature
    hot_apparent_c: float = 32.0
    extreme_apparent_c: float = 36.0
    cold_apparent_c: float = 12.0
    min_heat_exposure_min: float = 10.0
    # UV (index units)
    uv_high: float = 6.0
    uv_very_high: float = 8.0
    min_uv_exposure_min: float = 10.0
    # wind gusts
    gust_strong_kmh: float = 45.0  # umbrellas unreliable
    gust_severe_kmh: float = 65.0
    # trip model
    access_walk_min: float = 5.0
    wait_buffer_min: float = 2.0
    egress_walk_min: float = 5.0
    lead_slack_min: float = 5.0  # 'leave now' if you should go within this many minutes
    wait_search_horizon_min: int = 120
    wait_search_step_min: int = 15
    delay_note_min: float = 5.0
    connection_risk_delay_min: float = 10.0
    round_suggestion_to_min: int = 5


@dataclass(frozen=True)
class _Window:
    label: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    def shifted(self, delta: timedelta) -> _Window:
        return replace(self, start=self.start + delta, end=self.end + delta)


@dataclass
class _Eval:
    summary: HazardSummary
    worst: dict[str, _Window] = field(default_factory=dict)  # hazard -> window it peaks in
    mid_probability: bool = False
    uncovered: list[_Window] = field(default_factory=list)  # windows with no forecast slot


# --------------------------------------------------------------------------- helpers


def _dt(iso: str) -> datetime:
    d = datetime.fromisoformat(iso)
    return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)


def _round(dt: datetime, minutes: int, *, up: bool) -> datetime:
    """Round to a multiple of ``minutes`` past the hour (down for a safe early start, up when
    deliberately waiting for the weather to pass)."""
    dt = dt.replace(second=0, microsecond=0)
    rem = dt.minute % minutes
    if rem == 0:
        return dt
    return dt + timedelta(minutes=(minutes - rem) if up else -rem)


def slots_for(forecast: Forecast, start: datetime, end: datetime) -> list[HourPoint]:
    """Hourly slots relevant to [start, end]: slot T covers (T-1h, T], so s < T < e + 1h."""
    return [h for h in forecast.hours if start < h.time < end + timedelta(hours=1)]


def rain_level(slot: HourPoint, th: Thresholds) -> int:
    mm, p = slot.rain_mm or 0.0, slot.rain_prob_pct or 0.0
    if slot.weather_code in th.thunderstorm_codes or mm >= th.rain_heavy_mm_h:
        return 3
    if p >= th.rain_likely_prob_pct or mm >= th.rain_likely_mm_h:
        return 2
    if p >= th.rain_possible_prob_pct or mm >= th.rain_possible_mm_h:
        return 1
    return 0


def _evaluate(windows: list[_Window], forecast: Forecast, th: Thresholds) -> _Eval:
    exposed = sum(w.minutes for w in windows)
    s = HazardSummary(exposed_minutes=round(exposed, 1))
    ev = _Eval(s)
    rain_w: _Window | None = None
    rain_by_window: list[tuple[int, _Window]] = []
    heat_w: _Window | None = None
    wind_w: _Window | None = None
    uv_w: _Window | None = None
    for w in windows:
        slots = slots_for(forecast, w.start, w.end)
        if not slots:
            ev.uncovered.append(w)
            continue
        s.slots_used += len(slots)
        window_rain = 0
        for h in slots:
            lvl = rain_level(h, th)
            window_rain = max(window_rain, lvl)
            if h.weather_code in th.thunderstorm_codes:
                s.thunderstorm = True
            s.rain_level = max(s.rain_level, lvl)
            if lvl >= 1:
                s.rain_prob_pct = max(s.rain_prob_pct or 0.0, h.rain_prob_pct or 0.0)
                s.rain_mm_per_h = max(s.rain_mm_per_h or 0.0, h.rain_mm or 0.0)
            if h.rain_prob_pct is not None and (
                th.mid_probability_low_pct <= h.rain_prob_pct <= th.mid_probability_high_pct
            ):
                ev.mid_probability = True
            if h.apparent_c is not None:
                if s.max_apparent_c is None or h.apparent_c > s.max_apparent_c:
                    s.max_apparent_c, heat_w = h.apparent_c, w
                s.min_apparent_c = (
                    h.apparent_c
                    if s.min_apparent_c is None
                    else min(s.min_apparent_c, h.apparent_c)
                )
            if h.gust_kmh is not None and (s.max_gust_kmh is None or h.gust_kmh > s.max_gust_kmh):
                s.max_gust_kmh, wind_w = h.gust_kmh, w
            if h.uv_index is not None and (s.uv_index is None or h.uv_index > s.uv_index):
                s.uv_index, uv_w = h.uv_index, w
        rain_by_window.append((window_rain, w))
    if s.rain_level > 0:  # blame the longest exposure at the peak level
        rain_w = max(
            (w for lvl, w in rain_by_window if lvl == s.rain_level), key=lambda w: w.minutes
        )
    # a short dash outdoors is not worth worrying about for rain/heat (storms still count)
    if exposed < th.min_rain_exposure_min and not s.thunderstorm:
        s.rain_level = 0
    if s.max_apparent_c is not None and exposed >= th.min_heat_exposure_min:
        s.heat_level = (
            2
            if s.max_apparent_c >= th.extreme_apparent_c
            else 1
            if s.max_apparent_c >= th.hot_apparent_c
            else 0
        )
    if s.max_gust_kmh is not None:
        s.wind_level = (
            2
            if s.max_gust_kmh >= th.gust_severe_kmh
            else 1
            if s.max_gust_kmh >= th.gust_strong_kmh
            else 0
        )
    s.blocking_level = max(
        s.rain_level, 2 if s.heat_level == 2 else 0, 2 if s.wind_level == 2 else 0
    )
    for name, win in (("rain", rain_w), ("heat", heat_w), ("wind", wind_w), ("uv", uv_w)):
        if win is not None:
            ev.worst[name] = win
    return ev


def _build_windows(
    plan: RoutePlan, first_dep: datetime, th: Thresholds
) -> tuple[list[_Window], datetime]:
    """Exposure windows (outside vehicles) and the time you should step out ('leave by')."""
    legs = plan.legs
    windows: list[_Window] = []
    if legs[0].kind == "transit":
        start = first_dep - timedelta(minutes=th.access_walk_min + th.wait_buffer_min)
        windows.append(_Window("walking to the first stop", start, first_dep))
        leave_by = start
    else:
        leave_by = _dt(legs[0].depart_iso)
    for i, leg in enumerate(legs):
        if leg.kind == "walk":
            windows.append(
                _Window("walking between stops", _dt(leg.depart_iso), _dt(leg.arrive_iso))
            )
        if i + 1 < len(legs) and legs[i + 1].kind == "transit":
            gap_start = _dt(leg.arrive_iso)
            nxt = _eff_depart(legs[i + 1])
            if nxt > gap_start:
                windows.append(_Window("waiting at a stop to transfer", gap_start, nxt))
    last_arr = _dt(legs[-1].arrive_iso)
    windows.append(
        _Window(
            "walking from the last stop", last_arr, last_arr + timedelta(minutes=th.egress_walk_min)
        )
    )
    return windows, leave_by


def _eff_depart(leg: PlanLeg) -> datetime:
    d = _dt(leg.depart_iso)
    if leg.realtime and leg.realtime.delay_min:
        d += timedelta(minutes=leg.realtime.delay_min)
    return d


# --------------------------------------------------------------------------- main entry point


def commute_advice(
    plan: RoutePlan,
    forecast: Forecast | None,
    now: datetime,
    *,
    thresholds: Thresholds | None = None,
    weather: WeatherStatus | None = None,
) -> CommuteAdvice:
    th = thresholds or Thresholds()
    now = now.astimezone(TZ)
    status = weather or (
        WeatherStatus(source="live") if forecast else WeatherStatus(source="unavailable")
    )
    legs = plan.legs
    reasons: list[str] = []
    assumptions = [
        f"Assumes about {th.access_walk_min:.0f} min to walk to the first stop and "
        f"{th.egress_walk_min:.0f} min from the last stop (not in the timetable data).",
        "Waits between legs are assumed to be outdoors.",
    ]
    transit_legs = [leg for leg in legs if leg.kind == "transit"]

    # ---- transit state: cancellations first, they invalidate the plan
    cancelled = [leg for leg in transit_legs if leg.realtime and leg.realtime.status == "cancelled"]
    if cancelled:
        names = ", ".join(sorted({f"{leg.route or leg.mode or 'service'}" for leg in cancelled}))
        reasons.append(
            f"The {names} service in this plan is cancelled, so the plan no longer works. "
            "Get a new plan before heading out."
        )
        return CommuteAdvice(
            leave_now=False,
            reasons=reasons,
            bring=[],
            confidence=0.4,
            hazards=HazardSummary(),
            assumptions=assumptions,
            weather=status,
        )

    first_transit = transit_legs[0] if transit_legs else None
    first_dep = _eff_depart(first_transit) if first_transit else _dt(legs[0].depart_iso)
    if first_transit and first_transit.realtime and first_transit.realtime.delay_min:
        d = first_transit.realtime.delay_min
        if d >= th.delay_note_min:
            reasons.append(
                f"Your first service is running about {d:.0f} min late, so you can leave later."
            )
        elif d <= -th.delay_note_min:
            reasons.append(
                f"Your first service is running about {abs(d):.0f} min early; "
                "be at the stop sooner."
            )
    for leg in transit_legs[1:]:
        if leg.realtime and (leg.realtime.delay_min or 0) >= th.connection_risk_delay_min:
            reasons.append(
                f"The {leg.route or 'later'} leg is running {leg.realtime.delay_min:.0f} min late; "
                "check your connection."
            )

    windows, leave_by = _build_windows(plan, first_dep, th)
    go_time_now = leave_by <= now + timedelta(minutes=th.lead_slack_min)

    # ---- weather
    ev: _Eval | None = None
    if forecast is not None:
        ev = _evaluate(windows, forecast, th)
        if ev.summary.slots_used == 0:
            ev = None
            status = status.model_copy(
                update={
                    "note": (status.note + " " if status.note else "")
                    + "The forecast does not cover the time of this trip."
                }
            )
    leave_at = leave_by
    leave_now = go_time_now
    if ev is None:
        reasons.append("Weather data is unavailable for this trip, so this advice ignores weather.")
        hazards = HazardSummary()
        bring: list[str] = []
    else:
        hazards = ev.summary
        bring = _bring_list(ev.summary, th)
        reasons += _weather_reasons(ev, forecast, now, th)  # type: ignore[arg-type]
        if hazards.blocking_level >= 2:
            anchor = max(leave_by, now)
            found = None
            for step in range(
                th.wait_search_step_min, th.wait_search_horizon_min + 1, th.wait_search_step_min
            ):
                delta = anchor + timedelta(minutes=step) - windows[0].start
                shifted = [w.shifted(delta) for w in windows]
                cand = _evaluate(shifted, forecast, th)  # type: ignore[arg-type]
                if cand.summary.slots_used and cand.summary.blocking_level <= 1:
                    found = (step, cand)
                    break
            if found:
                step, cand = found
                leave_at = anchor + timedelta(minutes=step)
                leave_now = False
                reasons.append(
                    f"Waiting about {step} min should avoid the worst of it "
                    f"({_describe_blocker(ev.summary)} drops to {_describe_level(cand.summary)})."
                )
                assumptions.append(
                    "Suggesting a later start assumes an equivalent later service exists; "
                    "re-plan from the suggested time."
                )
            else:
                reasons.append(
                    f"Waiting up to {th.wait_search_horizon_min // 60} h would not help "
                    "(conditions stay bad), so go prepared rather than wait."
                )

    weather_wait = leave_at != leave_by
    if not leave_now and not weather_wait:
        reasons.insert(
            0,
            f"Your first service leaves around {fmt_local(first_dep, now)}; "
            f"head out about {fmt_local(leave_by, now)}.",
        )
    if leave_now:
        suggested = now.replace(second=0, microsecond=0)
    else:
        suggested = _round(max(leave_at, now), th.round_suggestion_to_min, up=weather_wait)

    conf = _confidence(ev, forecast, status, transit_legs, windows, now, th)
    return CommuteAdvice(
        leave_now=leave_now,
        suggested_leave_time=fmt_local(suggested, now),
        suggested_leave_time_iso=suggested.isoformat(timespec="minutes"),
        reasons=reasons or ["Conditions look fine for this trip."],
        bring=bring,
        confidence=conf,
        hazards=hazards,
        assumptions=assumptions,
        weather=status,
    )


# --------------------------------------------------------------------------- explanation helpers


def _bring_list(s: HazardSummary, th: Thresholds) -> list[str]:
    out: list[str] = []
    if s.rain_level >= 1:
        out.append("rain jacket (too windy for an umbrella)" if s.wind_level >= 1 else "umbrella")
    if s.heat_level >= 1:
        out.append("water")
    if (
        s.uv_index is not None
        and s.uv_index >= th.uv_high
        and s.exposed_minutes >= th.min_uv_exposure_min
    ):
        out.append("sunscreen and a hat")
    if s.min_apparent_c is not None and s.min_apparent_c <= th.cold_apparent_c:
        out.append("light jacket")
    return out


def _describe_level(s: HazardSummary) -> str:
    if s.rain_level >= 1 and s.rain_prob_pct is not None:
        return f"{s.rain_prob_pct:.0f}% rain chance"
    return "minor conditions"


def _describe_blocker(s: HazardSummary) -> str:
    if s.thunderstorm:
        return "the thunderstorm risk"
    if s.rain_level >= 2:
        return f"rain ({s.rain_prob_pct or 0:.0f}% chance)"
    if s.heat_level == 2:
        return "the extreme heat"
    return "the strong wind"


def _weather_reasons(ev: _Eval, forecast: Forecast, now: datetime, th: Thresholds) -> list[str]:
    s = ev.summary
    out: list[str] = []

    def when(key: str) -> str:
        w = ev.worst.get(key)
        return f" ({w.label}, {fmt_local(w.start, now)}-{fmt_local(w.end, now)[-5:]})" if w else ""

    if s.thunderstorm:
        out.append(
            f"Thunderstorm forecast while you are outside{when('rain')}: avoid being out in it."
        )
    elif s.rain_level >= 2:
        out.append(
            f"Rain is likely while you are outside{when('rain')}: "
            f"{s.rain_prob_pct or 0:.0f}% chance, up to {s.rain_mm_per_h or 0:.1f} mm/h."
        )
    elif s.rain_level == 1:
        out.append(
            f"Showers are possible while you are outside{when('rain')}: "
            f"{s.rain_prob_pct or 0:.0f}% chance."
        )
    elif s.exposed_minutes >= th.min_rain_exposure_min:
        out.append("No significant rain expected while you are outside.")
    if s.heat_level >= 1:
        out.append(
            f"It will feel hot ({s.max_apparent_c:.0f}°C feels-like){when('heat')}; "
            "take water and keep to shade."
        )
    if s.wind_level >= 1:
        out.append(f"Strong gusts up to {s.max_gust_kmh:.0f} km/h{when('wind')}.")
    if (
        s.uv_index is not None
        and s.uv_index >= th.uv_high
        and s.exposed_minutes >= th.min_uv_exposure_min
    ):
        out.append(f"UV index up to {s.uv_index:.0f} (high): sun protection recommended.")
    if s.min_apparent_c is not None and s.min_apparent_c <= th.cold_apparent_c:
        out.append(f"It will feel cool ({s.min_apparent_c:.0f}°C feels-like).")
    if ev.uncovered:
        out.append("The forecast does not cover part of this trip, so that part is not assessed.")
    first = next((h for h in forecast.hours if h.time > now), None)
    if first and s.rain_level >= 1:
        out.append(f"Outlook: {weather_text(first.weather_code)} in the next hour.")
    return out


def _confidence(
    ev: _Eval | None,
    forecast: Forecast | None,
    status: WeatherStatus,
    transit_legs: list[PlanLeg],
    windows: list[_Window],
    now: datetime,
    th: Thresholds,
) -> float:
    """0.90 baseline, reduced for: no weather (->0.25), stale weather (-0.15), forecast lead time
    (>12 h -0.10, >24 h -0.20), 30-70 % rain probability (-0.15, genuinely uncertain), and no
    realtime info on any transit leg (-0.10, timetable only). Clamped to [0.10, 0.95]."""
    if ev is None or forecast is None:
        return 0.25
    c = 0.90
    if status.stale:
        c -= 0.15
    lead_h = (windows[0].start - now).total_seconds() / 3600
    if lead_h > 24:
        c -= 0.20
    elif lead_h > 12:
        c -= 0.10
    if ev.mid_probability:
        c -= 0.15
    if transit_legs and not any(leg.realtime for leg in transit_legs):
        c -= 0.10
    return round(min(0.95, max(0.10, c)), 2)
