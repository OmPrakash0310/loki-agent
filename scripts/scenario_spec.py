"""The scenario format for Loki's evals: the fault catalog, a validator, and the grader.

A scenario is one JSON file under evals/scenarios/<split>/<id>.json:

    {
      "id": "s07-shop-config-timeout-b",
      "description": "...",
      "difficulty": "easy" | "medium" | "hard",
      "split": "train" | "heldout",
      "fault_types": ["config_error", ...],          # what this scenario exercises (FAULT_TYPES)
      "load": {"shape": "steady" | "burst" | "ramp", "extra_rps": 0..20, "duration_s": 0..900},
      "deploys": [{"ref": "d1", "service": "shop-api", "version": "v1.9.0", "author": "meera",
                   "message": "...", "kind": "code" | "config", "diff": "--- a/...", "culprit": true}],
      "steps": [                                      # run in at_s order, seconds after the start
        {"at_s": 0,  "action": "deploy", "ref": "d1"},
        {"at_s": 5,  "action": "fault", "service": "shop-api", "fault": "amount_field", "params": {}},
        {"at_s": 90, "action": "clear", "service": "shop-api", "fault": "amount_field"},
        {"at_s": 0,  "action": "mode", "mode": "bad_deploy"}    # chaos.py's original payments modes
      ],
      "ground_truth": {
        "root_cause_service": "shop-api",
        "cause_type": "config_error",                 # one of CAUSE_TYPES
        "triggers": ["deploy"],                       # report.trigger values accepted
        "deploy_ref": "d1" | null,                    # the deploy Loki must name, or null: name none
        "acceptable_services": ["shop-api", {"service": "payments-svc", "if_text_any": ["postgres"]}],
        "keywords_any": ["timeout"],                  # the explanation must mention one (optional)
        "actions_any": ["rollback"],                  # accepted suggested_action values (optional)
        "trap": "why a naive investigator gets this wrong",
        "trap_services": ["payments-svc"], "trap_deploy_refs": [],
        "alternatives": [ {...another full answer that also counts...} ]   # optional, e.g. two faults
      }
    }

Deploy ids are made at run time (a scenario can run many times), so specs name deploys by
"ref" and the runner maps each ref to the id it recorded.
"""
from __future__ import annotations

import re

# ---------- the fault catalog: what the demo services can be told to do ----------
# name -> service, params {name: (type, min, max, default)}, and what it looks like.
COMMON_PARAMS = {"flap_s": (float, 5, 300, None)}  # any fault: on for flap_s, off for flap_s, ...

FAULTS = {
    # payments-svc
    "slow_db": {"service": "payments-svc", "params": {"seconds": (float, 0.2, 8, 1.5), "rate": (float, 0.05, 1, 1.0)},
                "what": "every (or a share of) payment query waits in Postgres"},
    "card_hang": {"service": "payments-svc", "params": {"seconds": (float, 1, 10, 5)},
                  "what": "the card network hangs before answering"},
    "card_errors": {"service": "payments-svc", "params": {"rate": (float, 0.05, 1, 1.0), "status": (int, 500, 504, 503)},
                    "what": "the card network answers quickly with 5xx errors"},
    "risk_regression": {"service": "payments-svc", "params": {"min_amount": (float, 100, 5000, 1000)},
                        "what": "a code bug that fails only charges at or above min_amount"},
    "mem_leak": {"service": "payments-svc", "params": {"kb_per_request": (int, 16, 1024, 64), "max_mb": (int, 50, 700, 300)},
                 "what": "the receipt cache stops evicting: memory and latency creep up"},
    # shop-api
    "amount_field": {"service": "shop-api", "params": {},
                     "what": "shop-api sends amount_minor instead of amount: payments rejects with 422"},
    "payments_timeout_config": {"service": "shop-api", "params": {"timeout_s": (float, 0.005, 0.5, 0.05)},
                                "what": "a config value makes shop-api's payments timeout far too short"},
    "bad_product": {"service": "shop-api", "params": {"product_id": (int, 1, 3, 2)},
                    "what": "one product's catalog row has a NULL price: only its orders fail"},
    "cpu_burn": {"service": "shop-api", "params": {"rounds": (int, 1000, 5_000_000, 1_000_000)},
                 "what": "recommendation scoring burns CPU on every request"},
    "log_flood": {"service": "shop-api", "params": {"lines_per_request": (int, 1, 50, 5)},
                  "what": "unrelated error lines (analytics, recs cache) flood the logs"},
}
LEGACY_MODES = ["none", "bad_deploy", "slow_db", "timeout"]  # chaos.py's payments-svc modes
SERVICES = {"payments-svc", "shop-api"}

CAUSE_TYPES = {
    "bad_deploy", "partial_deploy_regression", "config_error", "slow_db", "dependency_errors",
    "dependency_hang", "memory_leak", "cpu_pressure", "data_issue",
}
# Tags for reporting. Cause types plus the shapes that make a scenario hard.
FAULT_TYPES = CAUSE_TYPES | {"log_noise", "flapping", "timing_trap", "multi_fault", "downstream_timeout",
                             "partial_failure", "upstream_blame_trap"}
TRIGGERS = {"deploy", "infrastructure", "dependency", "traffic", "unknown"}
ACTIONS = {"rollback", "fix_forward", "scale_up", "restart", "escalate", "none"}
DIFFICULTIES = ["easy", "medium", "hard"]
SPLITS = {"train", "heldout"}
SHAPES = {"steady", "burst", "ramp"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")


def validate(spec: dict) -> list[str]:
    """Every problem with a scenario, as readable strings. [] means valid."""
    errs: list[str] = []

    def need(key, kind):
        if not isinstance(spec.get(key), kind):
            errs.append(f"{key}: required {kind.__name__ if isinstance(kind, type) else 'value'}")
            return False
        return True

    if need("id", str) and not ID_RE.match(spec["id"]):
        errs.append("id: lowercase letters, digits and dashes only")
    need("description", str)
    if spec.get("difficulty") not in DIFFICULTIES:
        errs.append(f"difficulty: one of {DIFFICULTIES}")
    if spec.get("split") not in SPLITS:
        errs.append(f"split: one of {sorted(SPLITS)}")
    if need("fault_types", list):
        if not spec["fault_types"]:
            errs.append("fault_types: at least one")
        errs += [f"fault_types: unknown {t!r}" for t in spec["fault_types"] if t not in FAULT_TYPES]

    load = spec.get("load")
    if not isinstance(load, dict):
        errs.append("load: required object")
    else:
        if load.get("shape") not in SHAPES:
            errs.append(f"load.shape: one of {sorted(SHAPES)}")
        if not _num(load.get("extra_rps"), 0, 20):
            errs.append("load.extra_rps: number 0-20")
        if not _num(load.get("duration_s"), 0, 900):
            errs.append("load.duration_s: number 0-900")

    refs = {}
    for n, d in enumerate(spec.get("deploys") or []):
        where = f"deploys[{n}]"
        for key in ("ref", "service", "version", "author", "message", "diff"):
            if not isinstance(d.get(key), str) or not d.get(key):
                errs.append(f"{where}.{key}: required string")
        if d.get("service") not in SERVICES:
            errs.append(f"{where}.service: one of {sorted(SERVICES)}")
        if d.get("kind") not in ("code", "config"):
            errs.append(f"{where}.kind: code or config")
        if not isinstance(d.get("culprit"), bool):
            errs.append(f"{where}.culprit: required bool")
        if isinstance(d.get("diff"), str) and not d["diff"].startswith("--- a/"):
            errs.append(f"{where}.diff: must be a unified diff")
        if d.get("ref") in refs:
            errs.append(f"{where}.ref: duplicate {d['ref']!r}")
        refs[d.get("ref")] = d

    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        errs.append("steps: at least one")
        steps = []
    last, deployed, faulted = -1, set(), 0
    for n, s in enumerate(steps):
        where = f"steps[{n}]"
        if not _num(s.get("at_s"), 0, 600):
            errs.append(f"{where}.at_s: number 0-600")
        elif s["at_s"] < last:
            errs.append(f"{where}.at_s: steps must be in time order")
        else:
            last = s["at_s"]
        action = s.get("action")
        if action == "deploy":
            if s.get("ref") not in refs:
                errs.append(f"{where}.ref: no deploy {s.get('ref')!r}")
            deployed.add(s.get("ref"))
        elif action in ("fault", "clear"):
            fault = FAULTS.get(s.get("fault"))
            if fault is None:
                errs.append(f"{where}.fault: unknown {s.get('fault')!r}")
                continue
            if s.get("service") != fault["service"]:
                errs.append(f"{where}.service: {s.get('fault')} runs in {fault['service']}")
            if action == "fault":
                faulted += 1
                params = s.get("params", {})
                if not isinstance(params, dict):
                    errs.append(f"{where}.params: object")
                    continue
                allowed = {**fault["params"], **COMMON_PARAMS}
                for k, v in params.items():
                    if k not in allowed:
                        errs.append(f"{where}.params.{k}: unknown for {s['fault']}")
                    elif not _num(v, allowed[k][1], allowed[k][2]):
                        errs.append(f"{where}.params.{k}: {allowed[k][1]}-{allowed[k][2]}")
        elif action == "mode":
            if s.get("mode") not in LEGACY_MODES:
                errs.append(f"{where}.mode: one of {LEGACY_MODES}")
            faulted += s.get("mode") != "none"
        else:
            errs.append(f"{where}.action: deploy, fault, clear or mode")
    if steps and not faulted:
        errs.append("steps: no fault is ever switched on")
    errs += [f"deploys: {r!r} is never deployed by a step" for r in refs if r not in deployed]

    gt = spec.get("ground_truth")
    if not isinstance(gt, dict):
        errs.append("ground_truth: required object")
        return errs
    errs += _validate_truth(gt, refs, "ground_truth")
    for n, alt in enumerate(gt.get("alternatives") or []):
        errs += _validate_truth(alt, refs, f"ground_truth.alternatives[{n}]")
    if not isinstance(gt.get("trap"), str):
        errs.append("ground_truth.trap: required string (\"\" when there is none)")
    for r in gt.get("trap_deploy_refs") or []:
        if r not in refs:
            errs.append(f"ground_truth.trap_deploy_refs: no deploy {r!r}")
        elif refs[r]["culprit"]:
            errs.append(f"ground_truth.trap_deploy_refs: {r!r} is the culprit, not a trap")
    culprits = {r for r, d in refs.items() if d.get("culprit")}
    named = {gt.get("deploy_ref")} | {a.get("deploy_ref") for a in gt.get("alternatives") or []}
    for r in culprits - named:
        errs.append(f"deploys: {r!r} is a culprit but no ground truth names it")
    return errs


def _validate_truth(gt: dict, refs: dict, where: str) -> list[str]:
    errs = []
    if not isinstance(gt.get("root_cause_service"), str) or not gt.get("root_cause_service"):
        errs.append(f"{where}.root_cause_service: required string")
    if gt.get("cause_type") not in CAUSE_TYPES:
        errs.append(f"{where}.cause_type: one of {sorted(CAUSE_TYPES)}")
    triggers = gt.get("triggers")
    if not isinstance(triggers, list) or not triggers or set(triggers) - TRIGGERS:
        errs.append(f"{where}.triggers: non-empty subset of {sorted(TRIGGERS)}")
    ref = gt.get("deploy_ref")
    if ref is not None:
        if ref not in refs:
            errs.append(f"{where}.deploy_ref: no deploy {ref!r}")
        elif not refs[ref].get("culprit"):
            errs.append(f"{where}.deploy_ref: {ref!r} must be marked culprit")
        if triggers and "deploy" not in triggers:
            errs.append(f"{where}.triggers: must accept 'deploy' when a deploy is the cause")
    elif triggers and "deploy" in triggers:
        errs.append(f"{where}.triggers: 'deploy' needs a deploy_ref")
    acc = gt.get("acceptable_services")
    if not isinstance(acc, list) or not acc:
        errs.append(f"{where}.acceptable_services: at least one")
    else:
        for a in acc:
            ok = (isinstance(a, str) and a) or (isinstance(a, dict) and isinstance(a.get("service"), str)
                                               and isinstance(a.get("if_text_any"), list) and a["if_text_any"])
            if not ok:
                errs.append(f"{where}.acceptable_services: string or {{service, if_text_any}}")
    for key, allowed in (("keywords_any", None), ("actions_any", ACTIONS)):
        val = gt.get(key)
        if val is None:
            continue
        if not isinstance(val, list) or not all(isinstance(v, str) and v for v in val):
            errs.append(f"{where}.{key}: list of strings")
        elif allowed and set(val) - allowed:
            errs.append(f"{where}.{key}: subset of {sorted(allowed)}")
    return errs


def _num(v, lo, hi) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


# ---------- grading: one generic grader, driven by the ground truth ----------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def service_matches(reported: str, expected: str) -> bool:
    """'payments-svc' matches 'payments', 'Payments Svc' and 'payments-svc (postgres)'."""
    r, e = _norm(reported), _norm(expected)
    if not r or not e:
        return False
    stem = re.sub(r"-(svc|service|api)$", "", e)
    return e in r or (len(stem) >= 4 and stem in r) or (len(r) >= 4 and r in e)


def _check(report: dict, gt: dict, deploy_ids: dict, trap: dict) -> dict[str, bool]:
    text = f"{report.get('summary', '')} {report.get('root_cause', '')} {report.get('action_detail', '')}".lower()
    service = report.get("root_cause_service", "") or ""
    blamed = (report.get("deploy_id") or "").strip()
    checks = {}

    def acceptable(a) -> bool:
        if isinstance(a, str):
            return service_matches(service, a)
        return service_matches(service, a["service"]) and any(k.lower() in text for k in a["if_text_any"])

    checks["right_service"] = any(acceptable(a) for a in gt["acceptable_services"])
    checks["right_cause_type"] = report.get("trigger") in gt["triggers"]
    if gt.get("deploy_ref"):
        checks["right_deploy"] = bool(blamed) and blamed == deploy_ids.get(gt["deploy_ref"])
    else:
        checks["no_deploy_blamed"] = not blamed
    if gt.get("keywords_any"):
        checks["names_cause"] = any(k.lower() in text for k in gt["keywords_any"])
    if gt.get("actions_any"):
        checks["right_action"] = report.get("suggested_action") in gt["actions_any"]
    trap_ids = {deploy_ids.get(r) for r in trap.get("trap_deploy_refs") or []} - {None}
    trap_svcs = trap.get("trap_services") or []
    if trap_ids or trap_svcs:
        fooled_deploy = blamed in trap_ids
        fooled_service = not checks["right_service"] and any(service_matches(service, s) for s in trap_svcs)
        checks["not_fooled"] = not (fooled_deploy or fooled_service)
    return checks


def grade(report: dict, spec_or_truth: dict, deploy_ids: dict | None = None) -> dict:
    """Grade one report. Returns {"checks", "correct", "score", "fooled", "matched"}.

    A report is correct when every check passes against the ground truth or one of its
    alternatives (scenarios with two independent faults accept either). score is the
    share of checks passed: partial credit for e.g. the right service but the wrong deploy.
    """
    gt = spec_or_truth.get("ground_truth", spec_or_truth)
    deploy_ids = deploy_ids or {}
    trap = {"trap_deploy_refs": gt.get("trap_deploy_refs"), "trap_services": gt.get("trap_services")}
    best = None
    for n, cand in enumerate([gt, *(gt.get("alternatives") or [])]):
        checks = _check(report, cand, deploy_ids, trap)
        key = (all(checks.values()), sum(checks.values()) / len(checks))
        if best is None or key > best[0]:
            best = (key, n, checks)
    (correct, score), matched, checks = best
    return {"checks": checks, "correct": correct, "score": round(score, 3),
            "fooled": checks.get("not_fooled") is False, "matched": matched}
