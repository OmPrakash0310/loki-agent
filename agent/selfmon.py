"""Self-monitoring: readiness (DB + background worker heartbeat) and an operator
alert when Loki's own investigations keep failing.

Env:
  LOKI_OPERATOR_SLACK_WEBHOOK      Slack incoming webhook of the OPERATOR (not the
                                   customer's channel). Empty = no failure alerts.
  LOKI_FAILURE_ALERT_THRESHOLD     failed investigations in the window that trigger
                                   an alert (default 3)
  LOKI_FAILURE_ALERT_WINDOW_MIN    window in minutes (default 30)
  LOKI_FAILURE_ALERT_COOLDOWN_MIN  minimum minutes between two alerts (default 60)
"""
import logging
import os
import threading
import time

import httpx

import db

log = logging.getLogger("agent")

HEARTBEAT_INTERVAL_S = 30
# The worker is considered dead when its last beat is older than this.
HEARTBEAT_STALE_S = 120

_state = {"beat": 0.0, "last_alert": 0.0}
_started = threading.Event()


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def heartbeat_age(now: float | None = None) -> float | None:
    if not _state["beat"]:
        return None
    return (now or time.time()) - _state["beat"]


def db_ok() -> bool:
    try:
        with db.connect(admin=True) as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception as e:  # noqa: BLE001 - any failure means not ready
        log.warning("readiness: database check failed: %s", type(e).__name__)
        return False


def readiness(now: float | None = None) -> tuple[bool, dict]:
    """(ready, checks). checks holds only booleans: the endpoint is public."""
    age = heartbeat_age(now)
    checks = {"database": db_ok(), "worker": age is not None and age < HEARTBEAT_STALE_S}
    return all(checks.values()), checks


def recent_failures(conn, window_min: int) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM investigations WHERE status = 'failed' AND mode = 'live' "
        "AND finished_at > now() - make_interval(mins => %s)",
        (window_min,),
    ).fetchone()
    return int(row["n"])


def check_failures(conn, now: float | None = None, post=None) -> bool:
    """Post one operator alert when failures in the window reach the threshold.
    Returns True when an alert was sent."""
    url = os.environ.get("LOKI_OPERATOR_SLACK_WEBHOOK", "").strip()
    if not url:
        return False
    now = now or time.time()
    cooldown = _int_env("LOKI_FAILURE_ALERT_COOLDOWN_MIN", 60) * 60
    if _state["last_alert"] and now - _state["last_alert"] < cooldown:
        return False
    window = _int_env("LOKI_FAILURE_ALERT_WINDOW_MIN", 30)
    threshold = _int_env("LOKI_FAILURE_ALERT_THRESHOLD", 3)
    n = recent_failures(conn, window)
    if n < threshold:
        return False
    instance = os.environ.get("LOKI_PUBLIC_URL", "") or "loki"
    text = (f":rotating_light: Loki ({instance}): {n} investigations failed in the last {window} min. "
            "Check `docker compose logs agent` and `loki doctor`.")
    try:
        (post or httpx.post)(url, json={"text": text}, timeout=10)
    except Exception as e:  # noqa: BLE001 - alerting must never crash the worker
        log.warning("operator alert failed: %s", type(e).__name__)
        return False
    _state["last_alert"] = now
    log.warning("operator alert sent: %d failed investigations in %d min", n, window)
    return True


def _tick():
    _state["beat"] = time.time()
    try:
        with db.connect(admin=True) as conn:  # the operator's view: every tenant's failures
            check_failures(conn)
    except Exception as e:  # noqa: BLE001
        log.warning("health worker: %s", type(e).__name__)


def _loop():
    while True:
        _tick()
        time.sleep(HEARTBEAT_INTERVAL_S)


def start():
    """Start the background worker once per process."""
    if _started.is_set():
        return
    _started.set()
    threading.Thread(target=_loop, name="loki-health", daemon=True).start()
