"""The one-page setup: Slack and GitHub connected by clicking (setup.py, slackapp.py, githubapp.py).

The first part needs no database. The flow tests run against a real Postgres and are
skipped unless LOKI_TEST_DATABASE_URL is set (see tests/test_tenancy.py).
"""
import base64
import json
import logging
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import auth
import githubapp
import setup
import slackapp
import tenant

from test_tenancy import DB, OP, alert, needs_db, pg  # noqa: F401  (pg is a fixture)


@pytest.fixture
def app_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_SLUG", "loki-oncall")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem.replace("\n", "\\n"))  # as it sits in an env file
    githubapp._tokens.clear()
    return key


def _b64d(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# ---------------- no database ----------------

def test_app_jwt_is_a_valid_rs256_token_for_the_app(app_key):
    now = 1_800_000_000
    token = githubapp.app_jwt(now)
    header, payload, sig = token.split(".")
    assert json.loads(_b64d(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64d(payload))
    assert claims == {"iat": now - 60, "exp": now + 540, "iss": "12345"}
    app_key.public_key().verify(_b64d(sig), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert githubapp.configured()


def test_installation_tokens_are_fetched_once_and_renewed_before_expiry(app_key):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"].startswith("Bearer ey")  # the app JWT
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        return httpx.Response(201, json={"token": f"ghs_{len(calls)}", "expires_at": exp})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert githubapp.installation_token(77, http) == "ghs_1"
    assert githubapp.installation_token(77, http) == "ghs_1" and len(calls) == 1
    githubapp._tokens[77] = (time.time() + 60, "ghs_1")  # about to expire
    assert githubapp.installation_token(77, http) == "ghs_2"
    assert calls[0].url.path == "/app/installations/77/access_tokens"


def test_slack_exchange_returns_the_workspace_settings(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOKI_PUBLIC_URL", "https://loki.example.com")
    sent = []

    def handler(request):
        sent.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"ok": True, "access_token": "xoxb-1", "team": {"id": "T1", "name": "Acme"},
                                         "incoming_webhook": {"channel_id": "C1", "channel": "#incidents"}})
    got = slackapp.exchange("the-code", httpx.Client(transport=httpx.MockTransport(handler)))
    assert got == {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_CHANNEL": "C1", "SLACK_CHANNEL_NAME": "#incidents",
                   "SLACK_TEAM_ID": "T1", "SLACK_TEAM_NAME": "Acme"}
    assert sent[0] == {"client_id": "cid", "client_secret": "csecret", "code": "the-code",
                       "redirect_uri": "https://loki.example.com/setup/slack/callback"}
    url = slackapp.authorize_url("st")
    assert "scope=chat%3Awrite%2Cincoming-webhook" in url and "state=st" in url
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": False, "error": "invalid_code"})))
    with pytest.raises(slackapp.SlackError, match="invalid_code"):
        slackapp.exchange("x", bad)


def test_state_round_trips_and_rejects_forgery_and_other_tenants(monkeypatch):
    monkeypatch.setenv("LOKI_SECRET_KEY", Fernet.generate_key().decode())
    acme = tenant.Tenant(5, "acme", "Acme")
    monkeypatch.setattr(setup.tenants, "by_setup_token", lambda tok: acme if tok == "lks_good" else None)
    t, tok = setup.read_state(setup.make_state(acme, "lks_good"))
    assert t is acme and tok == "lks_good"
    with pytest.raises(setup.HTTPException) as e:
        setup.read_state("forged")
    assert e.value.status_code == 400
    with pytest.raises(setup.HTTPException):  # the setup link it was made for no longer works
        setup.read_state(setup.make_state(acme, "lks_old"))
    with pytest.raises(setup.HTTPException):  # made for another tenant id
        setup.read_state(setup.make_state(tenant.Tenant(6, "evil", "Evil"), "lks_good"))


def test_page_escapes_everything_and_never_echoes_secrets(monkeypatch):
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    evil = tenant.Tenant(5, "acme", "<script>alert(1)</script>",
                         {"SLACK_BOT_TOKEN": "xoxb-secret", "SLACK_CHANNEL": "C1", "SLACK_CHANNEL_NAME": "<b>x</b>",
                          "DATADOG_API_KEY": "dd-secret", "SENTRY_ORG": "o'rg\" onmouseover=x"})
    page = setup._page(evil, "lks_tok").body.decode()
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page
    assert "<b>x</b>" not in page
    assert "xoxb-secret" not in page and "dd-secret" not in page
    assert "o&#x27;rg&quot; onmouseover=x" in page
    assert setup.HEADERS["Referrer-Policy"] == "no-referrer"


def test_setup_tokens_and_oauth_values_are_masked_in_logs():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s "%s %s"', (
        "1.2.3.4", "GET", "/setup/lks_AbC-123_x/github?x=1"), None)
    auth.RedactTokenFilter().filter(rec)
    assert "lks_AbC" not in rec.getMessage() and "/setup/***/github" in rec.getMessage()
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s', (
        "/setup/slack/callback?code=1234.5678&state=gAAAA",), None)
    auth.RedactTokenFilter().filter(rec)
    assert "1234.5678" not in rec.getMessage() and "gAAAA" not in rec.getMessage()


def test_github_source_uses_the_installation_when_there_is_no_token(app_key):
    from sources.github import GitHubDeploys
    acme = tenant.Tenant(5, "acme", "Acme", {"GITHUB_INSTALLATION_ID": "77", "GITHUB_REPOS": "acme/shop=shop"})
    with tenant.use(acme):
        src = GitHubDeploys.from_env()
    assert isinstance(src.client.auth, githubapp.InstallationAuth) and src.client.auth.installation_id == 77
    assert src.repos == {"acme/shop": "shop"}


# ---------------- the whole flow, real Postgres ----------------

@pytest.fixture
def flow(pg, monkeypatch, app_key):  # noqa: F811
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setenv("LOKI_API_KEYS", "admin:op-key-" + "x" * 30)
    monkeypatch.setenv("LOKI_PUBLIC_URL", "https://loki.example.com")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(main, "GROUP_WINDOW_S", 0)
    monkeypatch.setattr(main.investigator, "investigate", lambda inv_id: None)
    client = TestClient(main.app)
    created = client.post("/tenants", json={"slug": "acme", "name": "Acme"}, headers=OP).json()
    path = created["setup_url"].replace("https://loki.example.com", "")
    return client, created, path


def _state_from(page: str, host: str) -> str:
    import html
    import re
    from urllib.parse import parse_qs, urlparse
    href = html.unescape(re.search(rf"href='({re.escape(host)}[^']+)'", page).group(1))
    return parse_qs(urlparse(href).query)["state"][0]


@needs_db
def test_whole_setup_flow(flow, monkeypatch):
    client, created, path = flow
    assert path.startswith("/setup/lks_")
    page = client.get(path)
    assert page.status_code == 200 and page.headers["referrer-policy"] == "no-referrer"
    assert "Add to Slack" in page.text and "Install GitHub App" in page.text

    # Slack: the state from the page's button comes back with a code
    monkeypatch.setattr(setup.slackapp, "exchange", lambda code: {
        "SLACK_BOT_TOKEN": "xoxb-acme", "SLACK_CHANNEL": "C9", "SLACK_CHANNEL_NAME": "#incidents",
        "SLACK_TEAM_ID": "T9", "SLACK_TEAM_NAME": "Acme"} if code == "good" else None)
    state = _state_from(page.text, "https://slack.com")
    r = client.get(f"/setup/slack/callback?code=good&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"{path}?slack=connected"
    assert "reports go to <b>#incidents</b> in Acme" in client.get(path).text

    # GitHub: install, then map repos to services
    state = _state_from(client.get(path).text, "https://github.com")
    r = client.get(f"/setup/github/callback?installation_id=4242&setup_action=install&state={state}", follow_redirects=False)
    assert r.status_code == 303
    monkeypatch.setattr(setup.githubapp, "installation_repos", lambda i: ["acme/shop", "acme/payments"])
    page = client.get(f"{path}?github=installed").text
    assert "acme/payments" in page and "Now confirm which service" in page
    r = client.post(f"{path}/github", content="svc%3Aacme%2Fshop=shop-api&svc%3Aacme%2Fpayments=",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert "Saved: 1 repository" in r.text

    import tenants
    t = tenants.get(created["id"])
    assert t.settings["GITHUB_INSTALLATION_ID"] == "4242" and t.settings["GITHUB_REPOS"] == "acme/shop=shop-api"
    assert t.settings["LOKI_DEPLOYS_SOURCE"] == "github" and t.settings["SLACK_TEAM_ID"] == "T9"
    assert tenants.by_slack_team("T9").id == created["id"]  # Slack button clicks will route here

    # Alerts: fresh webhook URLs that work, and the old token stops
    r = client.post(f"{path}/webhook")
    url = next(line for line in r.text.split("<code>") if "/alerts/generic?token=" in line).split("</code>")[0]
    token = url.split("token=")[1]
    assert client.post(f"/alerts/generic?token={token}", json=alert("HighErrorRate", "shop-api")).status_code == 200
    assert client.post(f"/alerts/generic?token={created['webhook_token']}", json=alert("X", "y")).status_code == 401
    assert len(client.get("/incidents", headers={**OP, "X-Loki-Tenant": "acme"}).json()) == 1


@needs_db
def test_setup_links_are_scoped_and_expire(flow):
    client, created, path = flow
    token = path.split("/")[2]
    assert client.get("/setup/lks_not-a-real-token").status_code == 404
    # the setup token is not an API key and opens nothing outside /setup
    assert client.get("/incidents", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    # forged or replayed-elsewhere state is refused
    assert client.get("/setup/slack/callback?code=x&state=forged").status_code == 400
    # a new link replaces the old one
    new = client.post(f"/tenants/{created['id']}/setup-link", headers=OP).json()["setup_url"]
    assert client.get(path).status_code == 404 and new.endswith("/setup/" + new.split("/setup/")[1])
    # expiry
    import db
    with db.connect(admin=True) as c:
        c.execute("UPDATE tenants SET setup_expires_at = now() - interval '1 minute' WHERE id = %s", (created["id"],))
    assert client.get(new.replace("https://loki.example.com", "")).status_code == 404
    # tenants can't mint setup links; the default tenant has none
    assert client.post(f"/tenants/{created['id']}/setup-link",
                       headers={"Authorization": f"Bearer {created['api_key']}"}).status_code == 403
    assert client.post("/tenants/1/setup-link", headers=OP).status_code == 400
