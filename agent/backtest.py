"""Backtesting: replay a past incident so Loki investigates it as if it were happening then.

The clock is set back to the replay time (telemetry.replay), so logs, metrics and
deploys from after that moment are invisible, exactly as they were to an on-call
engineer at the time. Backtest investigations are stored with mode = 'backtest' and
kept out of /stats, the answer cache and past-incident search.
"""
from datetime import datetime, timedelta, timezone

import investigator
import telemetry as tm

# Live, Loki starts working about a minute after an alert's startsAt: the rule's
# `for: 30s`, Alertmanager's 10s group_wait, then our 25s grouping wait. Replaying
# at the same delay shows what Loki would really have seen.
DEFAULT_DELAY_S = 60
# Recorded alerts this close to the requested time are replayed as they were.
NEAR = timedelta(minutes=5)
SEVERITY = {"HighErrorRate": "critical", "HighLatency": "warning"}
ALLOWED_MODELS = {"fake", *investigator.PRICES}


def parse_alerts(spec: str | list[str] | None) -> list[tuple[str, str]]:
    """'HighErrorRate:payments-svc,HighLatency:shop-api' -> [(alert, service), ...]."""
    items = spec.split(",") if isinstance(spec, str) else (spec or [])
    out = []
    for item in (i.strip() for i in items):
        if not item:
            continue
        alert, sep, service = item.partition(":")
        if not sep or not alert.strip() or not service.strip():
            raise ValueError(f"bad alert {item!r}: use AlertName:service, e.g. HighErrorRate:payments-svc")
        out.append((alert.strip(), service.strip()))
    return out


def _summary(alert: str, service: str, at: datetime) -> str:
    """The summary Alertmanager would have sent, from the metric at alert time."""
    try:
        if alert == "HighErrorRate":
            v = tm.metric_at("error_rate", service, at)
            return f"{service} error rate is {v:.2%}" if v is not None else f"{service} error rate is high"
        if alert == "HighLatency":
            v = tm.metric_at("p95_latency_seconds", service, at)
            return f"{service} p95 latency is {v:.3g}s" if v is not None else f"{service} p95 latency is high"
    except Exception:  # a missing summary shouldn't stop the replay
        pass
    return f"{alert} firing on {service}"


def synthetic_incidents(alerts: list[tuple[str, str]], at: datetime) -> list[dict]:
    """Alerts that fired at `at`. Call inside tm.replay so the summary can't use later data."""
    return [
        {"alert_name": a, "service": s, "severity": SEVERITY.get(a, "critical"),
         "summary": _summary(a, s, at), "started_at": at}
        for a, s in alerts
    ]


def recorded_incidents(conn, at: datetime) -> list[dict]:
    """Real alerts Loki received around `at`, to replay exactly what fired."""
    return conn.execute(
        "SELECT alert_name, service, severity, summary, started_at FROM incidents "
        "WHERE mode = 'live' AND started_at BETWEEN %s AND %s ORDER BY started_at",
        (at - NEAR, at + NEAR),
    ).fetchall()


def create(conn, at: datetime, alerts: list[tuple[str, str]], delay_s: int = DEFAULT_DELAY_S) -> int:
    """Store a backtest investigation and its incidents. Returns its id.
    `at` is when the (first) alert fired; Loki's clock is set to `delay_s` later."""
    if at.tzinfo is None:
        raise ValueError("time must include a timezone (e.g. 2026-10-16T21:03:00Z)")
    incidents = [] if alerts else recorded_incidents(conn, at)
    if not alerts and not incidents:
        raise ValueError(f"no recorded alerts within {NEAR} of {at.isoformat()}; pass alerts explicitly")
    first = min(i["started_at"] for i in incidents) if incidents else at
    replay_at = first + timedelta(seconds=delay_s)
    # Alerts recorded after Loki's replayed clock are the future (often the next
    # incident): replaying them would leak what happened next.
    incidents = [i for i in incidents if i["started_at"] <= replay_at]
    if replay_at > datetime.now(timezone.utc):
        raise ValueError(f"replay time {replay_at.isoformat()} is in the future")
    if alerts:
        with tm.replay(replay_at):
            incidents = synthetic_incidents(alerts, at)

    inv_id = conn.execute(
        "INSERT INTO investigations (mode, replay_at) VALUES ('backtest', %s) RETURNING id", (replay_at,)
    ).fetchone()["id"]
    for n, i in enumerate(incidents):
        conn.execute(
            # status 'backtest' (never 'open'): live dedup, resolve and grouping logic ignore these rows.
            "INSERT INTO incidents (fingerprint, alert_name, service, severity, summary, started_at, status, "
            "investigation_id, mode) VALUES (%s, %s, %s, %s, %s, %s, 'backtest', %s, 'backtest')",
            (f"backtest:{inv_id}:{n}", i["alert_name"], i["service"], i["severity"], i["summary"],
             i["started_at"], inv_id),
        )
    return inv_id
