"""payments-svc: charges cards and stores payments in Postgres.

Two real versions are built from this file (see Dockerfile): v2.4.0 charges in
the shop's currency; v2.5.0 adds multi-currency support with a bug. Loki's
verifier runs both images side by side to prove which one breaks.

It also has a hidden /chaos endpoint so we can break the running service on
purpose without a redeploy. The chaos mode is deliberately NOT logged, so the
agent has to find the cause from symptoms, just like a real incident.
"""
import hashlib
import json
import logging
import os
import random
import re
import threading
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

# Current failure mode: none | bad_deploy | slow_db | timeout (the original three),
# plus any number of composable faults with parameters (scripts/scenario_spec.py lists them).
# Hot paths only test membership in SWITCHES and index FAULTS inline, so no fault name or
# parameter ever lands in a request handler's locals (live probes capture locals).
CHAOS = {"mode": "none"}
FAULTS: dict = {}
SWITCHES: set = set()
# Card network authorization errors seen by the client library.
class CardNetworkError(Exception):
    pass


# Receipts are cached for idempotent retries. (The leak fault turns eviction off.)
_receipts: list = []


class Charge(BaseModel):
    order_id: int
    amount: float


class ChaosMode(BaseModel):
    mode: str | None = None
    faults: dict | None = None


@app.post("/charge")
def charge(body: Charge):
    # The demo's fault switch is read from the global, never stored in a local:
    # live probes capture locals, and the switch would give the answer away.
    payload = body.model_dump()
    amount = payload["amount"]

    # The live demo can switch the v2.5.0 code path on at runtime (bad_deploy),
    # so the running service breaks the same way the v2.5.0 image does.
    if MULTI_CURRENCY or CHAOS["mode"] == "bad_deploy":
        # Multi-currency: convert using the currency the shop sends (it sends none).
        currency = payload["currency"]  # KeyError -> 500
        amount = payload["amount"] * FX_RATES[currency]

    # Risk scoring (v2.6.0): large charges get a score from 0-1.
    if "risk_regression" in SWITCHES and amount >= FAULTS["risk_regression"].get("min_amount", 1000):
        score = amount / 1000  # bug: never normalized
        if score > 1:
            raise ValueError(f"risk score out of range: {score:.3f}")

    # Idempotency: keep recent receipts and check the new charge against them.
    # Old receipts are evicted; the mem_leak fault stops eviction (up to max_mb).
    if "mem_leak" not in SWITCHES:
        _receipts.append(bytearray(64))
        del _receipts[:-100]
    elif sum(map(len, _receipts)) < 1048576 * FAULTS["mem_leak"].get("max_mb", 300):
        _receipts.append(bytearray(1024 * FAULTS["mem_leak"].get("kb_per_request", 64)))
    for receipt in _receipts:  # checksum scan: gets slower as the cache grows
        hashlib.sha1(receipt).digest()

    # Authorize with the card network (simulated; normally ~30ms).
    with DEPENDENCY.labels("payments-svc", "card-network").time():
        # In "timeout" mode the card network hangs.
        time.sleep(5 if CHAOS["mode"] == "timeout" else
                   FAULTS["card_hang"].get("seconds", 5) if "card_hang" in SWITCHES else 0.03)
        if "card_errors" in SWITCHES and random.random() < FAULTS["card_errors"].get("rate", 1.0):
            raise CardNetworkError(f"card network returned {FAULTS['card_errors'].get('status', 503)} "
                                   "Service Unavailable (authorize)")

    # The timer covers connecting too: when the database is struggling, the wait
    # often happens while opening the connection, not in the query.
    with DEPENDENCY.labels("payments-svc", "postgres").time(), psycopg.connect(DB_URL) as conn:
        if CHAOS["mode"] == "slow_db":
            # Simulates a missing index / lock: every query takes 1.5s.
            conn.execute("SELECT pg_sleep(1.5)")
        elif "slow_db" in SWITCHES and random.random() < FAULTS["slow_db"].get("rate", 1.0):
            conn.execute("SELECT pg_sleep(%s)", (FAULTS["slow_db"].get("seconds", 1.5),))
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
        if scope["type"] != "http" or scope["path"] == "/chaos":
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

# Live probes (probes/python/loki_probe): POST /_loki/probes lets Loki capture variables at a
# line while the service runs. Only mounted when LOKI_PROBE_TOKEN is set; the image may not
# include the library at all (scripts/build_payments_images.sh builds without it).
try:
    from loki_probe.http import mount as mount_probes
except ImportError:
    mount_probes = None
if mount_probes and mount_probes(app):
    log.info("live probe endpoint enabled at /_loki/probes")


@app.post("/chaos")
def set_chaos(body: ChaosMode):
    # {"mode": X} keeps the original behaviour; "none" also clears every fault.
    # {"faults": {name: params}} replaces the composable fault set.
    if body.mode is not None:
        CHAOS["mode"] = body.mode
        if body.mode == "none":
            FAULTS.clear()
    if body.faults is not None:
        FAULTS.clear()
        FAULTS.update({k: dict(v or {}, since=time.monotonic()) for k, v in body.faults.items()})
    _refresh()
    return {"mode": CHAOS["mode"], "faults": sorted(FAULTS)}


def _refresh():
    """Recompute which faults are on now. A fault with flap_s is on and off in turns."""
    SWITCHES.clear()
    SWITCHES.update(k for k, v in FAULTS.items()
                    if not v.get("flap_s") or int((time.monotonic() - v["since"]) // v["flap_s"]) % 2 == 0)


def _refresher():
    while True:
        time.sleep(0.25)
        _refresh()


threading.Thread(target=_refresher, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True}
