"""shop-api: the front door. Customers browse products and place orders.

Placing an order calls payments-svc. If payments is broken, orders fail.

Like payments-svc, it has a hidden /chaos endpoint that switches faults on at
runtime (see scripts/scenario_spec.py). Nothing about it is logged or counted
in metrics, and request handlers never hold a fault name or parameter in a
local variable (live probes capture locals).
"""
import hashlib
import logging
import os
import random
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from metrics import instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s shop-api %(message)s")
log = logging.getLogger("shop")


class _HideFaultSwitch(logging.Filter):
    """Drop uvicorn access-log lines for the fault switch."""

    def filter(self, record):
        return "/chaos" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_HideFaultSwitch())

PAYMENTS_URL = os.environ["PAYMENTS_URL"]
app = FastAPI()
instrument(app, "shop-api")

PRODUCTS = [
    {"id": 1, "name": "Coffee", "price": 250.0},
    {"id": 2, "name": "Headphones", "price": 2999.0},
    {"id": 3, "name": "Notebook", "price": 120.0},
]

FAULTS: dict = {}
SWITCHES: set = set()
# Catalog rows as the (simulated) product database returns them. The bad_product
# fault replaces one row with a corrupted copy (price NULL).
CATALOG_ROWS: dict = {}

NOISE = [
    "failed to send analytics event batch: connection refused (analytics-collector:4317)",
    "recommendation cache refresh failed: upstream returned 404 for /v1/recs/trending",
    "session store: evicted 128 stale sessions older than 24h",
    "deprecated client header X-App-Version<5.2 seen; please upgrade the mobile app",
]


def _recommend(product):
    """Score related products (cheap, ~0ms). cpu_burn makes it expensive."""
    digest = str(product["id"]).encode()
    for _ in range(FAULTS["cpu_burn"].get("rounds", 20000) if "cpu_burn" in SWITCHES else 10):
        digest = hashlib.sha256(digest).digest()
    return digest[0] % len(PRODUCTS)


def _noise():
    for _ in range(FAULTS["log_flood"].get("lines_per_request", 5) if "log_flood" in SWITCHES else 0):
        log.error(random.choice(NOISE))


@app.get("/products")
def products():
    _noise()
    _recommend(PRODUCTS[0])
    return PRODUCTS


@app.post("/orders")
def create_order():
    _noise()
    order_id = random.randint(1, 10_000_000)
    product = random.choice(PRODUCTS)
    product = CATALOG_ROWS.get(product["id"], product) if "bad_product" in SWITCHES else product
    _recommend(product)
    amount = round(product["price"], 2)
    payment = {"order_id": order_id, "amount": amount}
    if "amount_field" in SWITCHES:  # v1.9.0 sends minor units; the payments API still expects "amount"
        payment = {"order_id": order_id, "amount_minor": int(amount * 100)}
    try:
        resp = httpx.post(
            f"{PAYMENTS_URL}/charge",
            json=payment,
            timeout=FAULTS["payments_timeout_config"].get("timeout_s", 0.05)
            if "payments_timeout_config" in SWITCHES else 2.0,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        log.error("payment failed order=%s: timeout calling payments-svc", order_id)
        raise HTTPException(502, "payment timeout")
    except httpx.HTTPStatusError as e:
        log.error("payment failed order=%s: payments-svc returned %s", order_id, e.response.status_code)
        raise HTTPException(502, "payment error")
    log.info("order placed order=%s product=%s", order_id, product["name"])
    return {"order_id": order_id, "status": "placed"}


@app.get("/health")
def health():
    return {"ok": True}


class FaultSet(BaseModel):
    mode: str | None = None
    faults: dict | None = None


@app.post("/chaos")
def set_chaos(body: FaultSet):
    # {"mode": "none"} clears everything (same call chaos.py makes on payments-svc).
    if body.mode == "none":
        FAULTS.clear()
    if body.faults is not None:
        FAULTS.clear()
        FAULTS.update({k: dict(v or {}, since=time.monotonic()) for k, v in body.faults.items()})
    CATALOG_ROWS.clear()
    if "bad_product" in FAULTS:
        row = next(p for p in PRODUCTS if p["id"] == FAULTS["bad_product"].get("product_id", 2))
        CATALOG_ROWS[row["id"]] = dict(row, price=None)
    _refresh()
    return {"faults": sorted(FAULTS)}


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
