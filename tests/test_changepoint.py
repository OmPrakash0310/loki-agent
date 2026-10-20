"""Change-point detection (changepoint.py) and how the briefing uses it (context.py)."""
import random
from datetime import datetime, timedelta, timezone

import pytest

import changepoint as cp
import context
import telemetry as tm
import tools

T0 = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)


def series(fn, n=80, step=15, start=T0):
    return [(start + timedelta(seconds=i * step), fn(i)) for i in range(n)]


def at(i, step=15):
    return T0 + timedelta(seconds=i * step)


rng = random.Random(7)


def noise(s):
    return rng.gauss(0, s)


# ---------------- detection ----------------

def test_step_up_is_found_at_the_right_moment():
    c = cp.detect_metric("error_rate", series(lambda i: (0.002 if i < 50 else 0.38) + abs(noise(0.002))))
    assert c["at"] == at(50) and c["direction"] == "up" and not c["gradual"]
    assert c["before"] < 0.01 and c["after"] > 0.3


def test_noise_and_blips_are_not_changes():
    assert cp.detect_metric("p95_latency_seconds", series(lambda i: 0.2 + noise(0.03))) is None
    assert cp.detect_metric("error_rate", series(lambda i: 0.5 if i == 40 else 0.002)) is None


def test_small_changes_below_the_bar_are_ignored():
    assert cp.detect_metric("error_rate", series(lambda i: 0.001 if i < 50 else 0.004)) is None  # +0.3 points


def test_ramp_is_reported_as_gradual():
    c = cp.detect_metric("p95_latency_seconds", series(lambda i: 0.2 + max(0, min(i, 60) - 30) * 0.03 + noise(0.01)))
    assert c and c["gradual"] and c["direction"] == "up"
    assert at(28) <= c["from"] <= at(36) and c["to"] > c["from"] + timedelta(minutes=3)
    assert "rose gradually" in cp.describe(c)


def test_drop_in_traffic():
    c = cp.detect_metric("request_rate", series(lambda i: (20 if i < 55 else 8) + noise(1)))
    assert c["at"] == at(55) and c["direction"] == "down" and cp.describe(c).startswith("dropped")


def test_the_earliest_of_two_changes_is_reported():
    c = cp.detect_metric("p95_latency_seconds", series(lambda i: 0.1 if i < 30 else (0.6 if i < 60 else 2.0)))
    assert c["at"] == at(30)


def test_too_few_points_and_nan_are_handled():
    assert cp.detect([(at(i), 1.0 if i < 3 else 9.0) for i in range(6)]) is None
    pts = series(lambda i: float("nan") if i % 7 == 0 else (0.0 if i < 40 else 1.0))
    assert cp.detect_metric("error_rate", pts)["at"] == at(40)


# ---------------- briefing ----------------

INCIDENT = [{"started_at": at(56), "alert_name": "HighErrorRate", "service": "payments-svc",
             "severity": "critical", "summary": "5xx > 5%"}]


def deploy(id_, svc, i):
    return {"id": id_, "service": svc, "version": "v1", "author": "a", "message": "m",
            "deployed_at": at(i).isoformat(), "deployed_at_dt": at(i), "diff": ""}


def install(monkeypatch, metric_fn, dep_fn=None, deploys=(), errors=()):
    """A fake telemetry layer: metric_fn(service, metric) -> value at index i."""
    now = at(80)
    monkeypatch.setattr(tm, "now", lambda: now)
    monkeypatch.setattr(tm, "prom_enabled", lambda: True)
    monkeypatch.setattr(tm, "metric_unavailable", lambda m: None)
    monkeypatch.setattr(tm, "dependency_unavailable", lambda: None)
    monkeypatch.setattr(tm, "services", lambda: ["payments-svc", "shop-api"])
    monkeypatch.setattr(tm, "services_with_dependencies", lambda: ["payments-svc"] if dep_fn else [])
    monkeypatch.setattr(tm, "metric_before", lambda m, s, t: 0.0)
    monkeypatch.setattr(tm, "metric_at", lambda m, s, t: 0.0)
    monkeypatch.setattr(tm, "dependency_p95_at", lambda s, t: {})

    def metric_series(metric, svc, start, end, step_s=30):
        n = int((end - T0).total_seconds() // 15)
        return [(at(i), metric_fn(svc, metric)(i)) for i in range(n) if at(i) >= start]

    def dependency_p95_series(svc, start, end, step_s=30):
        n = int((end - T0).total_seconds() // 15)
        return {"postgres": [(at(i), dep_fn(i)) for i in range(n) if at(i) >= start]} if dep_fn else {}

    monkeypatch.setattr(tm, "metric_series", metric_series)
    monkeypatch.setattr(tm, "dependency_p95_series", dependency_p95_series)
    monkeypatch.setattr(tm, "error_lines", lambda svc, s, e: list(errors))
    monkeypatch.setattr(tm, "logs_present", lambda s, e: True)
    monkeypatch.setattr(tm, "error_notes", lambda: [])
    monkeypatch.setattr(tm, "deploys_between", lambda s, e: list(deploys))
    monkeypatch.setattr(tm, "deploy_notes", lambda: [])


def flat(v):
    return lambda i: v


def test_bad_deploy_lines_up_with_the_jump(monkeypatch):
    def metrics(svc, metric):
        if svc == "payments-svc" and metric == "error_rate":
            return lambda i: 0.001 if i < 50 else 0.4
        return flat(0.1)
    install(monkeypatch, metrics, deploys=[deploy("abc123", "payments-svc", 47)],
            errors=[(at(50) + timedelta(seconds=2), "payments-svc", "KeyError: 'currency'")])
    b = context.build(INCIDENT)
    section = b.text.split("## When metrics changed")[1].split("\n\n")[0]
    assert "16:12:30 payments-svc error_rate jumped 0.001 -> 0.4 (earliest change)" in section
    assert "45s after deploy abc123 to payments-svc" in section
    assert "first error in payments-svc at 16:12:32 (same time)" in section
    top = b.suspects[0]
    assert top["deploy_id"] == "abc123" and any("error_rate changed 45s after it shipped" in r for r in top["reasons"])


def test_innocent_deploy_before_a_database_slowdown(monkeypatch):
    """The eval's trap: a harmless deploy, then the database slows 4 minutes later."""
    def metrics(svc, metric):
        if svc == "payments-svc" and metric == "p95_latency_seconds":
            return lambda i: 0.05 if i < 52 else 2.5
        return flat(0.1)
    install(monkeypatch, metrics, dep_fn=lambda i: 0.01 if i < 51 else 2.4,
            deploys=[deploy("d0", "payments-svc", 35)])
    b = context.build(INCIDENT)
    section = b.text.split("## When metrics changed")[1].split("\n\n")[0]
    first = section.splitlines()[1]
    assert "payments-svc -> postgres p95 jumped" in first and "(earliest change)" in first
    reasons = " ".join(next(s for s in b.suspects if s.get("deploy_id") == "d0")["reasons"])
    assert "stayed steady for 4 min after it shipped" in reasons
    assert "payments-svc -> postgres p95 changed first" in reasons


def test_timing_never_changes_suspect_scores(monkeypatch):
    """Facts only: the same incident with and without change detection ranks the same."""
    def metrics(svc, metric):
        return (lambda i: 0.05 if i < 52 else 2.5) if metric == "p95_latency_seconds" else flat(0.1)
    install(monkeypatch, metrics, deploys=[deploy("d0", "payments-svc", 35)])
    with_changes = [(s.get("deploy_id"), s["score"]) for s in context.build(INCIDENT).suspects]
    monkeypatch.setattr(context, "metric_changes", lambda *a: [])
    without = [(s.get("deploy_id"), s["score"]) for s in context.build(INCIDENT).suspects]
    assert with_changes == without


def test_detection_failure_never_breaks_the_briefing(monkeypatch):
    install(monkeypatch, lambda svc, metric: flat(0.1))

    def boom(*a, **k):
        raise RuntimeError("prometheus fell over")
    monkeypatch.setattr(context, "metric_changes", boom)
    b = context.build(INCIDENT)
    assert "## When metrics changed" not in b.text and "## Suspects" in b.text


def test_quiet_services_are_not_queried(monkeypatch):
    asked = set()

    def metrics(svc, metric):
        asked.add(svc)
        return flat(0.1)
    install(monkeypatch, metrics)
    context.build(INCIDENT)
    assert asked == {"payments-svc"}  # shop-api didn't alert or log errors


def test_metrics_tool_reports_the_change(monkeypatch):
    monkeypatch.setattr(tm, "metric_unavailable", lambda m: None)
    monkeypatch.setattr(tm, "now", lambda: at(80))
    monkeypatch.setattr(tm, "metric_series", lambda m, s, a, b, step_s=30: series(lambda i: 0.0 if i < 40 else 0.5))
    out = tools.query_metrics("error_rate", "payments-svc", minutes=20)
    assert out["change"] == "16:10:00: jumped 0.0 -> 0.5"


@pytest.mark.parametrize("step", [15, 30])
def test_resolution_follows_the_step(step):
    c = cp.detect_metric("error_rate", series(lambda i: 0.0 if i < 30 else 0.5, step=step))
    assert c["at"] == at(30, step)


def test_warm_up_of_a_series_that_starts_late_is_ignored(monkeypatch):
    """A service that only has data from mid-window just started: its ramp-up isn't a change."""
    def metrics(svc, metric):
        if metric == "request_rate":
            return lambda i: None if i < 40 else (2.0 if i < 44 else 3.5)  # starts at i=40, ramps at 44
        if metric == "error_rate":
            return lambda i: None if i < 40 else (0.0 if i < 60 else 0.8)  # a real change, well after start
        return flat(0.1)
    install(monkeypatch, metrics)
    monkeypatch.setattr(tm, "metric_series", lambda m, svc, a, b, step_s=30: [
        (at(i), metrics(svc, m)(i)) for i in range(80) if metrics(svc, m)(i) is not None and at(i) >= a])
    b = context.build(INCIDENT)
    section = b.text.split("## When metrics changed")[1].split("\n\n")[0]
    assert "request_rate" not in section and "payments-svc error_rate jumped" in section
