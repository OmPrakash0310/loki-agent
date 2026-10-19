"""loki doctor: check a Loki deployment's setup with read-only calls.

Run inside the container:   docker compose exec agent python doctor.py
or from a checkout:         python scripts/loki_doctor.py

Exit code 1 when any check fails (❌). Warnings (⚠️) don't fail.
No paid endpoint is called: the model check lists models (free) or only checks
the key is present with --offline.
"""
import argparse
import os
import sys
from dataclasses import dataclass

import httpx

from sources import httpkit

OK, WARN, FAIL = "✅", "⚠️", "❌"


@dataclass
class Result:
    status: str
    name: str
    detail: str


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _truthy(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes")


def check_required() -> list[Result]:
    out = []
    for name in ("DATABASE_URL", "LOKI_API_KEYS", "LOKI_WEBHOOK_TOKEN", "LOKI_PUBLIC_URL", "LOKI_MODEL"):
        out.append(Result(OK, f"env {name}", "set") if _env(name) else Result(FAIL, f"env {name}", "missing"))
    return out


def check_auth() -> list[Result]:
    import auth  # agent/auth.py
    out = []
    try:
        keys = auth.parse_keys(_env("LOKI_API_KEYS"))
    except ValueError as e:
        return [Result(FAIL, "auth keys", str(e))]
    if not keys:
        out.append(Result(FAIL, "auth keys", "LOKI_API_KEYS empty: every endpoint is open"))
    else:
        weak = [k.name for k in keys if len(k.secret) < 32]
        out.append(Result(WARN, "auth keys", f"short keys (<32 chars): {', '.join(weak)}") if weak
                   else Result(OK, "auth keys", f"{len(keys)} key(s): {', '.join(k.name for k in keys)}"))
    out.append(Result(OK, "LOKI_REQUIRE_AUTH", "1") if _truthy("LOKI_REQUIRE_AUTH")
               else Result(FAIL, "LOKI_REQUIRE_AUTH", "not 1: Loki would start open if keys go missing"))
    tok = _env("LOKI_WEBHOOK_TOKEN")
    if tok and len(tok) < 32:
        out.append(Result(WARN, "webhook token", "shorter than 32 chars"))
    for flag in ("LOKI_VERIFY", "LOKI_PROBES"):
        if _truthy(flag):
            out.append(Result(WARN, flag, "on: not recommended for a pilot"))
    return out


def check_database(connect=None) -> Result:
    try:
        if connect is None:
            import db
            connect = db.connect
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return Result(OK, "database", "reachable")
    except Exception as e:  # noqa: BLE001
        return Result(FAIL, "database", f"unreachable: {type(e).__name__}")


def _get(client, name, url, **kw) -> tuple[httpx.Response | None, Result | None]:
    try:
        r = client.get(url, **kw)
    except httpx.HTTPError as e:
        return None, Result(FAIL, name, f"unreachable: {type(e).__name__}")
    if r.status_code in (401, 403):
        return r, Result(FAIL, name, f"unauthorized (HTTP {r.status_code})")
    if r.status_code >= 400:
        return r, Result(FAIL, name, f"HTTP {r.status_code}")
    return r, None


def check_prometheus(client) -> Result:
    url = _env("LOKI_PROM_URL") if "LOKI_PROM_URL" in os.environ else _env("PROM_URL")
    if not url or url.lower() in ("off", "none"):
        return Result(WARN, "prometheus", "not configured (briefings use logs, errors, deploys only)")
    _, err = _get(client, "prometheus", f"{url.rstrip('/')}/api/v1/query", params={"query": "1"},
                  headers=httpkit.auth_headers("PROM"))
    return err or Result(OK, "prometheus", url)


def check_grafana_loki(client) -> Result:
    url = _env("LOKI_URL")
    if not url:
        if (_env("LOKI_ERRORS_SOURCE") or "loki") == "loki":
            return Result(WARN, "grafana loki", "LOKI_URL not set but errors source is loki")
        return Result(WARN, "grafana loki", "not configured")
    _, err = _get(client, "grafana loki", f"{url.rstrip('/')}/loki/api/v1/labels",
                  headers=httpkit.auth_headers("LOGS"))
    return err or Result(OK, "grafana loki", url)


def check_github(client) -> list[Result]:
    token, repos = _env("GITHUB_TOKEN"), _env("GITHUB_REPOS")
    if not token:
        if _env("LOKI_DEPLOYS_SOURCE") == "github":
            return [Result(FAIL, "github", "LOKI_DEPLOYS_SOURCE=github but GITHUB_TOKEN is empty")]
        return [Result(WARN, "github", "not configured (no deploy history)")]
    api = (_env("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    out = []
    r, err = _get(client, "github token", f"{api}/rate_limit", headers=headers)
    if err:
        return [err]
    scopes = r.headers.get("X-OAuth-Scopes")
    if scopes is None:
        out.append(Result(OK, "github token", "fine-grained token"))
    elif "repo" in [s.strip() for s in scopes.split(",")]:
        out.append(Result(WARN, "github token", "classic token with 'repo' scope (grants write); use fine-grained read-only"))
    else:
        out.append(Result(OK, "github token", f"classic token, scopes: {scopes or 'none'}"))
    if not repos:
        out.append(Result(FAIL, "github repos", "GITHUB_REPOS is empty"))
    for item in filter(None, (p.strip() for p in repos.split(","))):
        repo = item.split("=")[0].strip()
        _, err = _get(client, f"github {repo}", f"{api}/repos/{repo}", headers=headers)
        out.append(err or Result(OK, f"github {repo}", "readable"))
    return out


def check_sentry(client) -> list[Result]:
    token, org = _env("SENTRY_TOKEN"), _env("SENTRY_ORG")
    if not token:
        if _env("LOKI_ERRORS_SOURCE") == "sentry":
            return [Result(FAIL, "sentry", "LOKI_ERRORS_SOURCE=sentry but SENTRY_TOKEN is empty")]
        return [Result(WARN, "sentry", "not configured")]
    if not org:
        return [Result(FAIL, "sentry", "SENTRY_ORG is empty")]
    base = (_env("SENTRY_BASE_URL") or "https://sentry.io").rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    out = []
    for item in [p.strip() for p in _env("SENTRY_PROJECTS").split(",") if p.strip()] or [None]:
        if item is None:
            _, err = _get(client, "sentry org", f"{base}/api/0/organizations/{org}/projects/", headers=headers)
            out.append(err or Result(OK, "sentry org", f"{org} readable"))
            out.append(Result(FAIL, "sentry projects", "SENTRY_PROJECTS is empty"))
            break
        slug = item.split("=")[0].strip()
        _, err = _get(client, f"sentry {slug}", f"{base}/api/0/projects/{org}/{slug}/", headers=headers)
        out.append(err or Result(OK, f"sentry {slug}", "readable"))
    return out


def check_slack(client) -> list[Result]:
    out = []
    token, channel = _env("SLACK_BOT_TOKEN"), _env("SLACK_CHANNEL")
    if token:
        try:
            r = client.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {token}"})
            body = r.json()
            out.append(Result(OK, "slack bot", f"team {body.get('team', '?')}") if body.get("ok")
                       else Result(FAIL, "slack bot", f"auth.test: {body.get('error', 'failed')}"))
        except (httpx.HTTPError, ValueError) as e:
            out.append(Result(FAIL, "slack bot", f"unreachable: {type(e).__name__}"))
        if not channel:
            out.append(Result(FAIL, "slack channel", "SLACK_BOT_TOKEN set but SLACK_CHANNEL empty"))
        if not _env("SLACK_SIGNING_SECRET"):
            out.append(Result(WARN, "slack signing secret", "empty: button clicks are rejected"))
    elif _env("SLACK_WEBHOOK_URL"):
        ok = _env("SLACK_WEBHOOK_URL").startswith("https://hooks.slack.com/")
        out.append(Result(OK if ok else WARN, "slack webhook", "set (not posted to)" if ok else "not a hooks.slack.com URL"))
    else:
        out.append(Result(WARN, "slack", "not configured: reports only via API"))
    op = _env("LOKI_OPERATOR_SLACK_WEBHOOK")
    out.append(Result(OK, "operator alerts", "webhook set (not posted to)") if op.startswith("https://hooks.slack.com/")
               else Result(WARN, "operator alerts", "LOKI_OPERATOR_SLACK_WEBHOOK not set" if not op else "not a hooks.slack.com URL"))
    return out


def check_model(client, offline: bool) -> Result:
    model = _env("LOKI_MODEL") or "claude-haiku-4-5"
    if model == "fake":
        return Result(WARN, "model", "LOKI_MODEL=fake: scripted answers, for testing only")
    if _env("LOKI_OLLAMA_URL"):
        return Result(OK, "model", f"{model} via Ollama")
    key = _env("ANTHROPIC_API_KEY")
    if not key:
        return Result(FAIL, "model", f"{model} needs ANTHROPIC_API_KEY")
    if offline:
        return Result(OK, "model", "ANTHROPIC_API_KEY present (not checked: --offline)")
    _, err = _get(client, "model", "https://api.anthropic.com/v1/models",
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    return err or Result(OK, "model", f"{model}, key accepted (models list, free)")


def webhook_urls(show_token: bool) -> list[str]:
    base = _env("LOKI_PUBLIC_URL").rstrip("/") or "https://<LOKI_DOMAIN>"
    tok = _env("LOKI_WEBHOOK_TOKEN")
    tok = tok if show_token else (tok[:4] + "…" if tok else "<LOKI_WEBHOOK_TOKEN>")
    paths = [("Alertmanager", "/alerts"), ("Grafana", "/alerts/grafana"), ("Sentry", "/alerts/sentry"),
             ("Datadog", "/alerts/datadog"), ("PagerDuty", "/alerts/pagerduty"), ("Generic", "/alerts/generic")]
    return [f"  {n:<12} {base}{p}?token={tok}" for n, p in paths]


def run(offline: bool = False, client=None, connect=None) -> list[Result]:
    results = check_required() + check_auth() + [check_database(connect)]
    if offline:
        return results + [check_model(None, True)]
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        results += [check_prometheus(client), check_grafana_loki(client)]
        results += check_github(client) + check_sentry(client) + check_slack(client)
        results.append(check_model(client, False))
    finally:
        if own:
            client.close()
    return results


def render(results: list[Result]) -> str:
    width = max(len(r.name) for r in results)
    return "\n".join(f"{r.status}  {r.name:<{width}}  {r.detail}" for r in results)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true", help="no network calls except the database")
    ap.add_argument("--show-token", action="store_true", help="print webhook URLs with the full token")
    args = ap.parse_args(argv)
    results = run(offline=args.offline)
    print(render(results))
    print("\nWebhook URLs:")
    print("\n".join(webhook_urls(args.show_token)))
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    print(f"\n{len(results) - fails - warns} ok, {warns} warning(s), {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
