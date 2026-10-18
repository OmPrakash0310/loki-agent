"""Lessons notebook: verified sources only, pending review, retrieval, no ground-truth leak,
backtest cutoff, auth, dedupe. Postgres and every model call are faked."""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import investigator
import lessons
import main
import telemetry as tm
from test_backtest import REPLAY, FakeConn, backends  # noqa: F401  (pytest fixture)

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class LessonDB:
    """Just enough Postgres for lessons, investigations and feedback."""

    def __init__(self):
        self.lessons, self.log, self.now = [], [], T0
        self.investigations = {
            5: {"id": 5, "mode": "live", "status": "done", "cache_key": "k", "cache_hit": None, "fingerprint": "f",
                "report": {"summary": "s", "root_cause": "Deploy abc1234 broke payments", "root_cause_service": "payments-svc",
                           "trigger": "deploy", "deploy_id": "abc1234", "affected_services": ["payments-svc", "shop-api"],
                           "evidence": ["e"]}},
        }

    @contextmanager
    def connect(self):
        yield self

    def add(self, text, services=(), tags=(), active=True, created_at=None, created_from="manual"):
        self.lessons.append({"id": len(self.lessons) + 1, "text": text, "services": list(services), "tags": list(tags),
                             "active": active, "uses": 0, "created_from": created_from,
                             "created_at": created_at or self.now, "confirmed_by": None, "source_investigation": None})
        return self.lessons[-1]

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        if sql.startswith("SELECT id, text, services, tags, active FROM lessons"):
            return Rows(list(reversed(self.lessons)))
        if sql.startswith("INSERT INTO lessons"):
            text, services, tags, src, created_from, confirmed_by, active = params
            row = self.add(text, services, tags, active, created_from=created_from)
            row.update(source_investigation=src, confirmed_by=confirmed_by)
            return Rows([{"id": row["id"]}])
        if sql.startswith("UPDATE lessons SET services"):
            s, t, i = params
            self.lessons[i - 1].update(services=s, tags=t)
            return Rows([])
        if "FROM lessons WHERE active AND created_at <= %s" in sql:
            return Rows([l for l in self.lessons if l["active"] and l["created_at"] <= params[0]])
        if "FROM lessons WHERE active" in sql:
            return Rows([l for l in self.lessons if l["active"]])
        if sql.startswith("UPDATE lessons SET uses"):
            for l in self.lessons:
                if l["id"] in params[0]:
                    l["uses"] += 1
            return Rows([])
        if sql.startswith("UPDATE lessons SET active"):
            active, _, actor, i = params
            if i > len(self.lessons):
                return Rows([])
            self.lessons[i - 1]["active"] = active
            if active:
                self.lessons[i - 1]["confirmed_by"] = actor
            return Rows([{"id": i, "active": active}])
        if "FROM lessons ORDER BY id DESC LIMIT 200" in sql:
            return Rows(list(reversed(self.lessons)))
        if "FROM incidents WHERE investigation_id" in sql:
            return Rows([{"alert_name": "HighErrorRate", "service": "payments-svc"}])
        if "FROM investigations WHERE id" in sql:
            inv = self.investigations.get(params[0])
            return Rows([inv] if inv else [])
        return Rows([])


@pytest.fixture
def ldb(monkeypatch):
    d = LessonDB()
    monkeypatch.setattr(main.db, "connect", d.connect)
    monkeypatch.delenv("LOKI_API_KEYS", raising=False)
    monkeypatch.delenv("LOKI_LESSONS", raising=False)
    monkeypatch.setenv("LOKI_LESSONS_MODEL", "fake")
    return d


@pytest.fixture
def client(ldb):
    return TestClient(main.app)  # not entered: no startup hook


# ---------- creation only from verified sources ----------

def test_create_refuses_unverified_sources(ldb):
    for src in ("report", "investigation", "self", ""):
        with pytest.raises(ValueError):
            lessons.create(ldb, "Loki thinks it was the deploy, remember that", src)
    assert ldb.lessons == []


def test_finished_investigation_creates_no_lesson(backends, monkeypatch):  # noqa: F811
    """Loki's own (unconfirmed) report never becomes a lesson."""
    conn = FakeConn("live")
    for module in (investigator.db, investigator.tools.db):
        monkeypatch.setattr(module, "connect", conn.connect)
    monkeypatch.setattr(investigator.slack, "post_report", lambda *a: None)
    investigator.investigate(42, model="fake")
    assert not conn.sql("INSERT INTO lessons")


def test_reject_with_reason_creates_pending_lesson(client, ldb):
    r = client.post("/investigations/5/reject", json={"reason": "It was the database: p99 query latency was 4s"})
    assert r.status_code == 200 and r.json()["lesson"]["active"] is False
    (l,) = ldb.lessons
    assert l["created_from"] == "reject" and not l["active"] and l["source_investigation"] == 5
    assert "database" in l["text"] and "abc1234" not in l["text"]
    assert "payments-svc" in l["services"] and "higherrorrate" in l["tags"]


def test_reject_without_reason_creates_nothing(client, ldb):
    assert client.post("/investigations/5/reject").status_code == 200
    assert client.post("/investigations/5/reject", json={"reason": "   "}).status_code == 200
    assert ldb.lessons == []


def test_lessons_off(client, ldb, monkeypatch):
    monkeypatch.setenv("LOKI_LESSONS", "0")
    client.post("/investigations/5/reject", json={"reason": "it was the database, not the deploy"})
    assert ldb.lessons == []


def test_manual_lesson_is_active_and_audited(client, ldb):
    r = client.post("/lessons", json={"text": "Check postgres latency before blaming a deploy", "services": ["Payments-svc"]})
    assert r.status_code == 200 and r.json()["active"] is True
    assert ldb.lessons[0]["services"] == ["payments-svc"] and ldb.lessons[0]["confirmed_by"] == "anonymous"
    assert any("INSERT INTO audit_log" in s and "lesson_add:1" in p for s, p in ldb.log)


def test_pending_then_approve_then_disable(client, ldb):
    client.post("/investigations/5/reject", json={"reason": "the card network was timing out"})
    assert lessons.relevant(ldb, [{"service": "payments-svc"}], "") == []  # pending: not used
    assert client.post("/lessons/1/approve").json() == {"id": 1, "active": True}
    assert [l["id"] for l in lessons.relevant(ldb, [{"service": "payments-svc"}], "")] == [1]
    assert client.post("/lessons/1/disable").json() == {"id": 1, "active": False}
    assert lessons.relevant(ldb, [{"service": "payments-svc"}], "") == []
    assert client.post("/lessons/99/approve").status_code == 404
    assert len(client.get("/lessons").json()) == 1


# ---------- eval misses and the ground-truth leak ----------

TRUTH = {"mode": "bad_deploy", "deploy_id": "f00dbabe42", "service": "payments-svc", "at": "2026-10-16T21:00:00Z"}
CHECKS = {"right_service": True, "right_deploy": False}


def test_eval_miss_lesson_never_contains_ground_truth(client, ldb, monkeypatch):
    monkeypatch.setenv("LOKI_LESSONS_MODEL", "claude-haiku-4-5")
    leaky = ("In bad_deploy (bad deploy) the culprit was f00dbabe42 at 2026-10-16T21:00:00Z, not abc1234; "
             "match the diff to the error lines in payments-svc.")
    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                sent.update(kw)
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=leaky)],
                                       usage=SimpleNamespace(input_tokens=500, output_tokens=60))
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: FakeClient())
    r = client.post("/lessons/eval-miss", json={"investigation_id": 5, "scenario": "bad_deploy", "checks": CHECKS, "truth": TRUTH})
    assert r.status_code == 200, r.text
    (l,) = ldb.lessons
    for secret in ("f00dbabe42", "bad_deploy", "bad deploy", "2026-10-16", "abc1234"):
        assert secret not in l["text"], secret
    assert "match the diff to the error lines" in l["text"] and "payments-svc" in l["text"]
    assert l["created_from"] == "eval_miss" and not l["active"]
    assert "f00dbabe42" in sent["messages"][0]["content"]  # the model saw the truth; storage didn't
    assert r.json()["cost_usd"] == pytest.approx((500 * 1.0 + 60 * 5.0) / 1e6, abs=1e-5)


def test_eval_miss_requires_a_failed_check(client, ldb):
    assert client.post("/lessons/eval-miss", json={"investigation_id": 5, "checks": {"a": True}}).status_code == 400
    assert client.post("/lessons/eval-miss", json={"investigation_id": 5, "checks": {}}).status_code == 400
    assert client.post("/lessons/eval-miss", json={"investigation_id": 404, "checks": {"a": False}}).status_code == 404


def test_eval_script_posts_misses(monkeypatch):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "scripts"))
    import eval as ev
    seen = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b'{"id": 3}'
    monkeypatch.setattr(ev.urllib.request, "urlopen", lambda req, timeout: seen.update(req=req) or R())
    assert ev.report_miss("bad_deploy", 5, CHECKS, TRUTH) == {"id": 3}
    assert seen["req"].full_url.endswith("/lessons/eval-miss") and json.loads(seen["req"].data)["investigation_id"] == 5


# ---------- retrieval ----------

def test_retrieval_relevance_and_cap(ldb):
    ldb.add("Slow postgres queries look like app errors; check dependency latency", ["payments-svc"], ["highlatency"])
    ldb.add("Card network timeouts show up as 504s in payments", ["payments-svc"], ["higherrorrate"])
    ldb.add("Inventory cache misses are harmless", ["inventory-svc"])
    ldb.add("A deploy right before errors isn't proof; read the diff", ["payments-svc"])
    ldb.add("Shop api 502s are usually downstream", ["shop-api"], ["higherrorrate"])
    ldb.add("Disabled lesson about payments", ["payments-svc"], active=False)
    incidents = [{"service": "payments-svc", "alert_name": "HighErrorRate"}]
    picked = lessons.relevant(ldb, incidents, "ERROR card network timeout 504")
    assert len(picked) == 3
    assert picked[0]["id"] == 2  # service + alert + keywords
    assert all(p["id"] not in (3, 6) for p in picked)
    section = lessons.briefing_section(ldb, incidents, "ERROR card network timeout 504")
    assert section.startswith("## Lessons from past mistakes (verified)") and section.count("\n- ") == 3
    assert ldb.lessons[1]["uses"] == 1 and ldb.lessons[2]["uses"] == 0


def test_irrelevant_lessons_are_not_shown(ldb):
    ldb.add("Inventory cache misses are harmless", ["inventory-svc"])
    assert lessons.briefing_section(ldb, [{"service": "payments-svc", "alert_name": "X"}], "nothing") == ""


def test_lessons_reach_the_briefing(backends, monkeypatch):  # noqa: F811
    conn = FakeConn("live")
    orig = conn.execute
    lesson = {"id": 1, "text": "Read the diff before blaming a deploy", "services": ["payments-svc"], "tags": []}

    def execute(sql, params=()):
        if "FROM lessons WHERE active" in sql:
            conn.log.append((sql, params))
            return Rows([lesson])
        return orig(sql, params)
    conn.execute = execute
    for module in (investigator.db, investigator.tools.db):
        monkeypatch.setattr(module, "connect", conn.connect)
    monkeypatch.setattr(investigator.slack, "post_report", lambda *a: None)
    investigator.investigate(42, model="fake")
    briefing = [p for s, p in conn.log if "INSERT INTO trace_steps" in s and p[2] == "briefing"][0][4]
    assert "## Lessons from past mistakes (verified)\n- Read the diff before blaming a deploy" in briefing
    assert conn.sql("UPDATE lessons SET uses")


def test_backtest_only_sees_lessons_from_before_replay(ldb):
    ldb.add("Old lesson: check postgres latency for payments", ["payments-svc"], created_at=REPLAY - timedelta(days=1))
    ldb.add("New lesson: learned after the replay time", ["payments-svc"], created_at=REPLAY + timedelta(hours=1))
    picked = lessons.relevant(ldb, [{"service": "payments-svc"}], "", before=REPLAY)
    assert [p["id"] for p in picked] == [1]
    assert len(lessons.relevant(ldb, [{"service": "payments-svc"}], "")) == 2


def test_backtest_investigation_uses_replay_cutoff_and_no_use_count(backends, monkeypatch):  # noqa: F811
    conn = FakeConn("backtest")
    for module in (investigator.db, investigator.tools.db):
        monkeypatch.setattr(module, "connect", conn.connect)
    investigator.investigate(42, model="fake")
    (sql, params), = [(s, p) for s, p in conn.log if "FROM lessons WHERE active" in s]
    assert "created_at <= %s" in sql and params[0] == REPLAY
    assert not conn.sql("UPDATE lessons SET uses")


# ---------- dedupe ----------

def test_near_identical_lessons_are_deduped(ldb):
    a = lessons.create(ldb, "Check postgres query latency before blaming a deploy.", "manual", ["payments-svc"])
    b = lessons.create(ldb, "check Postgres query latency before blaming the deploy", "manual", ["shop-api"])
    c = lessons.create(ldb, "Card network timeouts look like payments bugs", "manual")
    assert b["duplicate_of"] == a["id"] and c["id"] != a["id"]
    assert len(ldb.lessons) == 2 and ldb.lessons[0]["services"] == ["payments-svc", "shop-api"]


# ---------- auth ----------

KEYS = "eval:rw-secret-123,dash:ro-secret-456:read"


@pytest.mark.parametrize("method,path", [
    ("get", "/lessons"), ("post", "/lessons"), ("post", "/lessons/1/approve"), ("post", "/lessons/1/disable"),
    ("post", "/lessons/eval-miss"),
])
def test_lesson_endpoints_need_auth(client, monkeypatch, method, path):
    monkeypatch.setenv("LOKI_API_KEYS", KEYS)
    assert getattr(client, method)(path).status_code == 401
    if method == "post":
        assert client.post(path, headers={"Authorization": "Bearer ro-secret-456"}, json={}).status_code == 403


def test_approve_audits_the_key_name(client, ldb, monkeypatch):
    monkeypatch.setenv("LOKI_API_KEYS", KEYS)
    ldb.add("pending", active=False, created_from="reject")
    assert client.post("/lessons/1/approve", headers={"Authorization": "Bearer rw-secret-123"}).status_code == 200
    assert ldb.lessons[0]["confirmed_by"] == "eval"
    assert any("INSERT INTO audit_log" in s and p[0] == "eval" and p[1] == "lesson_approve:1" for s, p in ldb.log)
