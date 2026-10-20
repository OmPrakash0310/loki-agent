"""Datadog logs source, against a mock of the Logs Search API."""
import json
from datetime import datetime, timezone

import httpx
import pytest

import doctor
import sources
import telemetry
from sources import datadog

START = datetime(2026, 10, 15, 16, 20, tzinfo=timezone.utc)
END = datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc)


def log(ts, service, message, status="error", attributes=None):
    return {"id": ts, "type": "log", "attributes": {"timestamp": ts, "status": status, "service": service,
                                                       "message": message, "attributes": attributes or {}}}


def make(handler, extra=""):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.datadoghq.com")
    return datadog.DatadogErrors(client, extra)


def page(logs, after=None, warnings=None):
    meta = {"page": {"after": after} if after else {}, "status": "done", "warnings": warnings or []}
    return httpx.Response(200, json={"data": logs, "meta": meta})


def test_error_lines_become_tuples_in_time_order_and_are_redacted():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return page([log("2026-10-15T16:25:00.000Z", "shop-api", "HTTPError: payments returned 500"),
                     log("2026-10-15T16:21:01.123Z", "payments-svc", "KeyError: 'currency' for bob@example.com")])

    lines = make(handler).error_lines(None, START, END)
    assert [(l[1]) for l in lines] == ["payments-svc", "shop-api"]
    assert lines[0][0] == datetime(2026, 10, 15, 16, 21, 1, 123000, tzinfo=timezone.utc)
    assert "bob@example.com" not in lines[0][2] and "KeyError" in lines[0][2]
    body = seen[0]
    assert body["filter"]["query"] == "status:(error OR critical OR alert OR emergency)"
    assert body["filter"]["from"] == "2026-10-15T16:20:00.000Z" and body["filter"]["to"] == "2026-10-15T16:30:00.000Z"
    assert body["sort"] == "timestamp"


def test_service_and_extra_query_are_added():
    seen = []
    make(lambda r: seen.append(json.loads(r.content)) or page([]), "env:production").error_lines("payments-svc", START, END)
    assert seen[0]["filter"]["query"] == (
        "status:(error OR critical OR alert OR emergency) service:payments-svc (env:production)")


def test_odd_service_names_are_refused_not_injected():
    with pytest.raises(ValueError):
        make(lambda r: page([])).error_lines("x OR service:*", START, END)


def test_follows_cursor_pages_and_notes_truncation(monkeypatch):
    monkeypatch.setattr(datadog, "MAX_PAGES", 2)
    cursors = []

    def handler(request):
        body = json.loads(request.content)
        cursors.append(body["page"].get("cursor"))
        n = len(cursors)
        return page([log(f"2026-10-15T16:2{n}:00.000Z", "svc", f"boom {n}")], after=f"c{n}")

    dd = make(handler)
    lines = dd.error_lines(None, START, END)
    assert cursors == [None, "c1"] and len(lines) == 2
    assert any("more than 2" in n for n in dd.notes())


def test_stops_when_no_more_pages():
    calls = []
    dd = make(lambda r: calls.append(1) or page([log("2026-10-15T16:21:00.000Z", "svc", "x")]))
    dd.error_lines(None, START, END)
    assert len(calls) == 1 and dd.notes() == []


@pytest.mark.parametrize("status,note", [(403, "HTTP 403 (check the keys"), (500, "HTTP 500")])
def test_http_errors_become_a_note_not_a_crash(status, note):
    dd = make(lambda r: httpx.Response(status, json={"errors": ["no"]}))
    assert dd.error_lines(None, START, END) == []
    assert note in dd.notes()[0]


def test_rate_limit_is_a_note():
    dd = make(lambda r: httpx.Response(429, headers={"x-ratelimit-reset": "30"}))
    assert dd.error_lines(None, START, END) == []
    assert dd.notes() == ["Datadog rate limited (retry after 30s): log data is partial or missing"]


def test_error_attributes_used_when_message_is_empty():
    dd = make(lambda r: page([log("2026-10-15T16:21:00.000Z", "svc", "", attributes={
        "error": {"kind": "KeyError", "message": "'currency'"}})]))
    assert dd.error_lines(None, START, END)[0][2] == "ERROR KeyError 'currency'"


def test_all_lines_search_everything_and_logs_present():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return page([log("2026-10-15T16:21:00.000Z", "svc", "hello", status="info")])

    dd = make(handler)
    assert dd.all_lines("svc", START, END)[0][2] == "hello"
    assert seen[0]["filter"]["query"] == "service:svc"
    assert dd.logs_present(START, END) is True
    assert make(lambda r: page([])).logs_present(START, END) is False
    # an unreachable API is not evidence that the log pipeline is broken
    assert make(lambda r: httpx.Response(500)).logs_present(START, END) is True


def test_auth_headers_and_site(monkeypatch):
    for k in ("DATADOG_API_KEY", "DATADOG_APP_KEY", "DATADOG_TOKEN", "DATADOG_SITE", "DATADOG_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError, match="needs DATADOG_API_KEY"):
        datadog.auth_headers()
    monkeypatch.setenv("DATADOG_API_KEY", "a")
    monkeypatch.setenv("DATADOG_APP_KEY", "b")
    assert datadog.auth_headers() == {"DD-API-KEY": "a", "DD-APPLICATION-KEY": "b"}
    monkeypatch.setenv("DATADOG_TOKEN", "t")
    assert datadog.auth_headers() == {"Authorization": "Bearer t"}
    assert datadog._base_url() == "https://api.datadoghq.com"
    monkeypatch.setenv("DATADOG_SITE", "datadoghq.eu")
    assert datadog._base_url() == "https://api.datadoghq.eu"
    monkeypatch.setenv("DATADOG_SITE", "evil.com/path?x=")
    with pytest.raises(ValueError, match="not a valid Datadog site"):
        datadog._base_url()


def test_registry_builds_datadog_from_env(monkeypatch):
    monkeypatch.setenv("LOKI_ERRORS_SOURCE", "datadog")
    monkeypatch.setenv("DATADOG_API_KEY", "a")
    monkeypatch.setenv("DATADOG_APP_KEY", "b")
    monkeypatch.delenv("DATADOG_TOKEN", raising=False)
    monkeypatch.delenv("DATADOG_BASE_URL", raising=False)
    sources.reset()
    try:
        e = sources.errors()
        assert isinstance(e, datadog.DatadogErrors)
        assert e.client.headers["DD-API-KEY"] == "a" and str(e.client.base_url) == "https://api.datadoghq.com"
    finally:
        sources.reset()


def test_registry_without_keys_fails_clearly(monkeypatch):
    monkeypatch.setenv("LOKI_ERRORS_SOURCE", "datadog")
    for k in ("DATADOG_API_KEY", "DATADOG_APP_KEY", "DATADOG_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    sources.reset()
    with pytest.raises(ValueError, match="needs DATADOG_API_KEY"):
        sources.errors()
    sources.reset()


def test_log_lines_use_the_source_and_never_return_future_lines(monkeypatch):
    class Src:
        def all_lines(self, service, start, end):
            return [(datetime(2026, 10, 15, 16, 21, tzinfo=timezone.utc), "svc", "ok"),
                    (datetime(2099, 1, 1, tzinfo=timezone.utc), "svc", "from the future")]

    monkeypatch.setattr(telemetry.sources, "errors", lambda: Src())
    at = datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc)
    with telemetry.replay(at):  # a backtest: nothing after the replay time may reach the model
        out = telemetry.log_lines("svc", datetime(2026, 10, 15, tzinfo=timezone.utc), datetime.now(timezone.utc))
    assert [l[2] for l in out] == ["ok"]


def test_log_lines_fall_back_to_grafana_loki_when_source_cannot_search(monkeypatch):
    class Src:
        def all_lines(self, service, start, end):
            return None

    monkeypatch.setattr(telemetry.sources, "errors", lambda: Src())
    monkeypatch.setattr(telemetry, "fetch_logs", lambda *a, **k: [("t", "svc", "from loki")])
    assert telemetry.log_lines("svc", START, END) == [("t", "svc", "from loki")]


# ---------------- doctor ----------------

def _doctor_client(status):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"data": []})))


def test_doctor_datadog_states(monkeypatch):
    for k in ("DATADOG_API_KEY", "DATADOG_APP_KEY", "DATADOG_TOKEN", "DATADOG_SITE", "DATADOG_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("LOKI_ERRORS_SOURCE", raising=False)
    assert doctor.check_datadog(None)[0].status == doctor.WARN
    monkeypatch.setenv("LOKI_ERRORS_SOURCE", "datadog")
    assert doctor.check_datadog(None)[0].status == doctor.FAIL
    monkeypatch.setenv("DATADOG_API_KEY", "a")
    monkeypatch.setenv("DATADOG_APP_KEY", "b")
    assert doctor.check_datadog(_doctor_client(200))[0].status == doctor.OK
    bad = doctor.check_datadog(_doctor_client(403))[0]
    assert bad.status == doctor.FAIL and "logs_read_data" in bad.detail
    monkeypatch.delenv("LOKI_ERRORS_SOURCE")
    res = doctor.check_datadog(_doctor_client(200))
    assert [r.status for r in res] == [doctor.OK, doctor.WARN]
