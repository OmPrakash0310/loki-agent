"""Loki's intake: Alertmanager calls POST /alerts, Loki groups alerts into one
investigation per alert storm and runs the investigator in the background.
"""
import json
import logging
import threading
import time
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

import db
import investigator
import slack
import tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s agent %(message)s")
log = logging.getLogger("agent")
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every telemetry request

# Alerts from one failure arrive over several seconds (payments first, then the
# services calling it). Wait this long before investigating so they all land in
# the same investigation instead of starting three.
GROUP_WINDOW_S = 25
# A new alert joins an investigation created within this many seconds.
JOIN_WINDOW_S = 300

app = FastAPI()
_group_lock = threading.Lock()


@app.on_event("startup")
def startup():
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
            "SELECT id FROM investigations WHERE status IN ('queued', 'running') "
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


@app.post("/alerts")
async def receive_alerts(request: Request):
    payload = await request.json()
    with db.connect() as conn:
        for alert in payload.get("alerts", []):
            labels = alert["labels"]
            fp = alert["fingerprint"]
            if alert["status"] == "firing":
                row = conn.execute(
                    """
                    INSERT INTO incidents (fingerprint, alert_name, service, severity, summary, started_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (fingerprint) WHERE status = 'open' DO NOTHING
                    RETURNING id
                    """,
                    (fp, labels.get("alertname"), labels.get("service"), labels.get("severity"),
                     alert.get("annotations", {}).get("summary"), alert["startsAt"]),
                ).fetchone()
                if not row:
                    log.info("duplicate alert for open incident, fingerprint=%s", fp)
                    continue
                inv_id, created = _attach_to_investigation(conn, row["id"])
                log.warning(
                    "NEW incident #%s: %s on %s -> investigation #%s%s",
                    row["id"], labels.get("alertname"), labels.get("service"), inv_id, " (new)" if created else " (grouped)",
                )
                if created:
                    threading.Thread(target=_run_after_grouping, args=(inv_id,), daemon=True).start()
            else:
                conn.execute(
                    "UPDATE incidents SET status='resolved', resolved_at=%s WHERE fingerprint=%s AND status='open'",
                    (alert["endsAt"], fp),
                )
                log.info("resolved incident fingerprint=%s", fp)
    return {"ok": True}


@app.get("/incidents")
def list_incidents():
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, alert_name, service, severity, summary, status, started_at, resolved_at, investigation_id "
            "FROM incidents ORDER BY id DESC LIMIT 50"
        ).fetchall()


@app.get("/investigations")
def list_investigations():
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, status, created_at, finished_at, steps, cost_usd, report->>'summary' AS summary, error "
            "FROM investigations ORDER BY id DESC LIMIT 20"
        ).fetchall()


def _get_investigation(conn, inv_id: int | None):
    if inv_id is None:
        row = conn.execute("SELECT * FROM investigations ORDER BY id DESC LIMIT 1").fetchone()
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


def _record_feedback(conn, inv_id: int, verdict: str, who: str | None, user_id: str | None, reason: str | None = None):
    conn.execute(
        "INSERT INTO feedback (investigation_id, verdict, user_id, user_name, reason) VALUES (%s, %s, %s, %s, %s)",
        (inv_id, verdict, user_id, who, reason),
    )


def _confirm(inv_id: int, who: str | None = None, user_id: str | None = None) -> dict:
    with db.connect() as conn:
        inv = _get_investigation(conn, inv_id)
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
    return {"confirmed": inv_id, "cache_key": inv["cache_key"]}


def _reject(inv_id: int, who: str | None = None, user_id: str | None = None, reason: str | None = None) -> dict:
    with db.connect() as conn:
        inv = _get_investigation(conn, inv_id)
        if inv["status"] != "done":
            raise HTTPException(409, "only a finished report can be rejected")
        # A wrong answer must never be reused for free, even if someone confirmed it
        # earlier. For a cache hit this un-confirms the original answer it came from.
        if inv["cache_key"]:
            conn.execute("UPDATE report_cache SET confirmed = false WHERE exact_key = %s", (inv["cache_key"],))
        _record_feedback(conn, inv_id, "rejected", who, user_id, reason)
    return {"rejected": inv_id}


@app.post("/investigations/{inv_id}/confirm")
def confirm(inv_id: int):
    """A human confirms the root cause was right (the fix worked). Only confirmed
    answers are reused for free when the exact same incident happens again."""
    return _confirm(inv_id)


@app.post("/investigations/{inv_id}/reject")
def reject(inv_id: int, reason: str | None = Body(None, embed=True)):
    """A human says the root cause was wrong. Optional JSON body: {"reason": "..."}."""
    return _reject(inv_id, reason=reason)


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
            "FROM investigations WHERE status = 'done'"
        ).fetchone()
        per_model = conn.execute(
            "SELECT model, count(*) AS calls, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, "
            "sum(cache_read_tokens) AS cache_read_tokens, sum(cost_usd) AS cost_usd "
            "FROM trace_steps WHERE tool = 'llm' GROUP BY model"
        ).fetchall()
    return {
        **totals,
        "daily_budget_usd": investigator.DAILY_BUDGET_USD,
        "per_investigation_cap_usd": investigator.MAX_COST_USD,
        "model": investigator.MODEL,
        "escalation_model": investigator.ESCALATE_MODEL or None,
        "per_model": per_model,
        "tool_cache": tools.tool_cache_stats,
    }
