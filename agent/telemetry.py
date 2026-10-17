"""Read-only access to logs (Grafana Loki), metrics (Prometheus) and deploy history.

Everything returned here is compact and redacted, ready to show the model.
"""
import contextvars
import json
import os
import re
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

from redact import redact

LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100")
PROM_URL = os.environ.get("PROM_URL", "http://prometheus:9090")
DEPLOYS_FILE = os.environ.get("DEPLOYS_FILE", "/state/deploys.json")

# Keep log lines that report a problem, plus exception summaries such as
# "KeyError: 'currency'". Skip stack-frame lines ("File ..., line 51"), which
# would otherwise drown out the one line that matters. Backticks = raw string.
ERROR_FILTER = (
    "|~ `(?i)(^\\S*\\d{4}-\\d\\d-\\d\\d.*(error|exception|failed|timeout|refused))"
    "|(^(ERROR|CRITICAL|WARNING)\\b)"
    "|(^[\\s|+]*[a-z_][\\w.]*(error|exception)\\b)`"
)


# ---------- time travel (backtesting) ----------
# A backtest replays a past incident: "now" becomes the replay time. Every query
# below caps its end at now() and drops anything stamped later, so data from after
# the replay time can never reach the briefing or a tool, even if a caller asks
# for it. A contextvar (not a global) so live and backtest investigations running
# in different threads never see each other's clock.
_replay_at: contextvars.ContextVar[datetime | None] = contextvars.ContextVar("replay_at", default=None)


@contextmanager
def replay(at: datetime):
    """Run the body as if the present were `at`."""
    if at.tzinfo is None:
        raise ValueError("replay time must be timezone-aware")
    if at > datetime.now(timezone.utc):
        raise ValueError("can't replay the future")
    token = _replay_at.set(at.astimezone(timezone.utc))
    try:
        yield
    finally:
        _replay_at.reset(token)


def replay_time() -> datetime | None:
    return _replay_at.get()


def _cap(end: datetime) -> datetime:
    """The honesty guard for query ranges: never ask past the present.
    Live investigations are left exactly as they were (no clock-skew surprises)."""
    at = replay_time()
    return min(end, at) if at else end


def _not_future(items, when=lambda x: x[0]):
    """The honesty guard for results: drop anything stamped after the present
    (range ends can be inclusive, and step alignment can round up)."""
    at = replay_time()
    return [x for x in items if when(x) <= at] if at else items


def _ns(t: datetime) -> int:
    return int(t.timestamp() * 1e9)


def _hms(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%H:%M:%S")


# ---------- logs ----------

def fetch_logs(service: str | None, start: datetime, end: datetime, errors_only: bool, limit: int = 5000):
    # Loki never investigates its own logs.
    selector = f'{{service="{service}"}}' if service else '{service=~".+", service!="agent"}'
    query = selector + (" " + ERROR_FILTER if errors_only else "")
    end = _cap(end)
    if start >= end:
        return []
    resp = httpx.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={"query": query, "start": _ns(start), "end": _ns(end), "limit": limit, "direction": "backward"},
        timeout=20,
    )
    resp.raise_for_status()
    lines = []
    for stream in resp.json()["data"]["result"]:
        svc = stream["stream"].get("service", "?")
        for ts, line in stream["values"]:
            lines.append((datetime.fromtimestamp(int(ts) / 1e9, timezone.utc), svc, line))
    lines.sort(key=lambda x: x[0])
    return _not_future(lines)


def normalize(line: str) -> str:
    """Turn a log line into a pattern so identical errors group together."""
    line = re.sub(r"^[\s|+]+", "", line)  # exception-group indentation
    line = re.sub(r"^\d{4}-\d\d-\d\d[ T][\d:,.]+\s*", "", line)  # leading timestamp
    line = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", line)
    line = re.sub(r"\d+(\.\d+)?", "<n>", line)
    return line.strip()[:220]


def cluster_logs(lines, top: int = 10):
    clusters = defaultdict(lambda: {"count": 0, "services": set(), "first": None, "last": None, "example": ""})
    for t, svc, line in lines:
        c = clusters[normalize(line)]
        c["count"] += 1
        c["services"].add(svc)
        c["first"] = c["first"] or t
        c["last"] = t
        c["example"] = line
    ranked = sorted(clusters.items(), key=lambda kv: kv[1]["count"], reverse=True)[:top]
    return [
        {
            "count": c["count"],
            "services": sorted(c["services"]),
            "first_seen": _hms(c["first"]),
            "last_seen": _hms(c["last"]),
            "example": redact(re.sub(r"^[\s|+]+", "", c["example"])[:400]),
        }
        for _, c in ranked
    ]


# ---------- metrics ----------

METRICS = {
    "error_rate": (
        '(sum(rate(http_requests_total{{service="{s}",status=~"5.."}}[1m])) or vector(0))'
        ' / sum(rate(http_requests_total{{service="{s}"}}[1m]))'
    ),
    "p95_latency_seconds": (
        'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{service="{s}"}}[1m])))'
    ),
    "request_rate": 'sum(rate(http_requests_total{{service="{s}"}}[1m]))',
}


def _label_window() -> dict:
    """In a backtest, only services that existed in the hour before the replay time."""
    at = replay_time()
    return {"start": (at - timedelta(hours=1)).timestamp(), "end": at.timestamp()} if at else {}


def services() -> list[str]:
    resp = httpx.get(f"{PROM_URL}/api/v1/label/service/values", params=_label_window(), timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]


def metric_series(metric: str, service: str, start: datetime, end: datetime, step_s: int = 30):
    query = METRICS[metric].format(s=service)
    end = _cap(end)
    if start > end:
        return []
    resp = httpx.get(
        f"{PROM_URL}/api/v1/query_range",
        params={"query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step_s},
        timeout=20,
    )
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return []
    points = []
    for ts, value in result[0]["values"]:
        v = float(value)
        if v != v:  # NaN when there was no traffic
            continue
        points.append((datetime.fromtimestamp(float(ts), timezone.utc), round(v, 4)))
    return _not_future(points)


def metric_before(metric: str, service: str, at: datetime):
    """Typical value in the 10 minutes before `at` (median), or the earliest data if the stack is new."""
    points = metric_series(metric, service, at - timedelta(minutes=10), at)
    if not points:
        return None
    values = sorted(v for _, v in points)
    return values[len(values) // 2]


def metric_at(metric: str, service: str, at: datetime):
    at = _cap(at)
    resp = httpx.get(
        f"{PROM_URL}/api/v1/query",
        params={"query": METRICS[metric].format(s=service), "time": at.timestamp()},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return None
    v = float(result[0]["value"][1])
    return None if v != v else round(v, 4)


DEPENDENCY_P95 = (
    'histogram_quantile(0.95, sum by (le, dependency) '
    '(rate(dependency_duration_seconds_bucket{{service="{s}"}}[1m])))'
)


def dependency_p95_at(service: str, at: datetime) -> dict[str, float]:
    """p95 latency of each dependency (database, external APIs) a service calls."""
    at = _cap(at)
    resp = httpx.get(f"{PROM_URL}/api/v1/query", params={"query": DEPENDENCY_P95.format(s=service), "time": at.timestamp()}, timeout=10)
    resp.raise_for_status()
    out = {}
    for r in resp.json()["data"]["result"]:
        v = float(r["value"][1])
        if v == v:
            out[r["metric"].get("dependency", "?")] = round(v, 4)
    return out


def dependency_p95_series(service: str, start: datetime, end: datetime, step_s: int = 30) -> dict[str, list]:
    end = _cap(end)
    if start > end:
        return {}
    resp = httpx.get(
        f"{PROM_URL}/api/v1/query_range",
        params={"query": DEPENDENCY_P95.format(s=service), "start": start.timestamp(), "end": end.timestamp(), "step": step_s},
        timeout=20,
    )
    resp.raise_for_status()
    out = {}
    for r in resp.json()["data"]["result"]:
        pts = [(datetime.fromtimestamp(float(t), timezone.utc), round(float(v), 4)) for t, v in r["values"] if float(v) == float(v)]
        out[r["metric"].get("dependency", "?")] = _not_future(pts)
    return out


def services_with_dependencies() -> list[str]:
    resp = httpx.get(f"{PROM_URL}/api/v1/label/service/values",
                     params={"match[]": "dependency_duration_seconds_count", **_label_window()}, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]


# ---------- deploys ----------

def load_deploys():
    try:
        with open(DEPLOYS_FILE) as f:
            deploys = json.load(f)
    except FileNotFoundError:
        return []
    for d in deploys:
        d["deployed_at_dt"] = datetime.fromisoformat(d["deployed_at"])
    # Deploys after the present don't exist yet (also hides their diffs from git_diff).
    return sorted(_not_future(deploys, when=lambda d: d["deployed_at_dt"]), key=lambda d: d["deployed_at_dt"])


def deploys_between(start: datetime, end: datetime):
    return [d for d in load_deploys() if start <= d["deployed_at_dt"] <= end]


def now() -> datetime:
    """The present: the wall clock, or the replay time during a backtest."""
    return replay_time() or datetime.now(timezone.utc)


def minutes_ago(m: float) -> datetime:
    return now() - timedelta(minutes=m)
