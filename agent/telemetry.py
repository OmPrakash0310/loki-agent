"""Read-only access to logs (Grafana Loki), metrics (Prometheus) and deploy history.

Deploys and error lines go through sources/ so other backends (GitHub, Sentry)
can stand in for the demo ones.

Everything returned here is compact and redacted, ready to show the model.
"""
import contextvars
import os
import re
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

import logparse
import metrics_config
import sources
from sources import httpkit
from redact import redact

# Grafana Loki (logs). Set it to empty (or "off") for a stack without Grafana Loki:
# log queries then return nothing instead of failing the investigation.
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100").strip()
if LOKI_URL.lower() in ("off", "none"):
    LOKI_URL = ""
# LOKI_PROM_URL wins when set. Set it to empty (or "off") for a stack without
# Prometheus: the briefing then uses logs, errors and deploys only.
PROM_URL = (os.environ["LOKI_PROM_URL"].strip() if "LOKI_PROM_URL" in os.environ
            else os.environ.get("PROM_URL", "http://prometheus:9090"))
if PROM_URL.lower() in ("off", "none"):
    PROM_URL = ""
DEPLOYS_FILE = os.environ.get("DEPLOYS_FILE", "/state/deploys.json")


def _get(kind: str, url: str, **kw):
    headers = httpkit.auth_headers(kind)
    if headers:
        kw["headers"] = headers
    return httpx.get(url, **kw)

# Keep log lines that report a problem, plus exception summaries such as
# "KeyError: 'currency'" or "java.lang.IllegalStateException: ...", "Caused by:"
# chains, Go panics and JSON lines logged at error level (pino 50/60, winston,
# zap, logrus). Skip stack-frame lines (Python "File ..., line 51", Java/Node
# "at ...", Go "goroutine 1 [running]:" and "/app/x.go:42 +0x1d"), which would
# otherwise drown out the one line that matters. A summary must end in ":", " [" or
# the line end, so Go frames like "main.handleError(...)" stay out.
# One RE2 regex (no lookarounds, no backreferences) in a backtick raw string.
ERROR_FILTER = (
    r"|~ `(?i)(^\S*\d{4}[-/]\d\d[-/]\d\d.*(error|exception|failed|timeout|refused|panic))"
    r"|(^(ERROR|CRITICAL|WARNING)\b)"
    r"|(^[\s|+]*([a-z_$][\w.$]*)?(error|exception|throwable)(:|\s*$|\s\[))"
    r"|(^\s*(caused by:|\[cause\]:|exception in thread |panic: |fatal error: ))"
    r"|(unhandledpromiserejection)"
    r'|(\b(level|severity)"?\s*[:=]\s*"?(error|fatal|panic|dpanic|critical|50|60)\b)`'
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
    cfg = metrics_config.current()
    selector = cfg.log_selector(service)
    query = selector + (" " + ERROR_FILTER if errors_only else "")
    end = _cap(end)
    if start >= end or not LOKI_URL:
        return []
    resp = _get(
        "LOGS", f"{LOKI_URL}/loki/api/v1/query_range",
        params={"query": query, "start": _ns(start), "end": _ns(end), "limit": limit, "direction": "backward"},
        timeout=20,
    )
    resp.raise_for_status()
    lines = []
    for stream in resp.json()["data"]["result"]:
        svc = stream["stream"].get(cfg.log_service_label, "?")
        for ts, line in stream["values"]:
            lines.append((datetime.fromtimestamp(int(ts) / 1e9, timezone.utc), svc, line))
    lines.sort(key=lambda x: x[0])
    return _not_future(lines)


# Values that vary per request but don't make a different error. Each is replaced
# before the generic digit rule below. None of them can fire on the Python demo's
# lines, whose patterns (and so the answer-cache fingerprints) must not change:
# tests/test_languages.py locks them.
_VOLATILE = [
    # request / trace ids: keep the key, drop the value
    (re.compile(r"\b((?:req(?:uest)?|trace|span|correlation)[-_]?id)([\"']?\s*[=:]\s*[\"']?)[\w-]+", re.I), r"\1\2<id>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),  # Go/C addresses
    (re.compile(r"(?<=[\w$])@[0-9a-f]{4,8}\b(?!\.\w)"), "@<hash>"),  # Java Object.toString(): Payment@1a2b3c4d
    # prefixed ids and pod names: ord_9f8e7d2a, payments-7d9f8c6b5
    (re.compile(r"\b[a-zA-Z]{2,8}[-_](?=[A-Za-z]*\d)[A-Za-z0-9]{6,}\b"), "<id>"),
    (re.compile(r"\b(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+\b"), "<dur>"),  # 250ms, 5.002s, 1m30s
    # quoted data (tokens, ids, emails), not quoted names: 'currency' stays, so
    # KeyError: 'currency' and KeyError: 'amount' remain different errors
    (re.compile(r"""(["'])(?=[^"'\s]*(?:[A-Za-z][^"'\s]*\d|\d[^"'\s]*[A-Za-z]|@))[^"'\s]+\1|(["'])[^"'\s]{24,}\2"""), "'<v>'"),
]


def normalize(line: str) -> str:
    """Turn a log line into a pattern so identical errors group together."""
    line = re.sub(r"^[\s|+]+", "", line)  # exception-group indentation
    line = re.sub(r"^\d{4}[-/]\d\d[-/]\d\d[ T][\d:,.]+(?:Z|[+-]\d\d:?\d\d)?\s*", "", line)  # leading timestamp
    for rx, repl in _VOLATILE:
        line = rx.sub(repl, line)
    line = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", line)
    line = re.sub(r"\d+(\.\d+)?", "<n>", line)
    return line.strip()[:220]


def _event_view(ev, stream_lang):
    """(pattern, example, extra keys) for one folded event.
    A single plain line is handled exactly as before the language work."""
    if len(ev.lines) == 1 and not (expanded := logparse.json_lines(ev.lines[0])):
        line = ev.lines[0]
        extra = {"language": stream_lang, "error_type": logparse.parse([line]).error_type}
        return normalize(line), re.sub(r"^[\s|+]+", "", line), extra
    lines = expanded if len(ev.lines) == 1 else ev.lines
    p = logparse.parse(lines, python=ev.python)
    lead = re.sub(r"^[\s|+]+", "", p.lead) if p.lead else None
    parts = [lead] if lead else []
    if p.outer and not (lead and p.outer.split(": ", 1)[-1] in lead):
        parts.append(p.outer)
    if p.root and p.root != p.outer:
        parts.append(f"caused by {p.root}")
    # Cap each part so a long logger header can't push the root cause out of the example.
    example = " | ".join(x[:200] for x in parts) or ev.lines[0]
    extra = {"language": logparse.detect_language(ev.lines) or stream_lang, "error_type": p.error_type,
             "location": p.location, "root_cause": p.root}
    return normalize(p.root or lead or ev.lines[0]), example, extra


def cluster_logs(lines, top: int = 10):
    """Group error lines into ranked patterns. Stack traces are folded into one event
    first (any language), so a trace counts once and is keyed by its root cause."""
    by_svc = defaultdict(list)
    for _, svc, line in lines:
        by_svc[svc].append(line)
    stream_lang = {svc: logparse.detect_language(ls) for svc, ls in by_svc.items()}
    clusters = defaultdict(lambda: {"count": 0, "services": set(), "first": None, "last": None, "example": "", "extra": {}})
    for ev in logparse.fold(lines):
        if all(not l.strip() or logparse.is_frame(l) for l in ev.lines):
            continue  # stray frames or blank lines: no error of their own
        pattern, example, extra = _event_view(ev, stream_lang[ev.svc])
        c = clusters[pattern]
        c["count"] += 1
        c["services"].add(ev.svc)
        c["first"] = c["first"] or ev.t
        c["last"] = ev.t
        c["example"] = example
        # The latest event's details win, but a location seen once is kept: the trace
        # that had frames says where, even if the latest line of the pattern didn't.
        c["extra"].update({k: v for k, v in extra.items() if v})
    ranked = sorted(clusters.items(), key=lambda kv: kv[1]["count"], reverse=True)[:top]
    return [_render(c) for _, c in ranked]


def _render(c) -> dict:
    loc = c["extra"].get("location")
    suffix = f" [at {loc}]" if loc else ""
    return {
        "count": c["count"],
        "services": sorted(c["services"]),
        "first_seen": _hms(c["first"]),
        "last_seen": _hms(c["last"]),
        "example": redact(c["example"][:400 - len(suffix)] + suffix),
        # optional: language, error_type, location ("src/charge.ts:42"), root_cause
        **{k: redact(v) for k, v in c["extra"].items()},
    }


# ---------- metrics ----------

# The metrics the briefing and query_metrics offer. Their PromQL comes from
# metrics_config (LOKI_METRICS_PRESET / LOKI_METRICS_CONFIG); the defaults are the demo's.
METRICS = metrics_config.METRIC_NAMES


def prom_enabled() -> bool:
    return bool(PROM_URL)


def _cfg() -> metrics_config.MetricsConfig:
    return metrics_config.current()


# Which metric families exist in Prometheus, looked up once and cached. A lookup
# that fails (old Prometheus, a compatible backend without the API) counts as
# "assume present", so the queries run and simply return no data as before.
NAMES_TTL_S = 300
_names_cache: dict[str, tuple[float, frozenset | None]] = {}
_names_lock = threading.Lock()


def _metric_names() -> frozenset | None:
    with _names_lock:
        hit = _names_cache.get(PROM_URL)
        if hit and time.monotonic() - hit[0] < NAMES_TTL_S:
            return hit[1]
    try:
        resp = _get("PROM", f"{PROM_URL}/api/v1/label/__name__/values", timeout=10)
        resp.raise_for_status()
        names = frozenset(resp.json()["data"])
    except Exception:
        names = None
    with _names_lock:
        _names_cache[PROM_URL] = (time.monotonic(), names)
    return names


def reset_metric_cache() -> None:
    with _names_lock:
        _names_cache.clear()


def _family_missing(series: str | None, what: str) -> str | None:
    if not prom_enabled():
        return "no Prometheus configured (LOKI_PROM_URL is empty)"
    if not series:
        return f"no {what} metric configured"
    names = _metric_names()
    if names is not None and series not in names:
        return f"no {what} metric found ({series} is not in Prometheus)"
    return None


def metric_unavailable(metric: str) -> str | None:
    """Why `metric` can't be queried here (not configured, not in Prometheus, no Prometheus), or None."""
    cfg = _cfg()
    if not prom_enabled():
        return "no Prometheus configured (LOKI_PROM_URL is empty)"
    reason = cfg.missing_reason(metric)
    if reason:
        return reason
    fam = cfg.families()
    if metric == "p95_latency_seconds":
        return _family_missing(fam["latency"], "latency")
    return _family_missing(fam["requests"], "request counter")


def dependency_unavailable() -> str | None:
    return _family_missing(_cfg().families()["dependency"], "dependency latency")


def _label_window() -> dict:
    """In a backtest, only services that existed in the hour before the replay time."""
    at = replay_time()
    return {"start": (at - timedelta(hours=1)).timestamp(), "end": at.timestamp()} if at else {}


def services() -> list[str]:
    if not prom_enabled():
        return []
    cfg = _cfg()
    match = cfg.services_match()
    params = {"match[]": match, **_label_window()} if match else _label_window()
    resp = _get("PROM", f"{PROM_URL}/api/v1/label/{cfg.service_label}/values", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]


def metric_series(metric: str, service: str, start: datetime, end: datetime, step_s: int = 30):
    if metric_unavailable(metric):
        return []
    query = _cfg().promql(metric, service)
    end = _cap(end)
    if start > end:
        return []
    resp = _get(
        "PROM",
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
    if metric_unavailable(metric):
        return None
    at = _cap(at)
    resp = _get(
        "PROM",
        f"{PROM_URL}/api/v1/query",
        params={"query": _cfg().promql(metric, service), "time": at.timestamp()},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        return None
    v = float(result[0]["value"][1])
    return None if v != v else round(v, 4)


def dependency_p95_at(service: str, at: datetime) -> dict[str, float]:
    """p95 latency of each dependency (database, external APIs) a service calls."""
    if dependency_unavailable():
        return {}
    cfg = _cfg()
    at = _cap(at)
    resp = _get("PROM", f"{PROM_URL}/api/v1/query", params={"query": cfg.dependency_promql(service), "time": at.timestamp()}, timeout=10)
    resp.raise_for_status()
    out = {}
    for r in resp.json()["data"]["result"]:
        v = float(r["value"][1])
        if v == v:
            out[r["metric"].get(cfg.dependency_label, "?")] = round(v, 4)
    return out


def dependency_p95_series(service: str, start: datetime, end: datetime, step_s: int = 30) -> dict[str, list]:
    if dependency_unavailable():
        return {}
    cfg = _cfg()
    end = _cap(end)
    if start > end:
        return {}
    resp = _get(
        "PROM",
        f"{PROM_URL}/api/v1/query_range",
        params={"query": cfg.dependency_promql(service), "start": start.timestamp(), "end": end.timestamp(), "step": step_s},
        timeout=20,
    )
    resp.raise_for_status()
    out = {}
    for r in resp.json()["data"]["result"]:
        pts = [(datetime.fromtimestamp(float(t), timezone.utc), round(float(v), 4)) for t, v in r["values"] if float(v) == float(v)]
        out[r["metric"].get(cfg.dependency_label, "?")] = _not_future(pts)
    return out


def services_with_dependencies() -> list[str]:
    if dependency_unavailable():
        return []
    cfg = _cfg()
    resp = _get("PROM", f"{PROM_URL}/api/v1/label/{cfg.service_label}/values",
                     params={"match[]": f"{cfg.dependency_histogram}_count", **_label_window()}, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]


# ---------- deploys and errors, through pluggable sources ----------
# Which backend answers is chosen by env vars (see sources/__init__.py). The
# defaults are the JSON deploy file and Grafana Loki, as before.

def _deployed_at(d: dict) -> datetime:
    return d["deployed_at_dt"]


def load_deploys():
    # Deploys after the present don't exist yet (also hides their diffs from git_diff).
    return _not_future(sources.deploys().list_deploys(None, None), when=_deployed_at)


def deploys_between(start: datetime, end: datetime):
    return _not_future(sources.deploys().list_deploys(start, _cap(end)), when=_deployed_at)


def deploy_diff(deploy_id: str) -> dict:
    # In a replay, a deploy that hasn't happened yet has no diff to show.
    if replay_time() and not any(d["id"] == deploy_id for d in load_deploys()):
        raise ValueError(f"no deploy with id {deploy_id!r}")
    return sources.deploys().get_diff(deploy_id)


def error_lines(service: str | None, start: datetime, end: datetime):
    """Error lines as (time, service, line), oldest first, from the configured errors source."""
    end = _cap(end)
    if start >= end:
        return []
    return _not_future(sources.errors().error_lines(service, start, end))


def logs_present(start: datetime, end: datetime) -> bool:
    return sources.errors().logs_present(start, end)


def deploy_notes() -> list[str]:
    return sources.deploys().notes()


def error_notes() -> list[str]:
    return sources.errors().notes()


def now() -> datetime:
    """The present: the wall clock, or the replay time during a backtest."""
    return replay_time() or datetime.now(timezone.utc)


def minutes_ago(m: float) -> datetime:
    return now() - timedelta(minutes=m)
