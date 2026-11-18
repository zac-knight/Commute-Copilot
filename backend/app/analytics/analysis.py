"""Delay analysis over polled observations. Pure functions on :class:`Obs` lists (no I/O), so every
number is unit-tested on synthetic data and the same code feeds the API and the notebook.

What it answers, and how it stays honest:
  * every section has a minimum-data rule; below it the result is ``None`` with a stated reason, never
    an estimate from a handful of trips;
  * intervals are bootstrap (medians) or Wilson (proportions);
  * the model is judged on a HELD-OUT LATER period against two baselines, and is only said to 'win'
    if the interval of its improvement over the best baseline excludes zero.

Definitions
  final row    the last observation of a (trip, stop, service date) made no later than 60 s after its
               predicted/scheduled departure: our best knowledge of what actually happened.
  delay        seconds late (negative = early), only where the realtime feed gave an estimate.
  cancelled    a final row whose schedule relationship is CANCELED.
  horizon      the model predicts the final delay from what was known 10 min before departure.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.analytics.store import Obs
from app.core.timeutil import TZ
from app.evaluation.stats import bootstrap_ci, paired_bootstrap_diff, percentile, rate

HORIZON_S = 600
MIN_FINAL = 50  # rows before any distribution is shown
MIN_GROUP = 15  # rows before a route/hour bar is shown
MIN_RAIN_GROUP = 30  # rows in each of the rainy and dry groups
RAIN_MM = 0.2  # an hour with at least this much rain counts as rainy
MIN_MODEL_DAYS = 7
MIN_MODEL_TEST = 100


@dataclass(frozen=True)
class Final:
    obs: Obs
    known: Obs | None  # the observation available HORIZON_S before departure (None = nothing then)

    @property
    def delay(self) -> int | None:
        return self.obs.delay_s if self.obs.rel == "SCHEDULED" else None

    @property
    def local(self) -> datetime:
        return datetime.fromtimestamp(self.obs.sched, TZ)


def final_rows(obs: list[Obs]) -> list[Final]:
    by_key: dict[tuple[str, str, str], list[Obs]] = defaultdict(list)
    for o in obs:
        by_key[(o.trip_id, o.stop_id, o.service_date)].append(o)
    out = []
    for rows in by_key.values():
        rows.sort(key=lambda o: o.ts)
        last = None
        known = None
        for o in rows:
            if o.ts <= (o.pred or o.sched) + 60:
                last = o
            if o.ts <= o.sched - HORIZON_S:
                known = o
        if last is not None:
            out.append(Final(last, known))
    out.sort(key=lambda f: f.obs.sched)
    return out


def _median(xs: Sequence[float]) -> float:
    return float(statistics.median(xs))


def _summ(delays: list[float]) -> dict[str, float | int]:
    return {"n": len(delays), "median_s": _median(delays), "q1_s": percentile(delays, 0.25),
            "q3_s": percentile(delays, 0.75), "p90_s": percentile(delays, 0.90)}  # fmt: skip


def _boot_median(delays: list[float]) -> tuple[float, float]:
    _, lo, hi = bootstrap_ci(delays, stat=_median, n_boot=1000)
    return lo, hi


def distributions(finals: list[Final]) -> dict[str, Any] | None:
    delayed = [f for f in finals if f.delay is not None]
    if len(delayed) < MIN_FINAL:
        return None
    groups: dict[str, dict[Any, list[Final]]] = {"route": defaultdict(list), "hour": defaultdict(list),
                                                  "weekday": defaultdict(list)}  # fmt: skip
    for f in finals:
        groups["route"][f.obs.route].append(f)
        groups["hour"][f.local.hour].append(f)
        groups["weekday"][f.local.weekday()].append(f)
    out: dict[str, Any] = {
        "overall": _summ([float(f.delay) for f in delayed if f.delay is not None])
    }
    for name, g in groups.items():
        rows = []
        for key, fs in sorted(g.items(), key=lambda kv: kv[0]):
            ds = [float(f.delay) for f in fs if f.delay is not None]
            canc = sum(f.obs.rel == "CANCELED" for f in fs)
            if len(ds) < MIN_GROUP:
                continue
            lo, hi = _boot_median(ds)
            rows.append({"key": key, **_summ(ds), "median_lo_s": lo, "median_hi_s": hi,
                         "cancelled": rate(canc, len(fs))})  # fmt: skip
        out[name] = rows
    return out


def cancellations(finals: list[Final]) -> dict[str, Any] | None:
    scheduled = [f for f in finals if f.obs.rel in ("SCHEDULED", "CANCELED")]
    if len(scheduled) < MIN_FINAL:
        return None
    return rate(sum(f.obs.rel == "CANCELED" for f in scheduled), len(scheduled))


def rain_effect(finals: list[Final], rain: dict[int, float]) -> dict[str, Any] | None:
    wet: list[float] = []
    dry: list[float] = []
    for f in finals:
        if f.delay is None:
            continue
        slot = (f.obs.sched // 3600 + 1) * 3600  # the hour ENDING after departure
        if slot not in rain:
            continue
        (wet if rain[slot] >= RAIN_MM else dry).append(float(f.delay))
    if len(wet) < MIN_RAIN_GROUP or len(dry) < MIN_RAIN_GROUP:
        return None
    import random

    rng = random.Random(0)
    diffs = sorted(
        _median([rng.choice(wet) for _ in wet]) - _median([rng.choice(dry) for _ in dry])
        for _ in range(1000)
    )
    return {"wet_n": len(wet), "dry_n": len(dry), "wet_median_s": _median(wet),
            "dry_median_s": _median(dry), "diff_s": _median(wet) - _median(dry),
            "ci_lo_s": diffs[25], "ci_hi_s": diffs[974],
            "note": "association only: rain and time of day are confounded"}  # fmt: skip


# ----------------------------------------------------------------------------- model


def _features(f: Final, prev_delay: float, group_med: float) -> list[float]:
    known = f.known.delay_s if f.known is not None and f.known.rel == "SCHEDULED" else None
    return [1.0, float(known) if known is not None else 0.0, prev_delay, group_med]


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    n = len(b)
    m = [[*row, b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-9:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                k = m[r][c] / m[c][c]
                m[r] = [x - k * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def _fit(xs: list[list[float]], ys: list[float], ridge: float = 1.0) -> list[float] | None:
    k = len(xs[0])
    ata = [[sum(x[i] * x[j] for x in xs) + (ridge if i == j and i > 0 else 0.0) for j in range(k)]
           for i in range(k)]  # fmt: skip
    atb = [sum(x[i] * y for x, y in zip(xs, ys, strict=True)) for i in range(k)]
    return _solve(ata, atb)


def model_eval(finals: list[Final]) -> dict[str, Any]:
    """Time-split evaluation. Train on the earlier ~70% of DAYS, test on the later days."""
    rows = [f for f in finals if f.delay is not None]
    days = sorted({f.local.date() for f in rows})
    base: dict[str, Any] = {"available": False, "min_days": MIN_MODEL_DAYS}
    if len(days) < MIN_MODEL_DAYS:
        return {**base, "reason": f"{len(days)} day(s) of data; at least {MIN_MODEL_DAYS} needed "
                                  "so a later period can be held out"}  # fmt: skip
    cut = days[max(1, int(len(days) * 0.7)) - 1]
    train = [f for f in rows if f.local.date() <= cut]
    test = [f for f in rows if f.local.date() > cut]
    if len(test) < MIN_MODEL_TEST or len(train) < MIN_MODEL_TEST:
        return {**base, "reason": f"{len(train)} training / {len(test)} test rows; "
                                  f"at least {MIN_MODEL_TEST} of each needed"}  # fmt: skip
    grp: dict[tuple[str, int], list[float]] = defaultdict(list)
    for f in train:
        grp[(f.obs.route, f.local.hour)].append(float(f.delay or 0))
    gmed = {k: _median(v) for k, v in grp.items()}
    overall = _median([float(f.delay or 0) for f in train])

    def prevs(fs: list[Final]) -> list[float]:
        """Delay of the most recent earlier trip on the same route that had ALREADY departed
        (>= HORIZON_S before this one, so it is knowable at prediction time), within 90 min."""
        hist: dict[str, list[tuple[int, float]]] = defaultdict(list)
        out = []
        for f in fs:  # chronological
            cands = [d for t, d in hist[f.obs.route]
                     if t <= f.obs.sched - HORIZON_S and f.obs.sched - t <= 5400]  # fmt: skip
            out.append(cands[-1] if cands else 0.0)
            hist[f.obs.route].append((f.obs.sched, float(f.delay or 0)))
        return out

    def feats(fs: list[Final]) -> list[list[float]]:
        return [_features(f, p, gmed.get((f.obs.route, f.local.hour), overall))
                for f, p in zip(fs, prevs(fs), strict=True)]  # fmt: skip

    w = _fit(feats(train), [float(f.delay or 0) for f in train])
    if w is None:
        return {**base, "reason": "model could not be fitted (singular features)"}
    xs = feats(test)
    y = [float(f.delay or 0) for f in test]
    pred_model = [sum(a * b for a, b in zip(w, x, strict=True)) for x in xs]
    pred_zero = [0.0] * len(y)
    pred_last = [x[1] for x in xs]  # persist the delay reported 10 min before (0 if none)

    def ae(p: list[float]) -> list[float]:
        return [abs(a - b) for a, b in zip(p, y, strict=True)]

    err = {
        "model": ae(pred_model),
        "always_on_time": ae(pred_zero),
        "last_observed_delay": ae(pred_last),
    }
    maes = {k: sum(v) / len(v) for k, v in err.items()}
    best = min(("always_on_time", "last_observed_delay"), key=lambda k: maes[k])
    gain, lo, hi = paired_bootstrap_diff(err[best], err["model"], n_boot=1000)
    return {
        "available": True, "train_rows": len(train), "test_rows": len(test),
        "train_until": cut.isoformat(), "test_days": len({f.local.date() for f in test}),
        "mae_s": maes, "best_baseline": best, "gain_over_best_baseline_s": gain,
        "gain_ci_s": [lo, hi], "beats_baselines": lo > 0,
        "coefficients": dict(zip(["intercept", "reported_delay_10min_before", "previous_trip_delay",
                                  "route_hour_median"], w, strict=True)),
    }  # fmt: skip


# ----------------------------------------------------------------------------- summary


def summarise(
    obs: list[Obs], rain: dict[int, float], poll: dict[str, int | None], now: float
) -> dict[str, Any]:
    finals = final_rows(obs)
    first, last = poll.get("first_ts"), poll.get("last_ts")
    hours = (last - first) / 3600 if first and last else 0.0
    delayed = [f for f in finals if f.delay is not None]
    dist, canc, rainfx = distributions(finals), cancellations(finals), rain_effect(finals, rain)
    model = model_eval(finals)
    running = bool(last and now - last < 180)
    need = f"at least {MIN_FINAL} observed departures (have {len(delayed)})"
    return {
        "coverage": {
            **poll,
            "hours_collected": round(hours, 2),
            "days_collected": len({f.local.date() for f in finals}),
            "final_rows": len(finals),
            "rows_with_delay": len(delayed),
            "collector_running": running,
            "success_rate": rate(int(poll.get("ok_polls") or 0), int(poll.get("polls") or 0)),
        },
        "distributions": dist,
        "distributions_reason": None if dist else need,
        "cancellations": canc,
        "cancellations_reason": None if canc else need,
        "rain": rainfx,
        "rain_reason": None
        if rainfx
        else (
            f"needs at least {MIN_RAIN_GROUP} departures in BOTH rainy and dry hours "
            f"(rain data hours: {len(rain)})"
        ),
        "model": model,
    }
