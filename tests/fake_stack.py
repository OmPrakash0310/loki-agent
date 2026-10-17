"""A fake Grafana Loki + Prometheus, served through httpx.MockTransport.

telemetry.py calls the module-level httpx.get, so tests swap that for a client
backed by this handler. No network, no Docker.
"""
from datetime import datetime, timezone

import httpx

NOW = datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc)


def _ns(h, m, s):
    return str(int(datetime(2026, 10, 15, h, m, s, tzinfo=timezone.utc).timestamp() * 1e9))


ERROR_STREAMS = [
    {"stream": {"service": "payments-svc"}, "values": [
        [_ns(16, 21, 5), "2026-10-15 16:21:05,120 ERROR charge failed: KeyError: 'currency'"],
        [_ns(16, 21, 2), "2026-10-15 16:21:02,001 ERROR charge failed: KeyError: 'currency'"],
        [_ns(16, 21, 1), "KeyError: 'currency'"],
    ]},
    {"stream": {"service": "shop-api"}, "values": [
        [_ns(16, 21, 3), "2026-10-15 16:21:03,500 ERROR checkout failed: payments-svc returned 500 for user a@b.com"],
    ]},
]


def handler(request: httpx.Request) -> httpx.Response:
    path, q = request.url.path, request.url.params
    if path == "/loki/api/v1/query_range":
        errors_only = "|~" in q["query"]
        return httpx.Response(200, json={"data": {"result": ERROR_STREAMS if errors_only else ERROR_STREAMS[:1]}})
    if path == "/api/v1/label/service/values":
        deps = "match[]" in q
        return httpx.Response(200, json={"data": ["payments-svc"] if deps else ["payments-svc", "shop-api"]})
    if path == "/api/v1/query":
        if "dependency" in q["query"]:
            return httpx.Response(200, json={"data": {"result": [{"metric": {"dependency": "postgres"}, "value": [0, "0.012"]}]}})
        return httpx.Response(200, json={"data": {"result": [{"metric": {}, "value": [0, "0.25"]}]}})
    if path == "/api/v1/query_range":
        return httpx.Response(200, json={"data": {"result": [{"metric": {}, "values": [[1, "0.01"], [2, "0.02"], [3, "NaN"]]}]}})
    return httpx.Response(404, json={"error": f"unexpected {path}"})


def install(monkeypatch, telemetry):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(telemetry.httpx, "get", client.get)
    monkeypatch.setattr(telemetry, "now", lambda: NOW)
