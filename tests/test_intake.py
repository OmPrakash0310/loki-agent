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

# ---------- sample payloads, trimmed from each provider's docs ----------

ALERTMANAGER = {
    "version": "4", "status": "firing", "receiver": "loki",
    "alerts": [{"status": "firing", "fingerprint": "a1b2c3",
                "labels": {"alertname": "HighErrorRate", "service": "payments-svc", "severity": "critical"},
                "annotations": {"summary": "payments-svc error rate is 40%"},
                "startsAt": "2026-10-16T21:03:00Z", "endsAt": "0001-01-01T00:00:00Z"}],
}

SENTRY_ISSUE = {
    "action": "triggered",
    "installation": {"uuid": "7a485448-a9e2-4c85-8a3c-4f44175783c9"},
    "data": {
        "event": {
            "event_id": "e4874d664c3540c1a32eab185f12c5ab", "issue_id": "1170820242", "project": 1,
            "level": "error", "title": "KeyError: 'currency'", "datetime": "2026-10-16T21:02:58.000000Z",
            "url": "https://sentry.io/api/0/projects/acme/payments/events/e4874d664c3540c1a32eab185f12c5ab/",
            "web_url": "https://sentry.io/organizations/acme/issues/1170820242/events/e4874d/",
        },
        "triggered_rule": "New error in payments",
    },
    "actor": {"type": "application", "id": "sentry", "name": "Sentry"},
}

SENTRY_METRIC = {
    "action": "critical",
    "data": {
        "metric_alert": {"id": "8", "identifier": "8", "date_started": "2026-10-16T21:03:00Z", "date_closed": None,
                         "alert_rule": {"name": "Checkout failure rate", "projects": ["payments"], "aggregate": "failure_rate()"}},
        "description_title": "Critical: Checkout failure rate", "description_text": "62% failure rate in 5 minutes",
        "web_url": "https://sentry.io/organizations/acme/alerts/rules/details/2/",
    },
}

DATADOG = {  # what the README's template renders to
    "id": "7034520137543254000", "alert_id": "12345678", "title": "[Triggered] High 5xx on payments",
    "alert_transition": "Triggered", "alert_scope": "service:payments-svc", "priority": "P1",
    "tags": "env:prod,service:payments-svc,team:payments", "date": "1792184580000",
    "body": "5xx rate above 5% for 5 minutes",
}

PAGERDUTY = {
    "event": {
        "id": "01DEN3HXMQNWF6XJGMSWNSZ1E8", "event_type": "incident.triggered", "resource_type": "incident",
        "occurred_at": "2026-10-16T21:03:05.000Z", "agent": None,
        "data": {"id": "PGR0VU2", "type": "incident", "number": 2, "title": "Checkout is failing",
                 "status": "triggered", "urgency": "high", "priority": {"summary": "P1"},
                 "service": {"id": "PF9KMXH", "type": "service_reference", "summary": "payments-svc"},
                 "html_url": "https://acme.pagerduty.com/incidents/PGR0VU2"},
    }
}

GRAFANA = {
    "receiver": "loki", "status": "firing", "orgId": 1, "version": "1", "groupKey": "{}:{}",
    "alerts": [{"status": "firing", "fingerprint": "57c6d9296de2ad39",
                "labels": {"alertname": "Latency high", "grafana_folder": "prod", "service": "shop-api"},
                "annotations": {"summary": "p99 over 2s"}, "startsAt": "2026-10-16T21:03:10Z",
                "endsAt": "0001-01-01T00:00:00Z", "generatorURL": "http://grafana/alerting/grafana/x/view"}],
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
    monkeypatch.setenv("SENTRY_PROJECTS", "payments=payments-svc")
    [a] = intake.from_sentry(SENTRY_ISSUE)
    assert (a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("sentry:issue:1170820242", "New error in payments", "payments-svc", "critical", "firing")
    assert a.summary == "KeyError: 'currency'"


def test_sentry_metric_alert_and_resolve(monkeypatch):
    monkeypatch.setenv("SENTRY_PROJECTS", "payments=payments-svc")
    [a] = intake.from_sentry(SENTRY_METRIC)
    assert (a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("sentry:metric:8", "Checkout failure rate", "payments-svc", "critical", "firing")
    resolved = json.loads(json.dumps(SENTRY_METRIC))
    resolved["action"] = "resolved"
    resolved["data"]["metric_alert"]["date_closed"] = "2026-10-16T21:20:00Z"
    [r] = intake.from_sentry(resolved)
    assert r.status == "resolved" and r.fingerprint == a.fingerprint and r.ends_at == "2026-10-16T21:20:00Z"


def test_sentry_unmapped_project_keeps_its_slug(monkeypatch):
    monkeypatch.delenv("SENTRY_PROJECTS", raising=False)
    assert intake.from_sentry(SENTRY_ISSUE)[0].service == "payments"


def test_datadog():
    [a] = intake.from_datadog(DATADOG)
    assert (a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("datadog:12345678:service:payments-svc", "[Triggered] High 5xx on payments", "payments-svc", "critical", "firing")
    assert a.starts_at.startswith("2026-10-16T21:03")
    [r] = intake.from_datadog({**DATADOG, "alert_transition": "Recovered"})
    assert r.status == "resolved" and r.fingerprint == a.fingerprint
    assert intake.from_datadog({**DATADOG, "alert_transition": "Warn"})[0].severity == "warning"
    assert intake.from_datadog({**DATADOG, "tags": "env:prod"})[0].service == "unknown"


def test_pagerduty():
    [a] = intake.from_pagerduty(PAGERDUTY)
    assert (a.fingerprint, a.alert_name, a.service, a.severity, a.status) == \
        ("pagerduty:PGR0VU2", "Checkout is failing", "payments-svc", "critical", "firing")
    resolved = json.loads(json.dumps(PAGERDUTY))
    resolved["event"]["event_type"] = "incident.resolved"
    assert intake.from_pagerduty(resolved)[0].status == "resolved"
    acked = json.loads(json.dumps(PAGERDUTY))
    acked["event"]["event_type"] = "incident.acknowledged"
    assert intake.from_pagerduty(acked) == []


def test_grafana():
    [a] = intake.from_alertmanager(GRAFANA, "grafana")
    assert (a.source, a.fingerprint, a.alert_name, a.service, a.status) == \
        ("grafana", "grafana:57c6d9296de2ad39", "Latency high", "shop-api", "firing")
    resolved = json.loads(json.dumps(GRAFANA))
    resolved["alerts"][0].update(status="resolved", endsAt="2026-10-16T21:30:00Z")
    [r] = intake.from_alertmanager(resolved, "grafana")
    assert r.status == "resolved" and r.ends_at == "2026-10-16T21:30:00Z"


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
    body = b'{"event": {}}'
    good = "v1=" + _sign("pd", body)
    assert intake.verify_pagerduty(body, f"v1=old, {good}", "pd")
    assert not intake.verify_pagerduty(body, "v1=bad", "pd")


# ---------- the shared flow ----------

def test_every_endpoint_accepts_its_payload(api):
    c, fake = api
    for path, payload in [("/alerts", ALERTMANAGER), ("/alerts/sentry", SENTRY_ISSUE), ("/alerts/datadog", DATADOG),
                          ("/alerts/pagerduty", PAGERDUTY), ("/alerts/grafana", GRAFANA), ("/alerts/generic", GENERIC)]:
        r = c.post(path, json=payload)
        assert r.status_code == 200, (path, r.text)
    assert len(fake.incidents) == 6
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
