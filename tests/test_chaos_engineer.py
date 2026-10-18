"""The chaos engineer: scenario schema, generator, generic grader, eval wiring, runner."""
import copy
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import chaos_engineer as ce  # noqa: E402
import scenario_runner  # noqa: E402
import scenario_spec as ss  # noqa: E402


@pytest.fixture(scope="module")
def specs():
    return ce.generate()


def by_template(specs, name):
    return [s for s in specs if s["generator"]["template"] == name]


# ---------- generator ----------

def test_generator_is_deterministic(specs):
    again = ce.generate()
    assert [ce.render(s) for s in specs] == [ce.render(s) for s in again]
    other = ce.generate(seed=2)
    assert [ce.render(s) for s in specs] != [ce.render(s) for s in other]


def test_committed_scenarios_match_the_generator(specs):
    on_disk = {s["id"]: s for s in ce.load_all()}
    assert set(on_disk) >= {s["id"] for s in specs}
    for s in specs:
        assert ce.render(on_disk[s["id"]]) == ce.render(s), f"{s['id']} is stale: run scripts/chaos_engineer.py"


def test_at_least_30_valid_unique_scenarios(specs):
    assert len(specs) >= 30
    assert len({s["id"] for s in specs}) == len(specs)
    for s in specs:
        assert ss.validate(s) == [], s["id"]


def test_split_covers_every_template_and_difficulty(specs):
    train = [s for s in specs if s["split"] == "train"]
    held = [s for s in specs if s["split"] == "heldout"]
    assert len(held) >= 10 and len(train) >= 20
    for name, _ in ce.TEMPLATES:
        splits = {s["split"] for s in by_template(specs, name)}
        assert splits == {"train", "heldout"}, name
    for split in (train, held):
        assert {s["difficulty"] for s in split} == set(ss.DIFFICULTIES)
    # held-out scenarios are not copies of training ones
    assert not {json.dumps(s["steps"]) + json.dumps(s["ground_truth"]) for s in held} & \
        {json.dumps(s["steps"]) + json.dumps(s["ground_truth"]) for s in train}


def test_coverage_of_faults_and_traps(specs):
    used_faults = {st["fault"] for s in specs for st in s["steps"] if st["action"] == "fault"}
    used_modes = {st["mode"] for s in specs for st in s["steps"] if st["action"] == "mode"}
    assert used_faults == set(ss.FAULTS)
    assert len(used_faults | used_modes) >= 10
    types = {t for s in specs for t in s["fault_types"]}
    for needed in ("timing_trap", "multi_fault", "log_noise", "flapping", "downstream_timeout",
                   "partial_failure", "upstream_blame_trap", "config_error", "data_issue", "memory_leak",
                   "cpu_pressure"):
        assert needed in types
    # blame must go to shop-api in some deploy scenarios, not always payments
    culprit_services = {d["service"] for s in specs for d in s["deploys"] if d["culprit"]}
    assert culprit_services == {"payments-svc", "shop-api"}
    assert any(not d["culprit"] for s in specs for d in s["deploys"])  # innocent red-herring deploys
    assert any(s["ground_truth"].get("alternatives") for s in specs)
    assert any(s["load"]["extra_rps"] for s in specs)


def test_check_mode_detects_stale_files(tmp_path, monkeypatch):
    ce.write(ce.generate(), tmp_path)
    monkeypatch.setattr(sys, "argv", ["chaos_engineer.py", "--check", "--out", str(tmp_path)])
    ce.main()
    victim = next((tmp_path / "train").glob("*.json"))
    victim.write_text(victim.read_text().replace('"description": "', '"description": "edited ', 1))
    with pytest.raises(SystemExit):
        ce.main()


# ---------- optional LLM mode: mocked, never a real call ----------

class FakeBlock:
    def __init__(self, text):
        self.text = text


class FakeClient:
    def __init__(self, text):
        self.text, self.calls = text, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return type("Msg", (), {"content": [FakeBlock(self.text)]})()


def test_llm_mode_keeps_only_valid_proposals(specs):
    good = copy.deepcopy(specs[0])
    good["id"] = "llm-novel-1"
    bad = copy.deepcopy(specs[0])
    bad["id"] = "llm-broken"
    bad["difficulty"] = "impossible"
    client = FakeClient("Here you go:\n" + json.dumps([good, bad, "junk"]))
    out = ce.llm_generate(2, specs, client=client, model="test-model")
    assert [s["id"] for s in out] == ["llm-novel-1"]
    assert out[0]["generator"] == {"mode": "llm"} and out[0]["split"] == "train"
    assert client.calls[0]["model"] == "test-model"
    assert ce.llm_generate(1, specs, client=FakeClient("no json here")) == []


def test_llm_mode_refuses_without_the_flag(specs, monkeypatch):
    monkeypatch.delenv("LOKI_CHAOS_LLM", raising=False)
    with pytest.raises(SystemExit):
        ce.llm_generate(1, specs)


# ---------- schema validation ----------

def test_validator_catches_broken_specs(specs):
    base = by_template(specs, "shop-bad-deploy")[0]

    def broken(mutate):
        s = copy.deepcopy(base)
        mutate(s)
        return ss.validate(s)

    assert broken(lambda s: s.update(id="Bad ID!"))
    assert broken(lambda s: s.update(difficulty="extreme"))
    assert broken(lambda s: s.update(split="test"))
    assert broken(lambda s: s["fault_types"].append("gremlins"))
    assert broken(lambda s: s["load"].update(extra_rps=500))
    assert broken(lambda s: s.update(steps=[]))
    assert broken(lambda s: s["steps"].append({"at_s": 1, "action": "fault", "service": "shop-api", "fault": "nope"}))
    assert broken(lambda s: s["steps"].append({"at_s": 1, "action": "fault", "service": "payments-svc",
                                               "fault": "amount_field", "params": {}}))  # wrong service
    assert broken(lambda s: s["steps"].append({"at_s": 1, "action": "fault", "service": "payments-svc",
                                               "fault": "slow_db", "params": {"seconds": 99}}))
    assert broken(lambda s: s["steps"].append({"at_s": 1, "action": "fault", "service": "payments-svc",
                                               "fault": "slow_db", "params": {"colour": 1}}))
    assert broken(lambda s: s["steps"].insert(0, {"at_s": 50, "action": "mode", "mode": "none"}))  # out of order
    assert broken(lambda s: s["steps"].append({"at_s": 99, "action": "mode", "mode": "explode"}))
    assert broken(lambda s: s["ground_truth"].update(deploy_ref="d9"))
    assert broken(lambda s: s["ground_truth"].update(deploy_ref=None))  # culprit d1 then unnamed + deploy trigger
    assert broken(lambda s: s["ground_truth"].update(cause_type="vibes"))
    assert broken(lambda s: s["ground_truth"].update(triggers=["astrology"]))
    assert broken(lambda s: s["ground_truth"].update(acceptable_services=[]))
    assert broken(lambda s: s["ground_truth"].update(actions_any=["pray"]))
    assert broken(lambda s: s["ground_truth"].pop("trap"))
    assert broken(lambda s: s["deploys"][0].update(diff="not a diff"))
    assert broken(lambda s: s["deploys"].append(dict(s["deploys"][0])))  # duplicate ref
    assert broken(lambda s: s["deploys"][0].update(culprit=False))
    assert broken(lambda s: s["ground_truth"].update(trap_deploy_refs=["d1"]))  # culprit can't be a trap
    assert ss.validate(base) == []


# ---------- the generic grader ----------

def report(service, trigger, deploy="", text="", action="escalate"):
    return {"summary": text, "root_cause": text, "root_cause_service": service, "trigger": trigger,
            "deploy_id": deploy, "suggested_action": action, "action_detail": ""}


def test_grader_shop_deploy_trap(specs):
    s = by_template(specs, "shop-bad-deploy")[1]  # variant with an innocent payments deploy
    ids = {"d1": "aaa1111", "d0": "bbb2222"}
    right = ss.grade(report("shop-api", "deploy", "aaa1111", "shop sends amount_minor, payments 422s", "rollback"), s, ids)
    assert right["correct"] and right["score"] == 1 and not right["fooled"]
    fooled = ss.grade(report("payments-svc", "deploy", "bbb2222", "payments deploy broke charges", "rollback"), s, ids)
    assert not fooled["correct"] and fooled["fooled"]
    assert not fooled["checks"]["right_service"] and not fooled["checks"]["right_deploy"]
    fooled_svc = ss.grade(report("payments-svc", "dependency", "", "payments rejects requests"), s, ids)
    assert fooled_svc["fooled"] and not fooled_svc["correct"]
    partial = ss.grade(report("shop-api", "deploy", "zzz9999", "a shop-api deploy changed the amount field", "rollback"), s, ids)
    assert not partial["correct"] and 0 < partial["score"] < 1 and not partial["fooled"]


def test_grader_timing_trap(specs):
    s = by_template(specs, "deploy-then-slow-db")[0]
    ids = {"d0": "ccc3333"}
    assert ss.grade(report("postgres", "infrastructure", "", "postgres queries take 2s"), s, ids)["correct"]
    # payments-svc counts only when the explanation names the database
    assert ss.grade(report("payments-svc", "dependency", "", "its database (postgres) is slow"), s, ids)["correct"]
    assert not ss.grade(report("payments-svc", "unknown", "", "something inside payments"), s, ids)["correct"]
    fooled = ss.grade(report("payments-svc", "deploy", "ccc3333", "the structlog bump broke payments"), s, ids)
    assert fooled["fooled"] and not fooled["correct"]
    assert fooled["checks"]["no_deploy_blamed"] is False


def test_grader_two_faults_accepts_either(specs):
    s = by_template(specs, "two-faults")[0]  # bad product + card 503s
    card = ss.grade(report("card-network", "dependency", "", "card network 503s"), s, {})
    data = ss.grade(report("shop-api", "unknown", "", f"product price is null"), s, {})
    assert card["correct"] and card["matched"] == 0
    assert data["correct"] and data["matched"] == 1
    assert not ss.grade(report("postgres", "infrastructure", "", "database slow"), s, {})["correct"]


def test_grader_payments_bad_deploy_and_action(specs):
    s = by_template(specs, "payments-bad-deploy")[0]
    ids = {"d1": "ddd4444"}
    ok = ss.grade(report("payments-svc", "deploy", "ddd4444", "deploy reads missing currency", "rollback"), s, ids)
    assert ok["correct"]
    wrong_action = ss.grade(report("payments-svc", "deploy", "ddd4444", "deploy reads missing currency", "restart"), s, ids)
    assert not wrong_action["correct"] and wrong_action["checks"]["right_action"] is False
    no_deploy = ss.grade(report("payments-svc", "unknown", "", "currency KeyError"), s, ids)
    assert no_deploy["checks"]["right_deploy"] is False


def test_grader_many_synthetic_reports(specs):
    """For every scenario: a report built from its own ground truth passes, and the
    classic wrong answers (wrong service, no/other deploy, wrong trigger) fail."""
    for s in specs:
        gt = s["ground_truth"]
        ids = {d["ref"]: f"id{n:05d}" for n, d in enumerate(s["deploys"])}
        svc = gt["acceptable_services"][0]
        svc = svc if isinstance(svc, str) else svc["service"]
        text = " ".join(gt.get("keywords_any", [])) + " " + " ".join(
            k for a in gt["acceptable_services"] if isinstance(a, dict) for k in a["if_text_any"])
        good = report(svc, gt["triggers"][0], ids.get(gt["deploy_ref"], "") if gt["deploy_ref"] else "",
                      text, (gt.get("actions_any") or ["escalate"])[0])
        assert ss.grade(good, s, ids)["correct"], s["id"]
        wrong_svc = dict(good, root_cause_service="loadgen")
        assert not ss.grade(wrong_svc, s, ids)["correct"], s["id"]
        wrong_trigger = dict(good, trigger="traffic")
        assert not ss.grade(wrong_trigger, s, ids)["correct"], s["id"]
        if gt["deploy_ref"]:
            assert not ss.grade(dict(good, deploy_id=""), s, ids)["correct"], s["id"]
        else:
            assert not ss.grade(dict(good, deploy_id="ffff000"), s, ids)["correct"], s["id"]


def test_service_matching():
    assert ss.service_matches("payments", "payments-svc")
    assert ss.service_matches("Payments Svc", "payments-svc")
    assert ss.service_matches("payments-svc (postgres)", "postgres")
    assert ss.service_matches("card network", "card-network")
    assert not ss.service_matches("shop-api", "payments-svc")
    assert not ss.service_matches("postgres", "payments-svc")
    assert not ss.service_matches("", "shop-api")


# ---------- eval.py wiring ----------

def test_eval_keeps_legacy_grader_and_summary():
    import eval as ev
    checks = ev.grade("bad_deploy", report("payments-svc", "deploy", "abc1234", "", "rollback"), {"deploy_id": "abc1234"})
    assert all(checks.values())
    rows = [{"scenario": "slow_db", "run": 1, "seconds": 30, "correct": True, "checks": {"a": True},
             "cost_usd": 0.01, "steps": 4}]
    out = ev.summarize(rows, "m", "t")
    assert "| slow_db | 1 | 1 | 100% |" in out and "By difficulty" not in out


def test_eval_spec_rows_and_breakdowns(specs):
    import eval as ev
    s = by_template(specs, "shop-bad-deploy")[0]
    inv = {"id": 7, "status": "done"}
    full = {"report": report("payments-svc", "deploy", "x", "payments broke", "rollback"), "cost_usd": "0.02", "steps": 5}
    row = ev.spec_row(s, 1, "fake", inv, 42, {"d1": "aaa1111"}, full)
    assert row["fooled"] and not row["correct"] and row["difficulty"] == "hard"
    miss = ev.spec_row(s, 2, "fake", None, 360, {}, None)
    assert miss["error"] and miss["score"] == 0
    out = ev.summarize([row, miss], "fake", "t")
    assert "## By difficulty" in out and "## By fault type" in out and "upstream_blame_trap" in out
    assert "| hard | 2 | 0 | 0% |" in out


def test_eval_selects_by_set_and_filters(monkeypatch):
    import eval as ev
    args = type("A", (), {"set": "heldout", "files": None, "difficulty": ["hard"], "fault_type": None})()
    sel = ev.select_specs(args)
    assert sel and all(s["split"] == "heldout" and s["difficulty"] == "hard" for s in sel)


# ---------- the runner (no network: post is faked) ----------

def test_runner_plays_steps_records_deploys_and_resets(specs, tmp_path):
    s = copy.deepcopy(by_template(specs, "shop-bad-deploy")[1])
    for st in s["steps"]:
        st["at_s"] = 0
    posts = []
    deploys = tmp_path / "deploys.json"
    deploys.write_text("[]")
    run = scenario_runner.ScenarioRun(s, {"payments-svc": "http://p", "shop-api": "http://s"}, deploys,
                                      tmp_path / "gt.jsonl", post=lambda url, body: posts.append((url, body)),
                                      log=lambda *_: None)
    run.start(background=False)
    assert ("http://s/chaos", {"faults": {"amount_field": {}}}) in posts
    recorded = json.loads(deploys.read_text())
    assert [d["id"] for d in recorded] == [run.deploy_ids["d0"], run.deploy_ids["d1"]]
    run.stop()
    assert ("http://p/chaos", {"mode": "none"}) in posts and ("http://s/chaos", {"mode": "none"}) in posts
    recorded = json.loads(deploys.read_text())
    assert len(recorded) == 3 and recorded[-1]["message"].startswith("Revert") and recorded[-1]["service"] == "shop-api"
    assert run.deploy_ids["d1"] in recorded[-1]["message"]
    truth = json.loads((tmp_path / "gt.jsonl").read_text())
    assert truth["scenario"] == s["id"] and truth["deploy_ids"] == run.deploy_ids


def test_runner_rejects_invalid_spec(specs):
    s = copy.deepcopy(specs[0])
    s["difficulty"] = "nope"
    with pytest.raises(ValueError):
        scenario_runner.ScenarioRun(s)


def test_revert_diff_swaps_lines():
    d = "--- a/x\n+++ b/x\n@@\n-old\n+new\n same\n"
    assert scenario_runner.revert_diff(d) == "--- a/x\n+++ b/x\n@@\n+old\n-new\n same\n"
