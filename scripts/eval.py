"""Loki's eval: break the system in known ways, let Loki investigate, and grade
every report against the ground truth. Like an exam with an answer key.

Usage:
    python3 scripts/eval.py                      # 5 runs of each original scenario
    python3 scripts/eval.py --runs 2 --scenarios slow_db timeout
    python3 scripts/eval.py --set train --runs 1           # scenario files (evals/scenarios/train)
    python3 scripts/eval.py --set heldout --difficulty hard
    python3 scripts/eval.py --files evals/scenarios/train/s05-slow-db-a.json
    python3 scripts/eval.py --set all --dry-run            # validate and list, touch nothing

Scenario files are made by scripts/chaos_engineer.py and graded by the generic grader in
scripts/scenario_spec.py; the original three modes keep their original graders.

Each run's grade is appended to evals/results/<started>.jsonl, and the summary
table is written to evals/latest.md.
"""
import argparse
import json
import statistics
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import chaos_engineer
import run_scenarios as rs
import scenario_runner
import scenario_spec

GROUND_TRUTH = rs.ROOT / "ground_truth.jsonl"
RESULTS_DIR = rs.ROOT / "evals" / "results"


def latest_truth(mode: str) -> dict:
    lines = [json.loads(l) for l in GROUND_TRUTH.read_text().splitlines() if l.strip()]
    return next(t for t in reversed(lines) if t["mode"] == mode)


def grade(mode: str, report: dict, truth: dict) -> dict[str, bool]:
    """Each check is one thing a correct report must get right."""
    text = f"{report.get('summary', '')} {report.get('root_cause', '')}".lower()
    service = report.get("root_cause_service", "").lower()
    trigger = report.get("trigger")
    blamed_deploy = report.get("deploy_id", "").strip()

    if mode == "bad_deploy":
        return {
            "right_service": "payments" in service,
            "right_cause_type": trigger == "deploy",
            "right_deploy": blamed_deploy == truth.get("deploy_id"),
            "right_action": report.get("suggested_action") == "rollback",
        }
    if mode == "slow_db":
        return {
            "right_service": "postgres" in service or ("payments" in service and "postgres" in text),
            "right_cause_type": trigger in ("infrastructure", "dependency"),
            "no_deploy_blamed": not blamed_deploy,
            "names_database": "postgres" in text or "database" in text,
        }
    if mode == "timeout":
        return {
            "right_service": "card" in service or ("payments" in service and "card" in text),
            "right_cause_type": trigger in ("dependency", "infrastructure"),
            "no_deploy_blamed": not blamed_deploy,
            "names_card_network": "card" in text,
        }
    raise ValueError(f"no grader for {mode}")


def report_miss(mode: str, inv_id: int, checks: dict, truth: dict) -> dict | None:
    """Send a run graded wrong to Loki, which turns it into a pending lesson (see agent/lessons.py).
    The ground truth is used to phrase the lesson and is never stored. Errors are printed, not raised."""
    body = json.dumps({"investigation_id": inv_id, "scenario": mode, "checks": checks, "truth": truth}).encode()
    req = urllib.request.Request(rs.AGENT + "/lessons/eval-miss", data=body, method="POST",
                                 headers={"Content-Type": "application/json", **rs.auth_headers()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # a lesson is a bonus; never fail the eval over it
        print(f"   (lesson not recorded: {e})", flush=True)
        return None


def break_and_investigate_spec(spec: dict):
    """Play a scenario file, wait for Loki's investigation, put things back.
    Returns (investigation or None, seconds, deploy ids by ref)."""
    rs.wait_until_quiet()
    last = max([i["id"] for i in rs.live_investigations()] or [0])
    run = scenario_runner.ScenarioRun(spec, log=lambda *_: None).start()
    started = time.time()
    limit = 360 + max([s["at_s"] for s in spec["steps"]] or [0])
    try:
        while time.time() - started < limit:
            new = [i for i in rs.live_investigations() if i["id"] > last]
            if new and new[-1]["status"] in ("done", "failed", "skipped"):
                return new[-1], time.time() - started, dict(run.deploy_ids)
            time.sleep(5)
        return None, time.time() - started, dict(run.deploy_ids)
    finally:
        run.stop()


def select_specs(args) -> list[dict]:
    specs = []
    if args.set:
        specs += chaos_engineer.load_all(split=args.set)
    for f in args.files or []:
        specs.append(json.loads(Path(f).read_text()))
    if args.difficulty:
        specs = [s for s in specs if s["difficulty"] in args.difficulty]
    if args.fault_type:
        specs = [s for s in specs if set(args.fault_type) & set(s["fault_types"])]
    bad = {s.get("id"): scenario_spec.validate(s) for s in specs}
    bad = {k: v for k, v in bad.items() if v}
    if bad:
        raise SystemExit("invalid scenarios: " + json.dumps(bad, indent=2))
    return specs


def spec_row(spec: dict, n: int, model: str, inv, seconds: float, deploy_ids: dict, full: dict | None) -> dict:
    row = {"scenario": spec["id"], "run": n, "model": model, "seconds": round(seconds),
           "difficulty": spec["difficulty"], "split": spec["split"], "fault_types": spec["fault_types"],
           "deploy_ids": deploy_ids}
    if inv is None or inv["status"] != "done" or full is None:
        row.update(correct=False, checks={}, score=0.0, fooled=False,
                   error=(inv or {}).get("error") or "no report in time")
        return row
    g = scenario_spec.grade(full["report"], spec, deploy_ids)
    row.update(investigation=inv["id"], correct=g["correct"], checks=g["checks"], score=g["score"],
               fooled=g["fooled"], cost_usd=float(full["cost_usd"]), steps=full["steps"],
               cache=full.get("cache_hit"), answer=full["report"]["summary"][:160])
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-lessons", action="store_true", help="don't turn misses into pending lessons")
    parser.add_argument("--runs", type=int, default=None, help="runs per scenario (default 5, or 1 for scenario files)")
    parser.add_argument("--scenarios", nargs="+", default=None, help="original modes: bad_deploy slow_db timeout")
    parser.add_argument("--set", choices=["train", "heldout", "all"], help="run scenario files from evals/scenarios")
    parser.add_argument("--files", nargs="+", help="run these scenario files")
    parser.add_argument("--difficulty", nargs="+", choices=scenario_spec.DIFFICULTIES)
    parser.add_argument("--fault-type", nargs="+", choices=sorted(scenario_spec.FAULT_TYPES))
    parser.add_argument("--dry-run", action="store_true", help="validate and list scenario files, run nothing")
    args = parser.parse_args()
    if args.set or args.files:
        return main_specs(args)
    args.runs = args.runs or 5
    args.scenarios = args.scenarios or list(rs.EXPECTED)

    started = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_file = RESULTS_DIR / f"{started}.jsonl"
    model = rs.get("/stats")["model"]
    print(f"Eval {started}: {args.runs} run(s) x {args.scenarios} on {model}\n", flush=True)

    rows = []
    for mode in args.scenarios:
        for n in range(1, args.runs + 1):
            inv, seconds = rs.break_and_investigate(mode)
            row = {"scenario": mode, "run": n, "model": model, "seconds": round(seconds)}
            if inv is None or inv["status"] != "done":
                row.update(correct=False, checks={}, error=(inv or {}).get("error") or "no report within 6 minutes")
            else:
                full = rs.get(f"/investigations/{inv['id']}")
                truth = latest_truth(mode)
                checks = grade(mode, full["report"], truth)
                row.update(
                    investigation=inv["id"], correct=all(checks.values()), checks=checks,
                    cost_usd=float(full["cost_usd"]), steps=full["steps"], cache=full.get("cache_hit"),
                    answer=full["report"]["summary"][:160],
                )
            if not row["correct"] and row.get("investigation") and not args.no_lessons:
                lesson = report_miss(mode, row["investigation"], checks, truth)
                if lesson and "id" in lesson:
                    row["lesson"] = lesson["id"]
            rows.append(row)
            with results_file.open("a") as f:
                f.write(json.dumps(row) + "\n")
            failed = [k for k, v in row["checks"].items() if not v]
            print(f"{'✅' if row['correct'] else '❌'} {mode} #{n}: {row['seconds']}s, "
                  f"${row.get('cost_usd', 0):.4f}" + (f"  missed: {', '.join(failed)}" if failed else "")
                  + (f"  error: {row['error']}" if row.get("error") else ""), flush=True)

    summary = summarize(rows, model, started)
    print("\n" + summary)
    (rs.ROOT / "evals" / "latest.md").write_text(summary + "\n")


def main_specs(args):
    specs = select_specs(args)
    runs = args.runs or 1
    if args.dry_run:
        for s in specs:
            print(f"{s['id']:<34} {s['split']:<8} {s['difficulty']:<7} {','.join(s['fault_types'])}")
        print(f"{len(specs)} valid scenario(s)")
        return
    started = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_file = RESULTS_DIR / f"{started}.jsonl"
    model = rs.get("/stats")["model"]
    print(f"Eval {started}: {runs} run(s) x {len(specs)} scenario file(s) on {model}\n", flush=True)
    rows = []
    for spec in specs:
        for n in range(1, runs + 1):
            inv, seconds, deploy_ids = break_and_investigate_spec(spec)
            full = rs.get(f"/investigations/{inv['id']}") if inv and inv["status"] == "done" else None
            row = spec_row(spec, n, model, inv, seconds, deploy_ids, full)
            rows.append(row)
            with results_file.open("a") as f:
                f.write(json.dumps(row) + "\n")
            failed = [k for k, v in row["checks"].items() if not v]
            print(f"{'✅' if row['correct'] else '❌'} {spec['id']} #{n}: {row['seconds']}s, "
                  f"${row.get('cost_usd', 0):.4f}, score {row['score']:.2f}"
                  + (f"  missed: {', '.join(failed)}" if failed else "")
                  + ("  FOOLED by trap" if row["fooled"] else "")
                  + (f"  error: {row['error']}" if row.get("error") else ""), flush=True)
    summary = summarize(rows, model, started)
    print("\n" + summary)
    (rs.ROOT / "evals" / "latest.md").write_text(summary + "\n")


def _bucket_table(title: str, rows: list[dict], keys_of) -> list[str]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        for k in keys_of(r):
            groups.setdefault(k, []).append(r)
    lines = ["", f"## {title}", "", "| Group | Runs | Correct | Accuracy | Avg score | Fooled | Avg time | Avg cost |",
             "|---|---|---|---|---|---|---|---|"]
    order = {d: n for n, d in enumerate(scenario_spec.DIFFICULTIES)}
    for k in sorted(groups, key=lambda k: (order.get(k, 99), k)):
        g = groups[k]
        ok = sum(r["correct"] for r in g)
        costs = [r["cost_usd"] for r in g if "cost_usd" in r]
        lines.append(f"| {k} | {len(g)} | {ok} | {ok / len(g):.0%} | {statistics.mean(r.get('score', 0) for r in g):.2f} | "
                     f"{sum(r.get('fooled', False) for r in g)} | {statistics.mean(r['seconds'] for r in g):.0f}s | "
                     f"${statistics.mean(costs) if costs else 0:.4f} |")
    return lines


def summarize(rows, model, started) -> str:
    lines = [f"# Loki eval {started} ({model})", "",
             "| Scenario | Runs | Correct | Accuracy | Avg time | Avg cost | Avg steps |",
             "|---|---|---|---|---|---|---|"]
    for mode in dict.fromkeys(r["scenario"] for r in rows):
        rs_ = [r for r in rows if r["scenario"] == mode]
        ok = sum(r["correct"] for r in rs_)
        done = [r for r in rs_ if "cost_usd" in r]
        lines.append(
            f"| {mode} | {len(rs_)} | {ok} | {ok / len(rs_):.0%} | "
            f"{statistics.mean(r['seconds'] for r in rs_):.0f}s | "
            f"${statistics.mean(r['cost_usd'] for r in done) if done else 0:.4f} | "
            f"{statistics.mean(r['steps'] for r in done) if done else 0:.1f} |"
        )
    ok = sum(r["correct"] for r in rows)
    total_cost = sum(r.get("cost_usd", 0) for r in rows)
    lines.append(f"| **Total** | {len(rows)} | {ok} | **{ok / len(rows):.0%}** | | ${total_cost:.4f} total | |")
    spec_rows = [r for r in rows if "difficulty" in r]
    if spec_rows:
        lines += _bucket_table("By difficulty", spec_rows, lambda r: [r["difficulty"]])
        lines += _bucket_table("By fault type", spec_rows, lambda r: r["fault_types"])
        lines += _bucket_table("By split", spec_rows, lambda r: [r["split"]])
        lines += ["", f"Total time {sum(r['seconds'] for r in rows)}s, total cost ${total_cost:.4f}, "
                      f"avg score {statistics.mean(r.get('score', 0) for r in spec_rows):.2f}."]
    misses = [r for r in rows if not r["correct"]]
    if misses:
        lines += ["", "## Misses"]
        for r in misses:
            failed = [k for k, v in r["checks"].items() if not v]
            lines.append(f"- {r['scenario']} run {r['run']} (#{r.get('investigation', '-')}): "
                         f"missed {', '.join(failed) or r.get('error')}. Loki said: {r.get('answer', '-')}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
