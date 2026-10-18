"""capture_variables: the agent side of live probes, with the service mocked."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import redact
import telemetry as tm
import tools

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "probes", "python"))
from loki_probe import redact as probe_redact  # noqa: E402

REAL_CLIENT = httpx.Client


class FakeService:
    """Stands in for payments-svc's /_loki/probes endpoints."""

    def __init__(self, hits=None, fail_get=False, reject=False):
        self.hits = hits or []
        self.fail_get = fail_get
        self.reject = reject
        self.calls = []
        self.deleted = []

    def handler(self, request: httpx.Request):
        self.calls.append((request.method, request.url.path))
        assert request.headers["x-loki-probe-token"] == "tok"
        if request.method == "POST":
            if self.reject:
                return httpx.Response(400, json={"error": "'x.py' is not a file under the allowed app directories"})
            self.body = json.loads(request.content)
            return httpx.Response(201, json={"id": "p1", "status": "active", "hits": []})
        if request.method == "GET":
            if self.fail_get:
                raise httpx.ConnectError("service went away")
            status = "done" if self.hits else "active"
            return httpx.Response(200, json={"id": "p1", "status": status, "hits": self.hits, "errors": 0})
        if request.method == "DELETE":
            self.deleted.append(request.url.path)
            return httpx.Response(200, json={"id": "p1", "status": "removed", "hits": self.hits, "errors": 0})
        return httpx.Response(405)


HIT = {"ts": 1760000000.0, "thread": "AnyIO worker thread", "stack": ["main.py:68 in charge"],
       "locals": {"payload": {"order_id": 7, "amount": 12.5}, "mode": "bad_deploy"}}


@pytest.fixture
def probes_on(monkeypatch):
    monkeypatch.setenv("LOKI_PROBES", "1")
    monkeypatch.setenv("LOKI_PROBE_ENDPOINTS", "payments-svc=http://payments-svc:8000, shop-api=http://shop:8000/")
    monkeypatch.setenv("LOKI_PROBE_TOKEN", "tok")
    monkeypatch.setattr(tools, "PROBE_POLL_S", 0.01)
    audits = []
    monkeypatch.setattr(tools, "_audit_probe", lambda *a: audits.append(a))
    tools.set_investigation(42)
    return audits


def use(monkeypatch, svc):
    transport = httpx.MockTransport(svc.handler)
    monkeypatch.setattr(tools.httpx, "Client", lambda **kw: REAL_CLIENT(transport=transport, **kw))
    monkeypatch.setattr(tools.httpx, "delete", lambda url, **kw: REAL_CLIENT(transport=transport).delete(url, **kw))


def test_endpoints_parsed():
    os.environ["LOKI_PROBE_ENDPOINTS"] = "a=http://a:1, b=http://b:2/ ,bad"
    try:
        assert tools.probe_endpoints() == {"a": "http://a:1", "b": "http://b:2"}
    finally:
        del os.environ["LOKI_PROBE_ENDPOINTS"]


def test_success_returns_redacted_captures_and_deletes(probes_on, monkeypatch):
    svc = FakeService(hits=[{**HIT, "locals": {**HIT["locals"], "note": "token=abc123 ops@example.com"}}])
    use(monkeypatch, svc)
    out, is_error = tools.run("capture_variables", {"service": "payments-svc", "file": "main.py", "line": 68,
                                                    "variables": ["payload"], "seconds": 5})
    assert not is_error, out
    data = json.loads(out)
    assert data["hit_count"] == 1 and data["captures"][0]["locals"]["payload"] == {"order_id": 7, "amount": 12.5}
    assert "abc123" not in out and "ops@example.com" not in out  # redacted again on the agent side
    assert svc.body == {"file": "main.py", "line": 68, "seconds": 10, "max_hits": tools.PROBE_MAX_HITS,
                        "variables": ["payload"]}
    assert svc.deleted == ["/_loki/probes/p1"]
    assert probes_on == [("payments-svc", "main.py", 68)]  # audited


def test_timeout_without_hits_still_deletes(probes_on, monkeypatch):
    svc = FakeService()
    use(monkeypatch, svc)
    out = tools.capture_variables("payments-svc", "main.py", 68, seconds=1)
    assert out["hit_count"] == 0 and "did not run" in out["note"]
    assert 0.9 <= out["waited_seconds"] < 3
    assert svc.deleted == ["/_loki/probes/p1"]


def test_returns_soon_after_first_hit(probes_on, monkeypatch):
    svc = FakeService(hits=[HIT])
    svc_status = svc.handler

    def still_active(request):  # a hit, but the probe stays active (max_hits not reached)
        resp = svc_status(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": "p1", "status": "active", "hit_count": 1, "hits": [HIT]})
        return resp
    svc.handler = still_active
    use(monkeypatch, svc)
    monkeypatch.setattr(tools, "PROBE_GRACE_S", 0.1)
    out = tools.capture_variables("payments-svc", "main.py", 68, seconds=30)
    assert out["hit_count"] == 1 and out["waited_seconds"] < 2 and svc.deleted


def test_cleanup_on_error(probes_on, monkeypatch):
    svc = FakeService(fail_get=True)
    use(monkeypatch, svc)
    out, is_error = tools.run("capture_variables", {"service": "payments-svc", "file": "main.py", "line": 68})
    assert is_error and "ConnectError" in out
    assert svc.deleted == ["/_loki/probes/p1"]  # removed even though polling failed


def test_rejected_probe_is_an_error(probes_on, monkeypatch):
    use(monkeypatch, FakeService(reject=True))
    out, is_error = tools.run("capture_variables", {"service": "payments-svc", "file": "x.py", "line": 1})
    assert is_error and "HTTP 400" in out


def test_unknown_service(probes_on):
    out, is_error = tools.run("capture_variables", {"service": "nope", "file": "main.py", "line": 1})
    assert is_error and "no probe endpoint" in out


def test_never_cached(probes_on, monkeypatch):
    svc = FakeService(hits=[HIT])
    use(monkeypatch, svc)
    args = {"service": "payments-svc", "file": "main.py", "line": 68}
    tools.run("capture_variables", args)
    tools.run("capture_variables", args)
    assert [c for c in svc.calls if c[0] == "POST"] == [("POST", "/_loki/probes")] * 2


def test_budget_per_investigation(probes_on, monkeypatch):
    use(monkeypatch, FakeService(hits=[HIT]))
    for _ in range(tools.PROBE_MAX_PER_INVESTIGATION):
        tools.capture_variables("payments-svc", "main.py", 68)
    with pytest.raises(PermissionError, match="budget"):
        tools.capture_variables("payments-svc", "main.py", 68)
    tools.set_investigation(43)  # a new investigation gets a fresh budget
    tools.capture_variables("payments-svc", "main.py", 68)


def test_audit_failure_means_no_probe(probes_on, monkeypatch):
    svc = FakeService(hits=[HIT])
    use(monkeypatch, svc)

    def broken(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(tools, "_audit_probe", broken)
    out, is_error = tools.run("capture_variables", {"service": "payments-svc", "file": "main.py", "line": 68})
    assert is_error and svc.calls == []


@pytest.mark.parametrize("env", [{}, {"LOKI_PROBES": "1"}, {"LOKI_PROBE_ENDPOINTS": "payments-svc=http://p:8000"},
                                 {"LOKI_PROBES": "0", "LOKI_PROBE_ENDPOINTS": "payments-svc=http://p:8000"}])
def test_gated_by_env(monkeypatch, env):
    for k in ("LOKI_PROBES", "LOKI_PROBE_ENDPOINTS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert "capture_variables" not in [t["name"] for t in tools.available_tools()]
    out, is_error = tools.run("capture_variables", {"service": "payments-svc", "file": "main.py", "line": 1})
    assert is_error and "PermissionError" in out


def test_offered_when_enabled(probes_on):
    names = [t["name"] for t in tools.available_tools()]
    assert names[-1] == "capture_variables" and "capture_variables" not in [t["name"] for t in tools.TOOLS]


def test_disabled_in_replay(probes_on, monkeypatch):
    svc = FakeService(hits=[HIT])
    use(monkeypatch, svc)
    with tm.replay(datetime.now(timezone.utc) - timedelta(hours=1)):
        assert "capture_variables" not in [t["name"] for t in tools.available_tools()]
        out, is_error = tools.run("capture_variables", {"service": "payments-svc", "file": "main.py", "line": 68})
    assert is_error and "backtest" in out and svc.calls == []


def test_investigator_offers_the_tool_list_from_available_tools():
    import inspect
    import investigator
    src = inspect.getsource(investigator._call)
    assert "tools.TOOLS" not in src and src.count("tools.available_tools()") == 3


def test_probe_redaction_patterns_match_the_agent():
    assert [(p.pattern, p.flags, r) for p, r in probe_redact.PATTERNS] == [(p.pattern, p.flags, r) for p, r in redact.PATTERNS]
    assert probe_redact.CARD.pattern == redact.CARD.pattern
    sample = "Bearer x.y.z password=abc 4242 4242 4242 4242 a@b.co postgres://u:p@h/db sk_live_123 eyJa.b.c"
    assert probe_redact.redact(sample) == redact.redact(sample)
