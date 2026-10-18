"""Signed, expiring, read-only links to one investigation's report and trace, so the
Slack "View trace" button works in a browser even when API keys are required.

    GET /share/trace/{id}?exp=<unix seconds>&sig=<hex HMAC-SHA256>

The signature covers "trace:<id>:<exp>", so a link opens exactly one investigation,
only until it expires, and only this read-only page (no other route accepts it).

Env:
  LOKI_LINK_SECRET  HMAC key for share links. If unset, one is derived from
                    LOKI_API_KEYS (HKDF-SHA256) and a warning is logged: rotating a
                    key then invalidates every link. With neither set (dev mode) a
                    random per-process secret is used, so links die on restart.
  LOKI_LINK_TTL_S   link lifetime in seconds (default 7 days).
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time

import auth
from redact import redact

log = logging.getLogger("agent")

DEFAULT_TTL_S = 7 * 24 * 3600
PATH_RE = re.compile(r"/share/trace/(\d+)")
_SALT = b"loki-share-link-v1"
_dev_secret = secrets.token_bytes(32)
_warned: set[str] = set()


def _warn_once(kind: str, msg: str):
    if kind not in _warned:
        _warned.add(kind)
        log.warning(msg)


def _hkdf(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA256 (extract + expand); stdlib has no HKDF."""
    prk = hmac.new(_SALT, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def secret() -> bytes:
    explicit = os.environ.get("LOKI_LINK_SECRET", "")
    if explicit:
        return explicit.encode()
    keys = auth.parse_keys(os.environ.get("LOKI_API_KEYS"))
    if keys:
        _warn_once("derived", "share links: LOKI_LINK_SECRET is not set; deriving the link key from "
                              "LOKI_API_KEYS. Set LOKI_LINK_SECRET so key rotation doesn't break links.")
        ikm = "\n".join(sorted(k.secret for k in keys)).encode()
        return _hkdf(ikm, b"share-trace")
    _warn_once("dev", "share links: no LOKI_LINK_SECRET or LOKI_API_KEYS; using a random per-process key")
    return _dev_secret


def _ttl() -> int:
    try:
        return int(os.environ.get("LOKI_LINK_TTL_S", "")) or DEFAULT_TTL_S
    except ValueError:
        return DEFAULT_TTL_S


def sign(inv_id: int, exp: int, key: bytes | None = None) -> str:
    msg = f"trace:{int(inv_id)}:{int(exp)}".encode()
    return hmac.new(key if key is not None else secret(), msg, hashlib.sha256).hexdigest()


def make_path(inv_id: int, now: float | None = None, ttl: int | None = None) -> str:
    exp = int((now if now is not None else time.time()) + (ttl if ttl is not None else _ttl()))
    return f"/share/trace/{int(inv_id)}?exp={exp}&sig={sign(inv_id, exp)}"


def verify(inv_id: int, exp: str, sig: str, now: float | None = None, key: bytes | None = None) -> str | None:
    """None if the link is valid, else a short reason."""
    if not exp or not sig:
        return "missing signature"
    try:
        exp_i = int(exp)
    except ValueError:
        return "bad link"
    expected = sign(inv_id, exp_i, key)
    # Compare first (constant time), then check expiry, so both failures take the same path.
    ok = hmac.compare_digest(expected.encode(), sig.strip().lower().encode())
    if not ok or str(exp_i) != exp:  # canonical form only: no padding, signs or leading zeros
        return "bad link"
    if exp_i < (now if now is not None else time.time()):
        return "link expired"
    return None


# ---------- the page ----------

def _e(value) -> str:
    """Redact, then HTML-escape (quotes too). Everything shown goes through this."""
    if not isinstance(value, str):
        value = json.dumps(value, default=str, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return html.escape(redact(value), quote=True)


_CSS = """body{font:14px/1.45 -apple-system,system-ui,sans-serif;max-width:980px;margin:0 auto;padding:16px;
color:#1d1d1f;background:#fff}pre{white-space:pre-wrap;word-break:break-word;background:#f5f5f7;padding:10px;
border-radius:6px;font:12px/1.4 ui-monospace,Menlo,monospace}table{border-collapse:collapse;width:100%}
td,th{border-top:1px solid #ddd;padding:6px;vertical-align:top;text-align:left}.muted{color:#666}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}pre{background:#1e1e1e}td,th{border-color:#333}
.muted{color:#999}}"""


def render_page(inv: dict, report_text: str, steps: list[dict], exp: int) -> str:
    """A static HTML page: no scripts, no external resources, every value escaped."""
    rows = "".join(
        f"<tr><td>{_e(s.get('step'))}</td><td>{_e(s.get('tool'))}</td>"
        f"<td><pre>{_e(s.get('input'))}</pre></td><td><pre>{_e(s.get('output'))}</pre></td></tr>"
        for s in steps
    )
    expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp))
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex\">"
        f"<title>Loki incident #{_e(inv['id'])}</title><style>{_CSS}</style></head><body>"
        f"<h1>Incident #{_e(inv['id'])}</h1>"
        f"<p class=\"muted\">Status: {_e(inv.get('status'))} · read-only link, expires {_e(expires)}</p>"
        f"<h2>Report</h2><pre>{_e(report_text)}</pre>"
        f"<h2>Trace ({len(steps)} steps)</h2>"
        f"<table><thead><tr><th>#</th><th>Tool</th><th>Input</th><th>Output</th></tr></thead><tbody>{rows}</tbody></table>"
        "</body></html>"
    )


# Strict headers: nothing on the page may run or load.
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
    "X-Robots-Tag": "noindex",
}
