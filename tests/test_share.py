"""Signed, expiring, read-only share links for the Slack "View trace" button."""
import logging
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import main
import share
import slack
from test_slack import REPORT

KEYS = "eval:rw-secret-123,dash:ro-secret-456:read"
XSS = '<script>alert(1)</script><img src=x onerror=alert(2)>"\'&'


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeDB:
    def __init__(self, invs, steps):
        self.invs, self.steps = invs, steps

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        if "FROM investigations WHERE id" in sql:
            inv = self.invs.get(params[0])
            return Rows([inv] if inv else [])
        if "FROM trace_steps" in sql:
            return Rows(self.steps.get(params[0], []))
        raise AssertionError(sql)


def _inv(i, report):
    return {"id": i, "status": "done", "report": report, "steps": 2, "cost_usd": 0.01, "error": None}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LOKI_API_KEYS", KEYS)
    monkeypatch.setenv("LOKI_LINK_SECRET", "link-secret-xyz")
    monkeypatch.delenv("LOKI_LINK_TTL_S", raising=False)
    evil = {**REPORT, "summary": f"boom {XSS}", "root_cause": "token=abc123 leaked",
            "evidence": ["e1"], "critique": None, "verification": None}
    db = FakeDB({12: _inv(12, evil), 13: _inv(13, REPORT)},
                {12: [{"step": 1, "tool": "query_logs", "input": {"q": XSS}, "output": f"out {XSS} password=hunter2",
                       "created_at": None}]})
    monkeypatch.setattr(main.db, "connect", lambda: db)
    return TestClient(main.app)


def _parts(path):
    u = urlsplit(path)
    q = parse_qs(u.query)
    return u.path, q["exp"][0], q["sig"][0]


def test_valid_link_renders_escaped_redacted_html(client):
    r = client.get(share.make_path(12))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "Incident #12" in body and "query_logs" in body
    assert "<script" not in body.lower() and "<img" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "hunter2" not in body and "abc123" not in body  # redacted as usual
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_no_markup_from_report_text(client):
    body = client.get(share.make_path(12)).text
    # The only tags are the page's own; none come from model/tool text.
    import re
    tags = set(re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)", body))
    assert tags <= {"html", "head", "meta", "title", "style", "body", "h1", "h2", "p", "pre", "table",
                    "thead", "tbody", "tr", "th", "td"}


def test_no_api_key_needed_but_normal_routes_still_need_one(client):
    assert client.get(share.make_path(12)).status_code == 200
    assert client.get("/investigations/12/trace").status_code == 401


def test_expired_link(client):
    path = share.make_path(12, now=time.time() - share.DEFAULT_TTL_S - 10)
    r = client.get(path)
    assert r.status_code == 403 and "expired" in r.json()["detail"]


def test_default_expiry_is_seven_days():
    _, exp, _ = _parts(share.make_path(1, now=1000))
    assert int(exp) == 1000 + 7 * 24 * 3600


def test_tampered_id(client):
    path, exp, sig = _parts(share.make_path(12))
    assert client.get(f"/share/trace/13?exp={exp}&sig={sig}").status_code == 403


def test_tampered_exp(client):
    path, exp, sig = _parts(share.make_path(12))
    assert client.get(f"{path}?exp={int(exp) + 86400}&sig={sig}").status_code == 403
    assert client.get(f"{path}?exp={exp}0&sig={sig}").status_code == 403
    assert client.get(f"{path}?exp=+{exp}&sig={sig}").status_code == 403


def test_tampered_or_missing_sig(client):
    path, exp, sig = _parts(share.make_path(12))
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    for q in (f"exp={exp}&sig={flipped}", f"exp={exp}", f"sig={sig}", "", f"exp=abc&sig={sig}"):
        assert client.get(f"{path}?{q}").status_code == 403, q


def test_wrong_secret(client, monkeypatch):
    path = share.make_path(12)
    monkeypatch.setenv("LOKI_LINK_SECRET", "a-different-secret")
    assert client.get(path).status_code == 403


def test_other_investigation_has_its_own_link(client):
    assert "Incident #13" in client.get(share.make_path(13)).text
    _, exp13, sig13 = _parts(share.make_path(13))
    assert client.get(f"/share/trace/12?exp={exp13}&sig={sig13}").status_code == 403


def test_read_only(client):
    path = share.make_path(12)
    # Writes don't get the share-link exemption: they need an API key like any route.
    assert client.post(path).status_code in (401, 405)
    assert client.delete(path).status_code in (401, 405)
    # The signature doesn't unlock any other route for the same id.
    _, exp, sig = _parts(path)
    for other in ("/investigations/12", "/investigations/12/trace", "/investigations/12/report"):
        assert client.get(f"{other}?exp={exp}&sig={sig}").status_code == 401
    assert client.get(f"/share/trace/12/extra?exp={exp}&sig={sig}").status_code in (401, 404)


def test_bad_signature_rejected_even_in_dev_mode(client, monkeypatch):
    monkeypatch.delenv("LOKI_API_KEYS")
    assert client.get("/share/trace/12?exp=9999999999&sig=00").status_code == 403


def test_secret_derived_from_api_keys_with_warning(monkeypatch, caplog):
    monkeypatch.delenv("LOKI_LINK_SECRET", raising=False)
    monkeypatch.setenv("LOKI_API_KEYS", KEYS)
    share._warned.clear()
    with caplog.at_level(logging.WARNING):
        k1 = share.secret()
    assert "LOKI_LINK_SECRET is not set" in caplog.text and len(k1) == 32
    assert "rw-secret-123" not in caplog.text and k1 != b"rw-secret-123"
    monkeypatch.setenv("LOKI_API_KEYS", "eval:other-key")
    assert share.secret() != k1


def test_sig_is_masked_in_access_logs():
    import auth
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("1.2.3.4", "GET",
                            "/share/trace/1?exp=1&sig=deadbeef"), None)
    auth.RedactTokenFilter().filter(rec)
    assert "deadbeef" not in rec.getMessage()


def test_slack_uses_signed_link(client, monkeypatch):
    monkeypatch.setattr(slack, "PUBLIC_URL", "https://loki.example.com")
    blocks = slack.build_blocks(12, REPORT, steps=4, cost=0.01)
    url = [e for e in blocks[-1]["elements"] if e["action_id"] == slack.TRACE][0]["url"]
    assert url.startswith("https://loki.example.com/share/trace/12?")
    assert client.get(url.removeprefix("https://loki.example.com")).status_code == 200
