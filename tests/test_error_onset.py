"""When did errors start? A stray line must not set the start (context._error_onset).

Both failures below happened for real: on the demo stack a startup error two minutes
before the break left the culprit deploy out of the suspects, and in the OTel Demo lab
leftover errors from the previous run made a deploy look like it came after the problem.
"""
from datetime import timedelta

import context
from test_changepoint import INCIDENT, at, deploy, flat, install


def errors(svc, *indexes, msg="KeyError: 'currency'"):
    return [(at(i), svc, msg) for i in indexes]


def test_burst_not_a_stray_line_sets_the_start():
    lines = errors("payments-svc", 40) + errors("payments-svc", 50, 50.5, 51, 52)
    lines.sort()
    assert context._error_onset(lines)["payments-svc"] == at(50)


def test_no_burst_falls_back_to_the_first_error():
    lines = errors("payments-svc", 10, 30, 60)  # one error every few minutes
    assert context._error_onset(lines)["payments-svc"] == at(10)


def burst(svc, start, n=8, msg="KeyError: 'currency'"):
    return errors(svc, *[start + k * 0.5 for k in range(n)], msg=msg)


def test_stray_error_before_a_deploy_no_longer_hides_it(monkeypatch):
    """Demo stack, real run: one shop-api error 2 min before the bad deploy, then the burst."""
    lines = sorted(errors("shop-api", 40, msg="startup: payments-svc not ready")
                   + burst("payments-svc", 49) + burst("shop-api", 49.2, msg="payments-svc returned 500"))
    install(monkeypatch, lambda svc, metric: flat(0.1), deploys=[deploy("914585c", "payments-svc", 48)], errors=lines)
    b = context.build(INCIDENT)
    top = b.suspects[0]
    assert top["kind"] == "deploy" and top["deploy_id"] == "914585c"
    assert "shop-api also logged 1 isolated error line(s) before that, from 16:10:00" in b.text
    assert "errors started per service (first burst): payments-svc 16:12:15" in b.text


def test_leftover_errors_from_an_earlier_incident(monkeypatch):
    """OTel lab, real run: a few leftover cart errors before the new deploy, then the new burst."""
    lines = sorted(errors("cart", 20, 31, 43, msg="Wasn't able to connect to redis")
                   + burst("cart", 52, msg="Wasn't able to connect to redis"))
    install(monkeypatch, lambda svc, metric: flat(0.1), deploys=[deploy("8449717", "cart", 50)], errors=lines)
    b = context.build(INCIDENT)
    assert b.suspects[0].get("deploy_id") == "8449717"
    assert "cart also logged 3 isolated error line(s)" in b.text


def test_a_service_with_only_a_stray_line_cannot_lead(monkeypatch):
    """frontend's single early error must not make it the source or move the start."""
    lines = sorted(errors("frontend", 30, msg="ERROR timeout") + burst("payments-svc", 49))
    install(monkeypatch, lambda svc, metric: flat(0.1), deploys=[deploy("abc", "payments-svc", 48)], errors=lines)
    b = context.build(INCIDENT)
    top = b.suspects[0]
    assert top["deploy_id"] == "abc" and any("where the failure started" in r for r in top["reasons"])


def test_a_real_early_burst_still_counts(monkeypatch):
    """Errors that genuinely began before a deploy keep that deploy out: the rule only ignores strays."""
    lines = sorted(burst("payments-svc", 30) + burst("payments-svc", 49))
    install(monkeypatch, lambda svc, metric: flat(0.1), deploys=[deploy("late", "payments-svc", 45)], errors=lines)
    b = context.build(INCIDENT)
    assert all(s.get("deploy_id") != "late" for s in b.suspects)
