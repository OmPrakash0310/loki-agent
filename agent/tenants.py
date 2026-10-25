"""Customers (tenants): create them, find them by their credentials, change their settings.

Operator-level code: lookups use an admin connection because the tenant isn't known
yet (that's what they find out). Tokens and API keys are stored as sha256 hashes and
shown only once, when created or rotated.
"""
import json
import threading
import time

import db
import tenant

CACHE_S = 30
_cache: dict[int, tuple[float, tenant.Tenant]] = {}
_lock = threading.Lock()


class TenancyUnavailable(RuntimeError):
    pass


def _row(where: str, params) -> dict | None:
    with db.connect(admin=True) as conn:
        return conn.execute(f"SELECT * FROM tenants WHERE active AND {where}", params).fetchone()


def get(tenant_id: int) -> tenant.Tenant | None:
    if tenant_id == tenant.DEFAULT_ID:
        return tenant.DEFAULT
    with _lock:
        hit = _cache.get(tenant_id)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
    row = _row("id = %s", (tenant_id,))
    t = tenant.from_row(row) if row else None
    if t:
        with _lock:
            _cache[tenant_id] = (time.monotonic(), t)
    return t


def forget(tenant_id: int) -> None:
    with _lock:
        _cache.pop(tenant_id, None)
    import sources
    sources.reset(tenant_id)


def by_webhook_token(token: str) -> tenant.Tenant | None:
    row = _row("webhook_token_hash = %s", (tenant.token_hash(token),)) if token else None
    return get(row["id"]) if row else None


def by_api_key(key: str) -> tenant.Tenant | None:
    row = _row("api_key_hash = %s", (tenant.token_hash(key),)) if key else None
    return get(row["id"]) if row else None


SETUP_DAYS = 7


def new_setup_link(tenant_id: int, days: int = SETUP_DAYS) -> str:
    """A fresh setup token for a tenant (the old one stops working). Returns the token."""
    if tenant_id == tenant.DEFAULT_ID:
        raise ValueError("the default tenant is configured with environment variables, not a setup page")
    token = tenant.new_token("lks")
    with db.connect(admin=True) as conn:
        n = conn.execute("UPDATE tenants SET setup_token_hash = %s, setup_expires_at = now() + make_interval(days => %s) "
                         "WHERE id = %s", (tenant.token_hash(token), days, tenant_id)).rowcount
    if not n:
        raise ValueError(f"no tenant {tenant_id}")
    return token


def by_setup_token(token: str) -> tenant.Tenant | None:
    row = _row("setup_token_hash = %s AND setup_expires_at > now()", (tenant.token_hash(token),)) if token else None
    return get(row["id"]) if row else None


def by_slug(slug: str) -> tenant.Tenant | None:
    if slug == tenant.DEFAULT.slug:
        return tenant.DEFAULT
    row = _row("slug = %s", (slug,)) if slug else None
    return get(row["id"]) if row else None


def by_slack_team(team_id: str) -> tenant.Tenant | None:
    row = _row("settings->>'SLACK_TEAM_ID' = %s", (team_id,)) if team_id else None
    return get(row["id"]) if row else None


def of_investigation(inv_id: int) -> tenant.Tenant | None:
    with db.connect(admin=True) as conn:
        row = conn.execute("SELECT tenant_id FROM investigations WHERE id = %s", (inv_id,)).fetchone()
    return get(row["tenant_id"]) if row else None


def create(slug: str, name: str) -> dict:
    """A new customer, with its webhook token and API key (returned once, stored hashed)."""
    if not db.rls_ready():
        raise TenancyUnavailable("the database can't enforce tenant isolation (no loki_app role): "
                                 "Loki runs as a single tenant only here")
    if not tenant.valid_slug(slug):
        raise ValueError("slug: 2-41 lowercase letters, digits or '-', starting with a letter or digit")
    webhook, api_key = tenant.new_token("lkw"), tenant.new_token("lka")
    with db.connect(admin=True) as conn:
        if conn.execute("SELECT 1 FROM tenants WHERE slug = %s", (slug,)).fetchone():
            raise ValueError(f"a tenant called {slug!r} already exists")
        row = conn.execute(
            "INSERT INTO tenants (slug, name, webhook_token_hash, api_key_hash) VALUES (%s, %s, %s, %s) RETURNING id",
            (slug, name, tenant.token_hash(webhook), tenant.token_hash(api_key)),
        ).fetchone()
    return {"id": row["id"], "slug": slug, "name": name, "webhook_token": webhook, "api_key": api_key}


def rotate(tenant_id: int, what: str) -> str:
    """A new webhook token or API key for a tenant; the old one stops working at once."""
    column = {"webhook_token": "webhook_token_hash", "api_key": "api_key_hash"}[what]
    value = tenant.new_token("lkw" if what == "webhook_token" else "lka")
    with db.connect(admin=True) as conn:
        conn.execute(f"UPDATE tenants SET {column} = %s WHERE id = %s", (tenant.token_hash(value), tenant_id))
    forget(tenant_id)
    return value


def update_settings(tenant_id: int, values: dict) -> list[str]:
    """Merge settings into a tenant; secrets are encrypted, None or "" removes a setting.
    Returns the names now set. The default tenant is configured by environment, not here."""
    if tenant_id == tenant.DEFAULT_ID:
        raise ValueError("the default tenant is configured with environment variables")
    if any(v is not None and not isinstance(v, str) for v in values.values()):
        raise ValueError("setting values must be strings")
    plain_new, secret_new = tenant.split_settings(values)
    with db.connect(admin=True) as conn:
        row = conn.execute("SELECT settings, secrets FROM tenants WHERE id = %s FOR UPDATE", (tenant_id,)).fetchone()
        if not row:
            raise ValueError(f"no tenant {tenant_id}")
        plain = dict(row["settings"] or {})
        secret = tenant.decrypt(row["secrets"]) if secret_new or row["secrets"] else {}
        for target, new in ((plain, plain_new), (secret, secret_new)):
            for k, v in new.items():
                if v in (None, ""):
                    target.pop(k, None)
                else:
                    target[k] = v
        conn.execute("UPDATE tenants SET settings = %s, secrets = %s WHERE id = %s",
                     (json.dumps(plain), tenant.encrypt(secret) if secret else None, tenant_id))
    forget(tenant_id)
    return sorted(set(plain) | set(secret))


def listing() -> list[dict]:
    with db.connect(admin=True) as conn:
        rows = conn.execute("SELECT id, slug, name, active, created_at, settings, secrets IS NOT NULL AS has_secrets "
                            "FROM tenants ORDER BY id").fetchall()
    return [{"id": r["id"], "slug": r["slug"], "name": r["name"], "active": r["active"],
             "created_at": r["created_at"].isoformat(), "settings": sorted(r["settings"] or {}),
             "has_secrets": r["has_secrets"]} for r in rows]
