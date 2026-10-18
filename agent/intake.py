"""Alert intake: turn each provider's webhook into one normalized Alert, then run
every source through the same dedup, grouping and investigation flow.

Parsers raise ValueError for a payload they can't read; main.py turns that into a 400.
Fingerprints from other sources are prefixed ("sentry:...") so two providers can
never collide on an id; Alertmanager's stay as they were.
"""
import hashlib
import hmac
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sources import parse_mapping

log = logging.getLogger("agent")

SEVERITIES = {"critical", "warning", "info"}


@dataclass
class Alert:
    source: str
    fingerprint: str
    alert_name: str
    service: str | None
    severity: str | None
    summary: str | None
    status: str  # firing | resolved
    starts_at: str
    ends_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(obj, key: str, where: str):
    if not isinstance(obj, dict) or obj.get(key) in (None, ""):
        raise ValueError(f"{where}: missing '{key}'")
    return obj[key]


def _dict(payload, where: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"{where}: expected a JSON object")
    return payload


# ---------- the shared flow ----------

def ingest(conn, alerts: list[Alert], attach, start) -> list[dict]:
    """Dedup by fingerprint, group into the current storm, resolve. `attach` and
    `start` come from main.py so grouping stays in one place."""
    results = []
    for a in alerts:
        if a.status == "firing":
            row = conn.execute(
                """
                INSERT INTO incidents (fingerprint, alert_name, service, severity, summary, started_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (fingerprint) WHERE status = 'open' DO NOTHING
                RETURNING id
                """,
                (a.fingerprint, a.alert_name, a.service, a.severity, a.summary, a.starts_at),
            ).fetchone()
            if not row:
                log.info("duplicate alert for open incident, fingerprint=%s", a.fingerprint)
                results.append({"fingerprint": a.fingerprint, "result": "duplicate"})
                continue
            inv_id, created = attach(conn, row["id"])
            log.warning(
                "NEW incident #%s: %s on %s -> investigation #%s%s",
                row["id"], a.alert_name, a.service, inv_id, " (new)" if created else " (grouped)",
            )
            if created:
                start(inv_id)
            results.append({"fingerprint": a.fingerprint, "result": "new" if created else "grouped",
                            "incident_id": row["id"], "investigation_id": inv_id})
        else:
            conn.execute(
                "UPDATE incidents SET status='resolved', resolved_at=%s WHERE fingerprint=%s AND status='open'",
                (a.ends_at or _now(), a.fingerprint),
            )
            log.info("resolved incident fingerprint=%s", a.fingerprint)
            results.append({"fingerprint": a.fingerprint, "result": "resolved"})
    return results


# ---------- Prometheus Alertmanager and Grafana ----------

def from_alertmanager(payload, source: str = "alertmanager") -> list[Alert]:
    """Alertmanager's webhook. Grafana unified alerting sends the same shape."""
    payload = _dict(payload, source)
    alerts = payload.get("alerts", [])
    if not isinstance(alerts, list):
        raise ValueError(f"{source}: 'alerts' must be a list")
    out = []
    for i, alert in enumerate(alerts):
        where = f"{source}: alerts[{i}]"
        fp = _require(alert, "fingerprint", where)
        labels = alert.get("labels") or {}
        status = "firing" if alert.get("status") == "firing" else "resolved"
        name = labels.get("alertname")
        if source == "alertmanager":
            # Unchanged behaviour: Alertmanager always sends these fields.
            starts = alert.get("startsAt") if status == "resolved" else _require(alert, "startsAt", where)
            service = labels.get("service")
        else:
            fp = f"{source}:{fp}"
            starts = alert.get("startsAt") or _now()
            service = labels.get("service") or labels.get("service_name") or labels.get("job") or "unknown"
            name = name or labels.get("rulename") or "GrafanaAlert"
        if status == "firing" and not name:
            raise ValueError(f"{where}: missing labels.alertname")
        out.append(Alert(source, fp, name, service, labels.get("severity"),
                         (alert.get("annotations") or {}).get("summary"), status, starts,
                         _real_end(alert.get("endsAt")) if status == "resolved" else None))
    return out


def _real_end(value):
    # Alertmanager and Grafana send endsAt "0001-01-01T00:00:00Z" for "not ended".
    return None if not value or str(value).startswith("0001-") else value


# ---------- Sentry ----------

def verify_sentry(body: bytes, signature: str, secret: str | None = None) -> bool:
    """Sentry-Hook-Signature is hex HMAC-SHA256 of the raw body with the integration's
    client secret. No secret configured means signatures are not checked."""
    secret = secret if secret is not None else os.environ.get("SENTRY_WEBHOOK_SECRET", "")
    if not secret:
        return True
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


def _sentry_service(slug: str | None) -> str:
    projects = parse_mapping(os.environ.get("SENTRY_PROJECTS", ""))
    return projects.get(slug or "", slug or "unknown")


def _sentry_severity(level: str | None) -> str:
    return {"fatal": "critical", "error": "critical", "warning": "warning"}.get(level or "", "info" if level else "warning")


def from_sentry(payload) -> list[Alert]:
    """Issue alerts (data.event) and metric alerts (data.metric_alert)."""
    data = _require(_dict(payload, "sentry"), "data", "sentry")
    if isinstance(data, dict) and isinstance(data.get("metric_alert"), dict):
        return [_sentry_metric(str(payload.get("action") or ""), data)]
    if isinstance(data, dict) and isinstance(data.get("event"), dict):
        return [_sentry_issue(data)]
    raise ValueError("sentry: expected data.event (issue alert) or data.metric_alert (metric alert)")


def _sentry_issue(data: dict) -> Alert:
    event = data["event"]
    issue = event.get("issue_id") or event.get("group_id") or event.get("event_id")
    if not issue:
        raise ValueError("sentry: data.event has no issue_id")
    # The event carries the project only as a numeric id; its API url has the slug.
    m = re.search(r"/projects/[^/]+/([^/]+)/", event.get("url") or "")
    slug = m.group(1) if m else event.get("project_slug") or event.get("project_name")
    ts = event.get("datetime") or event.get("timestamp")
    started = datetime.fromtimestamp(ts, timezone.utc).isoformat() if isinstance(ts, (int, float)) else ts or _now()
    title = event.get("title") or event.get("message") or "Sentry issue"
    return Alert("sentry", f"sentry:issue:{issue}", data.get("triggered_rule") or "SentryIssue",
                 _sentry_service(slug), _sentry_severity(event.get("level")), title, "firing", started)


def _sentry_metric(action: str, data: dict) -> Alert:
    ma = data["metric_alert"]
    rule = ma.get("alert_rule") or {}
    ident = ma.get("id") or ma.get("identifier")
    if not ident:
        raise ValueError("sentry: data.metric_alert has no id")
    projects = rule.get("projects") or []
    resolved = action == "resolved"
    return Alert("sentry", f"sentry:metric:{ident}", rule.get("name") or "SentryMetricAlert",
                 _sentry_service(projects[0] if projects else None),
                 "critical" if action == "critical" else "warning",
                 data.get("description_title") or data.get("description_text"),
                 "resolved" if resolved else "firing", ma.get("date_started") or _now(),
                 (ma.get("date_closed") or _now()) if resolved else None)


# ---------- Datadog ----------

def _datadog_time(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return value or _now()
    return datetime.fromtimestamp(n / 1000 if n > 1e12 else n, timezone.utc).isoformat()


def from_datadog(payload) -> list[Alert]:
    """The payload template documented in the README ($ALERT_ID, $ALERT_TRANSITION, $TAGS...)."""
    p = _dict(payload, "datadog")
    alert_id = _require(p, "alert_id", "datadog")
    tags = p.get("tags") or ""
    tags = tags if isinstance(tags, list) else [t.strip() for t in str(tags).split(",")]
    tag = dict(t.split(":", 1) for t in tags if ":" in t)
    transition = str(p.get("alert_transition") or "Triggered").lower()
    status = "resolved" if transition in ("recovered", "resolved") else "firing"
    severity = "warning" if transition == "warn" else tag.get("severity") or "critical"
    # One monitor alerts once per scope (host, service...), so the scope is part of the id.
    fp = f"datadog:{alert_id}" + (f":{p['alert_scope']}" if p.get("alert_scope") else "")
    when = _datadog_time(p.get("date"))
    return [Alert("datadog", fp, p.get("title") or "DatadogMonitor", tag.get("service") or "unknown",
                  severity, p.get("body"), status, when, when if status == "resolved" else None)]


# ---------- PagerDuty ----------

def verify_pagerduty(body: bytes, header: str, secret: str | None = None) -> bool:
    """X-PagerDuty-Signature is "v1=<hex hmac-sha256>", possibly several, comma-separated."""
    secret = secret if secret is not None else os.environ.get("PAGERDUTY_WEBHOOK_SECRET", "")
    if not secret:
        return True
    expected = "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s.strip()) for s in (header or "").split(","))


def from_pagerduty(payload) -> list[Alert]:
    """V3 webhooks. Only incident.triggered and incident.resolved matter; the rest are acked and ignored."""
    event = _require(_dict(payload, "pagerduty"), "event", "pagerduty")
    kind = _require(event, "event_type", "pagerduty: event")
    if kind not in ("incident.triggered", "incident.resolved"):
        return []
    inc = _require(event, "data", "pagerduty: event")
    inc_id = _require(inc, "id", "pagerduty: event.data")
    priority = (inc.get("priority") or {}).get("summary")
    severity = "critical" if inc.get("urgency") == "high" or priority in ("P1", "P2") else "warning"
    when = event.get("occurred_at") or _now()
    resolved = kind == "incident.resolved"
    return [Alert("pagerduty", f"pagerduty:{inc_id}", inc.get("title") or "PagerDutyIncident",
                  (inc.get("service") or {}).get("summary") or "unknown", severity, inc.get("title"),
                  "resolved" if resolved else "firing", when, when if resolved else None)]


# ---------- generic and manual ----------

def from_generic(payload) -> list[Alert]:
    """{service, alert_name, severity?, summary?, status?, fingerprint?}. The fingerprint
    defaults to service+alert_name, so resending the same alert dedups and a resolved one closes it."""
    p = _dict(payload, "generic")
    name = _require(p, "alert_name", "generic")
    service = p.get("service") or "unknown"
    status = p.get("status") or "firing"
    if status not in ("firing", "resolved"):
        raise ValueError("generic: status must be 'firing' or 'resolved'")
    severity = p.get("severity") or "warning"
    if severity not in SEVERITIES:
        raise ValueError(f"generic: severity must be one of {sorted(SEVERITIES)}")
    fp = "generic:" + str(p.get("fingerprint") or f"{service}:{name}")
    return [Alert("generic", fp, name, service, severity, p.get("summary"), status,
                  p.get("starts_at") or _now(), (p.get("ends_at") or _now()) if status == "resolved" else None)]


def manual(service: str, description: str, severity: str | None = None) -> Alert:
    """A human saying "something's wrong, look". Always a new incident, never a duplicate."""
    severity = severity or "warning"
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(SEVERITIES)}")
    return Alert("manual", f"manual:{uuid.uuid4().hex}", "ManualInvestigation", service or "unknown",
                 severity, description, "firing", _now())
