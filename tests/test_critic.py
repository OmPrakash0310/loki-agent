"""The critic: grounded review of a report against its own tool results. All model calls mocked, $0."""
import json
from types import SimpleNamespace

import pytest

import critic
import investigator
import slack
from test_backtest import FakeConn, backends  # noqa: F401  (pytest fixture)

OBS = [
    {"step": 0, "tool": "briefing", "input": {}, "output": "payments-svc error rate 40%. Deploy d1 at 21:01."},
    {"step": 1, "tool": "recent_deploys", "input": {"minutes": 60}, "output": "d1 payments-svc v2"},
    {"step": 2, "tool": "git_diff", "input": {"deploy_id": "d1"}, "output": "- currency = body.get('currency')\n+ currency = body['currency']"},
]
REPORT = {
    "summary": "s", "root_cause": "Deploy d1 made currency required", "root_cause_service": "payments-svc",
    "trigger": "deploy", "deploy_id": "d1", "affected_services": ["payments-svc"],
    "evidence": ["diff makes currency required", "connection pool exhausted"], "ruled_out": [],
    "confidence": 0.8, "suggested_action": "rollback", "action_detail": "roll back",
}


def claim(kind, i, status, steps):
    return {"kind": kind, "index": i, "status": status, "steps": steps, "note": ""}


def raw(verdict, root="supported", ev=("supported", "unsupported"), adj=0.0):
    return {"claims": [claim("root_cause", 0, root, [2]),
                       *[claim("evidence", i, s, [2] if s != "unsupported" else []) for i, s in enumerate(ev)]],
            "verdict": verdict, "confidence_adjustment": adj, "notes": "check the pool claim"}


# ---------- unit: normalize / apply / render ----------

def test_unsupported_evidence_removed_and_kept_for_audit():
    out = critic.apply(REPORT, critic.normalize(raw("accept"), REPORT, OBS))
    assert out["evidence"] == ["diff makes currency required"]
    assert out["critique"]["removed_evidence"] == ["connection pool exhausted"]
    assert out["critique"]["supported"] == 2 and out["critique"]["total"] == 3
    assert out["confidence"] == 0.8  # unsupported alone doesn't lower it; never raised
    assert critic.headline(out["critique"]) == "Critic: 1 claim removed"


def test_all_supported_headline():
    out = critic.apply(REPORT, critic.normalize(raw("accept", ev=("supported", "supported")), REPORT, OBS))
    assert critic.headline(out["critique"]) == "Critic: 3/3 claims supported"


def test_contradiction_lowers_confidence():
    out = critic.apply(REPORT, critic.normalize(raw("accept", ev=("supported", "contradicted"), adj=-0.05), REPORT, OBS))
    assert out["confidence"] == pytest.approx(0.6)
    assert out["critique"]["contradicted"] and "connection pool exhausted" not in out["evidence"]
    assert "contradiction" in critic.headline(out["critique"])


def test_critic_cannot_raise_confidence_or_cite_missing_steps():
    r = raw("accept", ev=("supported", "supported"), adj=0.5)
    r["claims"][1]["steps"] = [99]  # a step that doesn't exist
    crit = critic.normalize(r, REPORT, OBS)
    assert crit["confidence_adjustment"] == 0.0
    assert crit["claims"][1]["status"] == "unsupported"
    assert critic.apply(REPORT, crit)["confidence"] == 0.8


def test_unsupported_root_cause_forces_revise():
    assert critic.normalize(raw("accept", root="unsupported"), REPORT, OBS)["verdict"] == "revise"


def test_render_and_slack_show_the_critic_line():
    out = critic.apply(REPORT, critic.normalize(raw("accept"), REPORT, OBS))
    assert "Critic: 1 claim removed" in investigator.render(1, out, 3, 0.01)
    blocks = json.dumps(slack.build_blocks(1, out, 3, 0.01))
    assert "Critic: 1 claim removed" in blocks
    assert "Critic" not in investigator.render(1, REPORT, 3, 0.01)


def test_observations_are_truncated():
    big = [{"step": i, "tool": "t", "input": {}, "output": "x" * 5000} for i in range(40)]
    text = critic.format_observations(big)
    assert len(text) <= critic.MAX_TOTAL_CHARS + 500 and "chars cut" in text and "more results cut" in text


def test_fake_critic_is_grounded_in_observations():
    c = critic.fake_critique({"root_cause": "currency body required", "evidence": ["totally invented memory leak"]}, OBS)
    assert c["claims"][0]["status"] == "supported" and c["claims"][1]["status"] == "unsupported"


def test_enabled_defaults(monkeypatch):
    monkeypatch.delenv("LOKI_CRITIC", raising=False)
    assert critic.enabled("claude-haiku-4-5", False) and not critic.enabled("fake", False)
    assert not critic.enabled("qwen3:4b", True)
    monkeypatch.setenv("LOKI_CRITIC", "1")
    assert critic.enabled("fake", False)
    monkeypatch.setenv("LOKI_CRITIC", "0")
    assert not critic.enabled("claude-haiku-4-5", False)


# ---------- the investigator wiring ----------

USAGE = SimpleNamespace(input_tokens=2000, output_tokens=300, cache_creation_input_tokens=0, cache_read_input_tokens=0)


@pytest.fixture
def live(backends, monkeypatch):  # noqa: F811
    conn = FakeConn("live")
    for module in (investigator.db, investigator.tools.db):
        monkeypatch.setattr(module, "connect", conn.connect)
    monkeypatch.setattr(investigator.slack, "post_report", lambda *a: conn.log.append(("SLACK", a)))
    monkeypatch.setattr(investigator.anthropic, "Anthropic", lambda *a, **k: object())
    monkeypatch.setenv("LOKI_CRITIC", "1")
    monkeypatch.delenv("LOKI_CRITIC_MODEL", raising=False)
    calls = []

    def script(*verdicts):
        def fake_call(client, model, report, observations):
            calls.append((model, report, observations))
            v = verdicts[min(len(calls) - 1, len(verdicts) - 1)]
            ev = ["supported"] * len(report["evidence"])
            if v == "contradict":
                ev[-1] = "contradicted"
                v = "accept"
            return {"claims": [claim("root_cause", 0, "supported", [1])] +
                              [claim("evidence", i, s, [1]) for i, s in enumerate(ev)],
                    "verdict": v, "confidence_adjustment": 0.0, "notes": "n"}, USAGE
        monkeypatch.setattr(critic, "call", fake_call)
    return conn, calls, script


def final_update(conn):
    sql, params = [x for x in conn.log if "SET status = 'done'" in x[0]][-1]
    return json.loads(params[0]), params


def test_accept_one_critic_call_and_cost_recorded(live):
    conn, calls, script = live
    script("accept")
    investigator.investigate(42, model="fake")
    assert len(calls) == 1
    report, params = final_update(conn)
    assert report["critique"]["verdict"] == "accept" and report["critique"]["rounds"] == 1
    critic_cost = (2000 * 1.0 + 300 * 5.0) / 1e6
    assert float(params[4]) == pytest.approx(round(critic_cost, 4))  # cost_usd includes the critic
    traced = [p for s, p in conn.log if "INSERT INTO trace_steps" in s and p[2] == "critic"]
    assert traced and traced[0][5] == "claude-haiku-4-5" and traced[0][-1] == pytest.approx(round(critic_cost, 5))
    # The critic saw the briefing (step 0) and the tool results, numbered.
    assert [o["tool"] for o in calls[0][2]] == ["briefing", "recent_deploys", "git_diff"]


def test_revise_gets_one_more_round_then_stops(live):
    conn, calls, script = live
    script("revise", "revise", "revise")
    investigator.investigate(42, model="fake")
    assert len(calls) == 2, "critique, one revision, one re-critique; never more"
    report, _ = final_update(conn)
    assert report["critique"]["rounds"] == 2 and report["critique"]["first_pass"]["verdict"] == "revise"
    assert len([p for s, p in conn.log if "INSERT INTO trace_steps" in s and p[2] == "submit_report"]) == 2


def test_contradiction_lowers_confidence_end_to_end(live):
    conn, calls, script = live
    script("contradict")
    investigator.investigate(42, model="fake")
    report, _ = final_update(conn)
    assert report["confidence"] == pytest.approx(0.3) and report["critique"]["contradicted"]


def test_revision_respects_step_limit(live, monkeypatch):
    conn, calls, script = live
    monkeypatch.setattr(investigator, "MAX_STEPS", 3)  # the fake model uses all 3
    script("revise")
    investigator.investigate(42, model="fake")
    assert len(calls) == 1, "no steps left: no revision"
    report, _ = final_update(conn)
    assert report["critique"]["rounds"] == 1


def test_revision_respects_budget(live, monkeypatch):
    conn, calls, script = live
    monkeypatch.setattr(investigator, "MAX_COST_USD", 0.003)  # the first critic call (0.0035) uses it all
    script("revise")
    investigator.investigate(42, model="fake")
    assert len(calls) == 1


def test_critic_failure_keeps_report(live, monkeypatch):
    conn, calls, script = live
    monkeypatch.setattr(critic, "call", lambda *a: (_ for _ in ()).throw(RuntimeError("api down")))
    investigator.investigate(42, model="fake")
    report, _ = final_update(conn)
    assert "critique" not in report and report["summary"].startswith("[fake model]")


def test_skipped_on_cache_hit(live):
    conn, calls, script = live
    script("accept")
    cached = {"report": dict(REPORT), "investigation_id": 7}
    orig = conn.execute

    def execute(sql, params=()):
        if "FROM report_cache WHERE exact_key" in sql:
            conn.log.append((sql, params))
            return type("R", (), {"fetchone": lambda self: cached, "fetchall": lambda self: [cached]})()
        return orig(sql, params)
    conn.execute = execute
    investigator.investigate(42, model="fake")
    assert calls == []
    assert not [p for s, p in conn.log if "INSERT INTO trace_steps" in s and p[2] == "critic"]


def test_off_by_default_for_fake(live, monkeypatch):
    conn, calls, script = live
    monkeypatch.delenv("LOKI_CRITIC")
    script("accept")
    investigator.investigate(42, model="fake")
    assert calls == []
