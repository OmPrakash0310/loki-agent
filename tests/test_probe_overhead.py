"""Overhead micro-benchmark. Numbers are printed (pytest -s), only loosely asserted.

    .venv/bin/python -m pytest -s tests/test_probe_overhead.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "probes", "python"))
sys.path.insert(0, os.path.dirname(__file__))

import probe_target as t  # noqa: E402
from loki_probe.core import ProbeManager  # noqa: E402

N = 200_000
ROOTS = [os.path.realpath(os.path.dirname(__file__))]


def _per_call_ns(fn):
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter_ns()
        for i in range(N):
            fn(i)
        best = min(best, (time.perf_counter_ns() - start) / N)
    return best


def test_overhead():
    mgr = ProbeManager()
    base = _per_call_ns(t.other)
    # Probe on a line in a different function: t.other is never hooked.
    p = mgr.add("probe_target.py", t.line_of("PROBE_CHARGE"), seconds=60, max_hits=1, roots=ROOTS)
    other_fn = _per_call_ns(t.other)
    mgr.remove(p["id"])
    # Probe in the same function but on a line that never runs as a hit (condition never matches):
    # every call pays for the condition check.
    p = mgr.add("probe_target.py", t.line_of("PROBE_OTHER"), seconds=60, max_hits=1, roots=ROOTS,
                condition={"variable": "x", "equals": -1})
    same_line_skip = _per_call_ns(t.other)
    mgr.remove(p["id"])
    after = _per_call_ns(t.other)
    print(f"\nprobe overhead per call of other(): no probe {base:.0f} ns | probe in another function {other_fn:.0f} ns"
          f" | probe on this line, condition not matching {same_line_skip:.0f} ns | after removal {after:.0f} ns")
    assert mgr.active_hooks() == 0
    assert other_fn < base * 3 + 200  # loose: only catches a hook leaking onto unrelated code
