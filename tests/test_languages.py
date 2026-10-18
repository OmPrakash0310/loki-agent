"""Language-aware log processing: Java, Node.js/TypeScript, Go and Python.

The Python lock tests come first: answer-cache fingerprints are built from
normalize() output, so the Python demo's patterns must never drift.
"""
import os
import re

import telemetry

HERE = os.path.dirname(__file__)
LOGS = os.path.join(HERE, "fixtures", "logs")


def fixture_lines(name: str) -> list[str]:
    with open(os.path.join(LOGS, name)) as f:
        return f.read().splitlines()


def filter_regex() -> re.Pattern:
    """ERROR_FILTER's regex, compiled with Python re as an approximation of RE2."""
    body = re.fullmatch(r"\|~ `(.*)`", telemetry.ERROR_FILTER, re.S)
    assert body, "ERROR_FILTER must be a single |~ line filter with a backtick raw string"
    return re.compile(body.group(1))


# ---------- Python lock: outputs recorded on main before the multi-language work ----------

PYTHON_NORMALIZED = {
    "2026-10-15 16:21:05,120 ERROR charge failed: KeyError: 'currency'": "ERROR charge failed: KeyError: 'currency'",
    "KeyError: 'currency'": "KeyError: 'currency'",
    "2026-10-15 16:21:03,500 ERROR checkout failed: payments-svc returned 500 for user a@b.com":
        "ERROR checkout failed: payments-svc returned <n> for user a@b.com",
    "ERROR [PAYMENTS-1A] KeyError: 'currency' (charge in payments/charge.py)":
        "ERROR [PAYMENTS-<n>A] KeyError: 'currency' (charge in payments/charge.py)",
    "2026-10-15 16:24:10,442 ERROR shop-api payment failed order=2087: timeout calling payments-svc":
        "ERROR shop-api payment failed order=<n>: timeout calling payments-svc",
    "2026-10-15 16:24:10,442 ERROR shop-api payment failed order=2087: payments-svc returned 503":
        "ERROR shop-api payment failed order=<n>: payments-svc returned <n>",
    "2026-10-15 16:21:01,980 INFO payments-svc charged order=1041 amount=19.99":
        "INFO payments-svc charged order=<n> amount=<n>",
    "ERROR:    Exception in ASGI application": "ERROR:    Exception in ASGI application",
    "psycopg.OperationalError: connection failed: connection to server at \"10.0.0.5\", port 5432 failed: Connection refused":
        "psycopg.OperationalError: connection failed: connection to server at \"<n>.<n>\", port <n> failed: Connection refused",
    "RuntimeError: database unavailable": "RuntimeError: database unavailable",
    "    | ValueError: bad amount for order 1044": "ValueError: bad amount for order <n>",
    "  | ExceptionGroup: batch failed (2 sub-exceptions)": "ExceptionGroup: batch failed (<n> sub-exceptions)",
    "  File \"/app/main.py\", line 51, in charge": "File \"/app/main.py\", line <n>, in charge",
}

# Lines of python_uvicorn.log that the original ERROR_FILTER kept.
PYTHON_KEPT = [
    "ERROR:    Exception in ASGI application",
    "KeyError: 'currency'",
    "2026-10-15 16:21:02,001 ERROR payments-svc charge failed: KeyError: 'currency'",
    "2026-10-15 16:21:03,500 ERROR shop-api payment failed order=1042: payments-svc returned 500",
    "2026-10-15 16:21:04,010 ERROR shop-api payment failed order=1043: timeout calling payments-svc",
    "psycopg.OperationalError: connection failed: connection to server at \"10.0.0.5\", port 5432 failed: Connection refused",
    "RuntimeError: database unavailable",
    "    | ValueError: bad amount for order 1044",
    "    | ValueError: bad amount for order 1045",
]


def test_python_normalize_is_locked():
    for line, pattern in PYTHON_NORMALIZED.items():
        assert telemetry.normalize(line) == pattern, line


def test_python_filter_keeps_exactly_what_it_kept_before():
    rx = filter_regex()
    assert [l for l in fixture_lines("python_uvicorn.log") if rx.search(l)] == PYTHON_KEPT
