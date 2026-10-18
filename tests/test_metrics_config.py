"""Configurable metric and log label names.

The first block locks the exact queries the demo sends (written before the
refactor), so the default settings can never drift. All HTTP is mocked.
"""
import json
import os
import sys
from datetime import datetime, timezone

import httpx
import pytest

import context  # noqa: E402
import metrics_config  # noqa: E402
import sources  # noqa: E402
import telemetry  # noqa: E402
import tools  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
import fake_stack  # noqa: E402

AT = datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc)
HERE = os.path.dirname(__file__)
INCIDENT = [{"started_at": datetime(2026, 10, 15, 16, 22, tzinfo=timezone.utc), "alert_name": "HighErrorRate",
             "service": "payments-svc", "severity": "critical", "summary": "5xx > 5%"}]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in ("LOKI_METRICS_PRESET", "LOKI_METRICS_CONFIG", "LOKI_DEPLOYS_SOURCE", "LOKI_ERRORS_SOURCE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(telemetry, "DEPLOYS_FILE", os.path.join(HERE, "fixtures", "deploys.json"))
    telemetry.reset_metric_cache()
    sources.reset()
    yield
    telemetry.reset_metric_cache()
    sources.reset()


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
    # (the metric-name lookup is new and only reads; the queries themselves are unchanged)
    assert [x for x in _queries(calls) if x[0] != "/api/v1/label/__name__/values"] == [
        ("/api/v1/label/service/values", None),
        ("/api/v1/label/service/values", "dependency_duration_seconds_count"),
        ("/loki/api/v1/query_range", '{service="payments-svc"}'),
        ("/loki/api/v1/query_range", '{service=~".+", service!="agent"}'),
        ("/loki/api/v1/query_range", '{service=~".+", service!="agent"} ' + telemetry.ERROR_FILTER),
    ]


def test_default_preset_builds_the_same_strings():
    cfg = metrics_config.load()
    assert cfg == metrics_config.PRESETS["default"] == metrics_config.current()
    for metric, q in DEFAULT_PROMQL.items():
        assert cfg.promql(metric, "payments-svc") == q
    assert cfg.dependency_promql("payments-svc") == DEFAULT_DEP
    assert cfg.services_match() is None


# ---------------- presets ----------------

PRESET_QUERIES = {
    "otel": {
        "error_rate": ('(sum(rate(http_server_request_duration_seconds_count{service_name="checkout",'
                       'http_response_status_code=~"5.."}[1m])) or vector(0))'
                       ' / sum(rate(http_server_request_duration_seconds_count{service_name="checkout"}[1m]))'),
        "p95_latency_seconds": ('histogram_quantile(0.95, sum by (le) '
                                '(rate(http_server_request_duration_seconds_bucket{service_name="checkout"}[1m])))'),
        "request_rate": 'sum(rate(http_server_request_duration_seconds_count{service_name="checkout"}[1m]))',
        "dependency": ('histogram_quantile(0.95, sum by (le, server_address) '
                       '(rate(http_client_request_duration_seconds_bucket{service_name="checkout"}[1m])))'),
        "logs": '{service_name="checkout"}',
        "all_logs": '{service_name=~".+", service_name!="agent"}',
        "services_match": "http_server_request_duration_seconds_count",
    },
    "spring": {
        "error_rate": ('(sum(rate(http_server_requests_seconds_count{application="checkout",status=~"5.."}[1m]))'
                       ' or vector(0)) / sum(rate(http_server_requests_seconds_count{application="checkout"}[1m]))'),
        "p95_latency_seconds": ('histogram_quantile(0.95, sum by (le) '
                                '(rate(http_server_requests_seconds_bucket{application="checkout"}[1m])))'),
        "request_rate": 'sum(rate(http_server_requests_seconds_count{application="checkout"}[1m]))',
        "dependency": ('histogram_quantile(0.95, sum by (le, client_name) '
                       '(rate(http_client_requests_seconds_bucket{application="checkout"}[1m])))'),
        "logs": '{application="checkout"}',
        "all_logs": '{application=~".+", application!="agent"}',
        "services_match": "http_server_requests_seconds_count",
    },
    "express-prom": {
        "error_rate": ('(sum(rate(http_request_duration_seconds_count{job="checkout",status_code=~"5.."}[1m]))'
                       ' or vector(0)) / sum(rate(http_request_duration_seconds_count{job="checkout"}[1m]))'),
        "p95_latency_seconds": ('histogram_quantile(0.95, sum by (le) '
                                '(rate(http_request_duration_seconds_bucket{job="checkout"}[1m])))'),
        "request_rate": 'sum(rate(http_request_duration_seconds_count{job="checkout"}[1m]))',
        "dependency": None,
        "logs": '{job="checkout"}',
        "all_logs": '{job=~".+", job!="agent"}',
        "services_match": "http_request_duration_seconds_count",
    },
}


@pytest.mark.parametrize("preset", sorted(PRESET_QUERIES))
def test_preset_queries(monkeypatch, preset):
    monkeypatch.setenv("LOKI_METRICS_PRESET", preset)
    cfg, want = metrics_config.current(), PRESET_QUERIES[preset]
    for metric in metrics_config.METRIC_NAMES:
        assert cfg.promql(metric, "checkout") == want[metric]
    if want["dependency"]:
        assert cfg.dependency_promql("checkout") == want["dependency"]
    else:
        with pytest.raises(ValueError, match="no dependency metric configured"):
            cfg.dependency_promql("checkout")
    assert cfg.log_selector("checkout") == want["logs"]
    assert cfg.log_selector(None) == want["all_logs"]
    assert cfg.services_match() == want["services_match"]


def test_config_file_overrides_a_preset(monkeypatch, tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text('{"preset": "spring", "service_label": "job", "status_5xx": "5[0-9]{2}", '
                    '"dependency_histogram": null, "log_service_label": "app"}')
    monkeypatch.setenv("LOKI_METRICS_CONFIG", str(path))
    cfg = metrics_config.current()
    assert cfg.promql("error_rate", "pay").startswith(
        '(sum(rate(http_server_requests_seconds_count{job="pay",status=~"5[0-9]{2}"}[1m]))')
    assert cfg.dependency_histogram is None and cfg.log_selector("pay") == '{app="pay"}'


def test_bad_config_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="unknown metrics preset"):
        metrics_config.load("datadog")
    path = tmp_path / "m.json"
    path.write_text('{"servce_label": "job"}')
    with pytest.raises(ValueError, match="unknown metrics config keys: servce_label"):
        metrics_config.load(None, str(path))
    path.write_text('{"service_label": "job\\"}) or vector(1"}')
    with pytest.raises(ValueError, match="not a valid label name"):
        metrics_config.load(None, str(path))


def test_label_values_are_escaped():
    cfg = metrics_config.load()
    assert cfg.promql("request_rate", 'a"b') == 'sum(rate(http_requests_total{service="a\\"b"}[1m]))'


def test_custom_log_label_is_used_for_the_query_and_the_stream(monkeypatch):
    monkeypatch.setenv("LOKI_METRICS_PRESET", "otel")
    seen = []

    def handler(request):
        seen.append(request.url.params["query"])
        return httpx.Response(200, json={"data": {"result": [
            {"stream": {"service_name": "checkout"}, "values": [[str(int(AT.timestamp() * 1e9) - 10**9), "ERROR boom"]]}]}})
    recorder(monkeypatch, handler)
    lines = telemetry.fetch_logs(None, AT.replace(minute=10), AT, errors_only=True)
    assert seen == ['{service_name=~".+", service_name!="agent"} ' + telemetry.ERROR_FILTER]
    assert [svc for _, svc, _ in lines] == ["checkout"]


# ---------------- graceful degradation ----------------

def _stack_with_names(names):
    """fake_stack, plus a metric-name list that tells which families exist."""
    def handler(request):
        if request.url.path == "/api/v1/label/__name__/values":
            return httpx.Response(200, json={"status": "success", "data": names})
        return fake_stack.handler(request)
    return handler


def test_missing_latency_and_dependency_metrics_are_skipped_with_a_note(monkeypatch):
    calls = recorder(monkeypatch, _stack_with_names(["http_requests_total", "up"]))
    monkeypatch.setattr(telemetry, "now", lambda: fake_stack.NOW)
    text = context.build(INCIDENT).text
    assert ("- note: p95_latency_seconds skipped: no latency metric found "
            "(http_request_duration_seconds_bucket is not in Prometheus)") in text
    assert "p95_latency_seconds 0" not in text
    assert "- payments-svc: error_rate 0.02 -> 0.25; request_rate 0.02 -> 0.25" in text
    assert ("## Dependency latency\n- skipped: no dependency latency metric found "
            "(dependency_duration_seconds_bucket is not in Prometheus)") in text
    queries = [c.url.params.get("query", "") for c in calls]
    assert not any("duration_seconds_bucket" in q for q in queries)
    # the metric-name lookup happens once, then it's cached
    assert sum(c.url.path == "/api/v1/label/__name__/values" for c in calls) == 1

    out = tools.query_metrics("p95_latency_seconds", "payments-svc")
    assert out["points"] == [] and "no latency metric found" in out["note"]
    dep = tools.query_dependencies("payments-svc")
    assert dep["dependencies"] == {} and "no dependency latency metric found" in dep["note"]
    text_out, is_error = tools.run("query_metrics", {"metric": "p95_latency_seconds", "service": "x"})
    assert not is_error and "not available" in text_out


def test_preset_without_dependency_metric(monkeypatch):
    monkeypatch.setenv("LOKI_METRICS_PRESET", "express-prom")
    recorder(monkeypatch)
    assert telemetry.dependency_unavailable() == "no dependency latency metric configured"
    assert telemetry.services_with_dependencies() == []
    assert telemetry.dependency_p95_at("checkout", AT) == {}


def test_unconfigured_status_label_skips_only_error_rate(monkeypatch, tmp_path):
    path = tmp_path / "m.json"
    path.write_text('{"status_label": null}')
    monkeypatch.setenv("LOKI_METRICS_CONFIG", str(path))
    recorder(monkeypatch)
    assert telemetry.metric_unavailable("error_rate") == "no status-code label configured"
    assert telemetry.metric_unavailable("request_rate") is None
    assert telemetry.metric_at("error_rate", "payments-svc", AT) is None


def test_name_lookup_failure_assumes_metrics_exist(monkeypatch):
    recorder(monkeypatch)  # fake_stack answers 404 for the name list
    assert all(telemetry.metric_unavailable(m) is None for m in telemetry.METRICS)
    assert telemetry.dependency_unavailable() is None


def test_no_prometheus_briefing_uses_logs_errors_and_deploys(monkeypatch):
    monkeypatch.setattr(telemetry, "PROM_URL", "")
    calls = recorder(monkeypatch)
    monkeypatch.setattr(telemetry, "now", lambda: fake_stack.NOW)
    b = context.build(INCIDENT)
    assert all(c.url.path.startswith("/loki/") for c in calls), "no Prometheus request may be sent"
    assert ("## Metrics: typical value before the first alert -> now\n- skipped: no Prometheus configured "
            "(LOKI_PROM_URL is empty); this briefing uses logs, errors and deploys only\n\n## Error log patterns") in b.text
    assert "x2 in payments-svc" in b.text and "## Deploys in the 2 hours" in b.text
    assert b.suspects[0]["service"] == "payments-svc"
    assert telemetry.services() == [] and telemetry.services_with_dependencies() == []
    out = tools.query_metrics("error_rate", "payments-svc")
    assert "no Prometheus configured" in out["note"]
    assert "no Prometheus configured" in tools.query_dependencies("payments-svc")["note"]


# ---------------- replay guards still apply ----------------

def test_replay_caps_custom_queries_and_drops_future_points(monkeypatch):
    monkeypatch.setenv("LOKI_METRICS_PRESET", "otel")
    replay_at = datetime(2026, 10, 15, 16, 25, tzinfo=timezone.utc)
    seen = []

    def handler(request):
        seen.append(request)
        p = request.url.path
        if p == "/api/v1/label/__name__/values":
            return httpx.Response(200, json={"data": ["http_server_request_duration_seconds_count",
                                                      "http_server_request_duration_seconds_bucket",
                                                      "http_client_request_duration_seconds_bucket"]})
        if p == "/api/v1/query_range":
            ts = [replay_at.timestamp() - 30, replay_at.timestamp(), replay_at.timestamp() + 30]
            pts = [[t, "0.5"] for t in ts]
            metric = {"server_address": "db"} if "server_address" in request.url.params["query"] else {}
            return httpx.Response(200, json={"data": {"result": [{"metric": metric, "values": pts}]}})
        if p == "/api/v1/query":
            return httpx.Response(200, json={"data": {"result": [{"metric": {"server_address": "db"}, "value": [0, "0.3"]}]}})
        if p.startswith("/api/v1/label/"):
            return httpx.Response(200, json={"data": ["checkout"]})
        return httpx.Response(200, json={"data": {"result": [
            {"stream": {"service_name": "checkout"},
             "values": [[str(int((replay_at.timestamp() + 60) * 1e9)), "ERROR from the future"],
                        [str(int((replay_at.timestamp() - 60) * 1e9)), "ERROR from the past"]]}]}})
    recorder(monkeypatch, handler)
    with telemetry.replay(replay_at):
        pts = telemetry.metric_series("request_rate", "checkout", AT.replace(minute=0), AT)
        deps = telemetry.dependency_p95_series("checkout", AT.replace(minute=0), AT)
        telemetry.metric_at("error_rate", "checkout", AT)
        lines = telemetry.fetch_logs("checkout", AT.replace(minute=0), AT, errors_only=True)
        telemetry.services()
    assert [t for t, _ in pts] == [datetime.fromtimestamp(replay_at.timestamp() + d, timezone.utc) for d in (-30, 0)]
    assert len(deps["db"]) == 2
    assert [l for _, _, l in lines] == ["ERROR from the past"]
    for r in seen:
        q = r.url.params
        if r.url.path == "/api/v1/query_range":
            assert float(q["end"]) == replay_at.timestamp()
        if r.url.path == "/api/v1/query":
            assert float(q["time"]) == replay_at.timestamp()
        if r.url.path == "/loki/api/v1/query_range":
            assert int(q["end"]) == int(replay_at.timestamp() * 1e9)
        if r.url.path == "/api/v1/label/service_name/values":
            assert q["match[]"] == "http_server_request_duration_seconds_count"
            assert float(q["end"]) == replay_at.timestamp()


# ---------------- discovery ----------------

sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import discover_metrics  # noqa: E402


def _prom_api(names, series):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET"
        if request.url.path == "/api/v1/label/__name__/values":
            return httpx.Response(200, json={"status": "success", "data": names})
        if request.url.path == "/api/v1/series":
            return httpx.Response(200, json={"status": "success", "data": series.get(request.url.params["match[]"], [])})
        return httpx.Response(404, json={"error": "nope"})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://prom"), seen


def test_discovery_suggests_a_spring_config(capsys):
    names = ["up", "jvm_memory_used_bytes", "http_server_requests_seconds_bucket", "http_server_requests_seconds_count",
             "http_server_requests_seconds_sum", "http_client_requests_seconds_bucket",
             "http_client_requests_seconds_count", "process_cpu_seconds_total"]
    series = {
        "http_server_requests_seconds_count": [{"__name__": "x", "application": "orders", "job": "orders",
                                                "status": "200", "uri": "/orders", "instance": "a"}],
        "http_client_requests_seconds_count": [{"__name__": "x", "application": "orders", "client_name": "stripe"}],
    }
    client, seen = _prom_api(names, series)
    assert discover_metrics.main(["--prom", "http://prom", "--json"], client=client) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert cfg == {"service_label": "application", "status_label": "status",
                   "requests": "http_server_requests_seconds_count", "latency_histogram": "http_server_requests_seconds",
                   "dependency_histogram": "http_client_requests_seconds", "dependency_label": "client_name",
                   "services_from_requests": True}
    assert discover_metrics.closest_preset(cfg) == "spring"
    assert metrics_config.MetricsConfig(**cfg).promql("error_rate", "orders") == (
        '(sum(rate(http_server_requests_seconds_count{application="orders",status=~"5.."}[1m])) or vector(0))'
        ' / sum(rate(http_server_requests_seconds_count{application="orders"}[1m]))')
    assert all(r.method == "GET" for r in seen)


def test_discovery_finds_the_demo_and_reports_missing_status(capsys):
    names = ["http_requests_total", "http_request_duration_seconds_bucket", "dependency_duration_seconds_bucket",
             "dependency_duration_seconds_count", "python_gc_collections_total"]
    series = {"http_requests_total": [{"service": "payments-svc", "status": "500", "job": "apps"}],
              "dependency_duration_seconds_count": [{"service": "payments-svc", "dependency": "postgres"}]}
    client, _ = _prom_api(names, series)
    report = discover_metrics.suggest(client)
    assert report["preset"] == "default"
    assert report["candidates"]["requests"] == ["http_requests_total"]

    client, _ = _prom_api(["grpc_server_handling_seconds_bucket"], {"grpc_server_handling_seconds_count": [{"job": "api"}]})
    assert discover_metrics.main(["--prom", "http://prom"], client=client) == 0
    out = capsys.readouterr().out
    assert '"service_label": "job"' in out and "No status-code label found" in out


def test_discovery_reports_an_unreachable_prometheus(capsys):
    def handler(request):
        raise httpx.ConnectError("refused")
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://prom")
    assert discover_metrics.main(["--prom", "http://prom"], client=client) == 1
    assert "could not read" in capsys.readouterr().err
