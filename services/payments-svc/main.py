"""payments-svc: charges cards and stores payments in Postgres.

Two real versions are built from this file (see Dockerfile): v2.4.0 charges in
the shop's currency; v2.5.0 adds multi-currency support with a bug. Loki's
verifier runs both images side by side to prove which one breaks.

It also has a hidden /chaos endpoint so we can break the running service on
purpose without a redeploy. The chaos mode is deliberately NOT logged, so the
agent has to find the cause from symptoms, just like a real incident.
"""
import json
import logging
import os
import re
import time

import psycopg
from fastapi import FastAPI
from prometheus_client import Histogram
from pydantic import BaseModel

from metrics import instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s payments-svc %(message)s")
log = logging.getLogger("payments")

DB_URL = os.environ["DATABASE_URL"]
# Baked into the image at build time (docker build --build-arg PAYMENTS_VERSION=...).
VERSION = os.environ.get("PAYMENTS_VERSION", "v2.4.0")
# v2.5.0 shipped multi-currency charges; v2.5.1 reverted them.
MULTI_CURRENCY = VERSION == "v2.5.0"
FX_RATES = {"INR": 1.0, "USD": 83.5, "EUR": 90.2}

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
    payload = body.model_dump()
    amount = payload["amount"]

    # The live demo can switch the v2.5.0 code path on at runtime (bad_deploy),
    # so the running service breaks the same way the v2.5.0 image does.
    if MULTI_CURRENCY or mode == "bad_deploy":
        # Multi-currency: convert using the currency the shop sends (it sends none).
        currency = payload["currency"]  # KeyError -> 500
        amount = payload["amount"] * FX_RATES[currency]

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
            (body.order_id, amount),
        )
    log.info("charged order=%s amount=%.2f", body.order_id, body.amount)
    return {"status": "charged", "order_id": body.order_id}


# ---------- failed request samples ----------
# Like an error tracker, keep the request that broke so it can be replayed later
# (Loki's verifier does). Secrets are masked and at most one sample is logged
# every SAMPLE_EVERY_S, so an outage can't flood the logs.
SAMPLE_EVERY_S = float(os.environ.get("SAMPLE_EVERY_S", "30"))
MAX_SAMPLE_BYTES = 4096
SECRET_KEY = re.compile(r"(?i)(card|cvv|cvc|pan$|password|passwd|secret|token|api[_-]?key|auth|ssn|iban)")
_last_sample = [0.0]


def _mask(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET_KEY.search(str(k)) else _mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _leaf(exc: BaseException) -> BaseException:
    # Middleware can wrap the real error in exception groups; report the original.
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _log_sample(scope, raw: bytes, status: int, exc: BaseException | None) -> None:
    now = time.monotonic()
    if now - _last_sample[0] < SAMPLE_EVERY_S:
        return
    _last_sample[0] = now
    sample = {"method": scope["method"], "path": scope["path"], "status": status}
    if scope.get("query_string"):
        sample["query"] = scope["query_string"].decode(errors="replace")
    if exc is not None:
        leaf = _leaf(exc)
        sample["error"] = f"{type(leaf).__name__}: {leaf}"
    if raw and len(raw) <= MAX_SAMPLE_BYTES:
        try:
            sample["body"] = _mask(json.loads(raw))
        except ValueError:
            pass  # not JSON: leave it out rather than log something unmasked
    log.error("failed request sample: %s", json.dumps(sample, separators=(", ", ": ")))


class FailedRequestSampler:
    """Plain ASGI middleware: sees the request body and the final status without
    adding another task group (which would nest the error in the logs)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, status = [], [200]

        async def recv():
            msg = await receive()
            if msg["type"] == "http.request" and sum(map(len, chunks)) <= MAX_SAMPLE_BYTES:
                chunks.append(msg.get("body", b""))
            return msg

        async def snd(msg):
            if msg["type"] == "http.response.start":
                status[0] = msg["status"]
            await send(msg)

        try:
            await self.app(scope, recv, snd)
        except Exception as e:
            _log_sample(scope, b"".join(chunks), 500, e)
            raise
        if status[0] >= 500:
            _log_sample(scope, b"".join(chunks), status[0], None)


app.add_middleware(FailedRequestSampler)


@app.post("/chaos")
def set_chaos(body: ChaosMode):
    CHAOS["mode"] = body.mode
    return CHAOS


@app.get("/health")
def health():
    return {"ok": True}
