"""Language-aware log processing: Java, Node.js/TypeScript, Go and Python.

The Python lock tests come first: answer-cache fingerprints are built from
normalize() output, so the Python demo's patterns must never drift.
"""
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import context
import logparse
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


# ---------- the LogQL filter: one RE2-compatible regex ----------

def test_filter_has_no_re2_incompatible_constructs():
    rx = filter_regex().pattern
    assert "`" not in rx
    for bad in ("(?=", "(?!", "(?<=", "(?<!", "(?P=", "(?>", "*+", "++", "?+", "}+"):
        assert bad not in rx, f"RE2 does not support {bad!r}"
    assert not re.search(r"\\[1-9]", rx), "RE2 does not support backreferences"
    assert not re.search(r"\(\?[a-zA-Z]*[^a-zA-Z:)]", rx.replace("(?i)", "")), "only the (?i) flag group is used"


JAVA_KEPT = [
    "2026-10-15T16:21:02.017Z ERROR 1 --- [nio-8080-exec-2] c.a.payments.ChargeController            : "
    "charge failed for order=1042 requestId=0b9e2c4d-5f6a-4b7c-8d9e-0f1a2b3c4d5e",
    "com.acme.payments.ChargeException: could not charge order 1042",
    "Caused by: java.lang.IllegalStateException: currency missing for payment com.acme.payments.Payment@1a2b3c4d",
    "2026-10-15T16:21:02.019Z ERROR 1 --- [nio-8080-exec-2] o.a.c.c.C.[.[.[/].[dispatcherServlet]    : "
    "Servlet.service() for servlet [dispatcherServlet] in context with path [] threw exception [Request processing "
    "failed: com.acme.payments.ChargeException: could not charge order 1042] with root cause",
    "java.lang.IllegalStateException: currency missing for payment com.acme.payments.Payment@5e6f7a8b",
    'Exception in thread "main" java.lang.IllegalStateException: Failed to execute CommandLineRunner',
    "Caused by: org.postgresql.util.PSQLException: Connection to postgres:5432 refused. Check that the hostname and "
    "port are correct and that the postmaster is accepting TCP/IP connections.",
    "Caused by: java.net.ConnectException: Connection refused",
]

NODE_KEPT_PREFIXES = [
    '{"level":50,',
    '{"level":"error",',
    "TypeError: Cannot read properties of undefined (reading 'currency')",
    "Error: charge failed for order 1044",
    "  [cause]: Error: connect ECONNREFUSED 10.0.0.5:5432",
    "[UnhandledPromiseRejection: ",
]

GO_KEPT_PREFIXES = [
    '{"level":"error",',
    'time="2026-10-15T16:21:02Z" level=error',
    "2026/10/15 16:21:03 http: panic serving",
    "panic: runtime error: invalid memory address or nil pointer dereference [recovered]",
    "\tpanic: runtime error: invalid memory address or nil pointer dereference",
]


def kept(name):
    rx = filter_regex()
    return [l for l in fixture_lines(name) if rx.search(l)]


def test_filter_java_keeps_headers_summaries_and_causes_skips_frames():
    assert kept("java_spring.log") == JAVA_KEPT


def test_filter_node_keeps_summaries_causes_and_error_json():
    got = kept("node_ts.log")
    assert len(got) == len(NODE_KEPT_PREFIXES)
    assert all(l.startswith(p) for l, p in zip(got, NODE_KEPT_PREFIXES))


def test_filter_go_keeps_panics_and_error_json_skips_goroutine_dumps():
    got = kept("go_panic.log")
    assert len(got) == len(GO_KEPT_PREFIXES)
    assert all(l.startswith(p) for l, p in zip(got, GO_KEPT_PREFIXES))


def test_filter_skips_frames_and_noise_in_every_language():
    rx = filter_regex()
    skipped = [
        "\tat com.acme.payments.ChargeService.charge(ChargeService.java:57)",
        "\t... 48 common frames omitted",
        "\t... 5 more",
        "    at charge (/app/src/charge.ts:42:15)",
        "    at async /app/src/routes.ts:18:5 {",
        "    at Object.<anonymous> (/app/src/errorHandler.ts:9:3)",
        "goroutine 1 [running]:",
        "main.(*Server).charge(0xc0000a4000, {0x7a1b40, 0xc0001c2000}, 0xc0001b6000)",
        "main.handleError(0xc0000a4000)",  # a frame whose name contains "Error"
        "\t/app/charge.go:58 +0x1d",
        "  File \"/app/main.py\", line 51, in charge",
        '{"level":30,"time":1760545261884,"msg":"charging order 1041"}',
        '{"level":"info","ts":1760545261.884,"msg":"charging"}',
        "2026-10-15T16:21:01.884Z  INFO 1 --- [nio-8080-exec-1] c.a.payments.ChargeController : charging order=1041",
    ]
    for line in skipped:
        assert not rx.search(line), line
    for line in ["Error: connect ECONNREFUSED 10.0.0.5:5432", "java.lang.NullPointerException",
                 "TypeError [ERR_INVALID_ARG_TYPE]: The \"path\" argument must be of type string",
                 "fatal error: concurrent map writes", '{"level":60,"msg":"boom"}', '{"severity":"ERROR","message":"x"}']:
        assert rx.search(line), line


# ---------- folding and clustering ----------

T0 = datetime(2026, 10, 15, 16, 21, tzinfo=timezone.utc)


def entries(lines, svc="payments-svc", start=T0, step_ms=1):
    return [(start + timedelta(milliseconds=i * step_ms), svc, l) for i, l in enumerate(lines)]


def java_trace(n):
    return [
        f"2026-10-15T16:21:{n:02d}.017Z ERROR 1 --- [nio-8080-exec-{n}] c.a.payments.ChargeController : "
        f"charge failed for order={1000 + n} requestId={uuid.UUID(int=n * 7919)}",
        f"com.acme.payments.ChargeException: could not charge order {1000 + n}",
        "\tat com.acme.payments.ChargeService.charge(ChargeService.java:57)",
        "\tat org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:885)",
        f"Caused by: java.lang.IllegalStateException: currency missing for payment com.acme.payments.Payment@{n * 7919 + 4096:x}a",
        "\tat com.acme.payments.CurrencyResolver.resolve(CurrencyResolver.java:42)",
        f"\t... {40 + n} common frames omitted",
    ]


def node_trace(n):
    return [
        "TypeError: Cannot read properties of undefined (reading 'currency')",
        "    at charge (/app/src/charge.ts:42:15)",
        "    at /app/src/routes.ts:18:22",
        "    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)",
    ]


def node_json(n):
    stack = ("TypeError: Cannot read properties of undefined (reading 'currency')\\n"
             "    at charge (/app/src/charge.ts:42:15)\\n    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)")
    return [f'{{"level":50,"time":{1760545262017 + n},"pid":1,"hostname":"payments-7d9f8c6b5-x{n}x9k",'
            f'"reqId":"req-{n}","err":{{"type":"TypeError","message":"x","stack":"{stack}"}},"msg":"charge failed"}}']


def go_trace(n):
    return [
        "panic: runtime error: invalid memory address or nil pointer dereference [recovered]",
        "\tpanic: runtime error: invalid memory address or nil pointer dereference",
        f"[signal SIGSEGV: segmentation violation code=0x1 addr=0x{n:x}0 pc=0x6a8f{n:02x}]",
        "",
        f"goroutine {n + 30} [running]:",
        f"main.(*Server).charge(0xc0000a{n:03x}, {{0x7a1b40, 0xc0001c2000}}, 0xc0001b6000)",
        f"\t/app/charge.go:58 +0x{n + 16:x}",
        "net/http.HandlerFunc.ServeHTTP(0xc0000a6010, {0x7a1b40, 0xc0001c2000}, 0xc0001b6000)",
        "\t/usr/local/go/src/net/http/server.go:2166 +0x29",
        "exit status 2",
    ]


def python_trace(n):
    return [
        "Traceback (most recent call last):",
        '  File "/usr/local/lib/python3.12/site-packages/starlette/routing.py", line 72, in app',
        "    response = await func(request)",
        '  File "/app/main.py", line 51, in charge',
        '    currency = payload["currency"]  # KeyError -> 500',
        "               ~~~~~~~^^^^^^^^^^^^",
        "KeyError: 'currency'",
    ]


TRACES = {"java": java_trace, "node": node_trace, "node-json": node_json, "go": go_trace, "python": python_trace}


@pytest.mark.parametrize("lang", TRACES)
@pytest.mark.parametrize("shipping", ["line-per-entry", "line-per-entry-filtered", "one-entry-per-trace"])
def test_repeated_traces_make_one_pattern(lang, shipping):
    """N occurrences of the same error, with different ids, addresses and hashes, are one pattern."""
    n = 6
    rx = filter_regex()
    lines = []
    for i in range(1, n + 1):
        trace = TRACES[lang](i)
        if shipping == "one-entry-per-trace":
            lines.append((T0 + timedelta(seconds=10 * i), "payments-svc", "\n".join(trace)))
            continue
        if shipping == "line-per-entry-filtered":
            trace = [l for l in trace if rx.search(l)]
            assert trace, "the filter must keep something from every trace"
        lines += entries(trace, start=T0 + timedelta(seconds=10 * i))
    clusters = telemetry.cluster_logs(lines)
    assert [c["count"] for c in clusters] == [n], clusters
    assert clusters[0]["services"] == ["payments-svc"]


def test_python_single_lines_still_cluster_exactly_as_before():
    """The filtered Python stream is never folded: each kept line is its own event."""
    lines = entries(PYTHON_KEPT, step_ms=1)
    clusters = telemetry.cluster_logs(lines)
    assert sum(c["count"] for c in clusters) == len(PYTHON_KEPT)
    for c in clusters:
        assert telemetry.normalize(c["example"]) in {telemetry.normalize(l) for l in PYTHON_KEPT}


def test_traces_of_two_services_interleaved_are_folded_separately():
    a, b = java_trace(1), node_trace(1)
    lines = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            lines.append((T0 + timedelta(milliseconds=2 * i), "shop-api", a[i]))
        if i < len(b):
            lines.append((T0 + timedelta(milliseconds=2 * i + 1), "payments-svc", b[i]))
    clusters = telemetry.cluster_logs(lines)
    assert sorted((c["services"][0], c["count"]) for c in clusters) == [("payments-svc", 1), ("shop-api", 1)]


def test_lines_far_apart_in_time_are_not_folded():
    lines = entries(["com.acme.payments.ChargeException: a"], start=T0)
    lines += entries(["Caused by: java.net.ConnectException: Connection refused"], start=T0 + timedelta(seconds=30))
    assert len(telemetry.cluster_logs(lines)) == 2


# ---------- root causes and locations ----------

def by_lang(name):
    return telemetry.cluster_logs(entries(fixture_lines(name)))


def test_java_chain_prefers_the_deepest_cause_and_keeps_the_outer_type():
    lines = entries(java_trace(1))
    (c,) = telemetry.cluster_logs(lines)
    assert c["root_cause"].startswith("java.lang.IllegalStateException: currency missing")
    assert c["error_type"] == "java.lang.IllegalStateException"
    assert "com.acme.payments.ChargeException: could not charge order 1001" in c["example"]
    assert "caused by java.lang.IllegalStateException" in c["example"]
    assert c["location"] == "CurrencyResolver.java:42"
    assert c["language"] == "java"
    # Keyed by the root cause: a different wrapper with the same root is the same pattern.
    assert telemetry.normalize(c["root_cause"]) == "java.lang.IllegalStateException: currency missing for payment com.acme.payments.Payment@<hash>"


def test_java_fixture_folds_spring_root_cause_and_three_level_chains():
    clusters = by_lang("java_spring.log")
    currency = next(c for c in clusters if "currency missing" in c["example"])
    assert currency["count"] == 2  # logback's "Caused by" trace and Spring's "with root cause" line
    startup = next(c for c in clusters if "CommandLineRunner" in c["example"])
    assert startup["root_cause"] == "java.net.ConnectException: Connection refused"
    # The root cause's frames are all library code: fall back outward to the app's frame.
    assert startup["location"] == "Migrations.java:23"
    assert startup["example"].endswith("[at Migrations.java:23]")


def test_python_chain_root_cause_and_innermost_app_frame():
    clusters = by_lang("python_uvicorn.log")
    db = next(c for c in clusters if "database unavailable" in c["example"])
    assert db["error_type"] == "psycopg.OperationalError"
    assert db["root_cause"].startswith("psycopg.OperationalError: connection failed")
    assert db["example"].startswith("RuntimeError: database unavailable | caused by psycopg.OperationalError")
    assert db["location"] == "db.py:12"
    key = next(c for c in clusters if c["example"].startswith("KeyError"))
    assert key["example"] == "KeyError: 'currency' [at main.py:51]"


def test_node_locations_causes_and_rejections():
    clusters = by_lang("node_ts.log")
    te = next(c for c in clusters if c["example"].startswith("TypeError"))
    assert te["count"] == 2  # the pino JSON line and the uncaught crash
    assert te["example"] == "TypeError: Cannot read properties of undefined (reading 'currency') [at src/charge.ts:42]"
    assert te["language"] == "node"
    cause = next(c for c in clusters if "charge failed for order 1044" in c["example"])
    assert cause["root_cause"] == "Error: connect ECONNREFUSED 10.0.0.5:5432"
    assert cause["location"] == "src/charge.ts:51"  # node:net frames are skipped
    rej = next(c for c in clusters if c.get("error_type") == "UnhandledPromiseRejection")
    assert rej["example"] == "UnhandledPromiseRejection: ledger write failed for order 1043"


def test_go_panics_zap_and_locations():
    clusters = by_lang("go_panic.log")
    panic = next(c for c in clusters if c["example"].startswith("panic:"))
    assert panic["count"] == 2  # the recovered http panic and the crash
    assert panic["example"] == "panic: runtime error: invalid memory address or nil pointer dereference [at charge.go:58]"
    zap = next(c for c in clusters if "duplicate key" in c["example"])
    assert zap["example"].startswith("ERROR charge failed: ledger: write payment 1042")
    assert zap["location"] == "charge.go:47"


@pytest.mark.parametrize("line,expected", [
    ("\tat com.acme.payments.CurrencyResolver.resolve(CurrencyResolver.java:42)", ("CurrencyResolver.java:42", True)),
    ("\tat java.base/java.lang.Thread.run(Thread.java:1583)", ("Thread.java:1583", False)),
    ("\tat org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:885)", ("FrameworkServlet.java:885", False)),
    ("\tat java.base/sun.nio.ch.Net.pollConnect(Native Method)", None),
    ("    at charge (/app/src/charge.ts:42:15)", ("src/charge.ts:42", True)),
    ("    at async /app/src/routes.ts:18:5 {", ("src/routes.ts:18", True)),
    ("    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)", ("lib/router/layer.js:95", False)),
    ("    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)", ("node:internal/process/task_queues:95", False)),
    ("\t/app/charge.go:58 +0x1d", ("charge.go:58", True)),
    ("\t/usr/local/go/src/runtime/panic.go:770 +0x132", ("src/runtime/panic.go:770", False)),
    ("\t/go/pkg/mod/github.com/lib/pq@v1.10.9/conn.go:1061 +0x2b", ("lib/pq@v1.10.9/conn.go:1061", False)),
    ('  File "/app/main.py", line 51, in charge', ("main.py:51", True)),
    ('  File "/usr/local/lib/python3.12/site-packages/starlette/routing.py", line 72, in app', ("site-packages/starlette/routing.py:72", False)),
])
def test_frame_locations_skip_vendor_code(line, expected):
    assert logparse.frame_location(line) == expected


@pytest.mark.parametrize("name,lang", [("java_spring.log", "java"), ("node_ts.log", "node"),
                                       ("go_panic.log", "go"), ("python_uvicorn.log", "python")])
def test_language_hint(name, lang):
    assert logparse.detect_language(fixture_lines(name)) == lang


def test_no_language_hint_without_markers():
    assert logparse.detect_language(["2026-10-15 16:21:05,120 ERROR charge failed"]) is None


# ---------- normalize: volatile values don't split clusters, different errors stay apart ----------

@pytest.mark.parametrize("a,b", [
    ("charge failed requestId=7f3a9c2e-1b4d-4e8a-9c3f-2a1b0c9d8e7f", "charge failed requestId=0b9e2c4d-5f6a-4b7c-8d9e-0f1a2b3c4d5e"),
    ("lookup 123e4567-e89b-12d3-a456-426614174000 failed", "lookup 9f1c2d3e-aaaa-4bbb-8ccc-0123456789ab failed"),
    ("nil map at 0x1d", "nil map at 0xc000ab12f0"),
    ("bad state com.acme.Order@1a2b3c", "bad state com.acme.Order@9f8e7d6c"),
    ('{"req_id": "abc-XYZ"} failed', '{"req_id": "q-9"} failed'),
    ("trace_id=4bf92f3577b34da6 timeout", "trace_id=00f067aa0ba902b7 timeout"),
    ("call took 250ms", "call took 1.5s"),
    ("deadline after 1m30s", "deadline after 45s"),
    ("refund for ord_9f8e7d2a failed", "refund for ord_1a2b3c4d failed"),
    ("pod payments-7d9f8c6b5 OOM", "pod payments-5c4b3a291 OOM"),
    ("For input string: \"12abc\"", "For input string: \"7xyz9\""),
    ("unknown token 'eyJhbGciOiJIUzI1NiJ9abcdef'", "unknown token 'aGVsbG8gd29ybGQgdGhpcyBpcw'"),
    ("2026-10-15T16:21:02.017Z ERROR boom", "2026-10-16T09:01:00.5+02:00 ERROR boom"),
    ("2026/10/15 16:21:03 http: panic serving 172.18.0.7:51240: x", "2026/10/16 01:00:00 http: panic serving 10.0.0.9:3: x"),
])
def test_normalize_merges_volatile_values(a, b):
    assert telemetry.normalize(a) == telemetry.normalize(b)


@pytest.mark.parametrize("a,b", [
    ("KeyError: 'currency'", "KeyError: 'amount'"),
    ("TypeError: Cannot read properties of undefined (reading 'currency')",
     "TypeError: Cannot read properties of undefined (reading 'amount')"),
    ("java.lang.IllegalStateException: currency missing", "java.lang.IllegalArgumentException: currency missing"),
    ("panic: runtime error: index out of range [3] with length 3",
     "panic: runtime error: invalid memory address or nil pointer dereference"),
    ("connect ECONNREFUSED 10.0.0.5:5432", "connect ETIMEDOUT 10.0.0.5:5432"),
    ('duplicate key value violates unique constraint "payments_order_id_key"',
     'duplicate key value violates unique constraint "refunds_order_id_key"'),
])
def test_normalize_keeps_different_errors_apart(a, b):
    assert telemetry.normalize(a) != telemetry.normalize(b)


# ---------- victims vs sources across languages ----------

def test_source_service_in_a_mixed_java_and_node_incident():
    """shop-api (Spring) logs first, but its errors name payments-svc (Node), so
    payments-svc is the source and shop-api the victim."""
    shop = [
        "2026-10-15T16:21:01.100Z ERROR 1 --- [nio-8080-exec-4] c.a.shop.CheckoutController : "
        "checkout failed for order=2087: payments-svc returned 500",
        "org.springframework.web.client.HttpServerErrorException$InternalServerError: 500 Internal Server Error: "
        "\"{\"error\":\"internal\"}\"",
        "\tat org.springframework.web.client.DefaultResponseErrorHandler.handleError(DefaultResponseErrorHandler.java:102)",
        "\tat com.acme.shop.PaymentsClient.charge(PaymentsClient.java:37)",
        "\tat com.acme.shop.CheckoutController.checkout(CheckoutController.java:58)",
    ]
    rx = filter_regex()
    for filtered in (False, True):
        lines = []
        for i in range(4):
            start = T0 + timedelta(seconds=i)
            lines += entries(shop, "shop-api", start + timedelta(milliseconds=100))
            lines += entries(node_trace(i), "payments-svc", start + timedelta(milliseconds=600))
        lines.sort(key=lambda x: x[0])
        if filtered:
            lines = [x for x in lines if rx.search(x[2])]
        onset = context._error_onset(lines)
        assert next(iter(onset)) == "shop-api"  # timing alone would blame the victim
        clusters = telemetry.cluster_logs(lines)
        shop_c = next(c for c in clusters if c["services"] == ["shop-api"])
        assert shop_c["count"] == 4 and shop_c["language"] == "java"
        if not filtered:
            assert shop_c["location"] == "PaymentsClient.java:37"
        assert next(c for c in clusters if c["services"] == ["payments-svc"])["language"] == "node"
        source, why = context.source_service(onset, clusters)
        assert source == "payments-svc", why
        assert "point at payments-svc (4 lines)" in why
