"""payments-svc: charges cards and stores payments in Postgres.

It also has a hidden /chaos endpoint so we can break it on purpose.
The chaos mode is deliberately NOT logged, so the agent has to find the
cause from symptoms, just like a real incident.
"""
import logging
import os
import time

import psycopg
from fastapi import FastAPI
from prometheus_client import Histogram
from pydantic import BaseModel

from metrics import instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s payments-svc %(message)s")
log = logging.getLogger("payments")

DB_URL = os.environ["DATABASE_URL"]
app = FastAPI()
instrument(app, "payments-svc")

# Time every call to something outside this service, like real apps do.
DEPENDENCY = Histogram(
    "dependency_duration_seconds",
    "Time spent calling a dependency",
    ["service", "dependency"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

# Current failure mode: none | bad_deploy | slow_db | timeout
CHAOS = {"mode": "none"}


class Charge(BaseModel):
    order_id: int
    amount: float


class ChaosMode(BaseModel):
    mode: str


@app.post("/charge")
def charge(body: Charge):
    mode = CHAOS["mode"]

    if mode == "bad_deploy":
        # Simulates a buggy release: code expects a field that doesn't exist.
        payload = body.model_dump()
        currency = payload["currency"]  # KeyError -> 500

    # Authorize with the card network (simulated; normally ~30ms).
    with DEPENDENCY.labels("payments-svc", "card-network").time():
        # In "timeout" mode the card network hangs.
        time.sleep(5 if mode == "timeout" else 0.03)

    # The timer covers connecting too: when the database is struggling, the wait
    # often happens while opening the connection, not in the query.
    with DEPENDENCY.labels("payments-svc", "postgres").time(), psycopg.connect(DB_URL) as conn:
        if mode == "slow_db":
            # Simulates a missing index / lock: every query takes 1.5s.
            conn.execute("SELECT pg_sleep(1.5)")
        conn.execute(
            "INSERT INTO payments (order_id, amount) VALUES (%s, %s)",
            (body.order_id, body.amount),
        )
    log.info("charged order=%s amount=%.2f", body.order_id, body.amount)
    return {"status": "charged", "order_id": body.order_id}


@app.post("/chaos")
def set_chaos(body: ChaosMode):
    CHAOS["mode"] = body.mode
    return CHAOS


@app.get("/health")
def health():
    return {"ok": True}
