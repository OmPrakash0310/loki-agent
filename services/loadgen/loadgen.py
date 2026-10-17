"""Fake customers. Without traffic there are no metrics, so nothing to alert on."""
import random
import time

import httpx

SHOP = "http://shop-api:8000"

while True:
    try:
        if random.random() < 0.5:
            httpx.get(f"{SHOP}/products", timeout=5)
        else:
            httpx.post(f"{SHOP}/orders", timeout=5)
    except httpx.HTTPError:
        pass  # shop is down; keep trying, real customers would too
    time.sleep(0.2)
