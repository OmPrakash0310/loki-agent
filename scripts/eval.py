"""Loki's eval: break the system in known ways, let Loki investigate, and grade
every report against the ground truth. Like an exam with an answer key.

Usage:
    python3 scripts/eval.py                      # 5 runs of each scenario
    python3 scripts/eval.py --runs 2 --scenarios slow_db timeout

Each run's grade is appended to evals/results/<started>.jsonl, and the summary
table is written to evals/latest.md.
"""
import argparse
import json
import statistics
import urllib.request
from datetime import datetime, timezone

import run_scenarios as rs

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--scenarios", nargs="+", default=list(rs.EXPECTED))
    parser.add_argument("--no-lessons", action="store_true", help="don't turn misses into pending lessons")
    args = parser.parse_args()

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
