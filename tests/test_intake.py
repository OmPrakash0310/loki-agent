"""Alert intake: each provider's real webhook shape maps to one normalized alert, and
all sources share dedup, storm grouping and resolving. Postgres is faked with just
enough state (open fingerprints, the running investigation) to show grouping."""
import hashlib
import hmac
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import intake
import main

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import investigate as investigate_cli  # noqa: E402

# ---------- sample payloads, copied from each provider's docs ----------
# Where a doc only gives a schema, or one example for one state, the values were
# filled in / the state changed as noted. Long fields irrelevant to intake were trimmed.

# https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
# (the doc gives the shape with placeholders; values filled in)
ALERTMANAGER = {
    "version": "4", "groupKey": "{}:{alertname=\"HighErrorRate\"}", "truncatedAlerts": 0,
    "status": "firing", "receiver": "loki", "groupLabels": {"alertname": "HighErrorRate"},
    "commonLabels": {"alertname": "HighErrorRate"}, "commonAnnotations": {},
    "externalURL": "http://alertmanager:9093",
    "alerts": [{"status": "firing", "fingerprint": "a1b2c3",
                "labels": {"alertname": "HighErrorRate", "service": "payments-svc", "severity": "critical"},
                "annotations": {"summary": "payments-svc error rate is 40%"},
                "startsAt": "2026-10-16T21:03:00Z", "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus:9090/graph?g0.expr=x"}],
}

# https://docs.sentry.io/organization/integrations/integration-platform/webhooks/issue-alerts/
# (verbatim apart from trimming exception/request/sdk/contexts/tags)
SENTRY_ISSUE = {
    "action": "triggered",
    "actor": {"id": "sentry", "name": "Sentry", "type": "application"},
    "data": {
        "event": {
            "_ref": 1, "_ref_version": 2, "culprit": "?(<anonymous>)",
            "datetime": "2019-08-19T21:06:17.677000Z",
            "event_id": "e4874d664c3540c1a32eab185f12c5ab",
            "fingerprint": ["{{ default }}"], "hashes": ["29f7ffc4903a8a990408b80a3b4c95a2"],
            "issue_url": "https://sentry.io/api/0/issues/1117540176/",
            "issue_id": "1117540176", "key_id": "667532", "level": "error", "location": "<anonymous>",
            "logger": "", "message": "",
            "metadata": {"filename": "<anonymous>", "type": "ReferenceError", "value": "heck is not defined"},
            "platform": "javascript", "project": 1, "received": 1566248777.677, "release": None,
            "timestamp": 1566248777.677, "title": "ReferenceError: heck is not defined", "type": "error",
            "url": "https://sentry.io/api/0/projects/test-org/front-end/events/e4874d664c3540c1a32eab185f12c5ab/",
            "user": {"ip_address": "162.218.85.90"}, "version": "7",
            "web_url": "https://sentry.io/organizations/test-org/issues/1117540176/events/e4874d664c3540c1a32eab185f12c5ab/",
        },
        "triggered_rule": "Very Important Alert!",
        "issue_alert": {"title": "Very Important Alert!", "settings": [{"name": "channel", "value": "#general"}]},
    },
    "installation": {"uuid": "a8e5d37a-696c-4c54-adb5-b3f28d64c7de"},
}

# https://docs.sentry.io/organization/integrations/integration-platform/webhooks/metric-alerts/
# The doc's example is the "resolved" action; "action" is one of critical, warning,
# resolved, so this firing copy changes action and description_title only.
SENTRY_METRIC_RESOLVED = {
    "action": "resolved",
    "actor": {"id": "sentry", "name": "Sentry", "type": "application"},
    "data": {
        "description_text": "1000 events in the last 10 minutes\nFilter: level:error",
        "description_title": "Resolved: Too many errors",
        "metric_alert": {
            "alert_rule": {
                "aggregate": "count()", "created_by": None, "dataset": "events",
                "date_created": "2020-09-13T12:26:40.000000Z", "date_modified": "2020-09-13T12:26:40.000000Z",
                "environment": None, "id": "7", "include_all_projects": False, "name": "Too many errors",
                "organization_id": "5", "projects": ["bar"], "query": "level:error", "resolution": 1,
                "resolve_threshold": None, "status": 0, "threshold_period": 1, "threshold_type": 0,
                "time_window": 10, "triggers": [],
            },
            "date_closed": None, "date_created": "2020-09-13T12:26:40.000000Z",
            "date_detected": "2020-09-13T12:26:40.000000Z", "date_started": "2020-09-13T12:26:40.000000Z",
            "id": "4", "identifier": "1", "organization_id": "5", "projects": ["bar"], "status": 2,
            "status_method": 3, "title": "Sacred Marmot", "type": 2,
        },
        "web_url": "https://sentry.io/organizations/baz/alerts/1/",
    },
    "installation": {"uuid": "a8e5d37a-696c-4c54-adb5-b3f28d64c7de"},
}
SENTRY_METRIC = json.loads(json.dumps(SENTRY_METRIC_RESOLVED))
SENTRY_METRIC["action"] = "critical"
SENTRY_METRIC["data"]["description_title"] = "Critical: Too many errors"

# https://docs.datadoghq.com/integrations/webhooks/ has no full payload example, only
# the variable table. This is the README's template rendered with that table's example
# values ($ALERT_ID 1234, $DATE 1406662672000, $ALERT_SCOPE/$TAGS examples...), plus a
# service: tag so the service mapping is exercised.
DATADOG = {
    "id": "1234567", "alert_id": "1234", "title": "[Triggered] [Memory Alert]",
    "alert_transition": "Triggered", "alert_type": "error",
    "alert_scope": "availability-zone:us-east-1a, role:computing-node", "priority": "P1",
    "tags": "monitor, name:myService, role:computing-node, service:payments-svc", "date": "1406662672000",
    "body": "@webhook-url Sending to the webhook",
}

# https://docs.pagerduty.com/developer/webhooks-overview (the incident.priority_updated
# example, with event_type changed to incident.triggered; conference_bridge trimmed)
PAGERDUTY = {
    "event": {
        "id": "5ac64822-4adc-4fda-ade0-410becf0de4f",
        "event_type": "incident.triggered",
        "resource_type": "incident",
        "occurred_at": "2020-10-02T18:45:22.169Z",
        "agent": {"html_url": "https://acme.pagerduty.com/users/PLH1HKV", "id": "PLH1HKV",
                  "self": "https://api.pagerduty.com/users/PLH1HKV", "summary": "Tenex Engineer",
                  "type": "user_reference"},
        "client": {"name": "PagerDuty"},
        "data": {
            "id": "PGR0VU2", "type": "incident", "self": "https://api.pagerduty.com/incidents/PGR0VU2",
            "html_url": "https://acme.pagerduty.com/incidents/PGR0VU2", "number": 2, "status": "triggered",
            "incident_key": "d3640fbd41094207a1c11e58e46b1662", "created_at": "2020-04-09T15:16:27Z",
            "title": "A little bump in the road",
            "service": {"html_url": "https://acme.pagerduty.com/services/PF9KMXH", "id": "PF9KMXH",
                        "self": "https://api.pagerduty.com/services/PF9KMXH", "summary": "API Service",
                        "type": "service_reference"},
            "priority": {"html_url": "https://acme.pagerduty.com/account/incident_priorities", "id": "PSO75BM",
                         "self": "https://api.pagerduty.com/priorities/PSO75BM", "summary": "P1",
                         "type": "priority_reference"},
            "urgency": "high", "resolve_reason": None,
        },
    }
}

# https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/
# (verbatim; "message" shortened)
GRAFANA = {
    "receiver": "My Super Webhook", "status": "firing", "orgId": 1,
    "alerts": [
        {"status": "firing",
         "labels": {"alertname": "High memory usage", "team": "blue", "zone": "us-1"},
         "annotations": {"description": "The system has high memory usage",
                         "runbook_url": "https://myrunbook.com/runbook/1234",
                         "summary": "This alert was triggered for zone us-1"},
         "startsAt": "2021-10-12T09:51:03.157076+02:00", "endsAt": "0001-01-01T00:00:00Z",
         "generatorURL": "https://play.grafana.org/alerting/1afz29v7z/edit", "fingerprint": "c6eadffa33fcdf37",
         "silenceURL": "https://play.grafana.org/alerting/silence/new?alertmanager=grafana&matchers=alertname%3DT2%2Cteam%3Dblue%2Czone%3Dus-1",
         "dashboardURL": "", "panelURL": "", "values": {"B": 44.23943737541908, "C": 1}},
        {"status": "firing",
         "labels": {"alertname": "High CPU usage", "team": "blue", "zone": "eu-1"},
         "annotations": {"description": "The system has high CPU usage",
                         "runbook_url": "https://myrunbook.com/runbook/1234",
                         "summary": "This alert was triggered for zone eu-1"},
         "startsAt": "2021-10-12T09:56:03.157076+02:00", "endsAt": "0001-01-01T00:00:00Z",
         "generatorURL": "https://play.grafana.org/alerting/d1rdpdv7k/edit", "fingerprint": "bc97ff14869b13e3",
         "silenceURL": "https://play.grafana.org/alerting/silence/new?alertmanager=grafana&matchers=alertname%3DT1%2Cteam%3Dblue%2Czone%3Deu-1",
         "dashboardURL": "", "panelURL": "", "values": {"B": 44.23943737541908, "C": 1}},
    ],
    "groupLabels": {}, "commonLabels": {"team": "blue"}, "commonAnnotations": {},
    "externalURL": "https://play.grafana.org/", "version": "1", "groupKey": "{}:{}", "truncatedAlerts": 0,
    "title": "[FIRING:2]  (blue)", "state": "alerting", "message": "**Firing**\n\nLabels:\n - alertname = T2\n",
}

GENERIC = {"service": "payments-svc", "alert_name": "CheckoutErrors", "severity": "critical",
           "summary": "checkout returns 500", "status": "firing"}


# ---------- fake Postgres ----------

class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class StatefulDB:
    """Keeps open fingerprints and investigations so dedup and grouping really happen."""

    def __init__(self):
        self.incidents, self.investigations, self.log = {}, [], []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        if sql.strip().startswith("INSERT INTO incidents"):
            fp = params[0]
            if any(i["fp"] == fp and i["status"] == "open" for i in self.incidents.values()):
                return Rows([])
            iid = len(self.incidents) + 1
            self.incidents[iid] = {"fp": fp, "service": params[2], "severity": params[3], "status": "open", "inv": None}
            return Rows([{"id": iid}])
        if "FROM investigations WHERE status IN" in sql:
            return Rows([{"id": self.investigations[-1]}] if self.investigations else [])
        if "INSERT INTO audit_log" in sql:
            self.audit = getattr(self, "audit", []) + [params]
        if "INSERT INTO investigations" in sql:
            self.investigations.append(len(self.investigations) + 1)
            return Rows([{"id": self.investigations[-1]}])
        if sql.startswith("UPDATE incidents SET investigation_id"):
            self.incidents[params[1]]["inv"] = params[0]
        elif "status='resolved'" in sql:
            for i in self.incidents.values():
                if i["fp"] == params[1]:
                    i["status"] = "resolved"
        return Rows([])


@pytest.fixture
def api(monkeypatch):
    fake = StatefulDB()
    started = []
    monkeypatch.setattr(main.db, "connect", fake.connect)
    monkeypatch.setattr(main, "_run_after_grouping", started.append)
    monkeypatch.setenv("SENTRY_PROJECTS", "payments=payments-svc")
    monkeypatch.delenv("SENTRY_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PAGERDUTY_WEBHOOK_SECRET", raising=False)
    for var in ("LOKI_API_KEYS", "LOKI_WEBHOOK_TOKEN", "LOKI_REQUIRE_AUTH"):  # dev mode unless a test sets keys
        monkeypatch.delenv(var, raising=False)
    fake.started = started
    return TestClient(main.app), fake  # not entered: no startup hook, no real database


def _wait_started(fake, n):
    import time
    for _ in range(100):  # the investigation starts in a thread
        if len(fake.started) >= n:
            return
        time.sleep(0.01)


# ---------- mapping per provider ----------

def test_alertmanager_maps_like_before():
    [a] = intake.from_alertmanager(ALERTMANAGER)
    assert (a.source, a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("alertmanager", "a1b2c3", "HighErrorRate", "payments-svc", "critical", "firing")
    assert a.summary == "payments-svc error rate is 40%" and a.starts_at == "2026-10-16T21:03:00Z"


def test_sentry_issue_alert(monkeypatch):
    monkeypatch.setenv("SENTRY_PROJECTS", "front-end=shop-web")
    [a] = intake.from_sentry(SENTRY_ISSUE)
    assert (a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("sentry:issue:1117540176", "Very Important Alert!", "shop-web", "critical", "firing")
    assert a.summary == "ReferenceError: heck is not defined"
    assert a.starts_at == "2019-08-19T21:06:17.677000Z"


def test_sentry_metric_alert_and_resolve(monkeypatch):
    monkeypatch.setenv("SENTRY_PROJECTS", "bar=payments-svc")
    [a] = intake.from_sentry(SENTRY_METRIC)
    assert (a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("sentry:metric:4", "Too many errors", "payments-svc", "critical", "firing")
    assert a.starts_at == "2020-09-13T12:26:40.000000Z"
    warn = json.loads(json.dumps(SENTRY_METRIC))
    warn["action"] = "warning"
    assert intake.from_sentry(warn)[0].severity == "warning"
    # The documented resolved example has date_closed null: fall back to now.
    [r] = intake.from_sentry(SENTRY_METRIC_RESOLVED)
    assert r.status == "resolved" and r.fingerprint == a.fingerprint and r.ends_at
    closed = json.loads(json.dumps(SENTRY_METRIC_RESOLVED))
    closed["data"]["metric_alert"]["date_closed"] = "2020-09-13T12:40:00.000000Z"
    assert intake.from_sentry(closed)[0].ends_at == "2020-09-13T12:40:00.000000Z"


def test_sentry_unmapped_project_keeps_its_slug(monkeypatch):
    monkeypatch.delenv("SENTRY_PROJECTS", raising=False)
    assert intake.from_sentry(SENTRY_ISSUE)[0].service == "front-end"


def test_datadog():
    [a] = intake.from_datadog(DATADOG)
    assert (a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("datadog:1234:availability-zone:us-east-1a,role:computing-node", "[Triggered] [Memory Alert]",
         "payments-svc", "critical", "firing")
    assert a.starts_at.startswith("2014-07-29T19:37:52")  # $DATE is epoch milliseconds
    [r] = intake.from_datadog({**DATADOG, "alert_transition": "Recovered"})
    assert r.status == "resolved" and r.fingerprint == a.fingerprint


@pytest.mark.parametrize("transition,status,severity", [
    ("Triggered", "firing", "critical"), ("Re-Triggered", "firing", "critical"), ("Renotify", "firing", "critical"),
    ("Warn", "firing", "warning"), ("Re-Warn", "firing", "warning"),
    ("No Data", "firing", "warning"), ("Re-No Data", "firing", "warning"), ("Recovered", "resolved", "critical"),
])
def test_datadog_transitions(transition, status, severity):
    [a] = intake.from_datadog({**DATADOG, "alert_transition": transition})
    assert (a.status, a.severity) == (status, severity)


def test_datadog_tags_and_type():
    assert intake.from_datadog({**DATADOG, "tags": "env:prod"})[0].service == "unknown"
    assert intake.from_datadog({**DATADOG, "alert_type": "warning"})[0].severity == "warning"
    assert intake.from_datadog({**DATADOG, "tags": "service:x, severity:info"})[0].severity == "info"


def test_pagerduty():
    [a] = intake.from_pagerduty(PAGERDUTY)
    assert (a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("pagerduty:PGR0VU2", "A little bump in the road", "API Service", "critical", "firing")
    assert a.starts_at == "2020-10-02T18:45:22.169Z"
    reopened = json.loads(json.dumps(PAGERDUTY))
    reopened["event"]["event_type"] = "incident.reopened"
    assert intake.from_pagerduty(reopened)[0].status == "firing"
    resolved = json.loads(json.dumps(PAGERDUTY))
    resolved["event"]["event_type"] = "incident.resolved"
    assert intake.from_pagerduty(resolved)[0].status == "resolved"
    acked = json.loads(json.dumps(PAGERDUTY))
    acked["event"]["event_type"] = "incident.acknowledged"
    assert intake.from_pagerduty(acked) == []


def test_grafana():
    a, b = intake.from_alertmanager(GRAFANA, "grafana")
    assert (a.source, a.fingerprint, a.alert_name, a.service, a.status, a.summary) == \
        ("grafana", "grafana:c6eadffa33fcdf37", "High memory usage", "unknown", "firing",
         "This alert was triggered for zone us-1")
    assert a.starts_at == "2021-10-12T09:51:03.157076+02:00" and b.fingerprint == "grafana:bc97ff14869b13e3"
    labelled = json.loads(json.dumps(GRAFANA))
    labelled["alerts"][0]["labels"]["service"] = "shop-api"
    assert intake.from_alertmanager(labelled, "grafana")[0].service == "shop-api"
    resolved = json.loads(json.dumps(GRAFANA))
    resolved["alerts"][0].update(status="resolved", endsAt="2021-10-12T10:30:00+02:00")
    r = intake.from_alertmanager(resolved, "grafana")[0]
    assert r.status == "resolved" and r.ends_at == "2021-10-12T10:30:00+02:00"


def test_generic_defaults():
    [a] = intake.from_generic({"alert_name": "Weird"})
    assert (a.service, a.severity, a.status, a.fingerprint) == ("unknown", "warning", "firing", "generic:unknown:Weird")


# ---------- signatures ----------

def _sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_sentry_signature(api, monkeypatch):
    c, fake = api
    monkeypatch.setenv("SENTRY_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(SENTRY_ISSUE).encode()
    headers = {"Sentry-Hook-Resource": "event_alert", "Content-Type": "application/json"}
    assert c.post("/alerts/sentry", content=body, headers={**headers, "Sentry-Hook-Signature": "nope"}).status_code == 401
    assert c.post("/alerts/sentry", content=body, headers=headers).status_code == 401
    assert not fake.incidents
    ok = c.post("/alerts/sentry", content=body, headers={**headers, "Sentry-Hook-Signature": _sign("s3cret", body)})
    assert ok.status_code == 200 and ok.json()["alerts"][0]["result"] == "new"


def test_sentry_without_secret_accepts_unsigned(api):
    c, _ = api
    assert c.post("/alerts/sentry", json=SENTRY_ISSUE).status_code == 200


def test_pagerduty_signature():
    # Header format per https://docs.pagerduty.com/developer/verifying-webhook-signatures:
    # one "v1=<hex HMAC-SHA256 of the raw body>" per signing secret, comma-separated.
    body = json.dumps(PAGERDUTY).encode()
    good = "v1=" + _sign("pd", body)
    assert intake.verify_pagerduty(body, f"v1=f03de6f61df6e454f3620c4d6aca17ad072d3f8bbb2760eac3b2ad391b5e8073,{good}", "pd")
    assert intake.verify_pagerduty(body, f"v1=old, {good}", "pd")
    assert not intake.verify_pagerduty(body, "v1=bad", "pd")
    assert not intake.verify_pagerduty(body, _sign("pd", body), "pd"), "the v1= prefix is required"


def test_pagerduty_signature_endpoint(api, monkeypatch):
    c, fake = api
    monkeypatch.setenv("PAGERDUTY_WEBHOOK_SECRET", "pd")
    body = json.dumps(PAGERDUTY).encode()
    h = {"Content-Type": "application/json"}
    assert c.post("/alerts/pagerduty", content=body, headers={**h, "X-PagerDuty-Signature": "v1=bad"}).status_code == 401
    ok = c.post("/alerts/pagerduty", content=body, headers={**h, "X-PagerDuty-Signature": "v1=" + _sign("pd", body)})
    assert ok.status_code == 200 and len(fake.incidents) == 1


def test_sentry_non_alert_resources_are_ignored(api):
    c, fake = api
    for resource in ("installation", "issue", "error", "comment"):
        r = c.post("/alerts/sentry", json=SENTRY_ISSUE, headers={"Sentry-Hook-Resource": resource})
        assert r.status_code == 200 and r.json()["alerts"] == []
    assert c.post("/alerts/sentry", json=SENTRY_METRIC, headers={"Sentry-Hook-Resource": "metric_alert"}).status_code == 200
    assert len(fake.incidents) == 1


def test_grafana_signature(api, monkeypatch):
    # https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/
    c, fake = api
    body = json.dumps(GRAFANA).encode()
    h = {"Content-Type": "application/json"}
    monkeypatch.setenv("GRAFANA_WEBHOOK_SECRET", "g")
    assert c.post("/alerts/grafana", content=body, headers=h).status_code == 401
    assert c.post("/alerts/grafana", content=body,
                  headers={**h, "X-Grafana-Alerting-Signature": _sign("g", body)}).status_code == 200
    monkeypatch.setenv("GRAFANA_WEBHOOK_TIMESTAMP_HEADER", "X-Grafana-Alerting-Timestamp")
    import time as _t
    ts = str(int(_t.time()))
    sig = _sign("g", ts.encode() + b":" + body)
    assert c.post("/alerts/grafana", content=body, headers={**h, "X-Grafana-Alerting-Signature": sig}).status_code == 401
    assert c.post("/alerts/grafana", content=body, headers={**h, "X-Grafana-Alerting-Signature": sig,
                                                             "X-Grafana-Alerting-Timestamp": ts}).status_code == 200
    old = str(int(_t.time()) - 3600)
    assert not intake.verify_grafana(body, {"X-Grafana-Alerting-Signature": _sign("g", old.encode() + b":" + body),
                                            "X-Grafana-Alerting-Timestamp": old})


# ---------- the shared flow ----------

def test_every_endpoint_accepts_its_payload(api):
    c, fake = api
    for path, payload in [("/alerts", ALERTMANAGER), ("/alerts/sentry", SENTRY_ISSUE), ("/alerts/datadog", DATADOG),
                          ("/alerts/pagerduty", PAGERDUTY), ("/alerts/grafana", GRAFANA), ("/alerts/generic", GENERIC)]:
        r = c.post(path, json=payload)
        assert r.status_code == 200, (path, r.text)
    assert len(fake.incidents) == 7  # the Grafana example carries two alerts
    assert len(fake.investigations) == 1, "one storm, one investigation"


def test_sentry_and_alertmanager_join_one_investigation(api):
    c, fake = api
    first = c.post("/alerts", json=ALERTMANAGER).json()["alerts"][0]
    second = c.post("/alerts/sentry", json=SENTRY_ISSUE).json()["alerts"][0]
    assert first["result"] == "new" and second["result"] == "grouped"
    assert first["investigation_id"] == second["investigation_id"]
    _wait_started(fake, 1)
    assert fake.started == [first["investigation_id"]], "the investigation starts once"


def test_duplicates_are_ignored_and_resolved_reopens(api):
    c, fake = api
    c.post("/alerts/datadog", json=DATADOG)
    assert c.post("/alerts/datadog", json=DATADOG).json()["alerts"][0]["result"] == "duplicate"
    c.post("/alerts/datadog", json={**DATADOG, "alert_transition": "Recovered"})
    assert [i["status"] for i in fake.incidents.values()] == ["resolved"]
    assert c.post("/alerts/datadog", json=DATADOG).json()["alerts"][0]["result"] != "duplicate"


def test_resolve_flow_per_source(api):
    c, fake = api
    c.post("/alerts/generic", json=GENERIC)
    c.post("/alerts/generic", json={**GENERIC, "status": "resolved"})
    resolved = json.loads(json.dumps(PAGERDUTY))
    resolved["event"]["event_type"] = "incident.resolved"
    c.post("/alerts/pagerduty", json=PAGERDUTY)
    c.post("/alerts/pagerduty", json=resolved)
    assert all(i["status"] == "resolved" for i in fake.incidents.values()) and len(fake.incidents) == 2


@pytest.mark.parametrize("path,body", [
    ("/alerts", {"alerts": [{"status": "firing", "labels": {}}]}),
    ("/alerts", {"alerts": "nope"}),
    ("/alerts/sentry", {"data": {}}),
    ("/alerts/datadog", {"title": "no id"}),
    ("/alerts/pagerduty", {"event": {"event_type": "incident.triggered"}}),
    ("/alerts/grafana", ["not", "an", "object"]),
    ("/alerts/generic", {"service": "x"}),
    ("/alerts/generic", {"alert_name": "A", "status": "maybe"}),
])
def test_malformed_payloads_are_400(api, path, body):
    c, fake = api
    r = c.post(path, json=body)
    assert r.status_code == 400 and r.json()["detail"]
    assert not fake.incidents


def test_invalid_json_is_400(api):
    c, _ = api
    r = c.post("/alerts/generic", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "JSON" in r.json()["detail"]


# ---------- manual trigger ----------

def test_manual_trigger_starts_an_investigation(api):
    c, fake = api
    r = c.post("/investigations/manual", json={"service": "payments-svc", "description": "checkout failing"})
    assert r.status_code == 200 and r.json()["result"] == "new"
    [inc] = fake.incidents.values()
    assert inc["service"] == "payments-svc" and inc["severity"] == "warning" and inc["fp"].startswith("manual:")
    # Asking twice is two incidents, grouped into the running investigation.
    again = c.post("/investigations/manual", json={"service": "payments-svc", "description": "still failing"}).json()
    assert again["result"] == "grouped" and again["investigation_id"] == r.json()["investigation_id"]


def test_manual_trigger_validates(api):
    c, _ = api
    assert c.post("/investigations/manual", json={"service": "x", "description": " "}).status_code == 400
    assert c.post("/investigations/manual", json={"service": "x", "description": "d", "severity": "huge"}).status_code == 400
    assert c.post("/investigations/manual", json={"service": "x"}).status_code == 422


def test_cli_builds_the_request(monkeypatch):
    monkeypatch.delenv("LOKI_API_KEY", raising=False)
    args = investigate_cli.parse_args(["--service", "payments-svc", "--description", "checkout failing",
                                       "--agent", "http://loki:8080/"])
    req = investigate_cli.build_request(args)
    assert req.full_url == "http://loki:8080/investigations/manual" and req.get_method() == "POST"
    assert json.loads(req.data) == {"service": "payments-svc", "description": "checkout failing"}


# ---------- with auth on ----------

@pytest.fixture
def secured(api, monkeypatch):
    monkeypatch.setenv("LOKI_API_KEYS", "ops:w-key:read+write,viewer:r-key:read")
    monkeypatch.setenv("LOKI_WEBHOOK_TOKEN", "hook-token")
    return api


def test_webhooks_need_the_token_and_signatures_still_apply(secured, monkeypatch):
    c, fake = secured
    assert c.post("/alerts/datadog", json=DATADOG).status_code == 401
    assert c.post("/alerts/datadog?token=hook-token", json=DATADOG).status_code == 200
    assert c.post("/alerts/generic", json=GENERIC, headers={"Authorization": "Bearer w-key"}).status_code == 200
    monkeypatch.setenv("SENTRY_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(SENTRY_ISSUE).encode()
    bad = c.post("/alerts/sentry?token=hook-token", content=body, headers={"Sentry-Hook-Signature": "nope"})
    assert bad.status_code == 401
    good = c.post("/alerts/sentry?token=hook-token", content=body, headers={"Sentry-Hook-Signature": _sign("s3cret", body)})
    assert good.status_code == 200


def test_manual_needs_a_write_key_and_is_audited(secured):
    c, fake = secured
    body = {"service": "payments-svc", "description": "checkout failing"}
    assert c.post("/investigations/manual?token=hook-token", json=body).status_code == 401
    assert c.post("/investigations/manual", json=body, headers={"Authorization": "Bearer r-key"}).status_code == 403
    r = c.post("/investigations/manual", json=body, headers={"Authorization": "Bearer w-key"})
    assert r.status_code == 200
    assert fake.audit == [("ops", "manual", r.json()["investigation_id"])]


def test_cli_sends_the_api_key(monkeypatch):
    monkeypatch.setenv("LOKI_API_KEY", "w-key")
    req = investigate_cli.build_request(investigate_cli.parse_args(["--service", "s", "--description", "d"]))
    assert req.get_header("Authorization") == "Bearer w-key"
