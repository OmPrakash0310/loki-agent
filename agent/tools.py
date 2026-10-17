"""The tools Loki's investigator can call. All of them are read-only."""
import json
import threading
import time
from datetime import datetime

import db
import telemetry as tm
from redact import redact

MAX_OUTPUT_CHARS = 6000

TOOLS = [
    {
        "name": "query_logs",
        "description": (
            "Search recent logs and return them grouped into patterns (count, services, first/last seen, one "
            "example line). Use it to see what errors a service is producing and when they started. "
            "Secrets are already redacted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name, e.g. payments-svc. Omit for all services."},
                "contains": {"type": "string", "description": "Only lines containing this text (case-insensitive)."},
                "minutes": {"type": "integer", "description": "How far back to look. Default 20, max 120."},
                "errors_only": {"type": "boolean", "description": "Only error-like lines. Default true."},
            },
        },
    },
    {
        "name": "query_metrics",
        "description": (
            "Get a metric for one service as a time series sampled every 30 seconds, plus min, max and the value "
            "before and after the window. Metrics: error_rate (fraction of requests returning 5xx), "
            "p95_latency_seconds, request_rate (requests per second)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": list(tm.METRICS)},
                "service": {"type": "string"},
                "minutes": {"type": "integer", "description": "How far back to look. Default 20, max 120."},
            },
            "required": ["metric", "service"],
        },
    },
    {
        "name": "query_dependencies",
        "description": (
            "p95 latency of the calls a service makes to its dependencies (its database, external APIs like a "
            "card network), as a time series. Use it when a service is slow or timing out, to see which "
            "dependency it is waiting on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "minutes": {"type": "integer", "description": "How far back to look. Default 20, max 120."},
            },
            "required": ["service"],
        },
    },
    {
        "name": "recent_deploys",
        "description": "List deploys (id, service, version, author, message, time) in the last N minutes.",
        "input_schema": {
            "type": "object",
            "properties": {"minutes": {"type": "integer", "description": "Default 120, max 1440."}},
        },
    },
    {
        "name": "git_diff",
        "description": "Show the code change (unified diff) shipped in a deploy.",
        "input_schema": {
            "type": "object",
            "properties": {"deploy_id": {"type": "string"}},
            "required": ["deploy_id"],
        },
    },
    {
        "name": "search_past_incidents",
        "description": "Search earlier incidents whose root cause a human confirmed, and how they were resolved.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords, e.g. a service or error name."}},
            "required": ["query"],
        },
    },
    {
        "name": "submit_report",
        "description": (
            "Submit your final root-cause report. Call this exactly once, when you are done investigating. "
            "Every evidence item must be a fact you saw in a tool result or the briefing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One sentence: what broke and why."},
                "root_cause": {"type": "string", "description": "The underlying cause, in 1-3 sentences."},
                "root_cause_service": {"type": "string"},
                "trigger": {"type": "string", "enum": ["deploy", "infrastructure", "dependency", "traffic", "unknown"]},
                "deploy_id": {"type": "string", "description": "The deploy that caused it, if trigger is deploy. Empty string otherwise."},
                "affected_services": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "ruled_out": {"type": "array", "items": {"type": "string"}, "description": "Suspects you checked and excluded, with the reason."},
                "confidence": {"type": "number", "description": "0 to 1. How sure you are, given the evidence."},
                "suggested_action": {"type": "string", "enum": ["rollback", "fix_forward", "scale_up", "restart", "escalate", "none"]},
                "action_detail": {"type": "string", "description": "Exactly what a human should do next."},
            },
            "required": [
                "summary", "root_cause", "root_cause_service", "trigger", "deploy_id", "affected_services",
                "evidence", "ruled_out", "confidence", "suggested_action", "action_detail",
            ],
        },
    },
]


def _clip(minutes, default, maximum):
    try:
        return max(1, min(int(minutes), maximum))
    except (TypeError, ValueError):
        return default


def query_logs(service=None, contains=None, minutes=20, errors_only=True):
    m = _clip(minutes, 20, 120)
    lines = tm.fetch_logs(service or None, _since(m), tm.now(), errors_only is not False)
    if contains:
        lines = [l for l in lines if contains.lower() in l[2].lower()]
    clusters = tm.cluster_logs(lines)
    return {"window_minutes": m, "matching_lines": len(lines), "patterns": clusters}


def query_metrics(metric, service, minutes=20):
    if metric not in tm.METRICS:
        raise ValueError(f"unknown metric {metric!r}; use one of {list(tm.METRICS)}")
    m = _clip(minutes, 20, 120)
    points = tm.metric_series(metric, service, _since(m), tm.now())
    if not points:
        return {"service": service, "metric": metric, "points": [], "note": "no data (unknown service or no traffic)"}
    values = [v for _, v in points]
    return {
        "service": service,
        "metric": metric,
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "points": [[tm._hms(t), v] for t, v in points],
    }


def recent_deploys(minutes=120):
    m = _clip(minutes, 120, 1440)
    return [
        {k: d[k] for k in ("id", "service", "version", "author", "message", "deployed_at")}
        for d in tm.deploys_between(tm.minutes_ago(m), tm.now())
    ]


def git_diff(deploy_id):
    for d in tm.load_deploys():
        if d["id"] == deploy_id:
            return {"deploy_id": deploy_id, "service": d["service"], "version": d["version"], "diff": d["diff"]}
    raise ValueError(f"no deploy with id {deploy_id!r}")


def search_past_incidents(query):
    words = [w for w in query.split() if len(w) > 2][:5] or [query]
    with db.connect() as conn:
        rows = conn.execute(
            # Only answers a human confirmed. Unconfirmed reports may be wrong, and feeding
            # them back in makes Loki repeat its own mistakes.
            "SELECT i.id, i.finished_at, i.report FROM investigations i "
            "JOIN report_cache c ON c.investigation_id = i.id AND c.confirmed "
            # Live incidents only, and only ones already solved by "now": in a backtest,
            # an answer found after the replay time would be knowledge from the future.
            "WHERE i.mode = 'live' AND i.finished_at <= %s AND c.created_at <= %s "
            "AND (" + " OR ".join(["i.report::text ILIKE %s"] * len(words)) + ") ORDER BY i.id DESC LIMIT 5",
            [tm.now(), tm.now(), *[f"%{w}%" for w in words]],
        ).fetchall()
    return [
        {
            "investigation": r["id"],
            "date": r["finished_at"].isoformat() if r["finished_at"] else None,
            "summary": r["report"].get("summary"),
            "action": r["report"].get("suggested_action"),
        }
        for r in rows
    ]


# The investigation's time window. Tools never look earlier than this, so
# leftovers from a previous, already-resolved incident can't mislead Loki.
_scope = threading.local()


def set_window_floor(floor: datetime | None) -> None:
    _scope.floor = floor


def _since(minutes: int) -> datetime:
    start = tm.minutes_ago(minutes)
    floor = getattr(_scope, "floor", None)
    return max(start, floor) if floor else start


def query_dependencies(service, minutes=20):
    m = _clip(minutes, 20, 120)
    series = tm.dependency_p95_series(service, _since(m), tm.now())
    if not series:
        return {"service": service, "dependencies": {}, "note": "no dependency timing for this service"}
    return {
        "service": service,
        "metric": "p95 latency in seconds of calls from this service to each dependency",
        "dependencies": {
            dep: {"first": pts[0][1], "last": pts[-1][1], "max": max(v for _, v in pts),
                  "points": [[tm._hms(t), v] for t, v in pts]}
            for dep, pts in series.items() if pts
        },
    }


HANDLERS = {
    "query_logs": query_logs,
    "query_metrics": query_metrics,
    "recent_deploys": recent_deploys,
    "git_diff": git_diff,
    "search_past_incidents": search_past_incidents,
    "query_dependencies": query_dependencies,
}


# Tool-result cache: the same query within TOOL_CACHE_TTL_S returns the saved
# result instead of hitting Grafana Loki / Prometheus again. Errors aren't cached.
TOOL_CACHE_TTL_S = 45
_tool_cache: dict[str, tuple[float, str]] = {}
_tool_cache_lock = threading.Lock()
tool_cache_stats = {"hits": 0, "misses": 0}


def run(name: str, args: dict) -> tuple[str, bool]:
    """Run a tool. Returns (output text, is_error)."""
    # The present is part of the key: a backtest must never get a live result (from
    # the future) back, and a live investigation must never get a stale backtest one.
    replay_at = tm.replay_time()
    key = name + json.dumps(args, sort_keys=True) + (replay_at.isoformat() if replay_at else "")
    with _tool_cache_lock:
        hit = _tool_cache.get(key)
        if hit and time.monotonic() - hit[0] < TOOL_CACHE_TTL_S:
            tool_cache_stats["hits"] += 1
            return hit[1], False
        tool_cache_stats["misses"] += 1
    out, is_error = _run_uncached(name, args)
    if not is_error:
        with _tool_cache_lock:
            _tool_cache[key] = (time.monotonic(), out)
    return out, is_error


def _run_uncached(name: str, args: dict) -> tuple[str, bool]:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool {name!r}.", True
    try:
        out = json.dumps(handler(**args), default=str)
    except Exception as e:  # tool failures go back to the model, not up the stack
        return f"{type(e).__name__}: {e}", True
    out = redact(out)
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "... [truncated: narrow the query]"
    return out, False

