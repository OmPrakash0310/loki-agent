"""The one-page setup a customer opens from the link the operator sends them.

    /setup/<token>                  the page: connect Slack and GitHub by clicking, get alert URLs
    /setup/slack/callback           Slack's consent screen comes back here
    /setup/github/callback          GitHub's install screen comes back here (the App's "Setup URL")

The token in the link (tenants.new_setup_link: hashed, expires after 7 days) is the
customer's credential for this page only; auth.identify turns it into their tenant.
What leaves for Slack or GitHub is an encrypted, 30-minute `state` carrying the tenant
and the setup token, so a callback can only attach a workspace or an installation to
the tenant whose page started it. The page sends no referrer (its URL holds the token),
can't be framed or cached, and runs no JavaScript.
"""
import html
import json
import os
import re
import secrets
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import githubapp
import slackapp
import tenant
import tenants

router = APIRouter()
STATE_TTL_S = 1800
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}
ALERT_PATHS = [("Prometheus Alertmanager", "/alerts"), ("Grafana", "/alerts/grafana"), ("Sentry", "/alerts/sentry"),
               ("Datadog", "/alerts/datadog"), ("PagerDuty", "/alerts/pagerduty"), ("Anything else (JSON)", "/alerts/generic")]
# Optional integrations the page offers as a form: (setting, label, is a secret).
FIELDS = {
    "Sentry (errors)": [("SENTRY_ORG", "Organization slug", False), ("SENTRY_PROJECTS", "Projects: project=service, ...", False),
                        ("SENTRY_TOKEN", "Auth token (project:read, event:read)", True),
                        ("SENTRY_WEBHOOK_SECRET", "Webhook client secret", True)],
    "Datadog (logs)": [("DATADOG_SITE", "Site, e.g. datadoghq.com or datadoghq.eu", False),
                       ("DATADOG_API_KEY", "API key", True), ("DATADOG_APP_KEY", "Application key (logs_read_data)", True)],
    "Prometheus (metrics)": [("LOKI_PROM_URL", "Query URL", False), ("LOKI_PROM_BEARER_TOKEN", "Bearer token, if needed", True)],
    "Grafana Loki (logs)": [("LOKI_URL", "Base URL", False), ("LOKI_LOGS_BEARER_TOKEN", "Bearer token, if needed", True)],
}
_FIELD_NAMES = {name for group in FIELDS.values() for name, _, _ in group}


# ---------- state that leaves for Slack/GitHub and comes back ----------

def make_state(t: tenant.Tenant, setup_token: str) -> str:
    return tenant._fernet().encrypt(json.dumps({"t": t.id, "s": setup_token, "n": secrets.token_hex(8)}).encode()).decode()


def read_state(state: str) -> tuple[tenant.Tenant, str]:
    """The tenant and setup token a state was made for. HTTPException(400) if forged,
    expired, or the setup link no longer works."""
    from cryptography.fernet import InvalidToken
    try:
        data = json.loads(tenant._fernet().decrypt(state.encode(), ttl=STATE_TTL_S))
    except (InvalidToken, ValueError, TypeError):
        raise HTTPException(400, "this link expired or wasn't made by Loki: start again from your setup page")
    t = tenants.by_setup_token(data.get("s", ""))
    if t is None or t.id != data.get("t"):
        raise HTTPException(400, "your setup link expired: ask for a new one")
    return t, data["s"]


def _setup_identity(request: Request) -> tenant.Tenant:
    identity = getattr(request.state, "identity", None)
    if not getattr(identity, "actor", "").startswith("setup:"):
        raise HTTPException(404, "setup link not found or expired")
    return tenant.current()


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {k: v[-1].strip() for k, v in parse_qs(body, keep_blank_values=True).items()}


def _page(t: tenant.Tenant, token: str, notice: str = "", error: str = "", urls: list | None = None) -> HTMLResponse:
    e = html.escape
    base = f"/setup/{e(token)}"
    s = t.settings
    sections = []

    # 1. Slack
    if s.get("SLACK_BOT_TOKEN") and s.get("SLACK_CHANNEL"):
        where = f"#{e(s.get('SLACK_CHANNEL_NAME', '').lstrip('#') or s['SLACK_CHANNEL'])}"
        team = f" in {e(s['SLACK_TEAM_NAME'])}" if s.get("SLACK_TEAM_NAME") else ""
        body = (f"<p class=ok>✓ Connected: reports go to <b>{where}</b>{team}.</p>"
                f"<form method=post action='{base}/test-slack'><button>Send a test message</button></form>"
                + (f"<p><a class=btn2 href='{e(slackapp.authorize_url(make_state(t, token)))}'>Choose another channel</a></p>"
                   if slackapp.configured() else ""))
    elif slackapp.configured():
        body = (f"<p>Loki posts each root-cause report to a channel you choose.</p>"
                f"<p><a class=btn href='{e(slackapp.authorize_url(make_state(t, token)))}'>Add to Slack</a></p>")
    else:
        body = "<p class=muted>Slack connection isn't set up on this server yet. Your Loki operator can add it for you.</p>"
    sections.append(("1. Slack", body))

    # 2. GitHub
    installation = s.get("GITHUB_INSTALLATION_ID")
    if installation:
        current = {}
        for item in (s.get("GITHUB_REPOS") or "").split(","):
            repo, _, svc = item.strip().partition("=")
            if repo:
                current[repo] = svc or repo.rsplit("/", 1)[-1]
        try:
            repos = githubapp.installation_repos(int(installation)) if githubapp.configured() else sorted(current)
            rows = "".join(
                f"<tr><td>{e(r)}</td><td><input name='svc:{e(r)}' value='{e(current.get(r, r.rsplit('/', 1)[-1]))}' "
                f"maxlength=60></td></tr>" for r in repos)
            body = ("<p class=ok>✓ Installed.</p><p>Which service does each repository deploy? "
                    "Use the names your alerts use. Clear a name to leave that repository out.</p>"
                    f"<form method=post action='{base}/github'><table><tr><th>Repository</th><th>Service</th></tr>{rows}"
                    "</table><button>Save</button></form>") if repos else \
                   "<p>Installed, but no repositories are shared with Loki yet. Add some in GitHub's settings.</p>"
        except Exception as ex:  # noqa: BLE001 - the page must still render
            body = f"<p class=err>Couldn't list your repositories ({e(type(ex).__name__)}). Try reloading.</p>"
        if githubapp.configured():
            body += f"<p><a class=btn2 href='{e(githubapp.install_url(make_state(t, token)))}'>Change repositories</a></p>"
    elif githubapp.configured():
        body = ("<p>Loki reads recent deploys and their code changes (read-only) to find which change broke things.</p>"
                f"<p><a class=btn href='{e(githubapp.install_url(make_state(t, token)))}'>Install GitHub App</a></p>")
    else:
        body = "<p class=muted>GitHub connection isn't set up on this server yet. Your Loki operator can add it for you.</p>"
    sections.append(("2. GitHub", body))

    # 3. Alerts
    if urls:
        rows = "".join(f"<tr><td>{e(n)}</td><td><code>{e(u)}</code></td></tr>" for n, u in urls)
        body = ("<p class=ok>✓ Copy these now: they are shown once. Paste the one for your alerting tool as its webhook URL.</p>"
                f"<table>{rows}</table>")
    else:
        body = ("<p>Your alerting tool tells Loki when something breaks, through a webhook URL with your own token.</p>"
                f"<form method=post action='{base}/webhook'><button>Show my webhook URLs</button></form>"
                "<p class=muted>Each time you press this, a new token is made and the previous URLs stop working.</p>")
    sections.append(("3. Alerts", body))

    # 4. Optional integrations
    groups = []
    for title, fields in FIELDS.items():
        inputs = "".join(
            f"<label>{e(label)}{' <span class=muted>(set)</span>' if s.get(name) else ''}"
            f"<input name='{name}' {'type=password autocomplete=off' if secret else ''} "
            f"value='{'' if secret else e(s.get(name, ''))}' placeholder='{'leave empty to keep' if secret and s.get(name) else ''}'></label>"
            for name, label, secret in fields)
        groups.append(f"<fieldset><legend>{e(title)}</legend>{inputs}</fieldset>")
    sections.append(("4. More data (optional)",
                     "<p>Loki works with Slack and GitHub alone. These give it more evidence.</p>"
                     f"<form method=post action='{base}/integrations'>{''.join(groups)}<button>Save</button></form>"))

    msg = (f"<p class=ok>{e(notice)}</p>" if notice else "") + (f"<p class=err>{e(error)}</p>" if error else "")
    body = "".join(f"<section><h2>{h}</h2>{b}</section>" for h, b in sections)
    page = f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=referrer content=no-referrer><title>Set up Loki · {e(t.name)}</title>
<style>
body{{font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;background:#fafaf7;color:#1f2937}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 64px}} h1{{font-size:24px;margin:8px 0 4px}}
section{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 20px;margin:16px 0}}
h2{{font-size:17px;margin:0 0 8px}} .btn,.btn2,button{{display:inline-block;border-radius:8px;padding:9px 16px;font-weight:600;
text-decoration:none;border:0;cursor:pointer;font-size:14px}} .btn,button{{background:#12161C;color:#fff}}
.btn2{{background:#f3f4f6;color:#12161C}} .ok{{color:#166534}} .err{{color:#b91c1c}} .muted{{color:#6b7280}}
table{{border-collapse:collapse;width:100%;margin:8px 0}} td,th{{text-align:left;padding:6px 4px;border-bottom:1px solid #f3f4f6;
vertical-align:top}} code{{word-break:break-all;font-size:13px}} input{{width:100%;box-sizing:border-box;padding:7px;
border:1px solid #d1d5db;border-radius:6px;font:inherit}} label{{display:block;margin:6px 0}}
fieldset{{border:1px solid #f3f4f6;border-radius:8px;margin:10px 0;padding:8px 12px}} legend{{font-weight:600}}
.badge{{display:inline-block;background:#F7B801;color:#12161C;font-weight:700;border-radius:8px;padding:2px 8px}}
</style><main><p><span class=badge>Loki</span></p><h1>Set up Loki for {e(t.name)}</h1>
<p class=muted>An AI on-call engineer: when an alert fires, it finds the root cause and posts it in Slack with the
evidence. Read-only: it never changes your systems.</p>{msg}{body}</main></html>"""
    return HTMLResponse(page, headers=HEADERS)


# ---------- routes ----------

@router.get("/setup/slack/callback", include_in_schema=False)
def slack_callback(state: str = "", code: str = "", error: str = ""):
    t, token = read_state(state)
    if error or not code:
        return RedirectResponse(f"/setup/{token}?slack=cancelled", status_code=303, headers=HEADERS)
    try:
        values = slackapp.exchange(code)
    except slackapp.SlackError as e:
        with tenant.use(t):
            return _page(t, token, error=f"Slack wasn't connected: {e}")
    tenants.update_settings(t.id, values)
    return RedirectResponse(f"/setup/{token}?slack=connected", status_code=303, headers=HEADERS)


@router.get("/setup/github/callback", include_in_schema=False)
def github_callback(state: str = "", installation_id: str = "", setup_action: str = ""):
    t, token = read_state(state)
    if not re.fullmatch(r"[0-9]{1,15}", installation_id or ""):
        return RedirectResponse(f"/setup/{token}?github=cancelled", status_code=303, headers=HEADERS)
    tenants.update_settings(t.id, {"GITHUB_INSTALLATION_ID": installation_id})
    return RedirectResponse(f"/setup/{token}?github=installed", status_code=303, headers=HEADERS)


NOTICES = {("slack", "connected"): "Slack is connected.", ("slack", "cancelled"): "Slack wasn't connected.",
           ("github", "installed"): "GitHub App installed. Now confirm which service each repository deploys.",
           ("github", "cancelled"): "GitHub wasn't connected."}


@router.get("/setup/{token}", include_in_schema=False)
def setup_page(token: str, request: Request):
    t = _setup_identity(request)
    q = request.query_params
    notice = next((m for (k, v), m in NOTICES.items() if q.get(k) == v), "")
    return _page(t, token, notice=notice)


@router.post("/setup/{token}/github", include_in_schema=False)
async def setup_github(token: str, request: Request):
    t = _setup_identity(request)
    form = await _form(request)
    mapping = []
    for key, svc in form.items():
        if not key.startswith("svc:") or not svc:
            continue
        repo = key[4:]
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[\w.:/-]{1,60}", svc):
            return _page(t, token, error=f"Service names may use letters, digits and . _ - : / only ({svc!r}).")
        mapping.append(f"{repo}={svc}")
    tenants.update_settings(t.id, {"GITHUB_REPOS": ",".join(mapping), "LOKI_DEPLOYS_SOURCE": "github" if mapping else ""})
    return _page(tenants.get(t.id), token, notice=f"Saved: {len(mapping)} repositor{'y' if len(mapping) == 1 else 'ies'}.")


@router.post("/setup/{token}/integrations", include_in_schema=False)
async def setup_integrations(token: str, request: Request):
    t = _setup_identity(request)
    form = await _form(request)
    values = {}
    for name in _FIELD_NAMES:
        if name not in form:
            continue
        if tenant.is_secret(name) and not form[name]:
            continue  # an empty secret field means "keep what's there"
        values[name] = form[name]
    if values.get("SENTRY_TOKEN") or (form.get("SENTRY_ORG") and t.settings.get("SENTRY_TOKEN")):
        values["LOKI_ERRORS_SOURCE"] = "sentry"
    elif values.get("DATADOG_API_KEY") or (form.get("DATADOG_SITE") and t.settings.get("DATADOG_API_KEY")):
        values["LOKI_ERRORS_SOURCE"] = "datadog"
    try:
        tenants.update_settings(t.id, values)
    except (ValueError, tenant.SecretKeyMissing) as e:
        return _page(t, token, error=str(e))
    return _page(tenants.get(t.id), token, notice="Saved.")


@router.post("/setup/{token}/webhook", include_in_schema=False)
def setup_webhook(token: str, request: Request):
    t = _setup_identity(request)
    new = tenants.rotate(t.id, "webhook_token")
    base = os.environ.get("LOKI_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    return _page(t, token, urls=[(name, f"{base}{path}?token={new}") for name, path in ALERT_PATHS])


@router.post("/setup/{token}/test-slack", include_in_schema=False)
def setup_test_slack(token: str, request: Request):
    t = _setup_identity(request)
    problem = slackapp.post_test_message(t.settings.get("SLACK_BOT_TOKEN", ""), t.settings.get("SLACK_CHANNEL", ""))
    return _page(t, token, notice="" if problem else "Test message sent: check your channel.", error=problem or "")
