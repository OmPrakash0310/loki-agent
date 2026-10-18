import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
import slack

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"

REPORT = {
    "summary": "Checkout failing: payments-svc returns 500 after deploy",
    "root_cause": "Deploy d-42 renamed column <amount> & broke the INSERT",
    "root_cause_service": "payments-svc",
    "trigger": "deploy",
    "deploy_id": "d-42",
    "affected_services": ["payments-svc", "shop-api"],
    "evidence": [f"evidence {i}" for i in range(8)],
    "ruled_out": ["database is healthy"],
    "confidence": 0.87,
    "suggested_action": "rollback",
    "action_detail": "Roll back payments-svc to d-41.",
}


def _sign(body: bytes, ts: str, secret: str = SECRET) -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()


# --- signature verification ------------------------------------------------

def test_signature_valid():
    body, ts = b"payload=%7B%7D", str(int(time.time()))
    assert slack.verify_signature(body, ts, _sign(body, ts), secret=SECRET)


def test_signature_bad():
    body, ts = b"payload=%7B%7D", str(int(time.time()))
    assert not slack.verify_signature(body, ts, _sign(body, ts, "other-secret"), secret=SECRET)
    assert not slack.verify_signature(body + b"x", ts, _sign(body, ts), secret=SECRET)  # tampered body
    assert not slack.verify_signature(body, ts, "", secret=SECRET)
    assert not slack.verify_signature(body, "not-a-number", _sign(body, ts), secret=SECRET)


def test_signature_stale_timestamp():
    body = b"payload=%7B%7D"
    ts = str(int(time.time()) - 6 * 60)
    assert not slack.verify_signature(body, ts, _sign(body, ts), secret=SECRET)
    # Just inside the window still passes.
    now = int(ts) + 299
    assert slack.verify_signature(body, ts, _sign(body, ts), secret=SECRET, now=now)


def test_signature_refused_without_secret():
    body, ts = b"x", str(int(time.time()))
    assert not slack.verify_signature(body, ts, _sign(body, ts, ""), secret="")


# --- Block Kit rendering ---------------------------------------------------

def test_build_blocks(monkeypatch):
    monkeypatch.setattr(slack, "PUBLIC_URL", "https://loki.example.com")
    blocks = slack.build_blocks(12, REPORT, steps=4, cost=0.0123)
    types = [b["type"] for b in blocks]
    assert types == ["header", "section", "section", "section", "section", "context", "actions"]
    assert blocks[0]["text"]["text"] == "Incident #12 · deploy · 87% confidence"
    root = blocks[2]["text"]["text"]
    assert "`payments-svc`, deploy `d-42`" in root
    assert "&lt;amount&gt; &amp; broke" in root  # model text escaped for mrkdwn
    evidence = blocks[3]["text"]["text"]
    assert evidence.count("•") == slack.MAX_EVIDENCE and "and 3 more" in evidence
    assert "rollback" in blocks[4]["text"]["text"]
    footer = blocks[5]["elements"][0]["text"]
    assert "4 steps" in footer and "$0.0123" in footer and "not verified by reproduction yet" in footer
    buttons = {e["action_id"]: e for e in blocks[6]["elements"]}
    assert buttons[slack.CONFIRM]["value"] == "12" and buttons[slack.REJECT]["value"] == "12"
    url = buttons[slack.TRACE]["url"]
    assert url.startswith("https://loki.example.com/share/trace/12?exp=") and "&sig=" in url


def test_build_blocks_without_public_url_has_no_trace_button(monkeypatch):
    monkeypatch.setattr(slack, "PUBLIC_URL", "")
    blocks = slack.build_blocks(1, {**REPORT, "deploy_id": None}, steps=0, cost=0.0)
    assert [e["action_id"] for e in blocks[-1]["elements"]] == [slack.CONFIRM, slack.REJECT]
    assert "deploy" not in blocks[2]["text"]["text"].split("\n")[0]


def test_post_report_uses_web_api_then_webhook(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    monkeypatch.setattr(slack.httpx, "post", lambda url, **kw: calls.append((url, kw)) or Resp())
    monkeypatch.setattr(slack, "BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(slack, "CHANNEL", "#incidents")
    monkeypatch.setattr(slack, "WEBHOOK_URL", "https://hooks.example/x")
    slack.post_report(3, REPORT, 2, 0.01, "plain text")
    url, kw = calls[-1]
    assert url == "https://slack.com/api/chat.postMessage"
    assert kw["json"]["channel"] == "#incidents" and kw["json"]["blocks"][0]["type"] == "header"
    assert kw["headers"]["Authorization"] == "Bearer xoxb-test"

    monkeypatch.setattr(slack, "BOT_TOKEN", "")
    slack.post_report(3, REPORT, 2, 0.01, "plain text")
    assert calls[-1] == ("https://hooks.example/x", {"json": {"text": "plain text"}, "timeout": 10})


# --- button handling -------------------------------------------------------

def _payload(action_id: str, value: str = "12") -> dict:
    blocks = slack.build_blocks(12, REPORT, steps=4, cost=0.01)
    return {
        "type": "block_actions",
        "user": {"id": "U123", "username": "asha"},
        "response_url": "https://hooks.slack.com/actions/T/1/abc",
        "message": {"blocks": blocks},
        "actions": [{"action_id": action_id, "value": value, "block_id": slack.FEEDBACK_BLOCK}],
    }


def test_confirm_button(monkeypatch):
    monkeypatch.setattr(slack, "PUBLIC_URL", "https://loki.example.com")
    calls = []
    reply = slack.interaction_response(_payload(slack.CONFIRM), lambda *a, **k: calls.append(("c", a, k)),
                                       lambda *a, **k: calls.append(("r", a, k)))
    assert calls == [("c", (12,), {"who": "asha", "user_id": "U123"})]
    assert reply["replace_original"] is True
    verdict = [b for b in reply["blocks"] if b["type"] == "context"][-1]["elements"][0]["text"]
    assert verdict.startswith("✅ Root cause confirmed by <@U123>")
    # Feedback buttons are gone; the trace link stays.
    remaining = [e["action_id"] for b in reply["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert remaining == [slack.TRACE]


def test_reject_button():
    calls = []
    reply = slack.interaction_response(_payload(slack.REJECT), lambda *a, **k: calls.append("c"),
                                       lambda inv_id, **k: calls.append(("r", inv_id, k["user_id"])))
    assert calls == [("r", 12, "U123")]
    assert any("❌ Marked wrong by <@U123>" in json.dumps(b, ensure_ascii=False) for b in reply["blocks"])


def test_button_error_is_shown_only_to_clicker():
    def refuse(*a, **k):
        raise HTTPException(409, "only a finished, freshly investigated report can be confirmed")

    reply = slack.interaction_response(_payload(slack.CONFIRM), refuse, refuse)
    assert reply["response_type"] == "ephemeral" and reply["replace_original"] is False
    assert "freshly investigated" in reply["text"]


def test_trace_link_click_is_ignored():
    def boom(*a, **k):
        raise AssertionError("should not be called")

    assert slack.interaction_response(_payload(slack.TRACE), boom, boom) is None
    assert slack.interaction_response({"type": "view_submission"}, boom, boom) is None


# --- endpoint + DB logic (fake connection, no Postgres) --------------------

class FakeConn:
    def __init__(self, inv):
        self.inv, self.sql = inv, []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        inv = self.inv
        return type("R", (), {"fetchone": lambda s: inv})()


def _inv(**kw):
    return {"id": 12, "mode": "live", "status": "done", "cache_key": "fp|d-42", "fingerprint": "fp", "cache_hit": None,
            "report": REPORT, **kw}


def test_reject_unconfirms_cache_and_records_feedback(monkeypatch):
    conn = FakeConn(_inv())
    monkeypatch.setattr(main.db, "connect", lambda: conn)
    assert main._reject(12, who="asha", user_id="U123", reason="it was DNS") == {"rejected": 12}
    sqls = [s for s, _ in conn.sql]
    assert any("SET confirmed = false" in s for s in sqls)
    fb = [p for s, p in conn.sql if s.startswith("INSERT INTO feedback")]
    assert fb == [(12, "rejected", "U123", "asha", "it was DNS")]


def test_confirm_records_feedback_and_refuses_cache_hits(monkeypatch):
    conn = FakeConn(_inv())
    monkeypatch.setattr(main.db, "connect", lambda: conn)
    main._confirm(12, who="asha", user_id="U123")
    assert [p for s, p in conn.sql if s.startswith("INSERT INTO feedback")] == [(12, "confirmed", "U123", "asha", None)]

    monkeypatch.setattr(main.db, "connect", lambda: FakeConn(_inv(cache_hit="exact:3")))
    with pytest.raises(HTTPException) as e:
        main._confirm(12)
    assert e.value.status_code == 409


def test_interactions_endpoint(monkeypatch):
    monkeypatch.setattr(slack, "SIGNING_SECRET", SECRET)
    handled = []
    monkeypatch.setattr(slack, "handle_interaction", lambda p, c, r: handled.append((p, c, r)))
    client = TestClient(main.app)  # not as a context manager, so startup (db.init) doesn't run
    body = urlencode({"payload": json.dumps(_payload(slack.CONFIRM))}).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Slack-Request-Timestamp": ts}

    bad = client.post("/slack/interactions", content=body, headers={**headers, "X-Slack-Signature": "v0=00"})
    assert bad.status_code == 401 and not handled

    ok = client.post("/slack/interactions", content=body, headers={**headers, "X-Slack-Signature": _sign(body, ts)})
    assert ok.status_code == 200
    payload, confirm, reject = handled[0]
    assert payload["actions"][0]["action_id"] == slack.CONFIRM
    assert confirm is main._confirm and reject is main._reject
