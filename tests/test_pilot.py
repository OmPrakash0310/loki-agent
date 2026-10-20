"""Pilot package: loki doctor, /health/ready, operator failure alerts, env template."""
import os
import re
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

import doctor
import selfmon

ROOT = Path(__file__).resolve().parent.parent
GOOD = {
    "DATABASE_URL": "postgresql://x/y",
    "LOKI_API_KEYS": "admin:" + "a" * 64 + ",mcp:" + "b" * 64 + ":read",
    "LOKI_WEBHOOK_TOKEN": "t" * 64,
    "LOKI_PUBLIC_URL": "https://loki.acme.test",
    "LOKI_MODEL": "claude-haiku-4-5",
    "LOKI_REQUIRE_AUTH": "1",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "LOKI_PROM_URL": "http://prom:9090",
    "LOKI_URL": "http://grafana-loki:3100",
    "LOKI_DEPLOYS_SOURCE": "github",
    "GITHUB_TOKEN": "github_pat_x",
    "GITHUB_REPOS": "acme/shop=shop",
    "LOKI_ERRORS_SOURCE": "sentry",
    "SENTRY_TOKEN": "sntrys_x",
    "SENTRY_ORG": "acme",
    "SENTRY_PROJECTS": "shop=shop",
    "SLACK_BOT_TOKEN": "xoxb-x",
    "SLACK_CHANNEL": "C123",
    "SLACK_SIGNING_SECRET": "s",
    "LOKI_OPERATOR_SLACK_WEBHOOK": "https://hooks.slack.com/services/T/B/x",
    "LOKI_EMBEDDINGS": "local",
}
ENV_NAMES = set(GOOD) | {"PROM_URL", "LOKI_VERIFY", "LOKI_PROBES", "SLACK_WEBHOOK_URL", "LOKI_OLLAMA_URL",
                         "GITHUB_API_URL", "SENTRY_BASE_URL",
                         "DATADOG_API_KEY", "DATADOG_APP_KEY", "DATADOG_TOKEN", "DATADOG_SITE", "DATADOG_BASE_URL",
                         "LOKI_EMBEDDING_CACHE"}


@pytest.fixture
def env(monkeypatch):
    for k in ENV_NAMES:
        monkeypatch.delenv(k, raising=False)
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


class FakeConn:
    def __init__(self, n=0, fail=False):
        self.n, self.fail, self.sql = n, fail, []

    def execute(self, sql, params=None):
        if self.fail:
            raise OSError("down")
        self.sql.append((sql, params))
        return self

    def fetchone(self):
        return {"n": self.n, "?column?": 1}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def handler_factory(overrides=None, calls=None):
    overrides = overrides or {}

    def handler(request: httpx.Request):
        url = str(request.url)
        if calls is not None:
            calls.append((request.method, url))
        for frag, resp in overrides.items():
            if frag in url:
                return resp
        if "slack.com/api/auth.test" in url:
            return httpx.Response(200, json={"ok": True, "team": "Acme"})
        return httpx.Response(200, json={"status": "success", "data": []})
    return handler


def run(overrides=None, calls=None):
    client = httpx.Client(transport=httpx.MockTransport(handler_factory(overrides, calls)))
    return {r.name: r for r in doctor.run(client=client, connect=lambda: FakeConn())}


# ---------------- doctor ----------------

def test_doctor_all_green(env):
    calls = []
    res = run(calls=calls)
    # Datadog is an alternative to Sentry/Grafana Loki: its warning is expected when it isn't used.
    bad = {n: r for n, r in res.items() if r.status != doctor.OK and n != "datadog"}
    assert not bad, bad
    assert res["datadog"].status == doctor.WARN
    # Read-only: only GETs, plus Slack's auth.test.
    assert all(m == "GET" or "auth.test" in u for m, u in calls)
    assert any("api.anthropic.com/v1/models" in u for _, u in calls)  # the free models list, no messages call
    assert not any("/v1/messages" in u for _, u in calls)


def test_doctor_main_exit_codes(env, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run", lambda offline=False: [doctor.Result(doctor.OK, "a", "")])
    assert doctor.main([]) == 0
    monkeypatch.setattr(doctor, "run", lambda offline=False: [doctor.Result(doctor.WARN, "a", "")])
    assert doctor.main([]) == 0
    monkeypatch.setattr(doctor, "run", lambda offline=False: [doctor.Result(doctor.FAIL, "a", "x")])
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "❌" in out and "Webhook URLs" in out


def test_doctor_missing_required_and_auth(env):
    env.delenv("LOKI_API_KEYS")
    env.setenv("LOKI_REQUIRE_AUTH", "0")
    res = run()
    assert res["env LOKI_API_KEYS"].status == doctor.FAIL
    assert res["auth keys"].status == doctor.FAIL
    assert res["LOKI_REQUIRE_AUTH"].status == doctor.FAIL


def test_doctor_malformed_and_short_keys(env):
    env.setenv("LOKI_API_KEYS", "a:b:c:d")
    assert doctor.check_auth()[0].status == doctor.FAIL
    env.setenv("LOKI_API_KEYS", "admin:short")
    assert doctor.check_auth()[0].status == doctor.WARN


def test_doctor_database_down(env):
    def boom():
        raise OSError("connection refused")
    assert doctor.check_database(boom).status == doctor.FAIL


def test_doctor_unauthorized_sources(env):
    res = run({
        "prom:9090": httpx.Response(401),
        "api.github.com/repos/acme/shop": httpx.Response(404),
        "sentry.io": httpx.Response(403),
        "auth.test": httpx.Response(200, json={"ok": False, "error": "invalid_auth"}),
        "anthropic.com": httpx.Response(401),
    })
    assert res["prometheus"].status == doctor.FAIL and "unauthorized" in res["prometheus"].detail
    assert res["github acme/shop"].status == doctor.FAIL
    assert res["sentry shop"].status == doctor.FAIL
    assert res["slack bot"].status == doctor.FAIL and "invalid_auth" in res["slack bot"].detail
    assert res["model"].status == doctor.FAIL


def test_doctor_unreachable_source(env):
    def handler(request):
        if "grafana-loki" in str(request.url):
            raise httpx.ConnectError("no route")
        return handler_factory()(request)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = {r.name: r for r in doctor.run(client=client, connect=lambda: FakeConn())}
    assert res["grafana loki"].status == doctor.FAIL and "unreachable" in res["grafana loki"].detail


def test_doctor_classic_github_token_with_write_scope_warns(env):
    res = run({"rate_limit": httpx.Response(200, headers={"X-OAuth-Scopes": "repo, read:org"}, json={})})
    assert res["github token"].status == doctor.WARN


def test_doctor_unconfigured_sources_warn_not_fail(env):
    for k in ("LOKI_PROM_URL", "LOKI_URL", "GITHUB_TOKEN", "SENTRY_TOKEN", "SLACK_BOT_TOKEN",
              "LOKI_DEPLOYS_SOURCE", "LOKI_ERRORS_SOURCE"):
        env.delenv(k)
    env.setenv("LOKI_PROM_URL", "")
    env.setenv("LOKI_ERRORS_SOURCE", "sentry")
    res = run()
    for name in ("prometheus", "grafana loki", "github", "slack"):
        assert res[name].status == doctor.WARN, name
    assert res["sentry"].status == doctor.FAIL  # errors source sentry without a token


def test_doctor_model_fake_and_offline(env):
    env.setenv("LOKI_MODEL", "fake")
    assert doctor.check_model(None, False).status == doctor.WARN
    env.setenv("LOKI_MODEL", "claude-haiku-4-5")
    assert doctor.check_model(None, True).status == doctor.OK
    env.delenv("ANTHROPIC_API_KEY")
    assert doctor.check_model(None, True).status == doctor.FAIL


def test_doctor_offline_makes_no_http_calls(env):
    res = doctor.run(offline=True, client=None, connect=lambda: FakeConn())
    names = {r.name for r in res}
    assert "prometheus" not in names and "database" in names


def test_doctor_webhook_urls_mask_token(env):
    masked = "\n".join(doctor.webhook_urls(False))
    assert "t" * 64 not in masked and "https://loki.acme.test/alerts/sentry?token=tttt…" in masked
    assert "t" * 64 in "\n".join(doctor.webhook_urls(True))


# ---------------- readiness ----------------

def test_readiness(monkeypatch):
    monkeypatch.setattr(selfmon.db, "connect", lambda: FakeConn())
    monkeypatch.setitem(selfmon._state, "beat", 1000.0)
    assert selfmon.readiness(now=1010.0) == (True, {"database": True, "worker": True})
    assert selfmon.readiness(now=1000.0 + selfmon.HEARTBEAT_STALE_S + 1)[0] is False
    monkeypatch.setattr(selfmon.db, "connect", lambda: FakeConn(fail=True))
    assert selfmon.readiness(now=1010.0) == (False, {"database": False, "worker": True})
    monkeypatch.setitem(selfmon._state, "beat", 0.0)
    assert selfmon.readiness()[1]["worker"] is False


def test_health_ready_endpoint_public_and_dataless(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setenv("LOKI_API_KEYS", "admin:" + "a" * 40)
    client = TestClient(main.app)  # no `with`: startup (DB) doesn't run
    monkeypatch.setattr(main.selfmon, "readiness", lambda: (True, {"database": True, "worker": True}))
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json() == {"ok": True, "database": True, "worker": True}
    monkeypatch.setattr(main.selfmon, "readiness", lambda: (False, {"database": False, "worker": True}))
    r = client.get("/health/ready")
    assert r.status_code == 503 and set(r.json()) == {"ok", "database", "worker"}
    assert client.get("/incidents").status_code == 401  # everything else still needs a key


# ---------------- operator failure alerts ----------------

@pytest.fixture
def alerts(monkeypatch):
    monkeypatch.setenv("LOKI_OPERATOR_SLACK_WEBHOOK", "https://hooks.slack.com/services/T/B/x")
    monkeypatch.setenv("LOKI_FAILURE_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("LOKI_FAILURE_ALERT_WINDOW_MIN", "30")
    monkeypatch.setenv("LOKI_FAILURE_ALERT_COOLDOWN_MIN", "60")
    monkeypatch.setitem(selfmon._state, "last_alert", 0.0)
    sent = []
    return sent, (lambda url, json, timeout: sent.append((url, json)))


def test_failure_alert_below_threshold(alerts):
    sent, post = alerts
    assert selfmon.check_failures(FakeConn(n=2), now=10_000, post=post) is False
    assert not sent


def test_failure_alert_fires_once_then_cooldown(alerts):
    sent, post = alerts
    conn = FakeConn(n=3)
    assert selfmon.check_failures(conn, now=10_000, post=post) is True
    assert len(sent) == 1 and "3 investigations failed in the last 30 min" in sent[0][1]["text"]
    assert conn.sql[0][1] == (30,)  # window passed as a parameter
    assert selfmon.check_failures(FakeConn(n=9), now=10_000 + 59 * 60, post=post) is False
    assert selfmon.check_failures(FakeConn(n=9), now=10_000 + 61 * 60, post=post) is True
    assert len(sent) == 2


def test_failure_alert_off_without_webhook(alerts, monkeypatch):
    sent, post = alerts
    monkeypatch.delenv("LOKI_OPERATOR_SLACK_WEBHOOK")
    assert selfmon.check_failures(FakeConn(n=99), now=10_000, post=post) is False


def test_failure_alert_post_error_does_not_raise(alerts):
    def bad(*a, **k):
        raise httpx.ConnectError("x")
    assert selfmon.check_failures(FakeConn(n=5), now=10_000, post=bad) is False
    assert selfmon._state["last_alert"] == 0.0  # retried next tick


# ---------------- env template ----------------

def _template_vars():
    text = (ROOT / "deploy" / ".env.template").read_text()
    return set(re.findall(r"^(?:# )?([A-Z][A-Z0-9_]*)=", text, re.M))


def test_every_loki_var_in_agent_is_in_env_template():
    used = set()
    for path in (ROOT / "agent").rglob("*.py"):
        used |= set(re.findall(r"\bLOKI_[A-Z0-9_]+\b", path.read_text()))
    missing = sorted(used - _template_vars())
    assert not missing, f"add to deploy/.env.template: {missing}"


def test_env_template_covers_compose_and_integrations():
    tpl = _template_vars()
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text()
    needed = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose)) - {"COMPOSE_PROJECT_NAME"}
    needed |= {"ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITHUB_REPOS", "SENTRY_TOKEN", "SENTRY_ORG",
               "SENTRY_PROJECTS", "SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_SIGNING_SECRET"}
    assert not sorted(needed - tpl)


def test_env_template_has_no_inline_comments_or_secrets():
    for line in (ROOT / "deploy" / ".env.template").read_text().splitlines():
        if re.match(r"^[A-Z]", line):
            assert "#" not in line, line  # env_file parsers disagree on inline comments
            key, _, val = line.partition("=")
            if key in ("POSTGRES_PASSWORD", "LOKI_API_KEYS", "LOKI_WEBHOOK_TOKEN", "LOKI_PROBE_TOKEN",
                       "LOKI_LINK_SECRET", "ANTHROPIC_API_KEY"):
                assert val == "", key


# ---------------- no Grafana Loki ----------------

def test_fetch_logs_without_grafana_loki_returns_nothing(monkeypatch):
    from datetime import datetime, timedelta, timezone
    import telemetry
    monkeypatch.setattr(telemetry, "LOKI_URL", "")

    def no_http(*a, **k):
        raise AssertionError("must not call Grafana Loki when LOKI_URL is empty")
    monkeypatch.setattr(telemetry.httpx, "get", no_http)
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert telemetry.fetch_logs(None, end - timedelta(minutes=10), end, errors_only=True) == []
