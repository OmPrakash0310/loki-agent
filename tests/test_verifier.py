"""The verifier with a fake docker runner: verdicts, teardown, request samples,
and how a verdict shows up in the report and in Slack. No Docker needed."""
import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import investigator
import slack
import telemetry as tm
import verifier as v

BAD, GOOD = "loki-demo/payments-svc:v2.5.0", "loki-demo/payments-svc:v2.4.0"
REQUEST = {"method": "POST", "path": "/charge", "status": 500, "error": "KeyError: 'currency'",
           "body": {"order_id": 4242, "amount": 250.0}}
TRACEBACK = """ERROR:    Exception in ASGI application
  + Exception Group Traceback (most recent call last):
  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
    |     currency = payload["currency"]  # KeyError -> 500
    | KeyError: 'currency'
KeyError: 'currency'
"""


def _proc(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class FakeDocker:
    """Answers docker CLI calls like a healthy daemon. Knobs make it misbehave."""

    def __init__(self, bad_status=500, prev_status=200, bad_logs=TRACEBACK, prev_logs="INFO charged order=1",
                 fail=None, hang=None, crash=None, missing=None, running=True):
        self.calls = []
        self.status = {"bad": bad_status, "prev": prev_status}
        self.log_text = {"bad": bad_logs, "prev": prev_logs}
        self.fail, self.hang, self.crash, self.missing, self.is_running = fail, hang, crash, missing, running

    def _role(self, name):
        return "bad" if name.endswith("-bad") else "prev"

    def __call__(self, args, timeout):
        self.calls.append(args)
        cmd = args[0]
        if cmd == self.hang:
            raise subprocess.TimeoutExpired(["docker", *args], timeout)
        if cmd == self.crash:
            raise RuntimeError("docker exploded")
        if cmd == self.fail:
            return _proc(1, err=f"Error response from daemon: {cmd} broke")
        if args[:2] == ["image", "inspect"]:
            return _proc(1, err="No such image") if args[-1] == self.missing else _proc(0, "sha256:abc")
        if cmd == "exec":
            if "pg_isready" in args:
                return _proc(0, "accepting connections")
            url = args[6]
            if url.endswith("/health"):
                return _proc(0, json.dumps({"status": 200 if self.is_running else 0, "body": ""}))
            return _proc(0, json.dumps({"status": self.status[self._role(args[1])], "body": ""}))
        if cmd == "logs":
            return _proc(0, "", self.log_text[self._role(args[1])])
        if cmd == "inspect":
            return _proc(0, "true" if self.is_running else "false")
        return _proc(0)

    def removed(self):
        return [c for c in self.calls if c[0] == "rm"]

    def network_removed(self):
        return [c for c in self.calls if c[:2] == ["network", "rm"]]


# --- the verdict (pure) ----------------------------------------------------

def test_judge_confirmed_needs_the_production_error():
    status, reason = v.judge({"status": 500, "errors": ["KeyError: 'currency'"]}, {"status": 200, "errors": []},
                             "KeyError: 'currency'")
    assert status == v.CONFIRMED and "HTTP 500" in reason and "HTTP 200" in reason


def test_judge_different_error_is_not_reproduced():
    status, reason = v.judge({"status": 500, "errors": ["ValueError: bad amount"]}, {"status": 200, "errors": []},
                             "KeyError: 'currency'")
    assert status == v.NOT_REPRODUCED and "ValueError" in reason


def test_judge_ignores_numbers_in_the_signature():
    status, _ = v.judge({"status": 500, "errors": ["KeyError: 'order 99'"]}, {"status": 200, "errors": []},
                        "KeyError: 'order 12'")
    assert status == v.CONFIRMED


@pytest.mark.parametrize("bad,prev,why", [
    (200, 200, "both versions handle"),
    (500, 500, "previous version fails too"),
    (0, 0, "previous version fails too"),
    (200, 500, "doesn't fail"),
    (422, 200, "doesn't fail"),
])
def test_judge_not_reproduced(bad, prev, why):
    status, reason = v.judge({"status": bad, "errors": []}, {"status": prev, "errors": []})
    assert status == v.NOT_REPRODUCED and why in reason


def test_error_lines_pick_exception_summaries_not_groups():
    lines = v.error_lines(TRACEBACK + '2026-10-18 06:06:30 ERROR payments-svc failed request sample: {"x": 1}\n')
    assert "KeyError: 'currency'" in lines
    assert not any(l.startswith("ExceptionGroup") for l in lines)
    assert not any(v.SAMPLE_MARKER in l for l in lines)


# --- the sandbox with a fake docker -----------------------------------------

def test_verify_confirmed_and_torn_down():
    docker = FakeDocker()
    r = v.verify(BAD, GOOD, REQUEST, REQUEST["error"], docker=docker)
    assert r["status"] == v.CONFIRMED, r
    assert r["evidence"][0].startswith("Replayed POST /charge")
    assert f"{BAD}: HTTP 500, KeyError: 'currency'" in r["evidence"][1]
    assert f"{GOOD}: HTTP 200" in r["evidence"][2]
    assert isinstance(r["seconds"], float)
    # Everything it created is removed: three containers, then the network.
    (rm,) = docker.removed()
    assert len([a for a in rm if a.startswith("loki-verify-")]) == 3
    assert len(docker.network_removed()) == 1


def test_verify_not_reproduced_when_both_work():
    r = v.verify(GOOD, GOOD, REQUEST, REQUEST["error"], docker=FakeDocker(bad_status=200, bad_logs=""))
    assert r["status"] == v.NOT_REPRODUCED and "both versions handle" in r["reason"]


def test_sandbox_is_isolated_and_named():
    docker = FakeDocker()
    v.verify(BAD, GOOD, REQUEST, docker=docker)
    (net,) = [c for c in docker.calls if c[:2] == ["network", "create"]]
    assert "--internal" in net and net[-1].startswith("loki-verify-")
    for c in docker.calls:
        if c[0] in ("run", "create"):
            assert c[c.index("--name") + 1].startswith("loki-verify-")
            assert c[c.index("--network") + 1] == net[-1]
            assert "never" == c[c.index("--pull") + 1]  # never pulls
            assert "-p" not in c and "--publish" not in c and "-v" not in c  # no ports, no host mounts
            env = [c[i + 1] for i, a in enumerate(c) if a == "-e"]
            assert all("@postgres:" in e or e.startswith("POSTGRES_") for e in env)


@pytest.mark.parametrize("args,why", [
    ((None, GOOD, REQUEST), "no image recorded for the new version"),
    ((BAD, None, REQUEST), "no image recorded for the previous version"),
    ((BAD, GOOD, None), "no failed request"),
    ((BAD, GOOD, {"method": "POST"}), "no failed request"),
])
def test_inconclusive_without_inputs_never_touches_docker(args, why):
    docker = FakeDocker()
    r = v.verify(*args, docker=docker)
    assert r["status"] == v.INCONCLUSIVE and why in r["reason"]
    assert docker.calls == []


def test_inconclusive_when_image_missing():
    docker = FakeDocker(missing=BAD)
    r = v.verify(BAD, GOOD, REQUEST, docker=docker)
    assert r["status"] == v.INCONCLUSIVE and BAD in r["reason"]
    assert not any(c[0] in ("run", "create") for c in docker.calls)


@pytest.mark.parametrize("knob,why", [
    ({"fail": "run"}, "sandbox failed"),
    ({"fail": "cp"}, "sandbox failed"),
    ({"hang": "run"}, "timed out"),
    ({"hang": "exec"}, "timed out"),
    ({"crash": "start"}, "verifier error"),
    ({"running": False}, "exited during startup"),
])
def test_errors_are_inconclusive_and_always_torn_down(knob, why):
    docker = FakeDocker(**knob)
    r = v.verify(BAD, GOOD, REQUEST, docker=docker)
    assert r["status"] == v.INCONCLUSIVE and why in r["reason"], r
    assert r["evidence"][0].startswith("Tried to replay")
    assert docker.removed() and docker.network_removed()


def test_hard_time_cap(monkeypatch):
    # The app never becomes healthy; the cap must stop the wait.
    monkeypatch.setattr(v, "POLL_S", 0.05)
    docker = FakeDocker()
    real = docker.__call__

    def never_healthy(args, timeout):
        if args[0] == "exec" and args[-2].endswith("/health"):
            docker.calls.append(args)
            return _proc(0, json.dumps({"status": 0, "body": "refused"}))
        return real(args, timeout)

    r = v.verify(BAD, GOOD, REQUEST, docker=never_healthy, timeout_s=0.3)
    assert r["status"] == v.INCONCLUSIVE and "timed out" in r["reason"]
    assert r["seconds"] < 2
    assert docker.removed() and docker.network_removed()


def test_teardown_keeps_going_when_rm_fails():
    calls = []

    def docker(args, timeout):
        calls.append(args)
        if args[0] == "rm":
            raise subprocess.TimeoutExpired(args, timeout)
        return _proc(0)

    box = v.Sandbox(docker, deadline=float("inf"), run_id="t1")
    box.containers = ["loki-verify-t1-db"]
    box.network_created = True
    box.teardown()
    assert calls == [["rm", "-f", "-v", "loki-verify-t1-db"], ["network", "rm", "loki-verify-t1-net"]]


# --- request samples ---------------------------------------------------------

LINE = ('2026-10-18 06:06:30,680 ERROR payments-svc failed request sample: {"method": "POST", "path": "/charge", '
        '"status": 500, "error": "KeyError: \'currency\'", "body": {"order_id": 777, "card_token": "[REDACTED]"}}')


def test_parse_sample():
    s = v.parse_sample(LINE)
    assert s["method"] == "POST" and s["path"] == "/charge" and s["error"] == "KeyError: 'currency'"
    assert s["body"] == {"order_id": 777, "card_token": "[REDACTED]"}


@pytest.mark.parametrize("line", [
    "2026-10-18 ERROR payments-svc charge failed",
    "failed request sample: {not json",
    'failed request sample: {"method": "POST"}',
    'failed request sample: {"method": "POST", "path": "charge"}',
    'failed request sample: ["POST", "/charge"]',
])
def test_parse_sample_rejects_junk(line):
    assert v.parse_sample(line) is None


def test_find_sample_takes_the_latest(monkeypatch):
    t = datetime(2026, 10, 18, 6, tzinfo=timezone.utc)
    older = LINE.replace("777", "111")
    lines = [(t, "payments-svc", older), (t, "payments-svc", "KeyError: 'currency'"), (t, "payments-svc", LINE)]
    seen = {}
    monkeypatch.setattr(tm, "fetch_logs", lambda svc, start, end, errors_only: seen.update(svc=svc) or lines)
    assert v.find_sample("payments-svc", t, t)["body"]["order_id"] == 777
    assert seen["svc"] == "payments-svc"


def test_request_query_string_is_replayed():
    docker = FakeDocker()
    v.verify(BAD, GOOD, {**REQUEST, "query": "retry=1"}, docker=docker)
    urls = [c[6] for c in docker.calls if c[0] == "exec" and "-c" in c and not c[6].endswith("/health")]
    assert urls and all(u.endswith("/charge?retry=1") for u in urls)


# --- from a report to a verdict -----------------------------------------------

def _deploy(id_, version, minute, image=True):
    d = {"id": id_, "service": "payments-svc", "version": version,
         "deployed_at_dt": datetime(2026, 10, 18, 6, minute, tzinfo=timezone.utc)}
    if image:
        d["image"] = f"loki-demo/payments-svc:{version}"
    return d


DEPLOYS = [_deploy("aaa1111", "v2.4.0", 0), {**_deploy("sss0000", "v1.0", 1), "service": "shop-api"},
           _deploy("bbb2222", "v2.5.0", 5)]
REPORT = {"trigger": "deploy", "deploy_id": "bbb2222", "summary": "s", "root_cause": "r",
          "root_cause_service": "payments-svc", "affected_services": ["payments-svc"], "evidence": ["e"],
          "confidence": 0.9, "suggested_action": "rollback", "action_detail": "roll back"}


def test_find_suspect_and_images(monkeypatch):
    deploy, previous = v.find_suspect(DEPLOYS, "bbb2222")
    assert deploy["version"] == "v2.5.0" and previous["id"] == "aaa1111"  # same service only
    assert v.find_suspect(DEPLOYS, "aaa1111") == (DEPLOYS[0], None)
    assert v.find_suspect(DEPLOYS, "nope") == (None, None)
    assert v.image_for(DEPLOYS[0]) == "loki-demo/payments-svc:v2.4.0"
    bare = _deploy("c", "v2.5.1", 9, image=False)
    assert v.image_for(bare) is None
    monkeypatch.setattr(v, "IMAGE_TEMPLATE", "registry.local/{service}:{version}")
    assert v.image_for(bare) == "registry.local/payments-svc:v2.5.1"


def test_verify_report_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LOKI_VERIFY", raising=False)
    assert v.verify_report(REPORT, docker=FakeDocker()) is None


def test_verify_report_non_deploy_stays_unverified(monkeypatch):
    monkeypatch.setenv("LOKI_VERIFY", "1")
    r = v.verify_report({**REPORT, "trigger": "dependency", "deploy_id": ""}, docker=FakeDocker())
    assert r["status"] == v.UNVERIFIED and "not blamed on a deploy" in r["reason"]


def test_verify_report_end_to_end_with_fake_docker(monkeypatch):
    monkeypatch.setenv("LOKI_VERIFY", "1")
    monkeypatch.setattr(tm, "load_deploys", lambda: DEPLOYS)
    since = {}
    monkeypatch.setattr(v, "find_sample", lambda svc, at: since.update(svc=svc, at=at) or REQUEST)
    r = v.verify_report(REPORT, docker=FakeDocker())
    assert r["status"] == v.CONFIRMED
    assert since == {"svc": "payments-svc", "at": DEPLOYS[2]["deployed_at_dt"]}  # samples after the deploy only
    assert "deploy bbb2222 loki-demo/payments-svc:v2.5.0: HTTP 500" in r["evidence"][1]
    assert "previous deploy aaa1111 loki-demo/payments-svc:v2.4.0: HTTP 200" in r["evidence"][2]


def test_verify_report_inconclusive_cases(monkeypatch):
    monkeypatch.setenv("LOKI_VERIFY", "1")
    monkeypatch.setattr(tm, "load_deploys", lambda: DEPLOYS)
    monkeypatch.setattr(v, "find_sample", lambda svc, at: None)
    assert "no failed request" in v.verify_report(REPORT, docker=FakeDocker())["reason"]
    assert "no earlier payments-svc deploy" in v.verify_report({**REPORT, "deploy_id": "aaa1111"})["reason"]
    assert "isn't in the deploy history" in v.verify_report({**REPORT, "deploy_id": "zzz"})["reason"]

    def broken():
        raise RuntimeError("loki is down")
    monkeypatch.setattr(tm, "load_deploys", broken)
    r = v.verify_report(REPORT)
    assert r["status"] == v.INCONCLUSIVE and "loki is down" in r["reason"]


# --- report, render and Slack --------------------------------------------------

CONFIRMED = {"status": v.CONFIRMED, "reason": "the new version fails", "seconds": 12.8,
             "evidence": ["Replayed POST /charge", f"{BAD}: HTTP 500, KeyError: 'currency'", f"{GOOD}: HTTP 200"]}


def test_render_without_verdict_keeps_old_footer():
    text = investigator.render(1, REPORT, 3, 0.01)
    assert text.endswith("not verified by reproduction yet_") and "Verification" not in text


@pytest.mark.parametrize("verdict,shown", [
    (CONFIRMED, "✅ Verified by reproduction"),
    ({**CONFIRMED, "status": v.NOT_REPRODUCED}, "⚠️ Not reproduced"),
    ({"status": v.INCONCLUSIVE, "reason": "no failed request to replay", "evidence": [], "seconds": 0}, "Unverified"),
    ({"status": v.UNVERIFIED, "reason": "not blamed on a deploy", "evidence": [], "seconds": 0}, "Unverified"),
])
def test_render_and_slack_show_the_verdict(verdict, shown):
    r = {**REPORT, "verification": verdict}
    text = investigator.render(1, r, 3, 0.01)
    assert f"*Verification:* {shown}" in text and text.endswith(f"{shown}_")
    assert "not verified by reproduction yet" not in text
    assert verdict["reason"] in text

    blocks = slack.build_blocks(1, r, 3, 0.01)
    section = next(b["text"]["text"] for b in blocks if b.get("text", {}).get("text", "").startswith("*Verification:*"))
    assert shown in section
    footer = next(b for b in blocks if b["type"] == "context")["elements"][0]["text"]
    assert footer.endswith(shown) and "not verified by reproduction yet" not in footer
    if verdict is CONFIRMED:
        assert "in 13s" in section and "KeyError: 'currency'" in section and "HTTP 200" in section


def test_investigator_attaches_verdict_and_traces(monkeypatch):
    executed = []
    conn = SimpleNamespace(execute=lambda sql, params: executed.append((sql, params)))
    monkeypatch.setattr(investigator.verifier, "verify_report", lambda report: CONFIRMED)
    report = dict(REPORT)
    investigator._verify(conn, 7, 3, report)
    assert report["verification"] == CONFIRMED
    (sql, params), = executed
    assert "trace_steps" in sql and params[2] == "verify" and json.loads(params[4])["status"] == v.CONFIRMED


def test_investigator_leaves_report_alone_when_off(monkeypatch):
    monkeypatch.delenv("LOKI_VERIFY", raising=False)
    conn = SimpleNamespace(execute=lambda *a: pytest.fail("nothing to trace"))
    report = dict(REPORT)
    investigator._verify(conn, 7, 3, report)
    assert "verification" not in report
