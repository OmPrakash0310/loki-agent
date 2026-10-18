"""API auth: secure by default, allowlist, scopes, webhook token, dev mode."""
import logging

import pytest
from fastapi.testclient import TestClient

import auth
import main

KEYS = "eval:rw-secret-123,dash:ro-secret-456:read,bot:wo-secret-789:write"
HOOK = "hook-secret-abc"


@pytest.fixture(scope="module", autouse=True)
def dummy_routes():
    # Stand-ins for routes another branch might add later; nothing opts them in.
    @main.app.get("/zz-test/new-thing")
    def _new_get():
        return {"ok": 1}

    @main.app.post("/zz-test/new-thing")
    def _new_post():
        return {"ok": 2}

    @main.app.get("/alertszz-test")
    def _not_alerts():
        return {"ok": 4}

    @main.app.post("/alerts/zz-test-source")
    def _new_alert():
        return {"ok": 3}

    yield


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("LOKI_API_KEYS", KEYS)
    monkeypatch.setenv("LOKI_WEBHOOK_TOKEN", HOOK)
    monkeypatch.delenv("LOKI_REQUIRE_AUTH", raising=False)


@pytest.fixture
def client():
    return TestClient(main.app)  # not a context manager: startup (db.init) doesn't run


def bearer(k):
    return {"Authorization": f"Bearer {k}"}


@pytest.mark.parametrize("method,path", [
    ("get", "/investigations"), ("get", "/investigations/1"), ("get", "/investigations/1/trace"),
    ("get", "/stats"), ("post", "/investigations/1/confirm"), ("post", "/investigations/1/reject"),
    ("post", "/backtest"), ("get", "/deploys/d-1/diff"), ("post", "/alerts"),
])
def test_protected_routes_need_a_key(secured, client, method, path):
    assert getattr(client, method)(path).status_code == 401
    r = getattr(client, method)(path, headers=bearer("wrong-key"))
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_malformed_header_is_401(secured, client):
    for h in ("rw-secret-123", "Basic rw-secret-123", "Bearer ", "Bearer rw-secret-12"):
        assert client.get("/zz-test/new-thing", headers={"Authorization": h}).status_code == 401


def test_new_routes_are_protected_by_default(secured, client):
    assert client.get("/zz-test/new-thing").status_code == 401
    assert client.post("/zz-test/new-thing").status_code == 401
    assert client.get("/zz-test/new-thing", headers=bearer("rw-secret-123")).json() == {"ok": 1}


def test_scopes(secured, client):
    ro, wo = bearer("ro-secret-456"), bearer("wo-secret-789")
    assert client.get("/zz-test/new-thing", headers=ro).status_code == 200
    assert client.post("/zz-test/new-thing", headers=ro).status_code == 403
    assert client.post("/investigations/1/confirm", headers=ro).status_code == 403
    assert client.post("/investigations/1/reject", headers=ro).status_code == 403
    assert client.post("/backtest", headers=ro, json={}).status_code == 403
    assert client.post("/zz-test/new-thing", headers=wo).status_code == 200
    assert client.get("/zz-test/new-thing", headers=wo).status_code == 403
    assert client.post("/zz-test/new-thing", headers=bearer("rw-secret-123")).status_code == 200


def test_allowlist(secured, client):
    assert client.get("/health").json() == {"ok": True}
    # Reaches Slack's own signature check rather than our 401 for a missing key.
    r = client.post("/slack/interactions", content=b"payload=%7B%7D",
                    headers={"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=00"})
    assert r.status_code == 401 and r.json()["detail"] == "bad slack signature"


def test_webhook_token_on_any_alerts_path(secured, client):
    assert client.post("/alerts/zz-test-source").status_code == 401
    assert client.post("/alerts/zz-test-source?token=nope").status_code == 401
    assert client.post(f"/alerts/zz-test-source?token={HOOK}").json() == {"ok": 3}
    assert client.post("/alerts/zz-test-source", headers=bearer("wo-secret-789")).status_code == 200
    # The webhook token is not an API key anywhere else.
    assert client.get(f"/zz-test/new-thing?token={HOOK}").status_code == 401
    assert client.get("/zz-test/new-thing", headers=bearer(HOOK)).status_code == 401
    # Prefix match is on a path segment: /alertszz-test is not a webhook path.
    assert client.get(f"/alertszz-test?token={HOOK}").status_code == 401


def test_dev_mode_without_keys(monkeypatch, client, caplog):
    monkeypatch.delenv("LOKI_API_KEYS", raising=False)
    monkeypatch.delenv("LOKI_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("LOKI_REQUIRE_AUTH", raising=False)
    with caplog.at_level(logging.WARNING, logger="agent"):
        auth.check_startup()
    assert "AUTH DISABLED" in caplog.text
    assert client.post("/zz-test/new-thing").json() == {"ok": 2}
    assert client.post("/alerts/zz-test-source").json() == {"ok": 3}


def test_webhook_token_alone_guards_alerts(monkeypatch, client):
    monkeypatch.delenv("LOKI_API_KEYS", raising=False)
    monkeypatch.setenv("LOKI_WEBHOOK_TOKEN", HOOK)
    assert client.post("/alerts/zz-test-source").status_code == 401
    assert client.post(f"/alerts/zz-test-source?token={HOOK}").status_code == 200


def test_require_auth_guard(monkeypatch):
    monkeypatch.delenv("LOKI_API_KEYS", raising=False)
    monkeypatch.setenv("LOKI_REQUIRE_AUTH", "1")
    with pytest.raises(RuntimeError, match="refusing to start"):
        auth.check_startup()
    monkeypatch.setenv("LOKI_API_KEYS", KEYS)
    auth.check_startup()


def test_parse_keys():
    keys = auth.parse_keys(" a:k1 , k2, c:k3:read ,")
    assert [(k.name, k.secret, sorted(k.scopes)) for k in keys] == [
        ("a", "k1", ["read", "write"]), ("key2", "k2", ["read", "write"]), ("c", "k3", ["read"])]
    for bad in ("a:k:admin", "a::read", "a:b:c:d", "a:k:"):
        with pytest.raises(ValueError):
            auth.parse_keys(bad)


def test_keys_never_logged(secured, client, caplog):
    secrets = ["rw-secret-123", "ro-secret-456", "wo-secret-789", HOOK]
    with caplog.at_level(logging.DEBUG):
        auth.check_startup()
        client.get("/zz-test/new-thing", headers=bearer("rw-secret-123"))
        client.post("/zz-test/new-thing", headers=bearer("ro-secret-456"))
        client.post(f"/alerts/zz-test-source?token={HOOK}")
        client.get("/zz-test/new-thing", headers=bearer("wrong-key"))
    assert "eval" in caplog.text  # key names are logged
    assert not any(s in caplog.text for s in secrets + ["wrong-key"])


def test_access_log_masks_webhook_token():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "POST", f"/alerts?token={HOOK}&x=1", "1.1", 200), None)
    auth.RedactTokenFilter().filter(rec)
    assert HOOK not in rec.getMessage() and "token=***&x=1" in rec.getMessage()


def test_audit_records_key_name(secured, client, monkeypatch):
    from test_slack import FakeConn, _inv
    conn = FakeConn(_inv())
    monkeypatch.setattr(main.db, "connect", lambda: conn)
    assert client.post("/investigations/12/reject", headers=bearer("wo-secret-789")).status_code == 200
    assert [p for s, p in conn.sql if s.startswith("INSERT INTO audit_log")] == [("bot", "reject", 12)]
    main._confirm(12, who="asha", user_id="U1")
    assert [p for s, p in conn.sql if s.startswith("INSERT INTO audit_log")][-1] == ("slack:asha", "confirm", 12)
