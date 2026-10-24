"""Postgres access for the agent: incidents, investigations, and their traces.

Many customers (tenants, see tenant.py) share one database. Every table has a
tenant_id, and row-level security makes Postgres itself show and accept only the
current tenant's rows: connect() switches to the restricted role loki_app and sets
loki.tenant. A query that forgets to filter by tenant returns nothing, not someone
else's data. connect(admin=True) skips that, for the few operator-level jobs that
must see every tenant (finding a tenant by its token, restart recovery, health).
"""
import logging
import os

import psycopg
from psycopg.rows import dict_row

DB_URL = os.environ["DATABASE_URL"]

SCHEMA = """
-- Customers. Tenant 1 is the single-customer setup configured by environment variables.
CREATE TABLE IF NOT EXISTS tenants (
    id SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    settings JSONB NOT NULL DEFAULT '{}',  -- non-secret per-customer settings
    secrets TEXT,                          -- encrypted JSON (tenant.py, LOKI_SECRET_KEY)
    webhook_token_hash TEXT UNIQUE,        -- sha256 of the ?token= its alert webhooks carry
    api_key_hash TEXT UNIQUE,              -- sha256 of its API key
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The one-page setup link a customer is sent (setup.py): hashed, expiring.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS setup_token_hash TEXT UNIQUE;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS setup_expires_at TIMESTAMPTZ;
INSERT INTO tenants (id, slug, name) VALUES (1, 'default', 'Default') ON CONFLICT (id) DO NOTHING;
SELECT setval(pg_get_serial_sequence('tenants', 'id'), GREATEST((SELECT max(id) FROM tenants), 1));

CREATE TABLE IF NOT EXISTS incidents (
    id SERIAL PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    alert_name TEXT NOT NULL,
    service TEXT,
    severity TEXT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    started_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ
);
-- At most one OPEN incident per alert fingerprint = deduplication.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_fingerprint
    ON incidents (fingerprint) WHERE status = 'open';

-- One investigation can cover several incidents (an alert storm).
CREATE TABLE IF NOT EXISTS investigations (
    id SERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    report JSONB,
    error TEXT,
    steps INT NOT NULL DEFAULT 0,
    input_tokens INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10, 4) NOT NULL DEFAULT 0
);
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS investigation_id INT REFERENCES investigations(id);

ALTER TABLE investigations ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS fingerprint TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS cache_key TEXT;
-- How the cache was used: 'exact:<id>' (answer reused, $0), 'hint:<id>' (past answer given as a hint), or NULL.
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS cache_hit TEXT;

-- 'live' = a real alert; 'backtest' = a past incident replayed with the clock set back
-- (replay_at). Backtests are kept out of /stats, the answer cache and past-incident search.
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'live';
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS replay_at TIMESTAMPTZ;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS restarts INT NOT NULL DEFAULT 0;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'live';

-- Loki's answer cache. exact_key = incident fingerprint + suspect deploy, so a
-- new deploy never reuses an old answer. Only confirmed answers are reused as-is.
CREATE TABLE IF NOT EXISTS report_cache (
    exact_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    investigation_id INT NOT NULL REFERENCES investigations(id),
    report JSONB NOT NULL,
    confirmed BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    hits INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS report_cache_fingerprint ON report_cache (fingerprint);
-- Meaning of a confirmed answer as a vector (embeddings.py), filled in lazily on first search.
ALTER TABLE report_cache ADD COLUMN IF NOT EXISTS embedding REAL[];
ALTER TABLE report_cache ADD COLUMN IF NOT EXISTS embedding_model TEXT;

-- Every tool call Loki makes, for debugging and audit.
CREATE TABLE IF NOT EXISTS trace_steps (
    id SERIAL PRIMARY KEY,
    investigation_id INT NOT NULL REFERENCES investigations(id),
    step INT NOT NULL,
    tool TEXT NOT NULL,
    input JSONB,
    output TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Per model call: where the tokens and money went.
ALTER TABLE trace_steps ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE trace_steps ADD COLUMN IF NOT EXISTS input_tokens INT;
ALTER TABLE trace_steps ADD COLUMN IF NOT EXISTS output_tokens INT;
ALTER TABLE trace_steps ADD COLUMN IF NOT EXISTS cache_read_tokens INT;
ALTER TABLE trace_steps ADD COLUMN IF NOT EXISTS cache_write_tokens INT;
ALTER TABLE trace_steps ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10, 5);

-- What humans said about a report (Slack buttons or the API). Kept as a history,
-- so a later click doesn't erase an earlier one.
CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    investigation_id INT NOT NULL REFERENCES investigations(id),
    verdict TEXT NOT NULL,  -- confirmed | rejected
    user_id TEXT,           -- Slack user id, NULL from the API
    user_name TEXT,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feedback_investigation ON feedback (investigation_id);
-- Write actions (confirm, reject, backtest): which API key or Slack user, and when.
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    actor TEXT NOT NULL,    -- API key name, slack:<user>, webhook, or anonymous (dev mode)
    action TEXT NOT NULL,
    investigation_id INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lessons from verified mistakes only: a human reject with a reason, an eval miss
-- graded against ground truth, or a manual entry. Never from Loki's own unconfirmed
-- reports. Non-manual lessons start inactive until a human approves them.
CREATE TABLE IF NOT EXISTS lessons (
    id SERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    services TEXT[] NOT NULL DEFAULT '{}',
    tags TEXT[] NOT NULL DEFAULT '{}',
    source_investigation INT REFERENCES investigations(id),
    created_from TEXT NOT NULL CHECK (created_from IN ('reject', 'eval_miss', 'manual')),
    confirmed_by TEXT,
    active BOOLEAN NOT NULL DEFAULT false,
    uses INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


TENANT_TABLES = ("incidents", "investigations", "report_cache", "trace_steps", "feedback", "audit_log", "lessons")

# Run after SCHEMA. Idempotent: every startup re-applies it.
TENANCY = "".join(f"""
ALTER TABLE {t} ADD COLUMN IF NOT EXISTS tenant_id INT NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE {t} ALTER COLUMN tenant_id SET DEFAULT current_setting('loki.tenant', true)::int;
CREATE INDEX IF NOT EXISTS {t}_tenant ON {t} (tenant_id);
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON {t};
CREATE POLICY tenant_isolation ON {t}
    USING (tenant_id = current_setting('loki.tenant', true)::int OR current_setting('loki.admin', true) = 'on')
    WITH CHECK (tenant_id = current_setting('loki.tenant', true)::int OR current_setting('loki.admin', true) = 'on');
""" for t in TENANT_TABLES) + """
-- Uniqueness is per tenant: two customers can both have an open "HighErrorRate:checkout".
DROP INDEX IF EXISTS one_open_per_fingerprint;
CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_tenant_fingerprint
    ON incidents (tenant_id, fingerprint) WHERE status = 'open';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = 'report_cache'::regclass AND i.indisprimary AND a.attname = 'tenant_id') THEN
        ALTER TABLE report_cache DROP CONSTRAINT IF EXISTS report_cache_pkey;
        ALTER TABLE report_cache ADD PRIMARY KEY (tenant_id, exact_key);
    END IF;
END $$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants
    USING (id = current_setting('loki.tenant', true)::int OR current_setting('loki.admin', true) = 'on');
"""

# The restricted role every normal connection switches to. NOBYPASSRLS: the policies apply.
ROLE = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'loki_app') THEN
        CREATE ROLE loki_app NOLOGIN NOBYPASSRLS;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO loki_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO loki_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO loki_app;
GRANT loki_app TO CURRENT_USER;
"""

log = logging.getLogger("agent")
# False when the database user can't create or switch to loki_app (some managed
# Postgres plans): then Loki runs as a single tenant only, and refuses to add tenants.
_rls = {"ready": False}


def rls_ready() -> bool:
    return _rls["ready"]


def connect(admin: bool = False):
    import tenant
    conn = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)
    if admin:
        # Every tenant's rows. Superusers bypass row-level security anyway; a plain owner
        # (some managed Postgres) needs this flag, which the policies honour.
        conn.execute("SELECT set_config('loki.admin', 'on', false)")
        return conn
    if _rls["ready"]:
        conn.execute("SET ROLE loki_app")
    conn.execute("SELECT set_config('loki.admin', 'off', false), set_config('loki.tenant', %s, false)",
                  (str(tenant.current_id()),))
    return conn


def init():
    with connect(admin=True) as conn:
        conn.execute(SCHEMA)
        conn.execute(TENANCY)
        try:
            conn.execute(ROLE)
            conn.execute("SET ROLE loki_app")
            conn.execute("RESET ROLE")
            _rls["ready"] = True
        except psycopg.Error as e:
            _rls["ready"] = False
            log.warning("row-level security role unavailable (%s): running as a single tenant only",
                        str(e).splitlines()[0])
