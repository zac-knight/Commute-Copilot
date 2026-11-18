"""Small-sample statistics. With ~100 cases one percentage is not enough: show the interval."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Sequence
from statistics import median


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion (well behaved at 0%, 100% and small n)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(successes: int, n: int) -> dict[str, float | int]:
    lo, hi = wilson(successes, n)
    return {"k": successes, "n": n, "rate": successes / n if n else 0.0, "lo": lo, "hi": hi}


def pct(r: dict[str, float | int]) -> str:
    p, lo, hi = (100 * float(r[k]) for k in ("rate", "lo", "hi"))
    return f"{p:.0f}% ({r['k']}/{r['n']}, 95% CI {lo:.0f}-{hi:.0f}%)"


def percentile(values: Iterable[float], q: float) -> float:
    xs = sorted(values)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return xs[int(k)] if f == c else xs[f] + (xs[c] - xs[f]) * (k - f)


def latency_summary(ms: Iterable[float]) -> dict[str, float]:
    xs = list(ms)
    return {
        "p50": median(xs) if xs else 0.0,
        "p95": percentile(xs, 0.95),
        "mean": sum(xs) / len(xs) if xs else 0.0,
    }


def paired_differences(a: list[bool], b: list[bool]) -> dict[str, int]:
    """McNemar-style counts for comparing two models on the SAME cases."""
    assert len(a) == len(b)
    return {
        "both": sum(x and y for x, y in zip(a, b, strict=True)),
        "only_a": sum(x and not y for x, y in zip(a, b, strict=True)),
        "only_b": sum(y and not x for x, y in zip(a, b, strict=True)),
        "neither": sum(not x and not y for x, y in zip(a, b, strict=True)),
    }


def sign_test_p(only_a: int, only_b: int) -> float:
    """Two-sided exact binomial (sign) test on the discordant pairs: is one model really better?"""
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return float(min(1.0, 2 * tail))


# ----------------------------------------------------------------------------- bootstrap


def bootstrap_ci(
    values: Sequence[float],
    stat: Callable[[Sequence[float]], float] = lambda xs: sum(xs) / len(xs),
    *,
    n_boot: int = 4000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """(point, lo, hi): percentile bootstrap CI of ``stat`` over resampled cases. Seeded, so a
    report regenerated from the same results is identical."""
    if not values:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    boots = sorted(stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot))
    lo = boots[int((alpha / 2) * n_boot)]
    hi = boots[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (stat(values), lo, hi)


def paired_bootstrap_diff(
    a: Sequence[float], b: Sequence[float], *, n_boot: int = 4000, seed: int = 0
) -> tuple[float, float, float]:
    """(mean(a-b), lo, hi) resampling CASES, so both models keep the same resampled cases. If the
    interval excludes 0 the difference is unlikely to be case-sampling noise (it says nothing
    about cases outside this set)."""
    assert len(a) == len(b)
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    return bootstrap_ci(diffs, n_boot=n_boot, seed=seed)


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Chance-corrected agreement between two labelers (1 = perfect, 0 = chance)."""
    assert len(a) == len(b) and a
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    labels = set(a) | set(b)
    expected = sum((a.count(k) / n) * (b.count(k) / n) for k in labels)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)
