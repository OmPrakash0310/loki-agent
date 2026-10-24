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
PUBLIC_PATHS = {"/health", "/health/ready", "/slack/interactions"}
# Any path under this prefix also accepts LOKI_WEBHOOK_TOKEN as ?token=.
WEBHOOK_PREFIX = "/alerts"
# GET /share/trace/{id} needs no API key but must carry a valid signed ?exp=&sig=
# (see share.py); a bad or expired signature is a 403 here, before any route runs.
SHARE_METHODS = {"GET", "HEAD"}
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


def _lookup(fn, value):
    """A tenant from the database, or None. A database problem means "no such credential":
    a 401, never a 500 that would hint the credential format was right."""
    if not value:
        return None
    try:
        import tenants
        return fn(tenants)(value)
    except Exception as e:  # noqa: BLE001
        log.warning("tenant lookup failed: %s", type(e).__name__)
        return None


@dataclass(frozen=True)
class Identity:
    tenant: object
    actor: str
    scopes: frozenset = frozenset(SCOPES)
    operator: bool = False  # a server key (LOKI_API_KEYS) or dev mode: may manage tenants


def identify(request: Request) -> Identity:
    """Who is calling, and for which tenant. Raises HTTPException(401/403/404).

    Server keys (LOKI_API_KEYS) and LOKI_WEBHOOK_TOKEN belong to the default tenant; a
    server key may act for another tenant with the header X-Loki-Tenant: <slug>. Each
    other tenant has its own webhook token and API key (tenants.py)."""
    import tenant as tenant_mod
    default = tenant_mod.DEFAULT
    path = request.url.path
    if path in PUBLIC_PATHS:
        return Identity(default, "anonymous")
    import share  # share imports auth
    m = share.PATH_RE.fullmatch(path)
    if m and request.method in SHARE_METHODS:
        inv_id = int(m.group(1))
        reason = share.verify(inv_id, request.query_params.get("exp", ""), request.query_params.get("sig", ""))
        if reason:
            raise HTTPException(403, reason)
        owner = _lookup(lambda t: t.of_investigation, inv_id)
        return Identity(owner or default, "share-link", frozenset({"read"}))
    keys = _keys()
    dev = not keys
    is_webhook = path == WEBHOOK_PREFIX or path.startswith(WEBHOOK_PREFIX + "/")
    if is_webhook:
        token, expected = request.query_params.get("token", ""), _webhook_token()
        if expected and token and hmac.compare_digest(token.encode(), expected.encode()):
            return Identity(default, "webhook")
        owner = _lookup(lambda t: t.by_webhook_token, token)
        if owner:
            return Identity(owner, f"webhook:{owner.slug}")
        if dev and not expected:
            return Identity(default, "anonymous", operator=True)

    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    presented = presented.strip() if scheme.lower() == "bearer" else ""
    key = _match(presented, keys) if presented else None
    owner = None if key is not None else _lookup(lambda t: t.by_api_key, presented)
    if key is not None:
        identity = Identity(default, key.name, key.scopes, operator=True)
    elif owner is not None:
        identity = Identity(owner, f"{owner.slug}:api")
    elif dev and not is_webhook:
        identity = Identity(default, "anonymous", operator=True)  # dev mode: open, as always
    else:
        raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
    needed = "read" if request.method in READ_METHODS else "write"
    if needed not in identity.scopes:
        raise HTTPException(403, f"key '{identity.actor}' lacks the {needed} scope")
    acting = request.headers.get("X-Loki-Tenant", "").strip()
    if acting and identity.operator:
        target = _lookup(lambda t: t.by_slug, acting)
        if target is None:
            raise HTTPException(404, f"no tenant {acting!r}")
        identity = Identity(target, f"{identity.actor}@{acting}", identity.scopes, operator=True)
    return identity


def require_auth(request: Request):
    """App-level dependency. The identity was worked out by the tenant middleware in
    main.py (which also selects the tenant for the request); this enforces it."""
    found = getattr(request.state, "identity", None)
    if found is None:  # the middleware isn't installed (a bare app in a test)
        found = identify(request)
    if isinstance(found, HTTPException):
        raise found
    request.state.actor = found.actor


_TOKEN_RE = re.compile(r"((?:token|sig)=)[^&\s\"]+", re.IGNORECASE)


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
