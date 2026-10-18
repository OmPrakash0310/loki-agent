"""The Loki MCP server (mcp/loki_mcp.py) and the /deploys/{id}/diff endpoint it reads.

Loki's HTTP API is faked with httpx.MockTransport, so nothing here needs Docker.
"""
import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import loki_mcp  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

DIFF = (
    "--- a/payments-svc/charge.py\n+++ b/payments-svc/charge.py\n"
    "@@ -38,7 +38,10 @@ def charge(body: Charge):\n"
    "     log.debug(\"charging order %s\", body.order_id)\n"
    "     payload = body.model_dump()\n"
    "+    # Multi-currency: convert using the currency the shop sends\n"
    "+    currency = payload[\"currency\"]\n"
    "+    amount = payload[\"amount\"] * FX_RATES[currency]\n"
    "-    amount = payload[\"amount\"]\n"
    "     with psycopg.connect(DB_URL) as conn:\n"
)

REPORT = {
    "summary": "Deploy 2a5f2f8 to payments-svc reads a 'currency' field shop-api doesn't send.",
    "root_cause": "Deploy 2a5f2f8 reads payload[\"currency\"] without a default, so every charge raises KeyError.",
    "root_cause_service": "payments-svc",
    "trigger": "deploy",
    "deploy_id": "2a5f2f8",
    "affected_services": ["payments-svc", "shop-api"],
    "evidence": [
        "Deploy 2a5f2f8 at 19:22:29 shipped currency = payload[\"currency\"]",
        "384 KeyError: 'currency' errors in payments-svc starting 19:22:30, raised at charge.py:42",
    ],
    "ruled_out": ["Database: postgres p95 unchanged at 0.009s"],
    "confidence": 0.9,
    "suggested_action": "rollback",
    "action_detail": "Roll back 2a5f2f8.",
}


def _inv(inv_id, status="done", report=REPORT, mode="live", **kw):
    return {"id": inv_id, "status": status, "mode": mode, "report": report if status == "done" else None,
            "created_at": "2026-10-17T19:23:25Z", "finished_at": "2026-10-17T19:24:11Z" if status == "done" else None,
            "steps": 5, "cost_usd": 0.02, "model": "claude-haiku-4-5", "cache_hit": None, "error": None, **kw}


def _summary(inv):
    return {k: inv[k] for k in ("id", "mode", "status", "created_at", "finished_at", "steps", "cost_usd", "error")} | {
        "summary": (inv["report"] or {}).get("summary")}


TRACE = [
    {"step": 0, "tool": "briefing", "input": {}, "output": "# Incident briefing\n- x344 KeyError: 'currency'"},
    {"step": 1, "tool": "llm", "input": {}, "output": "stop_reason=tool_use"},
    {"step": 1, "tool": "git_diff", "input": {"deploy_id": "2a5f2f8"}, "output": '{"diff": "--- a/payments-svc/charge.py'},  # cut at 1500 chars
    {"step": 2, "tool": "submit_report", "input": REPORT, "output": "report submitted"},
]


class FakeLoki:
    """Routes GET requests to canned responses and records what was asked."""

    def __init__(self, investigations=None, diffs=None):
        self.investigations = {i["id"]: i for i in (investigations or [_inv(36)])}
        self.diffs = diffs if diffs is not None else {
            "2a5f2f8": {"deploy_id": "2a5f2f8", "service": "payments-svc", "version": "v2.5.0", "diff": DIFF}}
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET", "the MCP server must stay read-only"
        parts = request.url.path.strip("/").split("/")
        if parts == ["investigations"]:
            return httpx.Response(200, json=[_summary(i) for i in sorted(self.investigations.values(), key=lambda i: -i["id"])])
        if parts == ["incidents"]:
            return httpx.Response(200, json=[{"investigation_id": 36, "alert_name": "HighErrorRate", "service": "payments-svc"}])
        if parts[0] == "investigations":
            inv = self.investigations.get(int(parts[1]))
            if inv is None:
                return httpx.Response(404, json={"detail": "no such investigation"})
            return httpx.Response(200, json=TRACE if parts[2:] == ["trace"] else inv)
        if parts[0] == "deploys" and parts[-1] == "diff":
            d = self.diffs.get("/".join(parts[1:-1]))
            return httpx.Response(200, json=d) if d else httpx.Response(404, json={"detail": "no deploy"})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = FakeLoki(**kw)
        monkeypatch.setattr(loki_mcp, "loki", loki_mcp.Loki("http://loki.test", api_key="k", transport=httpx.MockTransport(f)))
        return f
    return install


# ---------- extraction ----------

def test_file_line_hints_from_colon_and_python_frames():
    hints = loki_mcp.file_line_hints(
        ['raised at charge.py:42 and payments-svc/api/routes.ts:7', 'File "/app/charge.py", line 41, in charge',
         "again charge.py:42", "time 19:22:30 is not a file"], "evidence")
    assert [(h["file"], h["line"]) for h in hints] == [
        ("charge.py", 42), ("payments-svc/api/routes.ts", 7), ("/app/charge.py", 41)]


def test_error_signature_keeps_the_quoted_value_not_the_sentence():
    assert loki_mcp.error_signature(["384 KeyError: 'currency' errors in payments-svc"]) == "KeyError: 'currency'"
    assert loki_mcp.error_signature(["psycopg.OperationalError: timed out, retrying"]) == "OperationalError: timed out"
    assert loki_mcp.error_signature(["latency went up"]) is None


def test_diff_files_counts_and_new_side_line_numbers():
    assert loki_mcp.diff_files(DIFF) == [
        {"file": "payments-svc/charge.py", "added": 3, "removed": 1, "changed_lines": "40-42"}]


# ---------- tools ----------

def test_get_root_cause_defaults_to_latest_finished_live_investigation(fake):
    f = fake(investigations=[_inv(36), _inv(37, mode="backtest"), _inv(38, status="running")])
    rc = loki_mcp.get_root_cause()
    assert rc["investigation_id"] == 36
    assert "#38 is running" in rc["note"]
    assert rc["hypothesis"]["deploy_id"] == "2a5f2f8" and rc["hypothesis"]["service"] == "payments-svc"
    assert rc["error_signature"] == "KeyError: 'currency'"
    assert {"file": "charge.py", "line": 42, "source": "evidence"} in rc["file_line_hints"]
    assert rc["deploy"]["files_changed"][0]["changed_lines"] == "40-42"
    assert rc["verification"] == "not verified by reproduction"
    assert all(r.headers["authorization"] == "Bearer k" for r in f.requests)


def test_fix_brief_separates_evidence_from_hypothesis(fake):
    fake()
    brief = loki_mcp.get_root_cause(36)["fix_brief"]
    assert "confidence 90%, not verified by reproduction" in brief
    assert "Suspected service: payments-svc" in brief
    assert "Hypothesis (Loki's diagnosis, not a fact)" in brief
    assert "Error signature (observed): KeyError: 'currency'" in brief
    assert "Deploy 2a5f2f8 (v2.5.0): payments-svc/charge.py +3/-1 (new lines 40-42)" in brief
    assert "charge.py:42" in brief
    assert "Do not deploy or touch production" in brief
    assert "regression test" in brief


def test_root_cause_hints_fall_back_to_the_trace(fake):
    report = {**REPORT, "evidence": ["error rate 100%"], "root_cause": "bad deploy"}
    fake(investigations=[_inv(36, report=report)])
    rc = loki_mcp.get_root_cause(36)
    assert rc["error_signature"] == "KeyError: 'currency'"  # from the briefing in the trace
    assert rc["file_line_hints"] == []


def test_root_cause_without_a_deploy_or_with_a_missing_diff(fake):
    fake(investigations=[_inv(36, report={**REPORT, "trigger": "infrastructure", "deploy_id": ""})])
    rc = loki_mcp.get_root_cause(36)
    assert rc["deploy"] is None and "Deploy 2a5f2f8 (" not in rc["fix_brief"]
    fake(diffs={})
    rc = loki_mcp.get_root_cause(36)
    assert "no deploy" in rc["deploy"]["error"] and rc["deploy"]["files_changed"] == []


def test_older_loki_without_the_diff_route_falls_back_to_the_trace(fake, monkeypatch):
    fake(diffs={})
    diff = {"deploy_id": "2a5f2f8", "service": "payments-svc", "version": "v2.5.0", "diff": DIFF}
    trace = [*TRACE[:2], {"step": 1, "tool": "git_diff", "input": {"deploy_id": "2a5f2f8"}, "output": json.dumps(diff)}]
    monkeypatch.setattr(sys.modules[__name__], "TRACE", trace)
    rc = loki_mcp.get_root_cause(36)
    assert rc["deploy"]["source"] == "investigation trace" and "error" not in rc["deploy"]
    assert rc["deploy"]["files_changed"][0]["changed_lines"] == "40-42"
    # Called directly, a missing route is explained rather than reported as an unknown deploy.
    monkeypatch.setattr(loki_mcp, "loki", loki_mcp.Loki("http://loki.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={"detail": "Not Found"}))))
    with pytest.raises(ToolError, match="older build"):
        loki_mcp.get_deploy_diff("2a5f2f8")


def test_root_cause_of_an_unfinished_investigation_says_so(fake):
    fake(investigations=[_inv(36, status="running")])
    assert loki_mcp.get_root_cause(36)["message"] == "Investigation #36 is running; no root cause yet."


def test_list_incidents_live_only_with_confidence_and_alerts(fake):
    fake(investigations=[_inv(36), _inv(37, mode="backtest"), _inv(38, status="failed", error="boom")])
    out = loki_mcp.list_incidents(limit=5)
    assert [i["id"] for i in out] == [38, 36]
    assert out[0]["confidence"] is None and out[0]["error"] == "boom"
    assert out[1]["confidence"] == 0.9 and out[1]["alerts"] == ["HighErrorRate on payments-svc"]
    assert len(loki_mcp.list_incidents(limit=1)) == 1


def test_get_evidence_drops_llm_rows(fake):
    fake()
    steps = loki_mcp.get_evidence(36)
    assert [s["tool"] for s in steps] == ["briefing", "git_diff", "submit_report"]


def test_get_deploy_diff_escapes_ids_with_slashes(fake):
    f = fake(diffs={"acme/shop:pr:7": {"deploy_id": "acme/shop:pr:7", "service": "shop", "version": "pr-7", "diff": DIFF}})
    d = loki_mcp.get_deploy_diff("acme/shop:pr:7")
    assert d["files_changed"][0]["added"] == 3
    assert "acme%2Fshop" in str(f.requests[-1].url)


# ---------- errors ----------

def test_unknown_investigation_is_a_clear_error(fake):
    fake()
    with pytest.raises(ToolError, match="no investigation #99"):
        loki_mcp.get_root_cause(99)
    with pytest.raises(ToolError, match="no investigation #99"):
        loki_mcp.get_evidence(99)
    with pytest.raises(ToolError, match="no deploy 'nope'"):
        loki_mcp.get_deploy_diff("nope")


def test_unreachable_loki_says_how_to_start_it(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)
    monkeypatch.setattr(loki_mcp, "loki", loki_mcp.Loki("http://localhost:1", transport=httpx.MockTransport(refuse)))
    with pytest.raises(ToolError, match=r"not reachable at http://localhost:1.*docker compose up -d"):
        loki_mcp.list_incidents()


def test_auth_and_server_errors(fake, monkeypatch):
    for status, match in [(401, "LOKI_API_KEY"), (500, "returned 500 for /investigations: kaput")]:
        monkeypatch.setattr(loki_mcp, "loki", loki_mcp.Loki(
            "http://loki.test", transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s, json={"detail": "kaput"}))))
        with pytest.raises(ToolError, match=match):
            loki_mcp.list_incidents()


def test_no_finished_investigation(fake):
    fake(investigations=[_inv(1, status="running")])
    with pytest.raises(ToolError, match="no finished investigation"):
        loki_mcp.get_root_cause()


# ---------- MCP surface ----------

def test_server_exposes_only_read_only_tools_and_the_prompt():
    tools = {t.name for t in asyncio.run(loki_mcp.mcp.list_tools())}
    assert tools == {"list_incidents", "get_root_cause", "get_evidence", "get_deploy_diff"}
    prompts = {p.name for p in asyncio.run(loki_mcp.mcp.list_prompts())}
    assert prompts == {"fix_incident"}
    text = loki_mcp.fix_incident("36")
    assert "get_root_cause with investigation_id=36" in text
    assert "do not deploy" in text and "human" in text


def test_over_the_mcp_protocol(fake):
    """A real MCP client session (in memory): structured results, and errors flagged as errors."""
    from mcp.shared.memory import create_connected_server_and_client_session
    fake()

    async def run():
        async with create_connected_server_and_client_session(loki_mcp.mcp) as session:
            ok = await session.call_tool("get_root_cause", {"investigation_id": 36})
            missing = await session.call_tool("get_root_cause", {"investigation_id": 99})
            prompt = await session.get_prompt("fix_incident", {})
            return ok, missing, prompt
    ok, missing, prompt = asyncio.run(run())
    assert not ok.isError and ok.structuredContent["hypothesis"]["deploy_id"] == "2a5f2f8"
    assert missing.isError and "no investigation #99" in missing.content[0].text
    assert "latest finished incident" in prompt.messages[0].content.text


# ---------- the agent endpoint ----------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app), main  # not entered: no startup hook, no real database


def test_deploy_diff_endpoint_returns_redacted_diff(client, monkeypatch):
    c, main = client
    asked = []

    def git_diff(deploy_id):
        asked.append(deploy_id)
        return {"deploy_id": deploy_id, "service": "payments-svc", "version": "v2",
                "diff": '+API_KEY = "sk_live_abc123"\n+password: hunter2"\n+    currency = payload["currency"]'}
    monkeypatch.setattr(main.tools, "git_diff", git_diff)
    r = c.get("/deploys/acme%2Fshop:pr:7/diff")
    assert r.status_code == 200 and asked == ["acme/shop:pr:7"]
    body = r.json()
    assert "sk_live_abc123" not in body["diff"] and "hunter2" not in body["diff"]
    assert 'payload["currency"]' in body["diff"] and body["service"] == "payments-svc"


def test_deploy_diff_endpoint_unknown_and_failing(client, monkeypatch):
    c, main = client

    def unknown(deploy_id):
        raise ValueError(f"no deploy with id {deploy_id!r}")
    monkeypatch.setattr(main.tools, "git_diff", unknown)
    r = c.get("/deploys/nope/diff")
    assert r.status_code == 404 and "no deploy with id 'nope'" in r.json()["detail"]

    def down(deploy_id):
        raise httpx.ConnectError("github down")
    monkeypatch.setattr(main.tools, "git_diff", down)
    assert c.get("/deploys/abc/diff").status_code == 502
