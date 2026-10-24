#!/usr/bin/env python3
"""Run many failures through the OTel Demo lab and score Loki on each, unattended.

    python3 otel-demo/eval.py                         every default failure x every deploy history, once
    python3 otel-demo/eval.py --runs 2 --failures paymentFailure,cartFailure --modes fresh,none
    python3 otel-demo/eval.py --resume                skip runs already in the results file
    python3 otel-demo/eval.py --table                 print the table from the results file only

For each run: wait until the store has recovered (no alert, no error rate, no recent
errors in the target), break it, wait for Loki's report, score it, reset. One JSON line
per run goes to otel-demo/eval-results.jsonl; the accuracy table is printed at the end
and written to otel-demo/eval-results.md.

Deploy histories (break.py --deploy): fresh = the bad deploy went out a minute before;
none = no matching deploy, only innocent ones; old = the matching deploy is hours old.
In "none" and "old" the right answer names the service and blames no deploy.

A run takes about 10-20 minutes (mostly waiting for the store to recover), so run it in
tmux: `tmux new -s eval`, then reattach with `tmux attach -t eval`.
"""
import argparse
import json
import sys
import time
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib  # noqa: E402

brk = importlib.import_module("break")

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "eval-results.jsonl"
TABLE = HERE / "eval-results.md"
# Failures seen to raise an alert in the minimal demo. Others can be named with --failures.
DEFAULT_FAILURES = ["paymentFailure", "adHighCpu", "cartFailure", "paymentUnreachable",
                    "productCatalogFailure", "adFailure"]
RECOVER_TIMEOUT_S = 25 * 60
REPORT_TIMEOUT_S = 12 * 60
POLL_S = 30


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_recovered(service):
    deadline = time.monotonic() + RECOVER_TIMEOUT_S
    while True:
        reason = brk.not_recovered(service)
        if reason is None:
            return None
        if time.monotonic() > deadline:
            return reason
        log(f"waiting for recovery: {reason}")
        time.sleep(POLL_S)


def wait_report(truth):
    deadline = time.monotonic() + REPORT_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            state, inv = brk.find_report(truth)
        except (urllib.error.URLError, OSError) as e:
            log(f"Loki not reachable ({e}); retrying")
            state, inv = "waiting", None
        if state == "done":
            return inv
        time.sleep(POLL_S)
    return None


def one_run(flag, mode, run):
    service = brk.FAILURES[flag][0]
    base = {"flag": flag, "mode": mode, "run": run, "service": service}
    stuck = wait_recovered(service)
    if stuck:
        return {**base, "outcome": "store_not_recovered", "detail": stuck}
    truth = brk.do_break(flag, mode)
    log(f"broke {flag} ({mode}, run {run}); waiting for Loki's report")
    started = time.monotonic()
    try:
        inv = wait_report(truth)
    finally:
        brk.cmd_reset()
    out = {**base, "started_at": truth["started_at"], "truth_deploy": truth["deploy_id"],
           "matching_deploy": truth["matching_deploy"]}
    if inv is None:
        return {**out, "outcome": "no_report",
                "detail": f"no investigation finished within {REPORT_TIMEOUT_S // 60} min (no alert?)"}
    v = brk.judge(inv.get("report_text"), truth)
    took = None
    if inv.get("finished_at"):
        took = (datetime.fromisoformat(inv["finished_at"]) - datetime.fromisoformat(truth["started_at"])).total_seconds()
    return {**out, **v, "outcome": "scored", "investigation": inv["id"], "status": inv.get("status"),
            "cost_usd": inv.get("cost_usd"), "seconds_to_report": took or round(time.monotonic() - started)}


def table(rows):
    """Markdown accuracy table: per failure x deploy history, plus totals per history."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["flag"], r["mode"])].append(r)
        groups[("ALL", r["mode"])].append(r)
        groups[("ALL", "all")].append(r)

    def line(key, rs):
        scored = [r for r in rs if r["outcome"] == "scored"]
        n = len(scored)

        def pct(k):
            return f"{sum(r[k] for r in scored)}/{n} ({100 * sum(r[k] for r in scored) // n}%)" if n else "-"
        both = sum(r["right_service"] and r["right_deploy"] for r in scored)
        conf = [r["confidence"] for r in scored if r.get("confidence") is not None]
        cost = [float(r["cost_usd"]) for r in scored if r.get("cost_usd") is not None]
        secs = [r["seconds_to_report"] for r in scored if r.get("seconds_to_report")]
        missed = len(rs) - n
        return (f"| {key[0]} | {key[1]} | {len(rs)} | {pct('right_service')} | {pct('right_deploy')} | "
                f"{f'{both}/{n}' if n else '-'} | {f'{sum(conf) / len(conf):.0%}' if conf else '-'} | "
                f"{f'${sum(cost) / len(cost):.3f}' if cost else '-'} | "
                f"{f'{sum(secs) / len(secs) / 60:.1f} min' if secs else '-'} | {missed or ''} |")

    head = ["| Failure | Deploy history | Runs | Right service | Right deploy call | Both right | "
            "Avg confidence | Avg cost | Avg time to report | No report |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    keys = sorted(k for k in groups if k[0] != "ALL") + sorted(k for k in groups if k[0] == "ALL")
    return "\n".join(head + [line(k, groups[k]) for k in keys])


def load_rows():
    return [json.loads(x) for x in RESULTS.read_text().splitlines() if x.strip()] if RESULTS.exists() else []


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--failures", default=",".join(DEFAULT_FAILURES))
    ap.add_argument("--modes", default=",".join(brk.DEPLOY_MODES))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--resume", action="store_true", help="skip (failure, mode, run) already in the results")
    ap.add_argument("--table", action="store_true", help="only print the table from existing results")
    a = ap.parse_args()
    if not a.table:
        flags = [f.strip() for f in a.failures.split(",") if f.strip()]
        modes = [m.strip() for m in a.modes.split(",") if m.strip()]
        bad = [f for f in flags if f not in brk.FAILURES] + [m for m in modes if m not in brk.DEPLOY_MODES]
        if bad:
            sys.exit(f"Unknown failure or mode: {', '.join(bad)}")
        done = {(r["flag"], r["mode"], r["run"]) for r in load_rows()} if a.resume else set()
        plan = [(f, m, n) for n in range(1, a.runs + 1) for f in flags for m in modes if (f, m, n) not in done]
        log(f"{len(plan)} run(s) planned, roughly {len(plan) * 15 // 60}h{len(plan) * 15 % 60:02d}m")
        brk.cmd_reset()
        for i, (flag, mode, n) in enumerate(plan, 1):
            log(f"--- run {i}/{len(plan)}: {flag} / {mode} / #{n}")
            row = one_run(flag, mode, n)
            row["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with RESULTS.open("a") as f:
                f.write(json.dumps(row) + "\n")
            if row["outcome"] == "scored":
                log(f"blamed {row['blamed_service']} / deploy {row['blamed_deploy'] or 'none'}: "
                    f"service {'OK' if row['right_service'] else 'WRONG'}, "
                    f"deploy call {'OK' if row['right_deploy'] else 'WRONG'}")
            else:
                log(f"{row['outcome']}: {row['detail']}")
    rows = load_rows()
    if not rows:
        sys.exit("No results yet.")
    text = table(rows)
    TABLE.write_text(f"# Loki on the OTel Demo lab\n\nGenerated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC "
                     f"from {RESULTS.name} ({len(rows)} runs).\n\n{text}\n")
    print("\n" + text)
    print(f"\nWritten to {TABLE}")


if __name__ == "__main__":
    main()
