"""Slack: post reports as Block Kit messages with feedback buttons, and handle
the button clicks Slack sends back to POST /slack/interactions.

With SLACK_BOT_TOKEN + SLACK_CHANNEL set, Loki posts through the Web API and the
buttons work. With only SLACK_WEBHOOK_URL set, it posts plain text (no buttons:
an incoming webhook can't receive clicks).
"""
import hashlib
import hmac
import logging
import os
import time

import httpx
from fastapi import HTTPException

import verifier

log = logging.getLogger("agent")

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
CHANNEL = os.environ.get("SLACK_CHANNEL", "")
SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
# Where people can reach the agent from a browser, for the "View trace" link.
PUBLIC_URL = os.environ.get("LOKI_PUBLIC_URL", "").rstrip("/")

# Slack resends old requests in replay attacks; refuse anything older than this.
MAX_REQUEST_AGE_S = 5 * 60
MAX_EVIDENCE = 5
FEEDBACK_BLOCK = "loki_feedback"
CONFIRM = "loki_confirm"
REJECT = "loki_reject"
TRACE = "loki_trace"


def _esc(text) -> str:
    # Slack reads <...> as links/mentions, so model text must be escaped.
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _cap(text, 3000)}}


def _verification(v: dict | None) -> list[dict]:
    """The reproduction verdict, when there is one (see verifier.py)."""
    if not v:
        return []
    lines = [f"• {_esc(e)}" for e in v.get("evidence", [])[:MAX_EVIDENCE]]
    return [_section("\n".join([f"*Verification:* {_esc(verifier.headline(v))}", *lines]))]


def build_blocks(inv_id: int, r: dict, steps: int, cost: float) -> list[dict]:
    header = f"Incident #{inv_id} · {r['trigger']} · {r['confidence']:.0%} confidence"
    cause_where = f"`{_esc(r['root_cause_service'])}`" + (f", deploy `{_esc(r['deploy_id'])}`" if r.get("deploy_id") else "")
    evidence = [f"• {_esc(e)}" for e in r["evidence"][:MAX_EVIDENCE]]
    if len(r["evidence"]) > MAX_EVIDENCE:
        evidence.append(f"_…and {len(r['evidence']) - MAX_EVIDENCE} more in the full report_")
    v = r.get("verification")
    footer = (f"Affected: {_esc(', '.join(r['affected_services']))} · {steps} steps · ${cost:.4f} · "
              f"{verifier.label(v)}")

    buttons = [
        {"type": "button", "action_id": CONFIRM, "style": "primary", "value": str(inv_id),
         "text": {"type": "plain_text", "text": "✅ Root cause correct", "emoji": True}},
        {"type": "button", "action_id": REJECT, "style": "danger", "value": str(inv_id),
         "text": {"type": "plain_text", "text": "❌ Wrong", "emoji": True}},
    ]
    if PUBLIC_URL:
        buttons.append({"type": "button", "action_id": TRACE, "url": f"{PUBLIC_URL}/investigations/{inv_id}/trace",
                        "text": {"type": "plain_text", "text": "View trace"}})

    return [
        {"type": "header", "text": {"type": "plain_text", "text": _cap(header, 150), "emoji": True}},
        _section(f"*{_esc(r['summary'])}*"),
        _section(f"*Root cause* ({cause_where})\n{_esc(r['root_cause'])}"),
        _section("*Evidence*\n" + "\n".join(evidence)),
        *_verification(v),
        _section(f"*Suggested action:* {_esc(r['suggested_action'])}\n{_esc(r['action_detail'])}"),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
        {"type": "actions", "block_id": FEEDBACK_BLOCK, "elements": buttons},
    ]


def post_report(inv_id: int, report: dict, steps: int, cost: float, text: str) -> None:
    """Post a finished report. `text` is the plain rendering, used for the webhook
    fallback. Slack failures are logged, never raised: the report is already saved."""
    try:
        if BOT_TOKEN and CHANNEL:
            resp = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {BOT_TOKEN}"},
                json={"channel": CHANNEL, "text": f"Incident #{inv_id}: {report['summary']}",
                      "blocks": build_blocks(inv_id, report, steps, cost)},
                timeout=10,
            )
            resp.raise_for_status()
            # The Web API answers 200 even on errors; the real result is in "ok".
            if not resp.json().get("ok"):
                log.error("slack chat.postMessage failed: %s", resp.json().get("error"))
        elif WEBHOOK_URL:
            httpx.post(WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()
    except httpx.HTTPError as e:
        log.error("slack post failed: %s", e)


def verify_signature(body: bytes, timestamp: str, signature: str, secret: str | None = None,
                     now: float | None = None) -> bool:
    """Check that a request really came from Slack (their v0 signing scheme)."""
    secret = SIGNING_SECRET if secret is None else secret
    if not secret or not timestamp or not signature:
        return False
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > MAX_REQUEST_AGE_S:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _mark_verdict(blocks: list[dict], line: str) -> list[dict]:
    """Swap the confirm/reject buttons for a line saying who decided. The trace link stays."""
    out = []
    for b in blocks:
        if b.get("block_id") == FEEDBACK_BLOCK:
            out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": line}]})
            rest = [e for e in b.get("elements", []) if e.get("action_id") not in (CONFIRM, REJECT)]
            if rest:
                out.append({"type": "actions", "block_id": FEEDBACK_BLOCK, "elements": rest})
        else:
            out.append(b)
    return out


def interaction_response(payload: dict, confirm, reject) -> dict | None:
    """Act on a button click and return what to send to Slack's response_url
    (None = nothing to do). confirm/reject are called as fn(inv_id, who=..., user_id=...)."""
    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return None
    action = payload["actions"][0]
    if action.get("action_id") not in (CONFIRM, REJECT):
        return None  # e.g. the "View trace" link button, which Slack also reports
    inv_id = int(action["value"])
    user = payload.get("user") or {}
    user_id = user.get("id")
    who = user.get("username") or user.get("name") or user_id
    confirmed = action["action_id"] == CONFIRM
    try:
        (confirm if confirmed else reject)(inv_id, who=who, user_id=user_id)
    except HTTPException as e:
        # Only the clicker sees this; the original message is left alone.
        return {"response_type": "ephemeral", "replace_original": False,
                "text": f"Couldn't record that for incident #{inv_id}: {e.detail}"}

    mention = f"<@{user_id}>" if user_id else _esc(who or "someone")
    ts = int(time.time())
    when = f"<!date^{ts}^{{date_short_pretty}} {{time}}|{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts))}>"
    line = (f"✅ Root cause confirmed by {mention} · {when}" if confirmed
            else f"❌ Marked wrong by {mention} · {when}")
    blocks = (payload.get("message") or {}).get("blocks") or []
    return {"replace_original": True, "text": f"Incident #{inv_id}: {line}", "blocks": _mark_verdict(blocks, line)}


def handle_interaction(payload: dict, confirm, reject) -> None:
    """Runs after Slack has been acked (it wants an answer within 3 seconds)."""
    try:
        reply = interaction_response(payload, confirm, reject)
        if reply and payload.get("response_url"):
            httpx.post(payload["response_url"], json=reply, timeout=10).raise_for_status()
    except Exception:
        log.exception("slack interaction failed")
