-- Runs once when Postgres first starts.
-- "shop" database = production data. "agent" database = the agent's own records.
CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    order_id INT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE DATABASE agent;
