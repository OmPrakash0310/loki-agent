"""Postgres access for the agent: incidents, investigations, and their traces."""
import os

import psycopg
from psycopg.rows import dict_row

DB_URL = os.environ["DATABASE_URL"]

SCHEMA = """
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
"""


def connect():
    return psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)


def init():
    with connect() as conn:
        conn.execute(SCHEMA)
