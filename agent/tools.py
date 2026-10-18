"""The tools Loki's investigator can call. All of them are read-only."""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import httpx

import db
import telemetry as tm
from redact import redact

MAX_OUTPUT_CHARS = 6000
log = logging.getLogger("agent")

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
    errors_only = errors_only is not False
    if errors_only:
        lines = tm.error_lines(service or None, _since(m), tm.now())
    else:  # full logs only exist in Grafana Loki
        lines = tm.fetch_logs(service or None, _since(m), tm.now(), False)
    if contains:
        lines = [l for l in lines if contains.lower() in l[2].lower()]
    clusters = tm.cluster_logs(lines)
    out = {"window_minutes": m, "matching_lines": len(lines), "patterns": clusters}
    notes = tm.error_notes() if errors_only else []
    return {**out, "notes": notes} if notes else out


def query_metrics(metric, service, minutes=20):
    if metric not in tm.METRICS:
        raise ValueError(f"unknown metric {metric!r}; use one of {list(tm.METRICS)}")
    missing = tm.metric_unavailable(metric)
    if missing:  # a clear answer the model can act on, not a failure
        return {"service": service, "metric": metric, "points": [], "note": f"{metric} is not available: {missing}"}
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
    deploys = [
        {k: d[k] for k in ("id", "service", "version", "author", "message", "deployed_at")}
        for d in tm.deploys_between(tm.minutes_ago(m), tm.now())
    ]
    notes = tm.deploy_notes()  # e.g. GitHub rate limit: say the list may be incomplete
    return {"deploys": deploys, "notes": notes} if notes else deploys


def git_diff(deploy_id):
    return tm.deploy_diff(deploy_id)


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
    missing = tm.dependency_unavailable()
    if missing:
        return {"service": service, "dependencies": {}, "note": f"dependency latency is not available: {missing}"}
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


# ---------- live probes (LOKI_PROBES=1) ----------
# capture_variables places a non-breaking probe in a running service (probes/python/loki_probe),
# waits briefly for the line to run, and always removes the probe again. Off unless
# LOKI_PROBES=1 and LOKI_PROBE_ENDPOINTS is set; never offered in a backtest.

CAPTURE_TOOL = {
    "name": "capture_variables",
    "description": (
        "Capture local variable values at one line of code in a RUNNING service, for a few seconds, "
        "without stopping or changing it (a non-breaking breakpoint). Use it only to confirm a hypothesis "
        "about a bad value at a specific line you already found in a diff or stack trace (e.g. what "
        "`payload` held when charge() failed). It waits for real traffic to hit the line. Don't use it "
        "when logs already answer the question. Values are redacted and size-capped."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service name, e.g. payments-svc."},
            "file": {"type": "string", "description": "Path relative to the service's app directory, e.g. main.py."},
            "line": {"type": "integer", "description": "Line number; values are captured just before it runs."},
            "variables": {"type": "array", "items": {"type": "string"}, "description": "Local names to capture. Default: all locals."},
            "seconds": {"type": "integer", "description": "How long to wait for hits. Default 20, max 60."},
            "condition": {
                "type": "object",
                "description": "Only capture when a local equals a value, e.g. {\"variable\": \"order_id\", \"equals\": 42}.",
                "properties": {"variable": {"type": "string"}, "equals": {"type": ["string", "number", "boolean", "null"]}},
                "required": ["variable", "equals"],
            },
        },
        "required": ["service", "file", "line"],
    },
}

PROBE_MAX_WAIT_S = 60
PROBE_MAX_HITS = 5
PROBE_POLL_S = 0.5
PROBE_GRACE_S = 2.0  # after the first hit, wait this long for more, then return
# Per investigation: how many probes and how many seconds of waiting it may spend.
PROBE_MAX_PER_INVESTIGATION = int(os.environ.get("LOKI_PROBE_MAX_PER_INVESTIGATION", "3"))
PROBE_MAX_WAIT_PER_INVESTIGATION_S = 120


def probe_endpoints() -> dict[str, str]:
    """LOKI_PROBE_ENDPOINTS="payments-svc=http://payments-svc:8000,shop-api=http://shop-api:8000"."""
    out = {}
    for part in os.environ.get("LOKI_PROBE_ENDPOINTS", "").split(","):
        name, _, url = part.partition("=")
        if name.strip() and url.strip():
            out[name.strip()] = url.strip().rstrip("/")
    return out


def probes_unavailable() -> str | None:
    """Why capture_variables can't be used right now, or None if it can."""
    if os.environ.get("LOKI_PROBES") != "1":
        return "live probes are off (LOKI_PROBES is not 1)"
    if not probe_endpoints():
        return "no probe endpoints configured (LOKI_PROBE_ENDPOINTS)"
    if tm.replay_time() is not None:
        return "live probes are never used in a backtest: they would read the present, not the replayed past"
    return None


def available_tools() -> list[dict]:
    """The tools to offer the model for this run."""
    return TOOLS + [CAPTURE_TOOL] if probes_unavailable() is None else TOOLS


def set_investigation(inv_id: int | None) -> None:
    """Called at the start of each investigation: probes are audited and budgeted per investigation."""
    _scope.inv_id = inv_id
    _scope.probes = 0
    _scope.probe_wait_s = 0.0


def _audit_probe(service: str, file: str, line: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor, action, investigation_id) VALUES (%s, %s, %s)",
            ("loki-agent", f"probe:{service}:{file}:{line}", getattr(_scope, "inv_id", None)),
        )


def capture_variables(service, file, line, variables=None, seconds=20, condition=None):
    why = probes_unavailable()
    if why:
        raise PermissionError(why)
    base = probe_endpoints().get(service)
    if base is None:
        raise ValueError(f"no probe endpoint for {service!r}; known: {sorted(probe_endpoints())}")
    used = getattr(_scope, "probes", 0)
    waited = getattr(_scope, "probe_wait_s", 0.0)
    if used >= PROBE_MAX_PER_INVESTIGATION or waited >= PROBE_MAX_WAIT_PER_INVESTIGATION_S:
        raise PermissionError(f"probe budget for this investigation is used up ({used} probes, {waited:.0f}s)")
    wait = min(_clip(seconds, 20, PROBE_MAX_WAIT_S), PROBE_MAX_WAIT_PER_INVESTIGATION_S - waited)
    body = {"file": file, "line": line, "seconds": int(wait) + 5, "max_hits": PROBE_MAX_HITS}
    if variables:
        body["variables"] = list(variables)
    if condition:
        body["condition"] = condition
    headers = {"X-Loki-Probe-Token": os.environ.get("LOKI_PROBE_TOKEN", "")}

    _audit_probe(service, file, line)  # fail closed: no audit row, no probe
    _scope.probes = used + 1
    started = time.monotonic()
    probe_id = None
    try:
        with httpx.Client(base_url=base, headers=headers, timeout=5) as http:
            resp = http.post("/_loki/probes", json=body)
            if resp.status_code != 201:
                raise RuntimeError(f"probe rejected by {service} (HTTP {resp.status_code}): {resp.text[:300]}")
            probe_id = resp.json()["id"]
            log.warning("probe %s placed on %s %s:%s for %ss", probe_id, service, file, line, wait)
            snap = resp.json()
            first_hit = None
            while time.monotonic() - started < wait:
                time.sleep(PROBE_POLL_S)
                snap = http.get(f"/_loki/probes/{probe_id}").json()
                if snap.get("status") != "active":
                    break
                if snap.get("hit_count") and first_hit is None:
                    first_hit = time.monotonic()
                if first_hit is not None and time.monotonic() - first_hit >= PROBE_GRACE_S:
                    break  # got something: a short grace for more hits, then stop
            # Final read happens via DELETE, which also returns the hits.
            final = http.delete(f"/_loki/probes/{probe_id}")
            probe_id = None
            if final.status_code == 200:
                snap = final.json()
    finally:
        _scope.probe_wait_s = waited + (time.monotonic() - started)
        if probe_id is not None:  # error mid-way: still remove the probe
            try:
                httpx.delete(f"{base}/_loki/probes/{probe_id}", headers=headers, timeout=5)
            except Exception:
                log.warning("could not remove probe %s on %s; it expires on its own", probe_id, service)
    hits = snap.get("hits", [])
    out = {
        "service": service, "file": file, "line": line, "waited_seconds": round(time.monotonic() - started, 1),
        "hit_count": len(hits),
        "captures": [{"time": datetime.fromtimestamp(h["ts"], timezone.utc).isoformat(timespec="milliseconds"),
                      "thread": h.get("thread"), "locals": h.get("locals"), "stack": h.get("stack")} for h in hits],
    }
    if not hits:
        out["note"] = (f"the line did not run in {wait:.0f}s" + (" with the condition matching" if condition else "")
                       + "; no traffic reached it, or the line number is wrong")
    if snap.get("errors"):
        out["probe_errors"] = snap["errors"]
    return out


HANDLERS["capture_variables"] = capture_variables
# Live data that changes each call: never served from the cache.
UNCACHED = {"capture_variables"}


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
    if name in UNCACHED:
        return _run_uncached(name, args)
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

