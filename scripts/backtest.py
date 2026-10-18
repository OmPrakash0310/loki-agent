"""Backtest Loki: replay past incidents and show what Loki would have said at the time.

Loki's clock is set back to the incident, so nothing that happened afterwards
(later logs, metrics, deploys or confirmed answers) can reach the investigation.

Usage:
    python3 scripts/backtest.py --at 21:03                       # HH:MM UTC today, alerts recorded then
    python3 scripts/backtest.py --at 2026-10-16T21:03:00Z --alerts "HighErrorRate:payments-svc,HighErrorRate:shop-api"
    python3 scripts/backtest.py --at 21:03 --model fake          # free scripted model
    python3 scripts/backtest.py --incidents-file past.jsonl      # several incidents, graded when the file has answers

past.jsonl has one incident per line:
    {"name": "bad currency deploy", "at": "2026-10-16T21:03:00Z", "alerts": "HighErrorRate:payments-svc",
     "expected": {"service": "payments-svc", "cause": "deploy", "deploy": "552a2c7"}}
"expected" is optional. "cause" may list several accepted triggers ("dependency|infrastructure");
"deploy": "" means no deploy should be blamed.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AGENT = "http://localhost:8088"


# ---------- arguments ----------

def parse_at(text: str, today: datetime | None = None) -> datetime:
    """An ISO timestamp (no timezone means UTC) or 'HH:MM[:SS]' (UTC today)."""
    now = today or datetime.now(timezone.utc)
    s = text.strip().removesuffix("UTC").strip()
    try:
        if re.fullmatch(r"\d{1,2}:\d\d(:\d\d)?", s):
            fmt = "%H:%M:%S" if s.count(":") == 2 else "%H:%M"
            at = datetime.combine(now.date(), datetime.strptime(s, fmt).time(), timezone.utc)
        else:
            at = datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad time {text!r}: use HH:MM (UTC today) or an ISO timestamp")
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    if at > now:
        raise argparse.ArgumentTypeError(f"{at.isoformat()} is in the future: a backtest replays the past")
    return at.astimezone(timezone.utc)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Replay past incidents through Loki with the clock set back.")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--at", type=parse_at, help="when the first alert fired: ISO timestamp or HH:MM (UTC today)")
    what.add_argument("--incidents-file", type=Path, help="JSONL of past incidents (at, alerts, optional expected)")
    p.add_argument("--alerts", help='"AlertName:service,..."; omit to replay the alerts Loki recorded at that time')
    p.add_argument("--model", help="model for the replay, e.g. fake (free); default: the agent's LOKI_MODEL")
    p.add_argument("--delay", type=int, default=60, help="seconds after the alert that Loki starts (default 60, as live)")
    p.add_argument("--agent", default=AGENT, help=f"Loki's URL (default {AGENT})")
    args = p.parse_args(argv)
    if args.incidents_file and args.alerts:
        p.error("--alerts goes inside the incidents file in batch mode")
    return args


def load_cases(path: Path) -> list[dict]:
    cases = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        case = json.loads(line)
        if "at" not in case:
            raise ValueError(f"{path}:{n}: missing 'at'")
        case["at"] = parse_at(case["at"])
        case.setdefault("name", f"line {n}")
        cases.append(case)
    return cases


# ---------- grading (the idea from eval.py, driven by the answers in the file) ----------

def grade(report: dict, expected: dict) -> dict[str, bool]:
    """Only the facts the file gives are checked."""
    checks = {}
    service = (report.get("root_cause_service") or "").lower()
    blamed = (report.get("deploy_id") or "").strip()
    if expected.get("service"):
        want = expected["service"].lower()
        checks["right_service"] = want in service or (service and service in want)
    if expected.get("cause"):
        checks["right_cause_type"] = report.get("trigger") in expected["cause"].split("|")
    if "deploy" in expected:
        if expected["deploy"]:
            checks["right_deploy"] = blamed == expected["deploy"]
        else:
            checks["no_deploy_blamed"] = not blamed
    return checks


# ---------- running ----------

def post_backtest(agent: str, at: datetime, alerts, model, delay: int) -> dict:
    body = json.dumps({"at": at.isoformat(), "alerts": alerts, "model": model, "delay_s": delay}).encode()
    headers = {"Content-Type": "application/json"}
    if os.environ.get("LOKI_API_KEY"):  # needs a key with the write scope
        headers["Authorization"] = f"Bearer {os.environ['LOKI_API_KEY']}"
    req = urllib.request.Request(agent + "/backtest", data=body, headers=headers)
    try:
        # Synchronous on the agent: a real model can take a few minutes.
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise SystemExit(f"backtest failed ({e.code}): {detail}")


def print_one(inv: dict) -> None:
    print(f"Backtest #{inv['id']}: Loki's clock set to {inv['replay_at']}")
    for i in inv.get("incidents", []):
        print(f"  alert {i['started_at']} {i['alert_name']} on {i['service']}: {i['summary']}")
    print()
    print(inv.get("report_text") or f"status {inv['status']}: {inv.get('error') or ''}")


def summarize(rows: list[dict]) -> str:
    lines = ["| Incident | Replayed at | Loki said | Trigger | Steps | Cost | Grade |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        rep = r.get("report") or {}
        missed = [k for k, v in r["checks"].items() if not v]
        verdict = ("-" if not r["checks"] else "correct" if not missed else "missed " + ", ".join(missed))
        said = (rep.get("summary") or r.get("error") or r["status"])[:90].replace("|", "/")
        lines.append(f"| {r['name']} | {r['replay_at']} | {said} | {rep.get('trigger', '-')} | "
                     f"{r.get('steps', 0)} | ${float(r.get('cost_usd') or 0):.4f} | {verdict} |")
    graded = [r for r in rows if r["checks"]]
    if graded:
        ok = sum(all(r["checks"].values()) for r in graded)
        lines.append(f"\n{ok}/{len(graded)} graded incidents correct ({ok / len(graded):.0%}).")
    return "\n".join(lines)


def run_batch(args) -> None:
    rows = []
    for case in load_cases(args.incidents_file):
        inv = post_backtest(args.agent, case["at"], case.get("alerts"), args.model, args.delay)
        report = inv.get("report") if inv["status"] == "done" else None
        checks = grade(report, case["expected"]) if report and case.get("expected") else {}
        if case.get("expected") and not report:
            checks = {"finished": False}
        rows.append({"name": case["name"], "replay_at": inv["replay_at"], "status": inv["status"], "report": report,
                     "error": inv.get("error"), "steps": inv.get("steps"), "cost_usd": inv.get("cost_usd"),
                     "checks": checks})
        print(f"#{inv['id']} {case['name']}: {inv['status']}", flush=True)
    print("\n" + summarize(rows))


def main(argv=None):
    args = parse_args(argv)
    if args.incidents_file:
        run_batch(args)
    else:
        print_one(post_backtest(args.agent, args.at, args.alerts, args.model, args.delay))


if __name__ == "__main__":
    sys.exit(main())
