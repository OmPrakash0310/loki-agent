"""shop-api: the front door. Customers browse products and place orders.

Placing an order calls payments-svc. If payments is broken, orders fail.
"""
import logging
import os
import random

import httpx
from fastapi import FastAPI, HTTPException

from metrics import instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s shop-api %(message)s")
log = logging.getLogger("shop")

PAYMENTS_URL = os.environ["PAYMENTS_URL"]
app = FastAPI()
instrument(app, "shop-api")

PRODUCTS = [
    {"id": 1, "name": "Coffee", "price": 250.0},
    {"id": 2, "name": "Headphones", "price": 2999.0},
    {"id": 3, "name": "Notebook", "price": 120.0},
]


@app.get("/products")
def products():
    return PRODUCTS


@app.post("/orders")
def create_order():
    order_id = random.randint(1, 10_000_000)
    product = random.choice(PRODUCTS)
    try:
        resp = httpx.post(
            f"{PAYMENTS_URL}/charge",
            json={"order_id": order_id, "amount": product["price"]},
            timeout=2.0,
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
