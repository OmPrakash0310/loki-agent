"""API authentication. Secure by default: one app-level dependency guards every
route, including routes added later, except an explicit allowlist.

Env:
  LOKI_API_KEYS       comma-separated keys. Each is "key", "name:key" or
                      "name:key:scopes" where scopes is "read", "write" or
                      "read+write" (default). Unset = dev mode, no auth.
  LOKI_WEBHOOK_TOKEN  shared secret alert webhooks may pass as ?token=... on any
                      /alerts path, for senders that can't set headers.
  LOKI_REQUIRE_AUTH   1 = refuse to start without LOKI_API_KEYS.

Keys are compared in constant time and never logged; only key names are.
"""
import hmac
import logging
import os
import re
from dataclasses import dataclass

from fastapi import HTTPException, Request

log = logging.getLogger("agent")

# Paths that need no API key. /slack/interactions checks Slack's signature instead.
PUBLIC_PATHS = {"/health", "/slack/interactions"}
# Any path under this prefix also accepts LOKI_WEBHOOK_TOKEN as ?token=.
WEBHOOK_PREFIX = "/alerts"
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
SCOPES = {"read", "write"}


@dataclass(frozen=True)
class Key:
    name: str
    secret: str
    scopes: frozenset


def parse_keys(raw: str | None) -> list[Key]:
    keys = []
    for i, item in enumerate(p.strip() for p in (raw or "").split(",")):
        if not item:
            continue
        parts = item.split(":")
        if len(parts) == 1:
            name, secret, scopes = f"key{i + 1}", parts[0], "read+write"
        elif len(parts) == 2:
            (name, secret), scopes = parts, "read+write"
        elif len(parts) == 3:
            name, secret, scopes = parts
        else:
            raise ValueError(f"LOKI_API_KEYS entry #{i + 1} has too many ':' (want name:key[:scopes])")
        scope_set = frozenset(s.strip() for s in scopes.split("+") if s.strip())
        if not secret or not scope_set or not scope_set <= SCOPES:
            raise ValueError(f"LOKI_API_KEYS entry #{i + 1} ({name or '?'}) is malformed")
        keys.append(Key(name.strip(), secret.strip(), scope_set))
    return keys


def _keys() -> list[Key]:
    # Read at request time, so tests and restarts pick up env changes.
    return parse_keys(os.environ.get("LOKI_API_KEYS"))


def _webhook_token() -> str:
    return os.environ.get("LOKI_WEBHOOK_TOKEN", "")


def check_startup():
    """Call once at startup: validate config, warn loudly in dev mode."""
    keys = _keys()  # raises on a malformed entry
    if keys:
        log.info("auth: %d API key(s) loaded: %s", len(keys), ", ".join(k.name for k in keys))
        return
    if os.environ.get("LOKI_REQUIRE_AUTH", "").strip() in ("1", "true", "yes"):
        raise RuntimeError("LOKI_REQUIRE_AUTH=1 but LOKI_API_KEYS is empty: refusing to start")
    log.warning("!" * 72)
    log.warning("AUTH DISABLED: LOKI_API_KEYS is not set, every endpoint is open.")
    log.warning("Fine for the local demo. Any exposed deployment MUST set LOKI_API_KEYS.")
    log.warning("!" * 72)


def _match(presented: str, keys: list[Key]) -> Key | None:
    found = None
    for k in keys:  # check every key so timing doesn't reveal which one matched
        if hmac.compare_digest(presented.encode(), k.secret.encode()):
            found = k
    return found


def require_auth(request: Request):
    """App-level dependency. Sets request.state.actor to the key name."""
    path = request.url.path
    request.state.actor = "anonymous"
    if path in PUBLIC_PATHS:
        return
    keys = _keys()
    is_webhook = path == WEBHOOK_PREFIX or path.startswith(WEBHOOK_PREFIX + "/")
    if is_webhook:
        token, expected = request.query_params.get("token", ""), _webhook_token()
        if expected and token and hmac.compare_digest(token.encode(), expected.encode()):
            request.state.actor = "webhook"
            return
        if not keys and not expected:
            return  # dev mode
    elif not keys:
        return  # dev mode

    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    key = _match(presented.strip(), keys) if scheme.lower() == "bearer" and presented.strip() else None
    if key is None:
        raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
    needed = "read" if request.method in READ_METHODS else "write"
    if needed not in key.scopes:
        raise HTTPException(403, f"key '{key.name}' lacks the {needed} scope")
    request.state.actor = key.name


_TOKEN_RE = re.compile(r"(token=)[^&\s\"]+", re.IGNORECASE)


def _redact_arg(a):
    if isinstance(a, (int, float)) or a is None:
        return a
    text = a if isinstance(a, str) else str(a)  # e.g. an httpx URL object
    return _TOKEN_RE.sub(r"\1***", text) if _TOKEN_RE.search(text) else a


class RedactTokenFilter(logging.Filter):
    """Uvicorn's access log prints query strings; hide ?token=... there."""

    def filter(self, record):
        if record.args:
            record.args = tuple(_redact_arg(a) for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _TOKEN_RE.sub(r"\1***", record.msg)
        return True


def install_log_filter():
    # httpx/httpx2 log request URLs (httpx2 is what Starlette's TestClient uses).
    for name in ("uvicorn.access", "uvicorn.error", "agent", "httpx", "httpx2"):
        logging.getLogger(name).addFilter(RedactTokenFilter())
