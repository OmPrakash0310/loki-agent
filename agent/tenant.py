"""Which customer (tenant) the current request or investigation belongs to, and its settings.

One Loki server can serve many customers. Each has its own integrations (Slack,
GitHub, Sentry, Datadog, Prometheus, Grafana Loki), budgets and data. The database
keeps their data apart with row-level security (db.py): every connection says which
tenant it acts for, and Postgres only shows and accepts that tenant's rows.

Tenant 1 ("default") is the single-customer setup Loki always had: its settings come
from environment variables, exactly as before. Other tenants keep theirs in the
database, secrets encrypted with LOKI_SECRET_KEY, and never fall back to the
environment, so a new customer can't end up using the operator's Slack or GitHub token.

    with tenant.use(t):      # in a request or an investigation thread
        tenant.setting("SLACK_BOT_TOKEN")
"""
import contextvars
import hashlib
import json
import os
import re
import secrets as _secrets
from contextlib import contextmanager
from dataclasses import dataclass, field

DEFAULT_ID = 1

# Settings that belong to one customer. Anything else (DATABASE_URL, ANTHROPIC_API_KEY,
# LOKI_MODEL, ...) is the operator's and is read from the environment for everyone.
PER_TENANT = {
    "SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_WEBHOOK_URL", "SLACK_TEAM_ID", "SLACK_CHANNEL_NAME", "SLACK_TEAM_NAME",
    "GITHUB_TOKEN", "GITHUB_REPOS", "GITHUB_ENVIRONMENT", "GITHUB_API_URL", "GITHUB_LOOKBACK_HOURS",
    "GITHUB_INSTALLATION_ID",
    "SENTRY_TOKEN", "SENTRY_ORG", "SENTRY_PROJECTS", "SENTRY_BASE_URL", "SENTRY_WEBHOOK_SECRET",
    "PAGERDUTY_WEBHOOK_SECRET", "GRAFANA_WEBHOOK_SECRET",
    "DATADOG_API_KEY", "DATADOG_APP_KEY", "DATADOG_TOKEN", "DATADOG_SITE", "DATADOG_LOG_QUERY", "DATADOG_BASE_URL",
    "LOKI_ERRORS_SOURCE", "LOKI_DEPLOYS_SOURCE", "LOKI_URL", "LOKI_PROM_URL", "PROM_URL",
    "LOKI_PROM_BEARER_TOKEN", "LOKI_PROM_BASIC_USER", "LOKI_PROM_BASIC_PASSWORD", "LOKI_PROM_ORG_ID",
    "LOKI_LOGS_BEARER_TOKEN", "LOKI_LOGS_BASIC_USER", "LOKI_LOGS_BASIC_PASSWORD", "LOKI_LOGS_ORG_ID",
    "LOKI_METRICS_PRESET", "LOKI_METRICS_CONFIG_JSON",
    "LOKI_DAILY_BUDGET_USD", "LOKI_MAX_COST_USD",
}
# Values encrypted at rest. Decided by name, so a new secret setting is safe by default.
_SECRET_NAME = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|WEBHOOK_URL")


def is_secret(name: str) -> bool:
    return bool(_SECRET_NAME.search(name))


@dataclass(frozen=True)
class Tenant:
    id: int
    slug: str
    name: str
    settings: dict = field(default_factory=dict)  # non-secret values, plus decrypted secrets

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_ID


DEFAULT = Tenant(DEFAULT_ID, "default", "Default")
_current: contextvars.ContextVar[Tenant] = contextvars.ContextVar("loki_tenant", default=DEFAULT)


def current() -> Tenant:
    return _current.get()


def current_id() -> int:
    return _current.get().id


@contextmanager
def use(t: Tenant):
    token = _current.set(t)
    try:
        yield t
    finally:
        _current.reset(token)


def setting(name: str, default=None):
    """A per-customer setting for the current tenant. The default tenant reads the
    environment (as Loki always did); other tenants read only their own stored value."""
    t = _current.get()
    if t.is_default:
        return os.environ.get(name, default)
    value = t.settings.get(name)
    return default if value in (None, "") else value


def has_setting(name: str) -> bool:
    t = _current.get()
    return name in os.environ if t.is_default else t.settings.get(name) not in (None, "")


# ---------- tokens: stored as hashes, shown once ----------

def new_token(prefix: str) -> str:
    return f"{prefix}_{_secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- secrets at rest ----------

class SecretKeyMissing(RuntimeError):
    pass


def _fernet():
    key = os.environ.get("LOKI_SECRET_KEY", "").strip()
    if not key:
        raise SecretKeyMissing("LOKI_SECRET_KEY is not set: it encrypts customers' secrets. "
                               "Generate one with: python3 -c \"from cryptography.fernet import Fernet; "
                               "print(Fernet.generate_key().decode())\"")
    from cryptography.fernet import Fernet
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecretKeyMissing("LOKI_SECRET_KEY must be a Fernet key (32 url-safe base64 bytes)") from e


def encrypt(values: dict) -> str:
    return _fernet().encrypt(json.dumps(values).encode()).decode()


def decrypt(blob: str | None) -> dict:
    if not blob:
        return {}
    return json.loads(_fernet().decrypt(blob.encode()))


def split_settings(values: dict) -> tuple[dict, dict]:
    """(plain, secret) for storage. Unknown names are refused: a typo must not be stored silently."""
    unknown = sorted(set(values) - PER_TENANT)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    plain = {k: v for k, v in values.items() if not is_secret(k)}
    secret = {k: v for k, v in values.items() if is_secret(k)}
    return plain, secret


def from_row(row: dict) -> Tenant:
    settings = dict(row.get("settings") or {})
    settings.update(decrypt(row.get("secrets")))
    return Tenant(row["id"], row["slug"], row["name"], settings)


def valid_slug(slug: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", slug or ""))
