"""Similar-incident search by meaning (embeddings.py + tools.similar_incidents)."""
import os
from datetime import datetime, timezone

import pytest

import doctor
import embeddings
import telemetry as tm
import tools

# A fake model: each text becomes a vector over a few "concepts", so texts that share a
# concept are similar even when they share no words (like a real embedding model).
CONCEPTS = {
    "database": ("postgres", "database", "db", "pool", "sql"),
    "deploy": ("deploy", "release", "keyerror", "field"),
    "memory": ("memory", "oom", "leak"),
    "network": ("gateway", "upstream", "card"),
}


def _vec(text):
    t = text.lower()
    return [1.0 + sum(t.count(w) for w in words) * 3 for words in CONCEPTS.values()]


class FakeModel:
    def __init__(self):
        self.passages = []

    def passage_embed(self, texts):
        self.passages += texts
        return [_vec(t) for t in texts]

    def query_embed(self, texts):
        return [_vec(t) for t in texts]


class Broken:
    def passage_embed(self, texts):
        raise RuntimeError("onnx exploded")

    def query_embed(self, texts):
        raise RuntimeError("onnx exploded")


def report(summary, root_cause, service="payments-svc"):
    return {"summary": summary, "root_cause": root_cause, "root_cause_service": service,
            "evidence": [], "suggested_action": "escalate"}


DONE = datetime(2026, 10, 10, tzinfo=timezone.utc)


def row(id_, rep, embedding=None, model=None):
    return {"id": id_, "finished_at": DONE, "report": rep, "exact_key": f"k{id_}",
            "embedding": embedding, "embedding_model": model}


class Conn:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        return self

    def fetchall(self):
        return [dict(r) for r in self.rows]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def connect(self):
        return self


ROWS = [
    row(1, report("Payments failing after deploy", "Deploy read a field the request doesn't send: KeyError")),
    row(2, report("Checkout slow", "Postgres pool exhausted, queries waited for a database connection")),
    row(3, report("payments-svc restarts", "Memory leak in receipt cache, OOM kills")),
]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("LOKI_EMBEDDINGS", "local")
    model = FakeModel()
    embeddings.set_backend(model)
    yield model
    embeddings.set_backend(None)


def test_finds_by_meaning_without_shared_words(fake, monkeypatch):
    conn = Conn(ROWS)
    monkeypatch.setattr(tools.db, "connect", conn.connect)
    out = tools.search_past_incidents("sql timeouts")  # no word in common with incident 2
    assert out[0]["investigation"] == 2 and out[0]["similarity"] >= tools.SEMANTIC_MIN_SCORE
    assert all(o["investigation"] != 3 for o in out)  # memory leak: unrelated, below the bar


def test_missing_vectors_are_backfilled_and_stored(fake, monkeypatch):
    conn = Conn(ROWS)
    monkeypatch.setattr(tools.db, "connect", conn.connect)
    tools.search_past_incidents("database pool")
    updates = [p for sql, p in conn.log if sql.startswith("UPDATE report_cache SET embedding")]
    assert len(updates) == 3 and all(p[1] == embeddings.model_name() for p in updates)
    assert len(fake.passages) == 3


def test_vectors_from_another_model_are_redone(fake, monkeypatch):
    rows = [row(2, ROWS[1]["report"], embedding=[1, 0, 0, 0], model="old-model"),
            row(1, ROWS[0]["report"], embedding=embeddings._normalize(_vec(embeddings.incident_text(ROWS[0]["report"]))),
                model=embeddings.model_name())]
    conn = Conn(rows)
    monkeypatch.setattr(tools.db, "connect", conn.connect)
    tools.search_past_incidents("postgres")
    assert len(fake.passages) == 1  # only the stale one


def test_off_means_keyword_search(monkeypatch):
    monkeypatch.setenv("LOKI_EMBEDDINGS", "off")
    conn = Conn([])
    monkeypatch.setattr(tools.db, "connect", conn.connect)
    tools.search_past_incidents("payments currency")
    assert "ILIKE" in conn.log[-1][0]


def test_broken_model_falls_back_to_keywords(monkeypatch):
    monkeypatch.setenv("LOKI_EMBEDDINGS", "local")
    embeddings.set_backend(Broken())
    try:
        conn = Conn(ROWS)
        monkeypatch.setattr(tools.db, "connect", conn.connect)
        tools.search_past_incidents("payments currency")
        assert "ILIKE" in conn.log[-1][0]
    finally:
        embeddings.set_backend(None)


def test_meaning_search_respects_backtest_time(fake, monkeypatch):
    """Same honesty rule as keyword search: nothing solved after the replay time."""
    at = datetime(2026, 10, 16, 21, 3, tzinfo=timezone.utc)
    conn = Conn([])
    monkeypatch.setattr(tools.db, "connect", conn.connect)
    with tm.replay(at):
        tools.search_past_incidents("payments")
    sql, params = conn.log[0]
    assert "i.mode = 'live'" in sql and "finished_at <= %s" in sql and "c.created_at <= %s" in sql
    assert params[0] == at and params[1] == at


def test_similar_incidents_limit_and_bar(fake):
    conn = Conn(ROWS)
    assert tools.similar_incidents(conn, "postgres database pool", limit=1, min_score=0.9)[0][1]["id"] == 2
    assert tools.similar_incidents(conn, "hello there", min_score=0.99) == []


def test_load_failure_is_remembered_and_disables(monkeypatch):
    monkeypatch.setenv("LOKI_EMBEDDINGS", "local")
    embeddings.set_backend(None)
    import builtins
    real_import = builtins.__import__

    def no_fastembed(name, *a, **k):
        if name == "fastembed":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_fastembed)
    assert embeddings.embed_query("x") is None
    assert embeddings._failed_at > 0
    embeddings.set_backend(None)


def test_incident_text_has_the_useful_parts():
    t = embeddings.incident_text({"summary": "S", "root_cause": "R", "root_cause_service": "svc", "evidence": ["a", "b"]})
    assert t.splitlines() == ["S", "R", "service svc", "a", "b"]


def test_doctor_embeddings(monkeypatch):
    monkeypatch.setenv("LOKI_EMBEDDINGS", "off")
    assert doctor.check_embeddings().status == doctor.WARN
    monkeypatch.setenv("LOKI_EMBEDDINGS", "local")
    monkeypatch.setenv("LOKI_EMBEDDING_CACHE", "/nonexistent/models")
    expected = doctor.FAIL
    assert doctor.check_embeddings().status == expected


@pytest.mark.skipif(os.environ.get("LOKI_TEST_REAL_EMBEDDINGS") != "1",
                    reason="set LOKI_TEST_REAL_EMBEDDINGS=1 to run the real model (downloads ~64 MB once)")
def test_real_model_matches_meaning(monkeypatch):
    monkeypatch.setenv("LOKI_EMBEDDINGS", "local")
    embeddings.set_backend(None)
    conn = Conn(ROWS)
    monkeypatch.setattr(tools.db, "connect", conn.connect)
    cases = {"connection refused to db": 2, "out of memory crash loop": 3,
             "missing field in request after release": 1}
    for query, want in cases.items():
        out = tools.search_past_incidents(query)
        assert out and out[0]["investigation"] == want, (query, out)
