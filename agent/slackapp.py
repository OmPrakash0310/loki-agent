"""Loki as a distributable Slack app: a customer adds it to their workspace by clicking.

The operator registers one Slack app (deploy/slack-app-manifest.yml, with public
distribution turned on) and gives Loki its client id and secret. "Add to Slack" on the
setup page sends the customer to Slack's own consent screen, where they also pick the
channel for reports (the incoming-webhook scope makes Slack show that picker). Slack
sends back a one-time code; Loki exchanges it for the workspace's bot token.

Env (operator): SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_SIGNING_SECRET (the same
app's, for button clicks), LOKI_PUBLIC_URL (for the redirect URL).
"""
import os
from urllib.parse import urlencode

import httpx

SCOPES = "chat:write,incoming-webhook"
AUTHORIZE = "https://slack.com/oauth/v2/authorize"
ACCESS = "https://slack.com/api/oauth.v2.access"
CALLBACK_PATH = "/setup/slack/callback"


def configured() -> bool:
    return bool(os.environ.get("SLACK_CLIENT_ID") and os.environ.get("SLACK_CLIENT_SECRET")
                and os.environ.get("LOKI_PUBLIC_URL"))


def redirect_uri() -> str:
    return os.environ.get("LOKI_PUBLIC_URL", "").rstrip("/") + CALLBACK_PATH


def authorize_url(state: str) -> str:
    return AUTHORIZE + "?" + urlencode({"client_id": os.environ.get("SLACK_CLIENT_ID", ""), "scope": SCOPES,
                                        "redirect_uri": redirect_uri(), "state": state})


class SlackError(Exception):
    pass


def exchange(code: str, http: httpx.Client | None = None) -> dict:
    """Trade the one-time code for the workspace's settings. Raises SlackError."""
    client = http or httpx.Client(timeout=15)
    try:
        r = client.post(ACCESS, data={"client_id": os.environ.get("SLACK_CLIENT_ID", ""),
                                      "client_secret": os.environ.get("SLACK_CLIENT_SECRET", ""),
                                      "code": code, "redirect_uri": redirect_uri()})
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SlackError(f"couldn't reach Slack: {type(e).__name__}") from e
    finally:
        if http is None:
            client.close()
    if not body.get("ok"):
        raise SlackError(f"Slack said: {body.get('error', 'unknown error')}")
    hook = body.get("incoming_webhook") or {}
    if not body.get("access_token") or not hook.get("channel_id"):
        raise SlackError("Slack didn't return a bot token and a channel")
    return {"SLACK_BOT_TOKEN": body["access_token"], "SLACK_CHANNEL": hook["channel_id"],
            "SLACK_CHANNEL_NAME": hook.get("channel", ""), "SLACK_TEAM_ID": (body.get("team") or {}).get("id", ""),
            "SLACK_TEAM_NAME": (body.get("team") or {}).get("name", "")}


def post_test_message(token: str, channel: str, http: httpx.Client | None = None) -> str | None:
    """Send a hello to the chosen channel. Returns None, or what went wrong."""
    client = http or httpx.Client(timeout=10)
    try:
        r = client.post("https://slack.com/api/chat.postMessage", headers={"Authorization": f"Bearer {token}"},
                        json={"channel": channel, "text": "👋 Loki is connected. Incident reports will appear here."})
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return f"couldn't reach Slack: {type(e).__name__}"
    finally:
        if http is None:
            client.close()
    return None if body.get("ok") else f"Slack said: {body.get('error', 'unknown error')}"
