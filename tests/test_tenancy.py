"""Many customers on one server: isolation, routing, settings, secrets.

The first part needs no database. The rest runs against a real Postgres, because
isolation is enforced by Postgres itself (row-level security), and is skipped unless
LOKI_TEST_DATABASE_URL points at a database it may wipe, e.g.
    LOKI_TEST_DATABASE_URL=postgresql://loki@localhost:55433/loki_test pytest tests/test_tenancy.py
"""
import json
import os
import threading
import time

import pytest
from cryptography.fernet import Fernet

import db
import metrics_config
import slack
import sources
import telemetry
import tenant
import tools
import verifier

ACME = tenant.Tenant(2, "acme", "Acme", {"SLACK_BOT_TOKEN": "xoxb-acme", "SLACK_CHANNEL": "C-ACME",
                                         "LOKI_PROM_URL": "https://prom.acme.example/", "LOKI_METRICS_PRESET": "otel"})


# ---------------- no database ----------------

def test_default_tenant_reads_the_environment_others_never_do(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-operator")
    assert tenant.setting("SLACK_BOT_TOKEN") == "xoxb-operator"
    with tenant.use(ACME):
        assert tenant.setting("SLACK_BOT_TOKEN") == "xoxb-acme"
        assert tenant.setting("GITHUB_TOKEN") is None  # set in neither: no fallback to the operator's env
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-operator")
    with tenant.use(ACME):
        assert tenant.setting("GITHUB_TOKEN") is None


def test_settings_are_checked_and_secrets_recognised():
    plain, secret = tenant.split_settings({"SLACK_CHANNEL": "C1", "SLACK_BOT_TOKEN": "x", "DATADOG_APP_KEY": "k"})
    assert plain == {"SLACK_CHANNEL": "C1"} and set(secret) == {"SLACK_BOT_TOKEN", "DATADOG_APP_KEY"}
    with pytest.raises(ValueError, match="unknown setting"):
        tenant.split_settings({"SLACK_BOT_TOKN": "x"})
    with pytest.raises(ValueError, match="unknown setting"):
        tenant.split_settings({"ANTHROPIC_API_KEY": "x"})  # the operator's, never a tenant's


def test_secrets_round_trip_and_need_the_key(monkeypatch):
    monkeypatch.delenv("LOKI_SECRET_KEY", raising=False)
    with pytest.raises(tenant.SecretKeyMissing):
        tenant.encrypt({"a": "b"})
    monkeypatch.setenv("LOKI_SECRET_KEY", Fernet.generate_key().decode())
    blob = tenant.encrypt({"SLACK_BOT_TOKEN": "xoxb-secret"})
    assert "xoxb-secret" not in blob and tenant.decrypt(blob) == {"SLACK_BOT_TOKEN": "xoxb-secret"}


def test_per_tenant_telemetry_slack_metrics_and_operator_only_features(monkeypatch):
    monkeypatch.setenv("LOKI_PROBES", "1")
    monkeypatch.setenv("LOKI_PROBE_ENDPOINTS", "payments-svc=http://payments-svc:8000")
    monkeypatch.setenv("LOKI_VERIFY", "1")
    assert tools.probes_unavailable() is None and verifier.enabled()
    with tenant.use(ACME):
        assert telemetry.prom_url() == "https://prom.acme.example"
        assert telemetry.loki_url() == ""  # not configured: no log source, not the demo's
        assert slack._bot_token() == "xoxb-acme" and slack._channel() == "C-ACME"
        assert metrics_config.current().service_label == "service_name"  # its own preset
        assert "single-customer" in tools.probes_unavailable()
        assert not verifier.enabled()
    with tenant.use(tenant.Tenant(3, "bare", "Bare")):
        assert telemetry.prom_url() == "" and not telemetry.prom_enabled()


def test_a_tenant_without_a_deploy_source_never_sees_the_demo_file():
    sources.reset()
    with tenant.use(ACME):
        d = sources.deploys()
        assert isinstance(d, sources.NoDeploys) and d.list_deploys(None, None) == []
    assert not isinstance(sources.deploys(), sources.NoDeploys)  # the default tenant keeps its file
    sources.reset()


def test_tool_cache_never_crosses_tenants(monkeypatch):
    calls = []
    monkeypatch.setitem(tools.HANDLERS, "zz_echo", lambda **a: calls.append(tenant.current_id()) or {"t": tenant.current_id()})
    tools.run("zz_echo", {})
    with tenant.use(ACME):
        out, _ = tools.run("zz_echo", {})
    assert calls == [1, 2] and '"t": 2' in out


# ---------------- real Postgres ----------------

DB = os.environ.get("LOKI_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB, reason="set LOKI_TEST_DATABASE_URL to a database the test may wipe")


@pytest.fixture
def pg(monkeypatch):
    monkeypatch.setattr(db, "DB_URL", DB)
    monkeypatch.setenv("LOKI_SECRET_KEY", Fernet.generate_key().decode())
    with db.connect(admin=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.init()
    import tenants
    tenants._cache.clear()
    sources.reset()
    yield
    sources.reset()


@pytest.fixture
def app(pg, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setenv("LOKI_API_KEYS", "admin:op-key-" + "x" * 30)
    monkeypatch.setenv("LOKI_WEBHOOK_TOKEN", "operator-hook-" + "y" * 30)
    monkeypatch.setattr(main, "GROUP_WINDOW_S", 0)
    started = []
    monkeypatch.setattr(main.investigator, "investigate", lambda inv_id: started.append((tenant.current_id(), inv_id)))
    return TestClient(main.app), started


OP = {"Authorization": "Bearer op-key-" + "x" * 30}


def alert(name, service):
    return {"alert_name": name, "service": service, "summary": "s", "severity": "critical", "status": "firing"}


@needs_db
def test_rls_hides_and_refuses_other_tenants_rows(pg):
    import psycopg
    import tenants
    acme = tenants.get(tenants.create("acme", "Acme")["id"])
    for who in (tenant.DEFAULT, acme, acme):
        with tenant.use(who), db.connect() as c:
            c.execute("INSERT INTO investigations DEFAULT VALUES")
            c.execute("INSERT INTO report_cache (exact_key, fingerprint, investigation_id, report, expires_at) "
                      "SELECT 'k', 'f', max(id), %s, now() + interval '1 day' FROM investigations "
                      "ON CONFLICT (tenant_id, exact_key) DO UPDATE SET report = EXCLUDED.report",
                      (json.dumps({"who": who.slug}),))
    with db.connect() as c:
        assert c.execute("SELECT count(*) n FROM investigations").fetchone()["n"] == 1
        assert [r["report"]["who"] for r in c.execute("SELECT report FROM report_cache")] == ["default"]
    with tenant.use(acme), db.connect() as c:
        assert c.execute("SELECT count(*) n FROM investigations").fetchone()["n"] == 2
        assert c.execute("UPDATE investigations SET status = 'failed'").rowcount == 2  # its own only
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO investigations (tenant_id) VALUES (1)")
        assert [r["slug"] for r in c.execute("SELECT slug FROM tenants")] == ["acme"]
    with db.connect() as c:
        assert c.execute("SELECT status FROM investigations").fetchone()["status"] == "queued"


@needs_db
def test_webhooks_api_keys_and_threads_follow_the_tenant(app):
    client, started = app
    created = client.post("/tenants", json={"slug": "acme", "name": "Acme"}, headers=OP).json()
    hook, key = created["webhook_token"], created["api_key"]
    acme_auth = {"Authorization": f"Bearer {key}"}

    assert client.post(f"/alerts/generic?token={hook}", json=alert("HighErrorRate", "checkout")).status_code == 200
    assert client.post("/alerts/generic?token=operator-hook-" + "y" * 30,
                       json=alert("HighErrorRate", "checkout")).status_code == 200  # same alert, other tenant
    deadline = time.time() + 5
    while len(started) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert sorted(t for t, _ in started) == [1, created["id"]]  # each investigation thread ran as its tenant

    mine = client.get("/incidents", headers=acme_auth).json()
    ops = client.get("/incidents", headers=OP).json()
    assert len(mine) == 1 and len(ops) == 1 and mine[0]["id"] != ops[0]["id"]
    # acme can't read the default tenant's investigation, even knowing its id
    other = client.get("/investigations", headers=OP).json()[0]["id"]
    assert client.get(f"/investigations/{other}", headers=acme_auth).status_code == 404
    # the operator can act for acme
    as_acme = client.get("/incidents", headers={**OP, "X-Loki-Tenant": "acme"}).json()
    assert [i["id"] for i in as_acme] == [i["id"] for i in mine]
    # a tenant can't manage tenants or impersonate another
    assert client.get("/tenants", headers=acme_auth).status_code == 403
    assert client.get("/incidents", headers={**acme_auth, "X-Loki-Tenant": "default"}).json() == mine
    assert client.post("/alerts/generic?token=wrong", json=alert("X", "y")).status_code == 401


@needs_db
def test_settings_are_stored_encrypted_and_used(app):
    client, _ = app
    t = client.post("/tenants", json={"slug": "acme", "name": "Acme"}, headers=OP).json()
    r = client.put(f"/tenants/{t['id']}/settings", headers=OP,
                   json={"SLACK_BOT_TOKEN": "xoxb-acme-secret", "SLACK_CHANNEL": "C9"})
    assert r.status_code == 200 and r.json()["settings"] == ["SLACK_BOT_TOKEN", "SLACK_CHANNEL"]
    with db.connect(admin=True) as c:
        row = c.execute("SELECT settings, secrets FROM tenants WHERE id = %s", (t["id"],)).fetchone()
    assert row["settings"] == {"SLACK_CHANNEL": "C9"} and "xoxb-acme-secret" not in (row["secrets"] or "")
    import tenants
    with tenant.use(tenants.get(t["id"])):
        assert slack._bot_token() == "xoxb-acme-secret"
    assert client.put(f"/tenants/{t['id']}/settings", headers=OP, json={"ANTHROPIC_API_KEY": "x"}).status_code == 400
    assert client.put("/tenants/1/settings", headers=OP, json={"SLACK_CHANNEL": "x"}).status_code == 400
    listing = client.get("/tenants", headers=OP).json()
    assert "xoxb" not in json.dumps(listing)  # names only, never values


@needs_db
def test_rotated_credentials_stop_working(app):
    client, _ = app
    t = client.post("/tenants", json={"slug": "acme", "name": "Acme"}, headers=OP).json()
    new = client.post(f"/tenants/{t['id']}/rotate/api_key", headers=OP).json()["api_key"]
    assert client.get("/incidents", headers={"Authorization": f"Bearer {t['api_key']}"}).status_code == 401
    assert client.get("/incidents", headers={"Authorization": f"Bearer {new}"}).status_code == 200


@needs_db
def test_restart_recovery_runs_each_tenant_as_itself(pg, monkeypatch):
    import main
    import tenants
    acme = tenants.get(tenants.create("acme", "Acme")["id"])
    for who in (tenant.DEFAULT, acme):
        with tenant.use(who), db.connect() as c:
            c.execute("INSERT INTO investigations (status) VALUES ('running')")
    ran = []
    done = threading.Event()

    def fake_start(inv_id):
        ran.append(tenant.current_id())
        if len(ran) == 2:
            done.set()
    monkeypatch.setattr(main, "_start_investigation", fake_start)
    monkeypatch.setattr(main.selfmon, "start", lambda: None)
    monkeypatch.delenv("LOKI_REQUIRE_AUTH", raising=False)
    main.startup()
    assert done.wait(2) and sorted(ran) == [1, acme.id]
