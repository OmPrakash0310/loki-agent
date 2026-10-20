"""When did a metric change? Change-point detection on a time series. Plain statistics.

"Error rate is 38% now" says something is wrong. "Error rate jumped from 0.1% to 38%
at 16:21:00, 55s after deploy b2e4a91" says what to look at. This module finds that
moment.

Method: a mean shift is located by the split that best divides the series into a
"before" and an "after" level (least squares, i.e. binary segmentation), then searched
again to the left so the *earliest* real change is the one reported. A change only
counts if it is large against the series' own noise (a robust estimate from the
median absolute difference between neighbouring points, so the step itself doesn't
inflate it), large in absolute terms, and it persists. The reported time is the first
point that crosses halfway between the two levels and stays there. When the move
from one level to the other takes minutes, it is reported as gradual (a leak or a
ramp, not a step).

Resolution is the series step (15s in the briefing), and the result is a lead for
the investigator, never a verdict.
"""
import math
from datetime import datetime

MIN_POINTS = 8  # fewer than this can't tell a change from noise
MIN_SIDE = 3  # points each side of a change: one odd sample is a blip, not a change
NOISE_SIGMAS = 5.0
GRADUAL_S = 120  # a move that takes longer than this is a ramp, not a step


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _noise(values) -> float:
    """Robust noise level: MAD of first differences, scaled to a standard deviation."""
    diffs = [b - a for a, b in zip(values, values[1:])]
    if not diffs:
        return 0.0
    med = _median(diffs)
    return 1.4826 * _median([abs(d - med) for d in diffs]) / math.sqrt(2)


def _best_split(values, lo, hi):
    """Index k in (lo, hi) that best splits values[lo:hi] into two levels, and its gain."""
    seg = values[lo:hi]
    n = len(seg)
    if n < 2 * MIN_SIDE:
        return None, 0.0
    prefix = [0.0]
    for v in seg:
        prefix.append(prefix[-1] + v)
    total = prefix[-1]
    best_k, best_gain = None, 0.0
    for k in range(MIN_SIDE, n - MIN_SIDE + 1):
        left, right = prefix[k], total - prefix[k]
        # Reduction in squared error from using two means instead of one.
        gain = left * left / k + right * right / (n - k) - total * total / n
        if gain > best_gain:
            best_k, best_gain = k, gain
    return (lo + best_k if best_k is not None else None), best_gain


def _significant(before, after, sigma, min_abs, min_rel) -> bool:
    delta = abs(after - before)
    if delta < min_abs:
        return False
    if delta < NOISE_SIGMAS * sigma:
        return False
    base = abs(before)
    return base == 0 or delta >= min_rel * base


def detect(points: list[tuple[datetime, float]], min_abs: float = 0.0, min_rel: float = 0.5) -> dict | None:
    """The earliest significant level change in `points` (time, value), oldest first.

    Returns {"at", "before", "after", "direction" ("up"/"down"), "gradual" (bool),
    "from", "to" (the transition, for gradual changes)}, or None when the series
    has no clear change."""
    points = [(t, float(v)) for t, v in points if v is not None and v == v]
    if len(points) < MIN_POINTS:
        return None
    times = [t for t, _ in points]
    values = [v for _, v in points]
    sigma = _noise(values)
    # Floor the noise so a perfectly flat series doesn't make every wiggle "significant".
    span = max(abs(v) for v in values) or 1.0
    sigma = max(sigma, 0.01 * span, 1e-9)

    # Find a significant split, then keep looking left of it for an earlier one.
    lo, hi, found = 0, len(values), None
    while True:
        k, _ = _best_split(values, lo, hi)
        if k is None:
            break
        before, after = _median(values[lo:k]), _median(values[k:hi])
        if not _significant(before, after, sigma, min_abs, min_rel):
            break
        found = (k, lo, hi, before, after)
        hi = k  # an earlier change can only be to the left
    if found is None:
        return None
    k, lo, hi, before, after = found
    # The "after" level is what the series settled at, measured to the end of the series.
    after = _median(values[k:])
    up = after > before
    mid = (before + after) / 2

    def crossed(v):
        return v > mid if up else v < mid

    # Onset: the first point near the split that crosses halfway and stays across.
    onset = None
    for i in range(max(lo, k - MIN_SIDE), len(values)):
        window = values[i:i + MIN_SIDE]
        if all(crossed(v) for v in window):
            onset = i
            break
    if onset is None:
        return None
    # How long the move took: last point still at the old level -> first point at the new one.
    band = max(3 * sigma, 0.1 * abs(after - before))
    start = onset
    while start > lo and abs(values[start - 1] - before) > band:
        start -= 1
    end = onset
    while end < len(values) - 1 and abs(values[end] - after) > band:
        end += 1
    t_from = times[start - 1] if start > lo else times[start]
    gradual = (times[end] - t_from).total_seconds() > GRADUAL_S
    return {
        "at": times[onset],
        "before": round(before, 4),
        "after": round(after, 4),
        "direction": "up" if up else "down",
        "gradual": gradual,
        "from": t_from,
        "to": times[end],
    }


# Per-metric thresholds: the smallest change worth telling an on-call engineer about.
THRESHOLDS = {
    "error_rate": {"min_abs": 0.01, "min_rel": 0.5},  # at least 1 percentage point, and +50%
    "p95_latency_seconds": {"min_abs": 0.05, "min_rel": 0.5},  # at least 50 ms, and +50%
    "request_rate": {"min_abs": 0.0, "min_rel": 0.3},  # traffic up or down by 30%
    "dependency_p95": {"min_abs": 0.05, "min_rel": 0.5},
}


def detect_metric(metric: str, points) -> dict | None:
    return detect(points, **THRESHOLDS.get(metric, {}))


def describe(change: dict, unit: str = "") -> str:
    """'jumped 0.0012 -> 0.38' or 'rose gradually 0.21 -> 0.89 (16:05:00 to 16:12:30)'."""
    b, a = change["before"], change["after"]
    if change["gradual"]:
        verb = "rose gradually" if change["direction"] == "up" else "fell gradually"
        return (f"{verb} {b}{unit} -> {a}{unit} "
                f"({change['from'].strftime('%H:%M:%S')} to {change['to'].strftime('%H:%M:%S')})")
    verb = "jumped" if change["direction"] == "up" else "dropped"
    return f"{verb} {b}{unit} -> {a}{unit}"
