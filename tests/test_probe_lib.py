"""The loki_probe library: capture, caps, redaction, expiry, safety."""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "probes", "python"))
sys.path.insert(0, os.path.dirname(__file__))

import probe_target as t  # noqa: E402
from loki_probe import core  # noqa: E402
from loki_probe.core import ProbeError, ProbeManager  # noqa: E402

ROOTS = [os.path.realpath(os.path.dirname(__file__))]
FILE = "probe_target.py"


@pytest.fixture
def mgr():
    m = ProbeManager()
    yield m
    m.remove_all()
    assert m.active_hooks() == 0


def add(m, marker, **kw):
    return m.add(FILE, t.line_of(marker), roots=ROOTS, **kw)


def test_captures_locals_at_the_line_without_changing_behaviour(mgr):
    p = add(mgr, "PROBE_CHARGE", max_hits=3)
    assert t.charge({"amount": 5}, n=1) == 11
    snap = mgr.get(p["id"])
    assert snap["hit_count"] == 1
    hit = snap["hits"][0]
    assert hit["locals"] == {"payload": {"amount": 5}, "n": 1}  # before the line runs: no `total` yet
    assert hit["thread"] == threading.current_thread().name
    assert hit["stack"][0].startswith("probe_target.py:") and "charge" in hit["stack"][0]


def test_only_requested_variables(mgr):
    p = add(mgr, "PROBE_CHARGE", variables=["payload", "missing"])
    t.charge({"amount": 1})
    assert mgr.get(p["id"])["hits"][0]["locals"] == {"payload": {"amount": 1}, "missing": "<not defined at this line>"}


def test_max_hits_then_uninstalls(mgr):
    p = add(mgr, "PROBE_CHARGE", max_hits=2)
    for i in range(5):
        t.charge({"amount": i})
    snap = mgr.get(p["id"])
    assert snap["hit_count"] == 2 and snap["status"] == "done" and snap["ended_reason"] == "max_hits"
    assert mgr.active_hooks() == 0


def test_expiry_uninstalls_automatically(mgr):
    p = add(mgr, "PROBE_CHARGE", seconds=0.3)
    deadline = time.monotonic() + 3
    while mgr.get(p["id"])["status"] == "active" and time.monotonic() < deadline:
        time.sleep(0.05)
    snap = mgr.get(p["id"])
    assert snap["status"] == "expired" and mgr.active_hooks() == 0
    t.charge({"amount": 1})
    assert mgr.get(p["id"])["hit_count"] == 0
    if core.HAS_MONITORING:
        assert mgr._tool_id is None  # the sys.monitoring tool id is released too


def test_probe_on_other_line_does_not_fire(mgr):
    p = add(mgr, "PROBE_OTHER")
    t.charge({"amount": 1})
    assert t.other(1) == 2
    assert mgr.get(p["id"])["hit_count"] == 1


def test_condition_is_a_plain_equality_filter(mgr):
    p = add(mgr, "PROBE_CHARGE", condition={"variable": "n", "equals": 7})
    t.charge({"amount": 1}, n=1)
    t.charge({"amount": 2}, n=7)
    snap = mgr.get(p["id"])
    assert snap["hit_count"] == 1 and snap["hits"][0]["locals"]["n"] == 7 and snap["skipped"] == 1


def test_condition_never_calls_user_eq(mgr):
    class Evil:
        def __eq__(self, other):
            raise AssertionError("user __eq__ ran")
        __hash__ = object.__hash__

    p = add(mgr, "PROBE_CHARGE", condition={"variable": "payload", "equals": 1})
    with pytest.raises(AttributeError):
        t.charge(Evil())  # the app's own error (no .get), not the probe's
    snap = mgr.get(p["id"])
    assert snap["errors"] == 0 and snap["skipped"] == 1


@pytest.mark.parametrize("cond", [{"variable": "n"}, {"variable": "n", "equals": [1]}, {"variable": "a.b", "equals": 1},
                                  {"variable": "n", "equals": 1, "op": "gt"}, "n == 1"])
def test_condition_rejects_anything_but_equality(mgr, cond):
    with pytest.raises(ProbeError):
        add(mgr, "PROBE_CHARGE", condition=cond)


def test_secrets_redacted(mgr):
    p = add(mgr, "PROBE_SECRETS")
    t.secrets("u")
    loc = mgr.get(p["id"])["hits"][0]["locals"]
    assert loc["password"] == "[REDACTED]" and loc["api_token"] == "[REDACTED]"
    assert "abc.def" not in loc["note"] and "[EMAIL]" in loc["note"] and "Bearer [TOKEN]" in loc["note"]


def test_card_numbers_and_secret_keys_redacted(mgr):
    p = add(mgr, "PROBE_CHARGE")
    t.charge({"amount": 1, "card_number": "4242424242424242", "memo": "card 4242 4242 4242 4242"})
    payload = mgr.get(p["id"])["hits"][0]["locals"]["payload"]
    assert payload["card_number"] == "[REDACTED]" and payload["memo"] == "card [CARD]"


def test_depth_and_size_caps_and_no_user_repr(mgr):
    p = add(mgr, "PROBE_BIG")
    t.big(500)
    snap = mgr.get(p["id"])
    assert snap["errors"] == 0
    loc = snap["hits"][0]["locals"]
    assert len(loc["blob"]) < 300 and loc["blob"].endswith("chars]")
    assert loc["nested"]["a"]["b"]["c"] == "<dict ...>"
    assert len(loc["many"]) == 21 and loc["many"][-1] == "...+480 more"
    assert loc["obj"] == {"__type__": "<probe_target.Opaque>", "visible": 1}


def test_total_byte_cap_stops_the_probe(mgr, monkeypatch):
    monkeypatch.setattr(core, "TOTAL_BYTES", 1500)
    p = add(mgr, "PROBE_BIG", max_hits=50)
    for _ in range(10):
        t.big(10)
    snap = mgr.get(p["id"])
    assert snap["ended_reason"] == "byte_cap" and snap["bytes"] <= 1500 and mgr.active_hooks() == 0


def test_per_hit_byte_cap(mgr, monkeypatch):
    monkeypatch.setattr(core, "HIT_BYTES", 200)
    p = add(mgr, "PROBE_BIG")
    t.big(10)
    assert "__truncated__" in mgr.get(p["id"])["hits"][0]["locals"]


def test_never_raises_into_the_app(mgr, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("capture failed")
    monkeypatch.setattr(core, "capture_locals", boom)
    p = add(mgr, "PROBE_CHARGE", max_hits=50)
    for i in range(15):
        assert t.charge({"amount": i}) == i * 2  # the app keeps working
    snap = mgr.get(p["id"])
    assert snap["errors"] == 10 and snap["ended_reason"] == "errors" and mgr.active_hooks() == 0


def test_never_raises_even_if_dispatch_breaks(mgr, monkeypatch):
    add(mgr, "PROBE_CHARGE")
    monkeypatch.setattr(mgr, "_dispatch", lambda *a: 1 / 0)
    assert t.charge({"amount": 3}) == 6
    assert mgr.internal_errors >= 1


@pytest.mark.parametrize("file", ["/etc/passwd", "../agent/main.py", "nope.py", "probe_target.txt", ""])
def test_disallowed_files_rejected(mgr, file):
    with pytest.raises(ProbeError):
        mgr.add(file, 1, roots=ROOTS)


def test_site_packages_rejected(mgr):
    import pytest as lib
    site = os.path.dirname(os.path.dirname(lib.__file__))
    with pytest.raises(ProbeError, match="library"):
        mgr.add("pytest/__init__.py", 1, roots=[site])


def test_limits_validated(mgr):
    line = t.line_of("PROBE_CHARGE")
    for kw in ({"seconds": 301}, {"seconds": 0}, {"max_hits": 0}, {"max_hits": 101}, {"variables": ["a b"]}):
        with pytest.raises(ProbeError):
            mgr.add(FILE, line, roots=ROOTS, **kw)
    with pytest.raises(ProbeError, match="no executable code"):
        mgr.add(FILE, 1, roots=ROOTS)


def test_remove_uninstalls_and_never_mutates(mgr):
    p = add(mgr, "PROBE_CHARGE")
    payload = {"amount": 4}
    t.charge(payload)
    assert payload == {"amount": 4}
    mgr.remove(p["id"])
    assert mgr.active_hooks() == 0 and mgr.get(p["id"]) is None


def test_works_from_other_threads(mgr):
    p = add(mgr, "PROBE_CHARGE")
    th = threading.Thread(target=t.charge, args=({"amount": 2},), name="worker-1")
    th.start(); th.join()
    assert mgr.get(p["id"])["hits"][0]["thread"] == "worker-1"
