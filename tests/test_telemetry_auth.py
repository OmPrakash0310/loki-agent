"""Credentials for the customer's Prometheus / Grafana Loki."""
import base64
from datetime import datetime, timedelta, timezone

import pytest

import telemetry
from sources import httpkit


def test_no_credentials_means_no_headers(monkeypatch):
    for k in ("BEARER_TOKEN", "BASIC_USER", "BASIC_PASSWORD", "ORG_ID"):
        monkeypatch.delenv(f"LOKI_PROM_{k}", raising=False)
    assert httpkit.auth_headers("PROM") == {}


def test_bearer_wins_over_basic_and_org_id_is_added(monkeypatch):
    monkeypatch.setenv("LOKI_PROM_BEARER_TOKEN", "tok")
    monkeypatch.setenv("LOKI_PROM_BASIC_USER", "u")
    monkeypatch.setenv("LOKI_PROM_ORG_ID", "tenant-1")
    assert httpkit.auth_headers("PROM") == {"Authorization": "Bearer tok", "X-Scope-OrgID": "tenant-1"}


def test_basic_auth(monkeypatch):
    monkeypatch.setenv("LOKI_LOGS_BASIC_USER", "123")
    monkeypatch.setenv("LOKI_LOGS_BASIC_PASSWORD", "secret")
    expect = "Basic " + base64.b64encode(b"123:secret").decode()
    assert httpkit.auth_headers("LOGS") == {"Authorization": expect}


@pytest.mark.parametrize("prefix,call", [
    ("PROM", lambda now: telemetry.metric_at("error_rate", "svc", now)),
    ("LOGS", lambda now: telemetry.fetch_logs("svc", now - timedelta(minutes=5), now, True)),
])
def test_requests_carry_the_right_credentials(monkeypatch, prefix, call):
    seen = []

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"result": []}, "status": "success"}

    monkeypatch.setattr(telemetry, "PROM_URL", "http://prom.test")
    monkeypatch.setattr(telemetry, "LOKI_URL", "http://logs.test")
    monkeypatch.setattr(telemetry, "metric_unavailable", lambda m: None)
    monkeypatch.setenv("LOKI_PROM_BEARER_TOKEN", "prom-token")
    monkeypatch.setenv("LOKI_LOGS_BEARER_TOKEN", "logs-token")
    monkeypatch.setattr(telemetry.httpx, "get", lambda url, **kw: seen.append((url, kw.get("headers"))) or Resp())
    try:
        call(datetime.now(timezone.utc))
    except Exception:
        pass  # parsing the empty fake response is irrelevant here; only the request matters
    assert seen, "no request made"
    want = "Bearer prom-token" if prefix == "PROM" else "Bearer logs-token"
    assert seen[0][1] == {"Authorization": want}
