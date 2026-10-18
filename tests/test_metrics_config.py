"""Configurable metric and log label names.

The first block locks the exact queries the demo sends (written before the
refactor), so the default settings can never drift. All HTTP is mocked.
"""
import os
import sys
from datetime import datetime, timezone

import httpx
import pytest

import telemetry  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
import fake_stack  # noqa: E402

AT = datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc)


def recorder(monkeypatch, handler=fake_stack.handler):
    calls = []

    def h(request):
        calls.append(request)
        return handler(request)
    client = httpx.Client(transport=httpx.MockTransport(h))
    monkeypatch.setattr(telemetry.httpx, "get", client.get)
    return calls


def _queries(calls):
    return [(c.url.path, c.url.params.get("query") or c.url.params.get("match[]")) for c in calls]


# ---------------- default queries are byte-identical to before ----------------

DEFAULT_PROMQL = {
    "error_rate": ('(sum(rate(http_requests_total{service="payments-svc",status=~"5.."}[1m])) or vector(0))'
                   ' / sum(rate(http_requests_total{service="payments-svc"}[1m]))'),
    "p95_latency_seconds": ('histogram_quantile(0.95, sum by (le) '
                            '(rate(http_request_duration_seconds_bucket{service="payments-svc"}[1m])))'),
    "request_rate": 'sum(rate(http_requests_total{service="payments-svc"}[1m]))',
}
DEFAULT_DEP = ('histogram_quantile(0.95, sum by (le, dependency) '
               '(rate(dependency_duration_seconds_bucket{service="payments-svc"}[1m])))')


def test_default_promql_is_unchanged(monkeypatch):
    calls = recorder(monkeypatch)
    for metric in ("error_rate", "p95_latency_seconds", "request_rate"):
        telemetry.metric_at(metric, "payments-svc", AT)
        telemetry.metric_series(metric, "payments-svc", AT.replace(minute=10), AT)
    telemetry.dependency_p95_at("payments-svc", AT)
    telemetry.dependency_p95_series("payments-svc", AT.replace(minute=10), AT)
    q = [x for x in _queries(calls) if x[0] != "/api/v1/label/__name__/values"]
    expected = []
    for metric in ("error_rate", "p95_latency_seconds", "request_rate"):
        expected += [("/api/v1/query", DEFAULT_PROMQL[metric]), ("/api/v1/query_range", DEFAULT_PROMQL[metric])]
    expected += [("/api/v1/query", DEFAULT_DEP), ("/api/v1/query_range", DEFAULT_DEP)]
    assert q == expected


def test_default_label_lookups_and_logql_are_unchanged(monkeypatch):
    calls = recorder(monkeypatch)
    assert telemetry.services() == ["payments-svc", "shop-api"]
    assert telemetry.services_with_dependencies() == ["payments-svc"]
    telemetry.fetch_logs("payments-svc", AT.replace(minute=10), AT, errors_only=False)
    telemetry.fetch_logs(None, AT.replace(minute=10), AT, errors_only=False)
    lines = telemetry.fetch_logs(None, AT.replace(minute=10), AT, errors_only=True)
    assert {svc for _, svc, _ in lines} == {"payments-svc", "shop-api"}
    assert _queries(calls) == [
        ("/api/v1/label/service/values", None),
        ("/api/v1/label/service/values", "dependency_duration_seconds_count"),
        ("/loki/api/v1/query_range", '{service="payments-svc"}'),
        ("/loki/api/v1/query_range", '{service=~".+", service!="agent"}'),
        ("/loki/api/v1/query_range", '{service=~".+", service!="agent"} ' + telemetry.ERROR_FILTER),
    ]
