"""The probe control endpoints, and the settrace fallback backend."""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "probes", "python"))
sys.path.insert(0, os.path.dirname(__file__))

import probe_target as t  # noqa: E402
from loki_probe import core  # noqa: E402
from loki_probe.http import mount  # noqa: E402

ROOT = os.path.realpath(os.path.dirname(__file__))
H = {"X-Loki-Probe-Token": "s3cret"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LOKI_PROBE_ROOTS", ROOT)
    mgr = core.ProbeManager()
    app = FastAPI()
    assert mount(app, token="s3cret", probe_manager=mgr)
    yield TestClient(app), mgr
    mgr.remove_all()


def test_disabled_without_token(monkeypatch):
    monkeypatch.delenv("LOKI_PROBE_TOKEN", raising=False)
    app = FastAPI()
    assert mount(app) is False
    c = TestClient(app)
    assert c.post("/_loki/probes", json={}, headers=H).status_code == 404
    assert c.get("/_loki/probes", headers=H).status_code == 404


def test_token_required(client):
    c, _ = client
    body = {"file": "probe_target.py", "line": t.line_of("PROBE_CHARGE")}
    assert c.post("/_loki/probes", json=body).status_code == 401
    assert c.post("/_loki/probes", json=body, headers={"X-Loki-Probe-Token": "wrong"}).status_code == 401
    assert c.get("/_loki/probes/abc").status_code == 401
    assert c.delete("/_loki/probes/abc").status_code == 401


def test_place_read_delete(client):
    c, mgr = client
    r = c.post("/_loki/probes", headers=H, json={"file": "probe_target.py", "line": t.line_of("PROBE_CHARGE"),
                                                 "seconds": 10, "max_hits": 2, "variables": ["payload"]})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    t.charge({"amount": 9})
    got = c.get(f"/_loki/probes/{pid}", headers=H).json()
    assert got["hits"][0]["locals"] == {"payload": {"amount": 9}}
    assert c.get("/_loki/probes", headers=H).json()["active_hooks"] == 1
    assert c.delete(f"/_loki/probes/{pid}", headers=H).json()["status"] == "removed"
    assert mgr.active_hooks() == 0
    assert c.get(f"/_loki/probes/{pid}", headers=H).status_code == 404


@pytest.mark.parametrize("body", [{"file": "/etc/passwd", "line": 1}, {"file": "probe_target.py", "line": 1, "expr": "x"},
                                  {"file": "probe_target.py", "line": "12"}, [1]])
def test_bad_requests(client, body):
    c, _ = client
    assert c.post("/_loki/probes", json=body, headers=H).status_code == 400


def test_settrace_fallback(monkeypatch):
    monkeypatch.setattr(core, "HAS_MONITORING", False)
    mgr = core.ProbeManager()
    try:
        p = mgr.add("probe_target.py", t.line_of("PROBE_CHARGE"), roots=[ROOT], max_hits=1)
        assert t.charge({"amount": 3}) == 6
        snap = mgr.get(p["id"])
        assert snap["hits"][0]["locals"]["payload"] == {"amount": 3} and snap["status"] == "done"
        assert mgr.active_hooks() == 0 and sys.gettrace() is None
    finally:
        mgr.remove_all()
        sys.settrace(None)
