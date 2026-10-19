"""Shared HTTP helpers for remote sources: auth'd GETs, pagination, rate limits."""
import base64
import os
import time

import httpx


class RateLimited(Exception):
    def __init__(self, retry_after_s: int | None):
        self.retry_after_s = retry_after_s
        super().__init__(f"rate limited (retry after {retry_after_s if retry_after_s is not None else '?'}s)")


def _retry_after(resp: httpx.Response) -> int | None:
    if resp.headers.get("retry-after", "").isdigit():
        return int(resp.headers["retry-after"])
    reset = resp.headers.get("x-ratelimit-reset", "")  # GitHub: epoch seconds
    if reset.isdigit():
        return max(0, int(reset) - int(time.time()))
    return None


def is_rate_limited(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    # GitHub signals both primary and secondary limits with 403. A 403 without
    # these markers is a real permission problem and should surface as such.
    return resp.status_code == 403 and (
        resp.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in resp.headers
        or "rate limit" in resp.text.lower()
    )


def get(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    """GET that raises RateLimited instead of sleeping: an investigation can't
    wait minutes, so callers return what they have plus a note."""
    resp = client.get(url, params=params)
    if is_rate_limited(resp):
        raise RateLimited(_retry_after(resp))
    resp.raise_for_status()
    return resp


def next_url(resp: httpx.Response) -> str | None:
    link = resp.links.get("next")
    if not link:
        return None
    # Sentry always sends rel="next" and says results="false" when there are none.
    if link.get("results") == "false":
        return None
    return link.get("url")


def auth_headers(kind: str) -> dict:
    """Credentials for the customer's Prometheus (kind "PROM") or Grafana Loki (kind "LOGS"), from env vars named LOKI_<kind>_...
    Read at call time. BEARER_TOKEN, or BASIC_USER + BASIC_PASSWORD (Grafana Cloud), and
    ORG_ID for multi-tenant Mimir/Loki (X-Scope-OrgID). Empty when none are set."""
    env = os.environ
    prefix = f"LOKI_{kind}"
    h = {}
    token = env.get(f"{prefix}_BEARER_TOKEN", "").strip()
    user = env.get(f"{prefix}_BASIC_USER", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    elif user:
        cred = base64.b64encode(f"{user}:{env.get(f'{prefix}_BASIC_PASSWORD', '')}".encode()).decode()
        h["Authorization"] = f"Basic {cred}"
    org = env.get(f"{prefix}_ORG_ID", "").strip()
    if org:
        h["X-Scope-OrgID"] = org
    return h
