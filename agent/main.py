"""Loki's intake: Alertmanager calls POST /alerts, Loki groups alerts into one
investigation per alert storm and runs the investigator in the background.
"""
import json
import logging
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

import auth
import backtest
import db
import intake
import investigator
import slack
import tools
from redact import redact

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s agent %(message)s")
log = logging.getLogger("agent")
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every telemetry request

# Alerts from one failure arrive over several seconds (payments first, then the
# services calling it). Wait this long before investigating so they all land in
# the same investigation instead of starting three.
GROUP_WINDOW_S = 25
# A new alert joins an investigation created within this many seconds.
JOIN_WINDOW_S = 300

# Secure by default: this dependency runs for every route, including ones added
# later. Public paths are an explicit allowlist in auth.py.
app = FastAPI(dependencies=[Depends(auth.require_auth)])
auth.install_log_filter()
_group_lock = threading.Lock()


@app.on_event("startup")
def startup():
    auth.check_startup()
    db.init()
    # Investigations run in background threads; a restart kills them. Don't leave them hanging.
    with db.connect() as conn:
        orphaned = conn.execute(
            "UPDATE investigations SET status = 'failed', finished_at = now(), error = 'agent restarted mid-investigation' "
            "WHERE status IN ('queued', 'running') RETURNING id"
        ).fetchall()
    if orphaned:
        log.warning("marked %d orphaned investigation(s) as failed: %s", len(orphaned), [r["id"] for r in orphaned])


def _attach_to_investigation(conn, incident_id: int) -> tuple[int, bool]:
    """Attach an incident to the current alert storm's investigation, or open a new one."""
    with _group_lock:
        row = conn.execute(
            # Never let a live alert join a backtest that happens to be running.
            "SELECT id FROM investigations WHERE status IN ('queued', 'running') AND mode = 'live' "
            "AND created_at > now() - make_interval(secs => %s) ORDER BY id DESC LIMIT 1",
            (JOIN_WINDOW_S,),
        ).fetchone()
        created = row is None
        inv_id = row["id"] if row else conn.execute("INSERT INTO investigations DEFAULT VALUES RETURNING id").fetchone()["id"]
        conn.execute("UPDATE incidents SET investigation_id = %s WHERE id = %s", (inv_id, incident_id))
        return inv_id, created


def _run_after_grouping(inv_id: int):
    time.sleep(GROUP_WINDOW_S)
    try:
        investigator.investigate(inv_id)
    except Exception as e:
        log.exception("investigation #%s failed", inv_id)
        with db.connect() as conn:
            conn.execute(
                "UPDATE investigations SET status = 'failed', finished_at = now(), error = %s WHERE id = %s",
                (f"{type(e).__name__}: {e}", inv_id),
            )


def _start_investigation(inv_id: int):
    threading.Thread(target=_run_after_grouping, args=(inv_id,), daemon=True).start()


def _ingest(parse, payload) -> dict:
    """Parse a provider payload into normalized alerts and run them through dedup and grouping."""
    try:
        alerts = parse(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except (TypeError, AttributeError, KeyError) as e:  # a payload of the wrong shape
        raise HTTPException(400, f"malformed payload: {type(e).__name__}: {e}")
    with db.connect() as conn:
        results = intake.ingest(conn, alerts, _attach_to_investigation, _start_investigation)
    return {"ok": True, "alerts": results}


async def _json(request: Request, body: bytes | None = None):
    try:
        return json.loads(body if body is not None else await request.body())
    except ValueError:
        raise HTTPException(400, "body is not valid JSON")


@app.post("/alerts")
async def receive_alerts(request: Request):
    """Prometheus Alertmanager."""
    return _ingest(intake.from_alertmanager, await _json(request))


@app.post("/alerts/grafana")
async def receive_grafana(request: Request):
    return _ingest(lambda p: intake.from_alertmanager(p, "grafana"), await _json(request))


@app.post("/alerts/sentry")
async def receive_sentry(request: Request):
    body = await request.body()
    if not intake.verify_sentry(body, request.headers.get("Sentry-Hook-Signature", "")):
        raise HTTPException(401, "bad sentry signature")
    if request.headers.get("Sentry-Hook-Resource") == "installation":
        return {"ok": True, "alerts": []}  # the integration being installed, not an alert
    return _ingest(intake.from_sentry, await _json(request, body))


@app.post("/alerts/datadog")
async def receive_datadog(request: Request):
    return _ingest(intake.from_datadog, await _json(request))


@app.post("/alerts/pagerduty")
async def receive_pagerduty(request: Request):
    body = await request.body()
    if not intake.verify_pagerduty(body, request.headers.get("X-PagerDuty-Signature", "")):
        raise HTTPException(401, "bad pagerduty signature")
    return _ingest(intake.from_pagerduty, await _json(request, body))


@app.post("/alerts/generic")
async def receive_generic(request: Request):
    return _ingest(intake.from_generic, await _json(request))


class ManualRequest(BaseModel):
    service: str
    description: str
    severity: str | None = None


@app.post("/investigations/manual")
def manual_investigation(req: ManualRequest, request: Request):
    """"Something's wrong, Loki, look": opens an incident now and investigates it
    (or adds it to the investigation already running for this storm). Not under
    /alerts, so the webhook token doesn't open it: it needs a key with write scope."""
    if not req.description.strip():
        raise HTTPException(400, "description must not be empty")
    result = _ingest(lambda _: [intake.manual(req.service, req.description, req.severity)], None)["alerts"][0]
    with db.connect() as conn:
        _audit(conn, request.state.actor, "manual", result["investigation_id"])
    return result


@app.get("/health")
def health():
    """Liveness probe. Public, so it returns no data."""
    return {"ok": True}


@app.get("/incidents")
def list_incidents():
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, alert_name, service, severity, summary, status, started_at, resolved_at, investigation_id, mode "
            "FROM incidents ORDER BY id DESC LIMIT 50"
        ).fetchall()


@app.get("/investigations")
def list_investigations():
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, mode, replay_at, status, created_at, finished_at, steps, cost_usd, "
            "report->>'summary' AS summary, error "
            "FROM investigations ORDER BY id DESC LIMIT 20"
        ).fetchall()


def _get_investigation(conn, inv_id: int | None):
    if inv_id is None:  # "latest" means the latest real incident, not a replay
        row = conn.execute("SELECT * FROM investigations WHERE mode = 'live' ORDER BY id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT * FROM investigations WHERE id = %s", (inv_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such investigation")
    return row


def _render_report(inv_id: int | None) -> str:
    with db.connect() as conn:
        inv = _get_investigation(conn, inv_id)
    if inv["status"] != "done":
        return f"Investigation #{inv['id']} is {inv['status']}." + (f"\n{inv['error']}" if inv["error"] else "")
    return investigator.render(inv["id"], inv["report"], inv["steps"], float(inv["cost_usd"]))


@app.get("/investigations/{inv_id}")
def get_investigation(inv_id: int):
    """The full investigation, including the structured report (used by the evals)."""
    with db.connect() as conn:
        return _get_investigation(conn, inv_id)


@app.get("/investigations/latest/report", response_class=PlainTextResponse)
def latest_report():
    return _render_report(None)


@app.get("/investigations/{inv_id}/report", response_class=PlainTextResponse)
def report(inv_id: int):
    return _render_report(inv_id)


@app.get("/investigations/{inv_id}/trace")
def trace(inv_id: int):
    with db.connect() as conn:
        _get_investigation(conn, inv_id)
        return conn.execute(
            "SELECT step, tool, input, left(output, 1500) AS output, created_at FROM trace_steps "
            "WHERE investigation_id = %s ORDER BY id",
            (inv_id,),
        ).fetchall()


def _redact_values(obj):
    """Redact each string on its own: redacting the JSON text could swallow a closing quote."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_values(v) for v in obj]
    return obj


@app.get("/deploys/{deploy_id:path}/diff")
def deploy_diff(deploy_id: str):
    """A deploy's diff, read-only, for the MCP server. `:path` because GitHub ids
    look like owner/repo:pr:123."""
    try:
        diff = tools.git_diff(deploy_id)
    except ValueError as e:  # the deploy sources raise ValueError for an unknown id
        raise HTTPException(404, str(e))
    except Exception as e:  # e.g. GitHub unreachable: say so instead of a bare 500
        raise HTTPException(502, f"could not fetch the diff: {type(e).__name__}: {e}")
    return _redact_values(diff)


def _record_feedback(conn, inv_id: int, verdict: str, who: str | None, user_id: str | None, reason: str | None = None):
    conn.execute(
        "INSERT INTO feedback (investigation_id, verdict, user_id, user_name, reason) VALUES (%s, %s, %s, %s, %s)",
        (inv_id, verdict, user_id, who, reason),
    )


def _audit(conn, actor: str, action: str, inv_id: int | None):
    """Who did a write action. actor is an API key name, "slack:<user>" or "anonymous" (dev mode)."""
    conn.execute(
        "INSERT INTO audit_log (actor, action, investigation_id) VALUES (%s, %s, %s)", (actor, action, inv_id)
    )


def _confirm(inv_id: int, who: str | None = None, user_id: str | None = None, actor: str | None = None) -> dict:
    with db.connect() as conn:
        inv = _get_investigation(conn, inv_id)
        if inv["mode"] != "live":
            raise HTTPException(409, "backtests can't be confirmed: they never feed the answer cache")
        if inv["status"] != "done" or not inv["cache_key"] or (inv["cache_hit"] or "").startswith("exact:"):
            raise HTTPException(409, "only a finished, freshly investigated report can be confirmed")
        conn.execute(
            "INSERT INTO report_cache (exact_key, fingerprint, investigation_id, report, confirmed, expires_at) "
            "VALUES (%s, %s, %s, %s, true, now() + make_interval(days => %s)) "
            "ON CONFLICT (exact_key) DO UPDATE SET investigation_id = EXCLUDED.investigation_id, "
            "report = EXCLUDED.report, confirmed = true, created_at = now(), expires_at = EXCLUDED.expires_at",
            (inv["cache_key"], inv["fingerprint"], inv_id, json.dumps(inv["report"]), investigator.CACHE_TTL_DAYS),
        )
        _record_feedback(conn, inv_id, "confirmed", who, user_id)
        _audit(conn, actor or f"slack:{who or user_id}", "confirm", inv_id)
    return {"confirmed": inv_id, "cache_key": inv["cache_key"]}


def _reject(inv_id: int, who: str | None = None, user_id: str | None = None, reason: str | None = None,
            actor: str | None = None) -> dict:
    with db.connect() as conn:
        inv = _get_investigation(conn, inv_id)
        if inv["status"] != "done":
            raise HTTPException(409, "only a finished report can be rejected")
        # A wrong answer must never be reused for free, even if someone confirmed it
        # earlier. For a cache hit this un-confirms the original answer it came from.
        # A backtest never came from the cache, so rejecting one leaves live answers alone.
        if inv["cache_key"] and inv["mode"] == "live":
            conn.execute("UPDATE report_cache SET confirmed = false WHERE exact_key = %s", (inv["cache_key"],))
        _record_feedback(conn, inv_id, "rejected", who, user_id, reason)
        _audit(conn, actor or f"slack:{who or user_id}", "reject", inv_id)
    return {"rejected": inv_id}


@app.post("/investigations/{inv_id}/confirm")
def confirm(inv_id: int, request: Request):
    """A human confirms the root cause was right (the fix worked). Only confirmed
    answers are reused for free when the exact same incident happens again."""
    return _confirm(inv_id, actor=request.state.actor)


@app.post("/investigations/{inv_id}/reject")
def reject(inv_id: int, request: Request, reason: str | None = Body(None, embed=True)):
    """A human says the root cause was wrong. Optional JSON body: {"reason": "..."}."""
    return _reject(inv_id, reason=reason, actor=request.state.actor)


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background: BackgroundTasks):
    """Button clicks from Slack. Ack at once; the DB work and message update run after."""
    body = await request.body()
    if not slack.verify_signature(body, request.headers.get("X-Slack-Request-Timestamp", ""),
                                  request.headers.get("X-Slack-Signature", "")):
        raise HTTPException(401, "bad slack signature")
    payload = json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
    background.add_task(slack.handle_interaction, payload, _confirm, _reject)
    return Response(status_code=200)


@app.get("/stats")
def stats():
    """Where the money goes: spend, cache savings and tool-cache hit rate."""
    with db.connect() as conn:
        totals = conn.execute(
            "SELECT count(*) AS investigations, coalesce(sum(cost_usd), 0) AS total_usd, "
            "coalesce(avg(cost_usd) FILTER (WHERE cache_hit IS NULL OR cache_hit LIKE 'hint:%%'), 0) AS avg_usd_per_ai_run, "
            "count(*) FILTER (WHERE cache_hit LIKE 'exact:%%') AS answered_from_cache, "
            "count(*) FILTER (WHERE cache_hit LIKE 'hint:%%') AS used_cache_hint, "
            "coalesce(sum(cost_usd) FILTER (WHERE created_at >= date_trunc('day', now())), 0) AS spent_today_usd "
            "FROM investigations WHERE status = 'done' AND mode = 'live'"
        ).fetchone()
        per_model = conn.execute(
            "SELECT t.model, count(*) AS calls, sum(t.input_tokens) AS input_tokens, sum(t.output_tokens) AS output_tokens, "
            "sum(t.cache_read_tokens) AS cache_read_tokens, sum(t.cost_usd) AS cost_usd "
            "FROM trace_steps t JOIN investigations i ON i.id = t.investigation_id "
            "WHERE t.tool = 'llm' AND i.mode = 'live' GROUP BY t.model"
        ).fetchall()
        # Backtests are reported apart. Their spend still counts toward the daily budget.
        backtests = conn.execute(
            "SELECT count(*) AS runs, coalesce(sum(cost_usd), 0) AS cost_usd FROM investigations WHERE mode = 'backtest'"
        ).fetchone()
    return {
        **totals,
        "daily_budget_usd": investigator.DAILY_BUDGET_USD,
        "per_investigation_cap_usd": investigator.MAX_COST_USD,
        "model": investigator.MODEL,
        "escalation_model": investigator.ESCALATE_MODEL or None,
        "per_model": per_model,
        "tool_cache": tools.tool_cache_stats,
        "backtests": backtests,
    }


class BacktestRequest(BaseModel):
    at: datetime  # when the (first) alert fired, ISO 8601 with a timezone
    alerts: list[str] | str | None = None  # "HighErrorRate:payments-svc"; omit to replay the alerts recorded then
    model: str | None = None  # e.g. "fake" for a free run; default LOKI_MODEL
    delay_s: int = backtest.DEFAULT_DELAY_S


@app.post("/backtest")
def run_backtest(req: BacktestRequest, request: Request):
    """Replay a past incident synchronously: Loki investigates it with the clock set
    back, so nothing after that moment is visible. Returns the full investigation."""
    if req.model and req.model not in backtest.ALLOWED_MODELS:
        raise HTTPException(400, f"model must be one of {sorted(backtest.ALLOWED_MODELS)}")
    try:
        alerts = backtest.parse_alerts(req.alerts)
        with db.connect() as conn:
            inv_id = backtest.create(conn, req.at, alerts, req.delay_s)
            _audit(conn, request.state.actor, "backtest", inv_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.warning("backtest #%s: replaying %s", inv_id, req.at.isoformat())
    try:
        investigator.investigate(inv_id, model=req.model)
    except Exception as e:
        log.exception("backtest #%s failed", inv_id)
        with db.connect() as conn:
            conn.execute(
                "UPDATE investigations SET status = 'failed', finished_at = now(), error = %s WHERE id = %s",
                (f"{type(e).__name__}: {e}", inv_id),
            )
    with db.connect() as conn:
        inv = _get_investigation(conn, inv_id)
        inv["incidents"] = conn.execute(
            "SELECT alert_name, service, severity, summary, started_at FROM incidents WHERE investigation_id = %s",
            (inv_id,),
        ).fetchall()
    if inv["status"] == "done":
        inv["report_text"] = investigator.render(inv_id, inv["report"], inv["steps"], float(inv["cost_usd"]))
    return inv
