"""Pluggable sources: registry, GitHub deploys, Sentry errors, and the unchanged default path.

All HTTP is mocked with httpx.MockTransport. Nothing here talks to the network.
"""
import json
import os
import sys
from datetime import datetime, timezone

import httpx
import pytest

import context  # noqa: E402
import sources  # noqa: E402
import telemetry  # noqa: E402
import tools  # noqa: E402
from sources import github, sentry  # noqa: E402
from sources.files import FileDeploys  # noqa: E402
from sources.grafana_loki import LokiErrors  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
import fake_stack  # noqa: E402

HERE = os.path.dirname(__file__)
ENV_VARS = ["LOKI_DEPLOYS_SOURCE", "LOKI_ERRORS_SOURCE", "GITHUB_TOKEN", "GITHUB_REPOS", "GITHUB_ENVIRONMENT",
            "SENTRY_TOKEN", "SENTRY_ORG", "SENTRY_PROJECTS", "SENTRY_BASE_URL"]


def t(h, m, s=0):
    return datetime(2026, 10, 15, h, m, s, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(telemetry, "DEPLOYS_FILE", os.path.join(HERE, "fixtures", "deploys.json"))
    sources.reset()
    yield
    sources.reset()


def mock_client(routes: dict, base_url: str, calls: list | None = None) -> httpx.Client:
    """routes: path -> Response, or a function(request) -> Response."""
    def handler(request):
        if calls is not None:
            calls.append(request)
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return route(request) if callable(route) else route
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


INCIDENT = [{"started_at": t(16, 22), "alert_name": "HighErrorRate", "service": "payments-svc",
             "severity": "critical", "summary": "5xx > 5%"}]


# ---------------- registry ----------------

def test_registry_defaults_are_file_and_loki():
    assert isinstance(sources.deploys(), FileDeploys)
    assert isinstance(sources.errors(), LokiErrors)


def test_registry_rejects_unknown_and_incomplete_config(monkeypatch):
    monkeypatch.setenv("LOKI_DEPLOYS_SOURCE", "jenkins")
    with pytest.raises(ValueError, match="unknown LOKI_DEPLOYS_SOURCE"):
        sources.deploys()
    monkeypatch.setenv("LOKI_DEPLOYS_SOURCE", "github")
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        sources.deploys()
    monkeypatch.setenv("LOKI_ERRORS_SOURCE", "sentry")
    with pytest.raises(ValueError, match="SENTRY_TOKEN"):
        sources.errors()


def test_registry_builds_github_and_sentry_from_env(monkeypatch):
    monkeypatch.setenv("LOKI_DEPLOYS_SOURCE", "GitHub")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOS", "acme/payments=payments-svc, acme/shop-api")
    monkeypatch.setenv("LOKI_ERRORS_SOURCE", "sentry")
    monkeypatch.setenv("SENTRY_TOKEN", "test-token")
    monkeypatch.setenv("SENTRY_ORG", "acme")
    monkeypatch.setenv("SENTRY_PROJECTS", "payments=payments-svc")
    monkeypatch.setenv("GITHUB_API_URL", "")  # docker-compose passes unset vars as empty strings
    monkeypatch.setenv("SENTRY_BASE_URL", "")
    d, e = sources.deploys(), sources.errors()
    assert isinstance(d, github.GitHubDeploys) and isinstance(e, sentry.SentryErrors)
    assert d.repos == {"acme/payments": "payments-svc", "acme/shop-api": "shop-api"}
    assert str(d.client.base_url) == "https://api.github.com"
    assert str(e.client.base_url) == "https://sentry.io"
    assert sources.deploys() is d  # one instance per configuration


# ---------------- default path is unchanged ----------------

def test_default_briefing_matches_pre_refactor_golden(monkeypatch):
    """The golden file was produced by context.build on main, before sources/ existed,
    against the same fake Grafana Loki + Prometheus and deploy file."""
    fake_stack.install(monkeypatch, telemetry)
    b = context.build(INCIDENT)
    with open(os.path.join(HERE, "golden_default_briefing.txt")) as f:
        assert b.text + "\n--\n" + b.fingerprint + " " + b.exact_key + "\n" == f.read()


def test_default_tools_read_file_and_loki(monkeypatch):
    fake_stack.install(monkeypatch, telemetry)
    with open(telemetry.DEPLOYS_FILE) as f:
        raw = {d["id"]: d for d in json.load(f)}
    deploys = tools.recent_deploys(minutes=1440)
    assert isinstance(deploys, list)  # no notes wrapper for the file source
    assert [d["id"] for d in deploys] == [d["id"] for d in sorted(raw.values(), key=lambda d: d["deployed_at"])
                                          if "2026-10-14T16:30" <= d["deployed_at"] <= "2026-10-15T16:30"]
    assert tools.git_diff("b2e4a91") == {"deploy_id": "b2e4a91", "service": "payments-svc",
                                         "version": raw["b2e4a91"]["version"], "diff": raw["b2e4a91"]["diff"]}
    with pytest.raises(ValueError, match="no deploy"):
        tools.git_diff("nope")
    logs = tools.query_logs(service="payments-svc", minutes=20)
    assert logs["matching_lines"] == 4 and "notes" not in logs


# ---------------- GitHub ----------------

PAY_SHA_OLD, PAY_SHA_NEW, PAY_SHA_ANCIENT = "b2" * 20, "c3" * 20, "a1" * 20


def _deployment(id_, sha, ref, created, login="meera", env="production", desc=""):
    return {"id": id_, "sha": sha, "ref": ref, "task": "deploy", "environment": env, "description": desc,
            "creator": {"login": login, "type": "User"}, "created_at": created,
            "url": f"https://api.github.com/repos/acme/payments/deployments/{id_}"}


PAGE2 = "https://api.github.com/repos/acme/payments/deployments?per_page=100&page=2"


def payments_deployments(request):
    if request.url.params.get("page") == "2":
        return httpx.Response(200, json=[
            _deployment(902, PAY_SHA_OLD, "v2.4.0", "2026-10-15T15:00:00Z"),
            _deployment(901, PAY_SHA_ANCIENT, "v2.3.0", "2026-10-15T12:00:00Z"),
        ])
    return httpx.Response(200, headers={"link": f'<{PAGE2}>; rel="next", <{PAGE2}>; rel="last"'}, json=[
        _deployment(903, PAY_SHA_NEW, "v2.5.0", "2026-10-15T16:10:00Z", desc="Deploy v2.5.0: currency parsing"),
    ])


def _file(name, patch, status="modified", add=1, rem=1):
    return {"sha": "f" * 40, "filename": name, "status": status, "additions": add, "deletions": rem,
            "changes": add + rem, "patch": patch}


CHARGE_PATCH = ("@@ -48,7 +48,7 @@ def charge(body: Charge):\n     conn = db()\n"
                "-    currency = body.get('currency', 'USD')\n+    currency = body['currency']\n")
COMPARE = {
    "status": "ahead", "ahead_by": 2, "total_commits": 2,
    "commits": [{"sha": PAY_SHA_NEW}],
    "files": [
        _file("package-lock.json", "@@ -1,3 +1,3 @@\n" + "-x\n+y\n" * 3000, add=3000, rem=3000),
        _file("tests/test_charge.py", "@@ -1,2 +1,3 @@\n+def test_currency():\n+    assert True\n", add=2, rem=0),
        _file("payments/charge.py", CHARGE_PATCH),
        _file("assets/logo.png", None, status="added"),
    ],
}

SHOP_PRS = [
    {"number": 42, "title": "Retry payments on 502", "user": {"login": "arjun"}, "merged_at": "2026-10-15T16:00:00Z",
     "updated_at": "2026-10-15T16:01:00Z", "merge_commit_sha": "d4" * 20,
     "base": {"ref": "main", "sha": "e5" * 20}, "head": {"ref": "retry", "sha": "f6" * 20}},
    {"number": 41, "title": "Closed without merging", "user": {"login": "arjun"}, "merged_at": None,
     "updated_at": "2026-10-15T15:45:00Z", "merge_commit_sha": None,
     "base": {"ref": "main", "sha": "e5" * 20}, "head": {"ref": "x", "sha": "aa" * 20}},
    {"number": 40, "title": "Old one", "user": {"login": "arjun"}, "merged_at": "2026-10-15T09:00:00Z",
     "updated_at": "2026-10-15T09:00:00Z", "merge_commit_sha": "bb" * 20,
     "base": {"ref": "main", "sha": "e5" * 20}, "head": {"ref": "y", "sha": "cc" * 20}},
]

WORKER_COMMITS = [{
    "sha": "9f" * 20, "author": None,
    "commit": {"message": "Bump batch size\n\nSigned-off-by: dev <dev@acme.io>",
               "author": {"name": "Dev Person", "email": "dev@acme.io", "date": "2026-10-15T16:15:00Z"},
               "committer": {"name": "GitHub", "email": "noreply@github.com", "date": "2026-10-15T16:15:30Z"}},
}]


def github_routes():
    return {
        "/repos/acme/payments/deployments": payments_deployments,
        f"/repos/acme/payments/compare/{PAY_SHA_OLD}...{PAY_SHA_NEW}": httpx.Response(200, json=COMPARE),
        "/repos/acme/shop/deployments": httpx.Response(200, json=[]),
        "/repos/acme/shop": httpx.Response(200, json={"full_name": "acme/shop", "default_branch": "main"}),
        "/repos/acme/shop/pulls": httpx.Response(200, json=SHOP_PRS),
        f"/repos/acme/shop/compare/{'e5' * 20}...{'f6' * 20}": httpx.Response(200, json={
            "total_commits": 1, "files": [_file("shop/checkout.py", "@@ -1 +1 @@\n-a\n+b\n")]}),
        "/repos/acme/shop/pulls/42": httpx.Response(200, json=SHOP_PRS[0]),
        "/repos/acme/worker/deployments": httpx.Response(200, json=[]),
        "/repos/acme/worker": httpx.Response(200, json={"default_branch": "trunk"}),
        "/repos/acme/worker/pulls": httpx.Response(200, json=[]),
        "/repos/acme/worker/commits": httpx.Response(200, json=WORKER_COMMITS),
        f"/repos/acme/worker/commits/{'9f' * 20}": httpx.Response(200, json={
            "sha": "9f" * 20, "files": [_file("worker/run.py", "@@ -3 +3 @@\n-BATCH = 10\n+BATCH = 1000\n")]}),
    }


def make_github(calls=None, routes=None, repos=None):
    repos = repos or {"acme/payments": "payments-svc", "acme/shop": "shop-api", "acme/worker": "worker"}
    return github.GitHubDeploys(repos, mock_client(routes or github_routes(), "https://api.github.com", calls))


def test_github_maps_deployments_prs_and_commits():
    calls = []
    gh = make_github(calls)
    deploys = gh.list_deploys(t(15, 30), t(16, 30))
    assert [(d["id"], d["service"], d["version"], d["author"]) for d in deploys] == [
        ("acme/shop:pr:42", "shop-api", "#42 d4d4d4d", "arjun"),
        ("acme/payments:deploy:903", "payments-svc", "v2.5.0", "meera"),
        ("acme/worker:commit:9f9f9f9f9f9f", "worker", "9f9f9f9", "Dev Person"),
    ]
    pay = deploys[1]
    assert pay["message"] == "Deploy v2.5.0: currency parsing"
    assert pay["deployed_at"] == "2026-10-15T16:10:00+00:00" and pay["deployed_at_dt"] == t(16, 10)
    assert set(pay) == {"id", "service", "version", "author", "message", "deployed_at", "deployed_at_dt", "diff"}
    assert deploys[2]["message"] == "Bump batch size"
    assert gh.notes() == []
    # Paginated once (to find the previous deployment), and the pulls listing used the default branch.
    assert sum(r.url.path == "/repos/acme/payments/deployments" for r in calls) == 2
    pulls = next(r for r in calls if r.url.path == "/repos/acme/shop/pulls")
    assert pulls.url.params["base"] == "main" and pulls.url.params["state"] == "closed"
    worker = next(r for r in calls if r.url.path == "/repos/acme/worker/commits")
    assert worker.url.params["sha"] == "trunk"


def test_github_diff_uses_compare_against_previous_deploy_and_truncates():
    gh = make_github()
    gh.list_deploys(t(15, 30), t(16, 30))
    out = gh.get_diff("acme/payments:deploy:903")
    diff = out["diff"]
    assert out["service"] == "payments-svc" and out["version"] == "v2.5.0"
    assert "4 files changed in 2 commits" in diff
    assert "modified package-lock.json (+3000 -3000)" in diff and "added assets/logo.png" in diff
    assert "+    currency = body['currency']" in diff
    assert "+y" not in diff  # lockfile hunks are never shown
    assert diff.index("payments/charge.py\n") < diff.index("+++ b/tests/test_charge.py")
    assert len(diff) <= github.MAX_DIFF_CHARS + 200


def test_github_diff_without_listing_first():
    gh = make_github()
    assert "+b" in gh.get_diff("acme/shop:pr:42")["diff"]
    assert "BATCH = 1000" in gh.get_diff(f"acme/worker:commit:{'9f' * 20}")["diff"]
    with pytest.raises(ValueError):
        gh.get_diff("other/repo:pr:1")


def test_summarize_diff_caps_size_and_keeps_file_list():
    files = [_file(f"src/mod{i}.py", "@@ -1,200 +1,200 @@\n" + "+line of code here\n" * 300, add=300, rem=0)
             for i in range(60)]
    diff = github.summarize_diff(files)
    assert len(diff) <= github.MAX_DIFF_CHARS + 600
    assert "60 files changed" in diff and "... and 20 more files" in diff
    assert "[more hunks in this file omitted]" in diff and "diff truncated" in diff


def test_github_rate_limit_returns_partial_data_and_note():
    routes = github_routes()
    routes["/repos/acme/payments/deployments"] = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "retry-after": "30"},
        json={"message": "API rate limit exceeded for user."})
    gh = make_github(routes=routes, repos={"acme/shop": "shop-api", "acme/payments": "payments-svc",
                                            "acme/worker": "worker"})
    deploys = gh.list_deploys(t(15, 30), t(16, 30))
    assert [d["id"] for d in deploys] == ["acme/shop:pr:42"]  # shop came first; worker skipped (same token)
    assert gh.notes() == ["GitHub rate limited (retry after 30s): deploy list is partial, stopped at acme/payments"]


def test_github_429_and_permission_errors(monkeypatch):
    routes = github_routes()
    routes["/repos/acme/payments/deployments"] = httpx.Response(429, headers={"retry-after": "5"})
    routes["/repos/acme/shop/deployments"] = httpx.Response(403, json={"message": "Resource not accessible by integration"})
    gh = make_github(routes=routes, repos={"acme/shop": "shop-api", "acme/payments": "payments-svc"})
    assert gh.list_deploys(t(15, 30), t(16, 30)) == []
    assert gh.notes() == ["GitHub: could not read deploys of acme/shop: HTTP 403",
                          "GitHub rate limited (retry after 5s): deploy list is partial, stopped at acme/payments"]


def test_recent_deploys_tool_surfaces_notes(monkeypatch):
    routes = github_routes()
    routes["/repos/acme/payments/deployments"] = httpx.Response(429, headers={"retry-after": "5"})
    gh = make_github(routes=routes)
    monkeypatch.setenv("LOKI_DEPLOYS_SOURCE", "github")
    monkeypatch.setattr(github.GitHubDeploys, "from_env", classmethod(lambda cls: gh))
    monkeypatch.setattr(telemetry, "now", lambda: t(16, 30))
    out = tools.recent_deploys(minutes=60)
    assert out["deploys"] == [] and "rate limited" in out["notes"][0]


# ---------------- Sentry ----------------

def _issue(id_, short, title, culprit, first, last, count="1"):
    return {"id": id_, "shortId": short, "title": title, "culprit": culprit, "level": "error", "status": "unresolved",
            "count": count, "userCount": 3, "firstSeen": first, "lastSeen": last,
            "permalink": f"https://acme.sentry.io/issues/{id_}/", "metadata": {"type": title.split(":")[0]}}


def _event(eid, when):
    return {"id": eid, "eventID": eid, "dateCreated": when, "title": "x", "message": "", "tags": [],
            "platform": "python", "user": None}


PAY_ISSUES = [
    _issue("4501", "PAYMENTS-1A", "KeyError: 'currency'", "charge in payments/charge.py",
           "2026-10-15T16:21:01.123000Z", "2026-10-15T16:29:40Z", "523"),
    _issue("4502", "PAYMENTS-1B", "ConnectionError: token=abc123 for bob@example.com", "db.connect",
           "2026-10-15T16:24:00Z", "2026-10-15T16:24:00Z"),
]
SHOP_ISSUES = [_issue("7700", "SHOP-API-9", "HTTPError: payments-svc returned 500", "checkout in shop/views.py",
                      "2026-10-15T16:21:03Z", "2026-10-15T16:29:00Z", "88")]
EV_NEXT = "https://sentry.io/api/0/organizations/acme/issues/4501/events/?cursor=0:100:0"


def sentry_routes():
    def pay_events(request):
        if request.url.params.get("cursor"):
            return httpx.Response(200, json=[_event("e3", "2026-10-15T16:21:01.123Z")], headers={
                "link": f'<{EV_NEXT}>; rel="previous"; results="true"; cursor="0:0:1", '
                        f'<{EV_NEXT}>; rel="next"; results="false"; cursor="0:200:0"'})
        return httpx.Response(200, json=[_event("e1", "2026-10-15T16:29:40Z"), _event("e2", "2026-10-15T16:21:30Z")],
                              headers={"link": f'<{EV_NEXT}>; rel="previous"; results="false"; cursor="0:0:1", '
                                               f'<{EV_NEXT}>; rel="next"; results="true"; cursor="0:100:0"'})
    return {
        "/api/0/projects/acme/payments/issues/": httpx.Response(200, json=PAY_ISSUES),
        "/api/0/projects/acme/shop/issues/": httpx.Response(200, json=SHOP_ISSUES),
        "/api/0/organizations/acme/issues/4501/events/": pay_events,
        "/api/0/organizations/acme/issues/4502/events/": httpx.Response(200, json=[]),
        "/api/0/organizations/acme/issues/7700/events/": httpx.Response(200, json=[
            _event("s1", "2026-10-15T16:21:03Z"), _event("s2", "2026-10-15T16:29:00Z")]),
    }


def make_sentry(calls=None, routes=None):
    return sentry.SentryErrors("acme", {"payments": "payments-svc", "shop": "shop-api"},
                               mock_client(routes or sentry_routes(), "https://sentry.io", calls))


def test_sentry_turns_issue_events_into_error_lines_and_patterns():
    calls = []
    se = make_sentry(calls)
    lines = se.error_lines(None, t(16, 7), t(16, 30))
    assert [(x[0], x[1]) for x in lines][:3] == [
        (datetime(2026, 10, 15, 16, 21, 1, 123000, tzinfo=timezone.utc), "payments-svc"),
        (t(16, 21, 3), "shop-api"),
        (t(16, 21, 30), "payments-svc"),
    ]
    clusters = telemetry.cluster_logs(lines)
    assert clusters[0] == {"count": 3, "services": ["payments-svc"], "first_seen": "16:21:01", "last_seen": "16:29:40",
                           "example": "ERROR [PAYMENTS-1A] KeyError: 'currency' (charge in payments/charge.py)"}
    # No events listed for 4502: falls back to its first/last seen, and secrets are redacted.
    leaked = [c for c in clusters if "PAYMENTS-1B" in c["example"]][0]
    assert leaked["count"] == 1 and "abc123" not in leaked["example"] and "bob@example.com" not in leaked["example"]
    assert "token=[REDACTED]" in leaked["example"]
    issues_req = next(r for r in calls if r.url.path == "/api/0/projects/acme/payments/issues/")
    assert issues_req.url.params["query"] == "lastSeen:>=2026-10-15T16:07:00"
    events_req = next(r for r in calls if r.url.path.endswith("/4501/events/"))
    assert events_req.url.params["start"] == "2026-10-15T16:07:00" and events_req.url.params["end"] == "2026-10-15T16:30:00"
    assert se.notes() == []


def test_sentry_filters_by_service_and_notes_truncation(monkeypatch):
    monkeypatch.setattr(sentry, "MAX_EVENT_PAGES", 1)
    se = make_sentry()
    lines = se.error_lines("payments-svc", t(16, 7), t(16, 30))
    assert {x[1] for x in lines} == {"payments-svc"}
    # Only the newest page was read, so the issue's firstSeen keeps the onset right.
    assert min(x[0] for x in lines if "1A" in x[2]).strftime("%H:%M:%S") == "16:21:01"
    assert "count is a lower bound" in se.notes()[0]


def test_sentry_rate_limit_is_partial_with_note():
    routes = sentry_routes()
    routes["/api/0/projects/acme/shop/issues/"] = httpx.Response(429, headers={"retry-after": "12"})
    se = make_sentry(routes=routes)
    lines = se.error_lines(None, t(16, 7), t(16, 30))
    assert lines and {x[1] for x in lines} == {"payments-svc"}
    assert se.notes() == ["Sentry rate limited (retry after 12s): error data is partial, stopped at project shop"]


def test_briefing_with_sentry_and_github(monkeypatch):
    """context.py's onset, clustering and suspect ranking work on Sentry + GitHub data."""
    fake_stack.install(monkeypatch, telemetry)
    se, gh = make_sentry(), make_github()
    monkeypatch.setenv("LOKI_ERRORS_SOURCE", "sentry")
    monkeypatch.setenv("LOKI_DEPLOYS_SOURCE", "github")
    monkeypatch.setattr(sentry.SentryErrors, "from_env", classmethod(lambda cls: se))
    monkeypatch.setattr(github.GitHubDeploys, "from_env", classmethod(lambda cls: gh))
    b = context.build(INCIDENT)
    assert "ERROR [PAYMENTS-1A] KeyError: 'currency'" in b.text
    assert "- first error per service: payments-svc 16:21:01, shop-api 16:21:03" in b.text
    assert "acme/payments:deploy:903 payments-svc v2.5.0 at 16:10:00 by meera" in b.text
    assert b.suspects[0]["deploy_id"] == "acme/payments:deploy:903"
    assert b.onset["payments-svc"].strftime("%H:%M:%S") == "16:21:01"
    # The model's git_diff tool then gets the compare diff for that id.
    out, is_error = tools._run_uncached("git_diff", {"deploy_id": "acme/payments:deploy:903"})
    assert not is_error and "body['currency']" in json.loads(out)["diff"]
