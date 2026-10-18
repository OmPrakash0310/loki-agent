"""Fault injection in the demo services: every fault works, the original chaos.py
modes behave as before, and nothing about the switch leaks to Loki (logs, metrics,
or the locals a live probe would capture)."""
import ast
import re
import importlib
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scenario_spec as ss  # noqa: E402

LEAK_WORDS = ["chaos", "fault", "switch", "flap", *ss.FAULTS, "inject"]


def load_service(name, monkeypatch):
    """Import services/<name>/main.py fresh (both services have main.py and metrics.py)."""
    for mod in ("main", "metrics"):
        sys.modules.pop(mod, None)
    monkeypatch.syspath_prepend(str(ROOT / "services" / name))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
    monkeypatch.setenv("PAYMENTS_URL", "http://payments.test")
    import prometheus_client
    for collector in list(prometheus_client.REGISTRY._collector_to_names):
        if collector.__class__.__name__ in ("Counter", "Histogram", "Gauge"):
            prometheus_client.REGISTRY.unregister(collector)
    mod = importlib.import_module("main")
    for m in ("main", "metrics"):
        sys.modules.pop(m, None)
    return mod


class FakeConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))


@pytest.fixture
def payments(monkeypatch):
    svc = load_service("payments-svc", monkeypatch)
    queries = []
    monkeypatch.setattr(svc.psycopg, "connect", lambda url: FakeConn(queries))
    real_time = svc.time

    class Clock:  # record the service's sleeps; the background refresher keeps really sleeping
        def __getattr__(self, name):
            return getattr(real_time, name)

        def sleep(self, s):
            if s == 0.25:
                return real_time.sleep(s)
            queries.append(("sleep", s))

    monkeypatch.setattr(svc, "time", Clock())
    svc.queries = queries
    client = TestClient(svc.app, raise_server_exceptions=False)
    yield svc, client
    client.post("/chaos", json={"mode": "none"})


class FakeResp:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        import httpx
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=self)


@pytest.fixture
def shop(monkeypatch):
    svc = load_service("shop-api", monkeypatch)
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResp(422 if "amount" not in json else 200)

    monkeypatch.setattr(svc.httpx, "post", fake_post)
    svc.calls = calls
    client = TestClient(svc.app, raise_server_exceptions=False)
    yield svc, client
    client.post("/chaos", json={"mode": "none"})


def set_faults(client, faults):
    r = client.post("/chaos", json={"faults": faults})
    assert r.status_code == 200


# ---------- the original three modes, unchanged ----------

def test_legacy_modes_still_work(payments):
    svc, c = payments
    assert c.post("/charge", json={"order_id": 1, "amount": 10}).status_code == 200
    assert c.post("/chaos", json={"mode": "bad_deploy"}).json()["mode"] == "bad_deploy"
    assert c.post("/charge", json={"order_id": 1, "amount": 10}).status_code == 500
    c.post("/chaos", json={"mode": "slow_db"})
    svc.queries.clear()
    assert c.post("/charge", json={"order_id": 1, "amount": 10}).status_code == 200
    assert ("SELECT pg_sleep(1.5)", None) in svc.queries
    c.post("/chaos", json={"mode": "timeout"})
    svc.queries.clear()
    c.post("/charge", json={"order_id": 1, "amount": 10})
    assert ("sleep", 5) in svc.queries
    c.post("/chaos", json={"mode": "none"})
    svc.queries.clear()
    assert c.post("/charge", json={"order_id": 1, "amount": 10}).status_code == 200
    assert ("sleep", 0.03) in svc.queries and not any("pg_sleep" in str(q[0]) for q in svc.queries)


def test_chaos_py_still_posts_the_same_modes():
    src = (ROOT / "chaos.py").read_text()
    assert 'MODES = ["none", "bad_deploy", "slow_db", "timeout"]' in src
    assert '"http://localhost:8002/chaos"' in src and 'json.dumps({"mode": mode})' in src
    out = subprocess.run([sys.executable, str(ROOT / "chaos.py"), "explode"], capture_output=True, text=True)
    assert out.returncode != 0 and "usage" in out.stderr


# ---------- every new fault ----------

def test_payments_faults(payments):
    svc, c = payments
    set_faults(c, {"card_errors": {"rate": 1.0, "status": 503}})
    assert c.post("/charge", json={"order_id": 1, "amount": 10}).status_code == 500
    set_faults(c, {"card_hang": {"seconds": 7}})
    svc.queries.clear()
    assert c.post("/charge", json={"order_id": 1, "amount": 10}).status_code == 200
    assert ("sleep", 7) in svc.queries
    set_faults(c, {"slow_db": {"seconds": 2.5}})
    svc.queries.clear()
    c.post("/charge", json={"order_id": 1, "amount": 10})
    assert ("SELECT pg_sleep(%s)", (2.5,)) in svc.queries
    set_faults(c, {"risk_regression": {"min_amount": 1000}})
    assert c.post("/charge", json={"order_id": 1, "amount": 250}).status_code == 200  # partial: small ok
    assert c.post("/charge", json={"order_id": 1, "amount": 2999}).status_code == 500
    set_faults(c, {"mem_leak": {"kb_per_request": 512, "max_mb": 2}})
    for _ in range(10):
        c.post("/charge", json={"order_id": 1, "amount": 10})
    assert 1024 * 1024 <= sum(map(len, svc._receipts)) <= 3 * 1024 * 1024  # grows, then capped
    c.post("/chaos", json={"mode": "none"})
    c.post("/charge", json={"order_id": 1, "amount": 10})
    assert len(svc._receipts) <= 100  # eviction is back


def test_shop_faults(shop):
    svc, c = shop
    assert c.post("/orders").status_code == 200 and svc.calls[-1][2] == 2.0
    set_faults(c, {"amount_field": {}})
    assert c.post("/orders").status_code == 502
    assert "amount_minor" in svc.calls[-1][1]
    set_faults(c, {"payments_timeout_config": {"timeout_s": 0.01}})
    c.post("/orders")
    assert svc.calls[-1][2] == 0.01
    set_faults(c, {"bad_product": {"product_id": 2}})
    codes = {}
    for _ in range(60):
        r = c.post("/orders")
        codes[r.status_code] = codes.get(r.status_code, 0) + 1
    assert codes.get(500) and codes.get(200)  # partial: only Headphones fails
    set_faults(c, {"cpu_burn": {"rounds": 200_000}})
    t = time.perf_counter()
    c.get("/products")
    assert time.perf_counter() - t > 0.05


def test_log_flood_and_flapping(shop, caplog):
    svc, c = shop
    set_faults(c, {"log_flood": {"lines_per_request": 4}})
    with caplog.at_level(logging.ERROR, logger="shop"):
        c.get("/products")
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 4
    set_faults(c, {"amount_field": {"flap_s": 5}})
    assert "amount_field" in svc.SWITCHES
    svc.FAULTS["amount_field"]["since"] -= 5  # 5s later: off
    svc._refresh()
    assert "amount_field" not in svc.SWITCHES
    svc.FAULTS["amount_field"]["since"] -= 5  # 10s later: on again
    svc._refresh()
    assert "amount_field" in svc.SWITCHES


def test_none_clears_every_fault(payments, shop):
    for svc, c in (payments, shop):
        set_faults(c, {k: {} for k, f in ss.FAULTS.items() if f["service"] == ("payments-svc" if svc is payments[0] else "shop-api")})
        assert svc.SWITCHES
        c.post("/chaos", json={"mode": "none"})
        assert not svc.FAULTS and not svc.SWITCHES


def test_catalog_matches_services():
    """Every fault in the catalog is read by the service it claims to live in."""
    for name, f in ss.FAULTS.items():
        src = (ROOT / "services" / f["service"] / "main.py").read_text()
        assert re.search(f'"{name}" (not )?in SWITCHES', src), name
        for p in f["params"]:
            assert f'"{p}"' in src, (name, p)


# ---------- no leaks ----------

def _faulted_requests(svc, c, kind):
    if kind == "payments":
        for faults in ({"card_errors": {"rate": 1.0}}, {"risk_regression": {"min_amount": 100}},
                       {"slow_db": {"seconds": 1}, "card_hang": {"seconds": 2}, "mem_leak": {"kb_per_request": 16}}):
            set_faults(c, faults)
            c.post("/charge", json={"order_id": 3, "amount": 2999})
        c.post("/chaos", json={"mode": "bad_deploy"})
        c.post("/charge", json={"order_id": 3, "amount": 2999})
    else:
        for faults in ({"amount_field": {}}, {"payments_timeout_config": {"timeout_s": 0.01}},
                       {"bad_product": {"product_id": 1}, "cpu_burn": {"rounds": 1000}, "log_flood": {"lines_per_request": 1}}):
            set_faults(c, faults)
            for _ in range(6):
                c.post("/orders")
                c.get("/products")


@pytest.mark.parametrize("kind", ["payments", "shop"])
def test_switch_never_in_request_locals(kind, payments, shop):
    """Trace every frame of the service's request code while faults are on: no local
    holds a fault name, the fault tables, or a parameter dict."""
    svc, c = payments if kind == "payments" else shop
    path = str(Path(svc.__file__).resolve())
    control = {"set_chaos", "_refresh", "_refresher", "<genexpr>", "<dictcomp>", "filter"}
    seen = []

    def tracer(frame, event, arg):
        code = frame.f_code
        if code.co_filename == path and code.co_name not in control:
            seen.append(code.co_name)
            for name, value in frame.f_locals.items():
                assert value is not svc.FAULTS and value is not svc.SWITCHES, (code.co_name, name)
                assert not (isinstance(value, dict) and value in svc.FAULTS.values() and value), (code.co_name, name)
                text = str(value).lower() if isinstance(value, (str, dict, list, set, tuple)) else ""
                for word in ss.FAULTS:
                    assert word not in text, (code.co_name, name, value)
                assert "flap_s" not in text and "since" not in name
        return tracer

    threading.settrace(tracer)
    sys.settrace(tracer)
    try:
        _faulted_requests(svc, c, kind)
    finally:
        sys.settrace(None)
        threading.settrace(None)
    assert ("charge" if kind == "payments" else "create_order") in seen


@pytest.mark.parametrize("kind", ["payments", "shop"])
def test_switch_never_in_logs_or_metrics(kind, payments, shop, caplog):
    svc, c = payments if kind == "payments" else shop
    with caplog.at_level(logging.DEBUG):
        _faulted_requests(svc, c, kind)
        logging.getLogger("uvicorn.access").info('172.0.0.1 - "POST /chaos HTTP/1.1" 200')
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.name in ("payments", "shop", "uvicorn.error", "fastapi")
                     or r.name == "uvicorn.access" and all(f.filter(r) for f in logging.getLogger("uvicorn.access").filters)).lower()
    assert text  # the faults did produce symptoms
    for word in LEAK_WORDS:
        assert word.lower() not in text, word
    metrics = c.get("/metrics").text
    assert "/chaos" not in metrics and "chaos" not in metrics.lower()
    assert "service_memory_rss_bytes" in metrics


def test_handlers_never_assign_fault_state_to_locals():
    """Static scan of both services: inside request handlers, no assignment's value is
    the fault table itself, and no loop iterates over it."""
    for svc in ("payments-svc", "shop-api"):
        tree = ast.parse((ROOT / "services" / svc / "main.py").read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if fn.name in ("set_chaos", "_refresh", "_refresher"):
                continue
            for node in ast.walk(fn):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                    v = node.value
                    while isinstance(v, (ast.Subscript, ast.Attribute)):
                        v = v.value
                    assert not (isinstance(v, ast.Name) and v.id in ("FAULTS", "SWITCHES", "CHAOS")), (svc, fn.name)
                if isinstance(node, (ast.For, ast.comprehension)):
                    it = node.iter
                    assert not (isinstance(it, ast.Name) and it.id in ("FAULTS", "SWITCHES")), (svc, fn.name)
                if isinstance(node, ast.withitem) and node.optional_vars is not None:
                    assert "FAULTS" not in ast.unparse(node.context_expr), (svc, fn.name)


def test_log_formats_and_images_carry_no_switch():
    for svc in ("payments-svc", "shop-api"):
        src = (ROOT / "services" / svc / "main.py").read_text()
        fmt = next(l for l in src.splitlines() if "logging.basicConfig" in l)
        assert "chaos" not in fmt.lower() and "fault" not in fmt.lower()
        for line in src.splitlines():
            if "log." in line and "(" in line:
                assert "FAULTS" not in line and "SWITCHES" not in line and "CHAOS" not in line, line
    compose = (ROOT / "docker-compose.yml").read_text().lower()
    assert "chaos" not in compose and not re.search(r"\bfaults?\b", compose)
