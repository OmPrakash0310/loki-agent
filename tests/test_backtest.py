"""Backtesting: the no-future-data guarantee, keeping backtests apart from live data, and the CLI.

Grafana Loki, Prometheus and Postgres are faked, so these run anywhere for free.
The fake backends deliberately ignore the requested time range and always return
data from after the replay time too: the guard must hold even when a backend
over-returns.
"""
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

import backtest as bt_agent
import context
import investigator
import telemetry as tm
import tools

REPLAY = datetime(2026, 10, 16, 21, 4, tzinfo=timezone.utc)
ALERT = REPLAY - timedelta(seconds=60)
FUTURE = 999.0  # metric value the fakes report for any time after REPLAY
MARK = "FUTURE_LEAK"


# ---------- fake telemetry backends ----------

class Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def _value_at(t: float) -> str:
    return str(FUTURE if t > REPLAY.timestamp() else 0.1)


class FakeBackends:
    """Grafana Loki + Prometheus that return past AND future data whatever is asked."""

    def __init__(self):
        self.requests = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.requests.append((url, params))
        if "/loki/api/v1/query_range" in url:
            past, future = ALERT - timedelta(seconds=30), REPLAY + timedelta(minutes=2)
            values = [[str(int(past.timestamp() * 1e9)), "2026-10-16 21:02:30 ERROR charge failed: KeyError 'currency'"],
                      [str(int(future.timestamp() * 1e9)), f"2026-10-16 21:06:00 ERROR {MARK} only seen later"]]
            return Resp({"data": {"result": [{"stream": {"service": "payments-svc"}, "values": values}]}})
        if "/label/service/values" in url:
            return Resp({"data": ["payments-svc"]})
        if "/api/v1/query_range" in url:
            start = float(params["start"])
            ts = [start + 30 * k for k in range(int((REPLAY.timestamp() + 600 - start) / 30))]
            values = [[t, _value_at(t)] for t in ts]
            metric = {"dependency": "postgres"} if "dependency" in params["query"] else {}
            return Resp({"data": {"result": [{"metric": metric, "values": values}]}})
        if "/api/v1/query" in url:
            t = float(params["time"])
            metric = {"dependency": "postgres"} if "dependency" in params["query"] else {}
            return Resp({"data": {"result": [{"metric": metric, "value": [t, _value_at(t)]}]}})
        raise AssertionError(f"unexpected request {url}")

    def latest_requested_time(self) -> float:
        """The latest moment any query asked about."""
        times = []
        for _, p in self.requests:
            for k in ("end", "time"):
                if k in p:
                    v = float(p[k])
                    times.append(v / 1e9 if v > 1e12 else v)  # Grafana Loki uses nanoseconds
        return max(times)


DEPLOYS = [
    {"id": "past001", "service": "payments-svc", "version": "v1", "author": "a", "message": "ok",
     "deployed_at": (ALERT - timedelta(minutes=2)).isoformat(), "diff": "past diff"},
    {"id": "future01", "service": "payments-svc", "version": "v2", "author": "b", "message": f"{MARK} revert",
     "deployed_at": (REPLAY + timedelta(minutes=5)).isoformat(), "diff": f"{MARK} diff"},
]


@pytest.fixture
def backends(monkeypatch, tmp_path):
    fake = FakeBackends()
    monkeypatch.setattr(tm.httpx, "get", fake.get)
    deploys = tmp_path / "deploys.json"
    deploys.write_text(json.dumps(DEPLOYS))
    monkeypatch.setattr(tm, "DEPLOYS_FILE", str(deploys))
    tools._tool_cache.clear()
    tools.set_window_floor(None)
    return fake


INCIDENT = {"alert_name": "HighErrorRate", "service": "payments-svc", "severity": "critical",
            "summary": "payments-svc error rate is 40%", "started_at": ALERT}


# ---------- 1. time travel: nothing after the replay time leaks ----------

def test_replay_sets_the_clock_and_resets_it():
    with tm.replay(REPLAY):
        assert tm.now() == REPLAY
        assert tm.minutes_ago(10) == REPLAY - timedelta(minutes=10)
    assert tm.replay_time() is None
    assert tm.now() > REPLAY


def test_replay_rejects_the_future_and_naive_times():
    with pytest.raises(ValueError):
        with tm.replay(datetime.now(timezone.utc) + timedelta(hours=1)):
            pass
    with pytest.raises(ValueError):
        with tm.replay(datetime(2026, 10, 16, 21, 0)):
            pass


def test_replay_clock_is_per_thread():
    """A live investigation in another thread keeps the real clock."""
    seen = {}
    with tm.replay(REPLAY):
        t = threading.Thread(target=lambda: seen.update(now=tm.now(), replay=tm.replay_time()))
        t.start()
        t.join()
    assert seen["replay"] is None and seen["now"] != REPLAY


def test_briefing_sees_nothing_after_replay_time(backends):
    with tm.replay(REPLAY):
        briefing = context.build([INCIDENT])
    assert MARK not in briefing.text
    assert str(FUTURE) not in briefing.text
    assert "future01" not in briefing.text
    assert "past001" in briefing.text  # the past is still there
    assert "KeyError" in briefing.text
    assert f"Current time: {REPLAY:%H:%M:%S} UTC" in briefing.text
    assert backends.latest_requested_time() <= REPLAY.timestamp()


def test_tools_see_nothing_after_replay_time(backends):
    calls = [
        ("query_logs", {"minutes": 60}),
        ("query_logs", {"service": "payments-svc", "errors_only": False}),
        ("query_metrics", {"metric": "error_rate", "service": "payments-svc", "minutes": 120}),
        ("query_dependencies", {"service": "payments-svc"}),
        ("recent_deploys", {"minutes": 1440}),
    ]
    with tm.replay(REPLAY):
        for name, args in calls:
            out, is_error = tools.run(name, args)
            assert not is_error, out
            assert MARK not in out and str(FUTURE) not in out and "future01" not in out, (name, out)
        # A deploy from the future doesn't exist yet, not even its diff.
        out, is_error = tools.run("git_diff", {"deploy_id": "future01"})
        assert is_error and MARK not in out
        assert "past diff" in tools.run("git_diff", {"deploy_id": "past001"})[0]
    assert backends.latest_requested_time() <= REPLAY.timestamp()


def test_live_mode_is_unchanged(backends):
    """Outside a replay nothing is filtered: the live path behaves as before."""
    out, _ = tools.run("query_logs", {"minutes": 60 * 24 * 365})
    assert MARK in out
    assert any(d["id"] == "future01" for d in tm.load_deploys())


def test_tool_cache_never_crosses_live_and_replay(backends):
    """A live result (which includes the 'future') must not be served to a backtest from the tool cache."""
    live, _ = tools.run("query_logs", {"minutes": 30})
    with tm.replay(REPLAY):
        replayed, _ = tools.run("query_logs", {"minutes": 30})
    assert MARK in live and MARK not in replayed


def test_past_incident_search_is_live_only_and_bounded_by_replay_time(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(tools.db, "connect", conn.connect)
    with tm.replay(REPLAY):
        tools.search_past_incidents("payments currency")
    sql, params = conn.log[-1]
    assert "i.mode = 'live'" in sql and "finished_at <= %s" in sql and "c.created_at <= %s" in sql
    assert params[0] == REPLAY and params[1] == REPLAY


# ---------- 2. backtests stay apart from live data ----------

class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    """Postgres stand-in: answers the queries the investigator makes and logs every statement."""

    def __init__(self, mode="backtest"):
        self.mode, self.log = mode, []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        if "SELECT mode, replay_at FROM investigations" in sql:
            return Rows([{"mode": self.mode, "replay_at": REPLAY if self.mode == "backtest" else None}])
        if "AS spent" in sql:
            return Rows([{"spent": 0}])
        if "FROM incidents WHERE investigation_id" in sql:
            return Rows([INCIDENT])
        if "max(resolved_at)" in sql:
            return Rows([{"t": None}])
        if "count(*)" in sql:
            return Rows([{}])
        if "RETURNING id" in sql:
            return Rows([{"id": 42}])
        if "FROM investigations WHERE id" in sql:
            return Rows([{"id": 42, "mode": self.mode, "status": "done", "cache_key": "k", "cache_hit": None,
                          "fingerprint": "f", "report": {}}])
        return Rows([])

    def sql(self, needle):
        return [s for s, _ in self.log if needle in s]


@pytest.fixture
def fake_db(monkeypatch):
    def make(mode):
        conn = FakeConn(mode)
        for module in (investigator.db, tools.db):
            monkeypatch.setattr(module, "connect", conn.connect)
        monkeypatch.setattr(investigator.slack, "post_report", lambda *a: conn.log.append(("SLACK", a)))
        return conn
    return make


def test_backtest_skips_answer_cache_and_slack(backends, fake_db):
    conn = fake_db("backtest")
    investigator.investigate(42, model="fake")
    assert not conn.sql("report_cache"), "backtests must neither read nor write the answer cache"
    assert not conn.sql("SLACK")
    assert conn.sql("status = 'done'")
    # The fake model's tool calls ran inside the replay too.
    traced = [p for s, p in conn.log if "INSERT INTO trace_steps" in s and p[2] == "recent_deploys"]
    assert traced and "future01" not in traced[0][4]


def test_live_investigation_still_uses_the_cache(backends, fake_db):
    conn = fake_db("live")
    investigator.investigate(42, model="fake")
    assert conn.sql("SELECT * FROM report_cache WHERE exact_key")
    assert conn.sql("INSERT INTO report_cache")


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    conn = FakeConn("backtest")
    monkeypatch.setattr(main.db, "connect", conn.connect)
    monkeypatch.setattr(main, "_run_after_grouping", lambda inv_id: None)
    return TestClient(main.app), conn  # not entered: no startup hook, no real database


def test_stats_and_grouping_exclude_backtests(client):
    c, conn = client
    assert c.get("/stats").status_code == 200
    aggregates = conn.sql("FROM investigations") + conn.sql("FROM trace_steps")
    assert aggregates and all("mode = 'live'" in s or "mode = 'backtest'" in s for s in aggregates)
    c.post("/alerts", json={"alerts": [{"status": "firing", "fingerprint": "x", "labels": {"alertname": "A", "service": "s"},
                                         "startsAt": ALERT.isoformat()}]})
    # RETURNING id makes the fake say "inserted"; the join query must only consider live investigations.
    assert all("mode = 'live'" in s for s in conn.sql("status IN ('queued', 'running')"))


def test_backtests_cannot_be_confirmed(client):
    c, _ = client
    assert c.post("/investigations/42/confirm").status_code == 409


def test_rejecting_a_backtest_leaves_live_answers_alone(client):
    """It may share a cache key with a confirmed live answer; that answer must stay confirmed."""
    c, conn = client
    assert c.post("/investigations/42/reject", json={"reason": "wrong"}).status_code == 200
    assert not conn.sql("report_cache") and conn.sql("INSERT INTO feedback")


def test_backtest_endpoint_validates_input(client):
    c, _ = client
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert c.post("/backtest", json={"at": REPLAY.isoformat(), "alerts": "nocolon"}).status_code == 400
    assert c.post("/backtest", json={"at": REPLAY.isoformat(), "alerts": "A:s", "model": "gpt-9"}).status_code == 400
    assert c.post("/backtest", json={"at": future, "alerts": "A:s"}).status_code == 400


def test_backtest_create_marks_rows_as_backtest(backends):
    conn = FakeConn()
    inv_id = bt_agent.create(conn, ALERT, [("HighErrorRate", "payments-svc")], delay_s=60)
    assert inv_id == 42
    inv_sql, inv_params = next((s, p) for s, p in conn.log if "INSERT INTO investigations" in s)
    assert "'backtest'" in inv_sql and inv_params == (REPLAY,)
    inc_sql, inc_params = next((s, p) for s, p in conn.log if "INSERT INTO incidents" in s)
    assert "'backtest'" in inc_sql and inc_params[0].startswith("backtest:")
    assert inc_params[4] == "payments-svc error rate is 10.00%"  # summary from the metric at alert time


def test_backtest_create_needs_alerts_when_none_were_recorded():
    with pytest.raises(ValueError, match="no recorded alerts"):
        bt_agent.create(FakeConn(), ALERT, [])


def test_parse_alerts():
    assert bt_agent.parse_alerts("HighErrorRate:payments-svc, HighLatency:shop-api") == [
        ("HighErrorRate", "payments-svc"), ("HighLatency", "shop-api")]
    assert bt_agent.parse_alerts(None) == []
    with pytest.raises(ValueError):
        bt_agent.parse_alerts("HighErrorRate")


# ---------- 3. the CLI ----------

def _cli():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "scripts" / "backtest.py"
    spec = importlib.util.spec_from_file_location("backtest_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _cli()
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text, expected", [
    ("09:05", datetime(2026, 10, 18, 9, 5, tzinfo=timezone.utc)),
    ("09:05:30 UTC", datetime(2026, 10, 18, 9, 5, 30, tzinfo=timezone.utc)),
    ("2026-10-16T21:03:00Z", datetime(2026, 10, 16, 21, 3, tzinfo=timezone.utc)),
    ("2026-10-16T23:03:00+02:00", datetime(2026, 10, 16, 21, 3, tzinfo=timezone.utc)),
    ("2026-10-16 21:03", datetime(2026, 10, 16, 21, 3, tzinfo=timezone.utc)),  # no timezone = UTC
])
def test_parse_at(text, expected):
    assert cli.parse_at(text, NOW) == expected


@pytest.mark.parametrize("text", ["13:00", "2026-10-19T00:00:00Z", "yesterday", "25:00"])
def test_parse_at_rejects_future_and_garbage(text):
    with pytest.raises(Exception):
        cli.parse_at(text, NOW)


def test_parse_args():
    args = cli.parse_args(["--at", "2026-10-16T21:03:00Z", "--alerts", "HighErrorRate:payments-svc", "--model", "fake"])
    assert args.at == datetime(2026, 10, 16, 21, 3, tzinfo=timezone.utc)
    assert args.alerts == "HighErrorRate:payments-svc" and args.model == "fake" and args.delay == 60
    assert cli.parse_args(["--incidents-file", "past.jsonl"]).incidents_file.name == "past.jsonl"
    for bad in ([], ["--at", "2026-10-16T21:03:00Z", "--incidents-file", "x"], ["--incidents-file", "x", "--alerts", "A:b"]):
        with pytest.raises(SystemExit):
            cli.parse_args(bad)


def test_load_cases_and_grade(tmp_path):
    f = tmp_path / "past.jsonl"
    f.write_text("\n".join([
        json.dumps({"name": "bad deploy", "at": "2026-10-16T21:03:00Z", "alerts": "HighErrorRate:payments-svc",
                    "expected": {"service": "payments-svc", "cause": "deploy", "deploy": "552a2c7"}}),
        "# a comment",
        json.dumps({"at": "2026-10-16T22:00:00Z", "expected": {"cause": "dependency|infrastructure", "deploy": ""}}),
    ]))
    cases = cli.load_cases(f)
    assert [c["name"] for c in cases] == ["bad deploy", "line 3"]
    good = {"root_cause_service": "payments-svc", "trigger": "deploy", "deploy_id": "552a2c7"}
    assert all(cli.grade(good, cases[0]["expected"]).values())
    wrong = {"root_cause_service": "shop-api", "trigger": "deploy", "deploy_id": "7c19e02"}
    assert cli.grade(wrong, cases[0]["expected"]) == {"right_service": False, "right_cause_type": True, "right_deploy": False}
    assert cli.grade({"trigger": "dependency", "deploy_id": ""}, cases[1]["expected"]) == {
        "right_cause_type": True, "no_deploy_blamed": True}


class RecordedConn(FakeConn):
    """Live alerts Loki recorded: one at the replayed time and one from the next incident, 3 minutes later."""

    def execute(self, sql, params=()):
        if "FROM incidents WHERE mode = 'live'" in sql:
            self.log.append((sql, params))
            row = {"alert_name": "HighErrorRate", "service": "payments-svc", "severity": "critical", "summary": "s"}
            return Rows([{**row, "started_at": ALERT}, {**row, "started_at": ALERT + timedelta(minutes=3)}])
        return super().execute(sql, params)


def test_recorded_alerts_after_the_replay_clock_are_not_replayed():
    """Found in a live run: alerts from the next incident leaked into the replay."""
    conn = RecordedConn()
    bt_agent.create(conn, ALERT, [], delay_s=60)
    replayed = [p[5] for s, p in conn.log if "INSERT INTO incidents" in s]
    assert replayed == [ALERT]
