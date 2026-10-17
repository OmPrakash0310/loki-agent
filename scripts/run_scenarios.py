"""Break the demo system in each way, let Loki investigate, and print what it found.

Usage: python3 scripts/run_scenarios.py [bad_deploy slow_db timeout]
This is the start of Loki's eval harness: each scenario has a known cause.
"""
import json
import subprocess
import sys
import time
import urllib.request

AGENT = "http://localhost:8088"
EXPECTED = {
    "bad_deploy": "deploy to payments-svc (rollback)",
    "slow_db": "slow database behind payments-svc (no deploy)",
    "timeout": "payments-svc hanging on a dependency (no deploy)",
}


def get(path):
    with urllib.request.urlopen(AGENT + path, timeout=10) as r:
        body = r.read().decode()
    return json.loads(body) if body.startswith(("[", "{")) else body


def chaos(mode):
    subprocess.run([sys.executable, "chaos.py", mode], check=True, capture_output=True)


def wait_until_quiet(timeout=300):
    """No open incidents and nothing being investigated."""
    start = time.time()
    while time.time() - start < timeout:
        open_incidents = [i for i in get("/incidents") if i["status"] == "open"]
        busy = [i for i in get("/investigations") if i["status"] in ("queued", "running")]
        if not open_incidents and not busy:
            return
        time.sleep(10)
    raise TimeoutError("system didn't settle")


def run(mode):
    wait_until_quiet()
    last = max([i["id"] for i in get("/investigations")] or [0])
    print(f"\n=== {mode}  (expected: {EXPECTED[mode]})", flush=True)
    chaos(mode)
    started = time.time()
    try:
        while True:
            new = [i for i in get("/investigations") if i["id"] > last]
            if new and new[-1]["status"] in ("done", "failed", "skipped"):
                inv = new[-1]
                break
            if time.time() - started > 360:
                print("no investigation finished within 6 minutes")
                return
            time.sleep(5)
    finally:
        chaos("none")
    print(f"investigation #{inv['id']}: {inv['status']}, {inv['steps']} steps, ${inv['cost_usd']}, "
          f"{time.time() - started:.0f}s from break to report")
    print(get(f"/investigations/{inv['id']}/report"))
    print("tools used:", ", ".join(s["tool"] for s in get(f"/investigations/{inv['id']}/trace") if s["tool"] not in ("llm", "briefing")))


if __name__ == "__main__":
    for mode in sys.argv[1:] or ["bad_deploy", "slow_db", "timeout"]:
        run(mode)
    stats = get("/stats")
    print(f"\nTotal spent today: ${stats['spent_today_usd']}  ·  per model: {json.dumps(stats['per_model'], default=str)}")
