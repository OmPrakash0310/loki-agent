#!/usr/bin/env python3
"""Break the OTel Demo on purpose, and write down what really broke.

    python3 otel-demo/break.py list               the failures you can cause
    python3 otel-demo/break.py paymentFailure     turn one on
    python3 otel-demo/break.py paymentFailure --deploy none   ...with no matching deploy (or: old)
    python3 otel-demo/break.py reset              turn everything off
    python3 otel-demo/break.py score              compare Loki's last report with the truth

Turning a failure on does four things:
  1. checks the store has recovered from the last failure: no error rate above the alert,
     no alert firing or pending, no recent error logs from the target (add --force to skip)
  2. flips the demo's feature flag (flagd reads the file and applies it within seconds)
  3. starts a fresh deploy history: a few innocent deploys plus one matching "bad
     deploy", so Loki has to pick the right one and old runs can't mislead it
  4. appends the true answer to runs.jsonl, so results can be scored later

Set DEMO_DIR if the demo isn't cloned at ~/opentelemetry-demo.
"""
import json
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO_DIR = Path(os.environ.get("DEMO_DIR", Path.home() / "opentelemetry-demo"))
FLAGS_FILE = DEMO_DIR / "src" / "flagd" / "demo.flagd.json"
DEPLOYS_FILE = HERE / "state" / "deploys.json"
RUNS_FILE = HERE / "runs.jsonl"
LOKI_URL = os.environ.get("LOKI_AGENT_URL", "http://localhost:8088")
PROM_URL = os.environ.get("LAB_PROM_URL", "http://localhost:9091")
LOGS_URL = os.environ.get("LAB_LOGS_URL", "http://localhost:3100")
# Error lines from the service about to be broken, in this window before the break,
# mean the last failure isn't over: they would show up in Loki's briefing as if this
# incident started before its deploy.
QUIET_MINUTES = 5
# Same expression and threshold as the HighErrorRate alert.
ERROR_RATE = ('sum by (service_name) (rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[5m]))'
              ' / sum by (service_name) (rate(traces_span_metrics_calls_total[5m])) > 0.05')

# flag -> (service at fault, what the "bad deploy" changed, short diff).
# Diffs carry no comments that name the problem: Loki has to find it in the evidence.
FAILURES = {
    "paymentFailure": ("payment", "Retry card charges with new gateway client",
                       "-  const result = await gateway.charge(card, amount)\n+  const result = await newGateway.charge(card, amount)"),
    "paymentUnreachable": ("payment", "Move payment service to new port",
                           "-  PAYMENT_PORT: 50051\n+  PAYMENT_PORT: 50052"),
    "adHighCpu": ("ad", "Rank ads with full scoring pass",
                  "-  return ads.subList(0, 2);\n+  for (Ad a : ads) for (Ad b : ads) score(a, b);"),
    "adManualGc": ("ad", "Free memory after each ad request",
                   "+  System.gc();"),
    "adFailure": ("ad", "Validate ad request context",
                  "+  if (random.nextInt(10) == 0) throw new StatusRuntimeException(Status.UNAVAILABLE);"),
    "cartFailure": ("cart", "Switch cart store connection settings",
                    "-  valkey: valkey-cart:6379\n+  valkey: valkey-cart-new:6379"),
    "productCatalogFailure": ("product-catalog", "Add product lookup validation",
                              "+  if id == \"OLJCESPC7Z\" { return nil, status.Errorf(codes.Internal, \"invalid product\") }"),
    "recommendationCacheFailure": ("recommendation", "Cache product list in memory",
                                   "+  cached_ids.extend(product_ids)"),
    "emailMemoryLeak": ("email", "Keep sent emails for audit",
                        "+  $sent_emails << email"),
    "imageSlowLoad": ("image-provider", "Serve images through new resize path",
                      "+  img = resize(img, params)"),
    "kafkaQueueProblems": ("kafka", "Raise order event batch size",
                           "-  batch.size=16384\n+  batch.size=1048576"),
}
INNOCENT = [
    ("frontend", "Update footer links", "-  <a href=\"/about\">About</a>\n+  <a href=\"/about-us\">About us</a>"),
    ("currency", "Add currency code comments", "+  // ISO 4217 codes"),
    ("shipping", "Bump logging library", "-  tracing = \"0.1.39\"\n+  tracing = \"0.1.40\""),
    ("quote", "Rename internal variable", "-  $q = ...\n+  $quote = ..."),
]


def _load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _set_flag(name, on):
    flags = _load(FLAGS_FILE, None)
    if flags is None:
        sys.exit(f"Can't find {FLAGS_FILE}. Clone the demo there or set DEMO_DIR.")
    flag = flags["flags"].get(name)
    if flag is None:
        sys.exit(f"The demo has no flag {name!r}. Run: python3 otel-demo/break.py list")
    variants = flag["variants"]
    if on:
        # The strongest setting: "on", else the largest number (e.g. "100%").
        flag["defaultVariant"] = "on" if "on" in variants else max(
            (v for v in variants if v != "off"), key=lambda v: variants[v])
    else:
        flag["defaultVariant"] = "off"
    _save(FLAGS_FILE, flags)


def _deploy(service, message, diff, minutes_ago):
    sha = "%07x" % random.getrandbits(28)
    return {
        "id": sha, "service": service, "version": f"v{random.randint(1, 4)}.{random.randint(0, 20)}.{random.randint(0, 9)}",
        "author": random.choice(["arjun", "meera", "sam", "li", "priya"]), "message": message,
        "deployed_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"),
        "diff": f"--- a/{service}\n+++ b/{service}\n{diff}\n",
    }


def _unhealthy():
    """Services still above the alert's error rate, as {service: rate}."""
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": ERROR_RATE})
    with urllib.request.urlopen(url, timeout=10) as r:
        result = json.load(r)["data"]["result"]
    return {x["metric"].get("service_name", "?"): float(x["value"][1]) for x in result}


def _active_alerts():
    """Alerts still firing or pending (any rule: errors, latency, CPU), as ['HighLatency checkout', ...]."""
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": "ALERTS"})
    with urllib.request.urlopen(url, timeout=10) as r:
        result = json.load(r)["data"]["result"]
    return sorted({f"{x['metric'].get('alertname')} {x['metric'].get('service_name', '?')} "
                   f"({x['metric'].get('alertstate')})" for x in result})


def _recent_errors(service):
    """How many error-looking lines `service` logged in the last QUIET_MINUTES."""
    now = datetime.now(timezone.utc)
    q = urllib.parse.urlencode({
        "query": f'{{service_name="{service}"}} |~ "(?i)error|exception|fail"',
        "start": int((now - timedelta(minutes=QUIET_MINUTES)).timestamp() * 1e9),
        "end": int(now.timestamp() * 1e9), "limit": 1000})
    with urllib.request.urlopen(f"{LOGS_URL}/loki/api/v1/query_range?{q}", timeout=10) as r:
        result = json.load(r)["data"]["result"]
    return sum(len(s["values"]) for s in result)


DEPLOY_MODES = {
    # The matching "bad deploy" went out a minute before the failure: the classic case.
    "fresh": (1, 1),
    # No matching deploy at all, only innocent ones (one of them minutes old): Loki must
    # name the service and blame no deploy.
    "none": None,
    # The matching deploy went out hours ago, long before anything broke: blaming it is wrong.
    "old": (180, 300),
}


def not_recovered(service, force=False):
    """Why the store isn't ready for a new failure in `service`, or None when it is."""
    if force:
        return None
    try:
        sick = _unhealthy()
    except Exception as e:
        return f"Couldn't check the store's health at {PROM_URL}: {e}"
    if sick:
        rates = ", ".join(f"{svc} {rate:.0%}" for svc, rate in sorted(sick.items()))
        return f"The store hasn't recovered yet ({rates} errors)."
    try:
        alerts, errors = _active_alerts(), _recent_errors(service)
    except Exception as e:
        return f"Couldn't check alerts at {PROM_URL} or logs at {LOGS_URL}: {e}"
    if alerts:
        return "Alerts from the last failure are still active: " + "; ".join(alerts) + "."
    if errors:
        return (f"{service} logged {errors} error line(s) in the last {QUIET_MINUTES} minutes; "
                "Loki would read them as part of this incident.")
    return None


def do_break(name, deploy_mode="fresh"):
    """Turn failure `name` on with a fresh deploy history; record and return the truth."""
    service, message, diff = FAILURES[name]
    others = random.sample([i for i in INNOCENT if i[0] != service], 3)
    deploys = [_deploy(*others[0], minutes_ago=random.randint(90, 180)),
               _deploy(*others[1], minutes_ago=random.randint(30, 60)),
               _deploy(*others[2], minutes_ago=random.randint(3, 20))]
    age = DEPLOY_MODES[deploy_mode]
    bad = _deploy(service, message, diff, minutes_ago=random.randint(*age)) if age else None
    if bad:
        deploys.append(bad)
    deploys.sort(key=lambda d: d["deployed_at"])
    _save(DEPLOYS_FILE, deploys)
    _set_flag(name, True)
    truth = {"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "flag": name, "service": service, "deploy_mode": deploy_mode,
             # Only a fresh matching deploy is the right one to blame.
             "deploy_id": bad["id"] if bad and deploy_mode == "fresh" else None,
             "matching_deploy": bad["id"] if bad else None}
    with RUNS_FILE.open("a") as f:
        f.write(json.dumps(truth) + "\n")
    return truth


def find_report(truth):
    """("waiting", None), ("running", inv) or ("done", inv): the first live investigation
    opened after this break, not one left from an earlier run. inv has its report text."""
    started = datetime.fromisoformat(truth["started_at"])
    with urllib.request.urlopen(f"{LOKI_URL}/investigations", timeout=10) as r:
        invs = json.load(r)
    mine = [i for i in invs if i["mode"] == "live" and datetime.fromisoformat(i["created_at"]) >= started]
    if not mine:
        return "waiting", None
    inv = next(i for i in invs if i["id"] == min(i["id"] for i in mine))
    if inv["status"] in ("queued", "running"):
        return "running", inv
    with urllib.request.urlopen(f"{LOKI_URL}/investigations/{inv['id']}/report", timeout=10) as r:
        inv = {**inv, "report_text": r.read().decode()}
    return "done", inv


def judge(report, truth):
    """Read the report's verdict line: *Root cause* (payment, deploy 6fbf7a2): ..."""
    m = re.search(r"\*Root cause\* \(([^,)]+)(?:, deploy ([0-9a-f]+))?\)", report or "")
    service, deploy = (m.group(1).strip(), m.group(2)) if m else (None, None)
    conf = re.search(r"confidence (\d+)%", report or "")
    return {"blamed_service": service, "blamed_deploy": deploy,
            "right_service": service == truth["service"],
            "right_deploy": deploy == truth["deploy_id"],
            "confidence": int(conf.group(1)) / 100 if conf else None}


def cmd_break(name, force=False, deploy_mode="fresh"):
    if name not in FAILURES:
        sys.exit(f"Unknown failure {name!r}. Run: python3 otel-demo/break.py list")
    if deploy_mode not in DEPLOY_MODES:
        sys.exit(f"Unknown deploy mode {deploy_mode!r}; use one of {', '.join(DEPLOY_MODES)}")
    reason = not_recovered(FAILURES[name][0], force)
    if reason:
        sys.exit(f"{reason} Run reset if you haven't, wait a few minutes and try again, or add --force.")
    truth = do_break(name, deploy_mode)
    bad = truth["matching_deploy"]
    print(f"Broke {truth['service']} ({name}). Deploy history: {deploy_mode}"
          + (f", matching deploy {bad}" if bad else ", no matching deploy") + ".")
    print("Wait about 2-3 minutes for the alert, then: python3 otel-demo/break.py score")


def cmd_reset():
    flags = _load(FLAGS_FILE, None)
    if flags is None:
        sys.exit(f"Can't find {FLAGS_FILE}.")
    for flag in flags["flags"].values():
        if "off" in flag["variants"]:
            flag["defaultVariant"] = "off"
    _save(FLAGS_FILE, flags)
    print("All failure flags are off. Give the store a few minutes to recover before the next one.")


def cmd_score():
    runs = [json.loads(line) for line in RUNS_FILE.read_text().splitlines()] if RUNS_FILE.exists() else []
    if not runs:
        sys.exit("Nothing broken yet.")
    truth = runs[-1]
    truth.setdefault("deploy_mode", "fresh")
    try:
        state, inv = find_report(truth)
    except urllib.error.URLError as e:
        sys.exit(f"Couldn't read Loki's report from {LOKI_URL}: {e}")
    if state == "waiting":
        sys.exit("Loki hasn't opened an investigation since this break yet. Try again in a minute.")
    if state == "running":
        sys.exit(f"Investigation #{inv['id']} is running. Try again in a minute.")
    v = judge(inv["report_text"], truth)
    print(inv["report_text"])
    print("\n" + "-" * 60)
    want = f"bad deploy {truth['deploy_id']}" if truth["deploy_id"] else "no deploy to blame"
    print(f"Truth: {truth['flag']} broke {truth['service']}, {want} (deploy history: {truth['deploy_mode']})")
    print(f"Loki blamed: {v['blamed_service'] or '(no root cause line)'}, deploy {v['blamed_deploy'] or '(none)'}")
    print(f"Right service: {'yes' if v['right_service'] else 'NO'}")
    print(f"Right deploy:  {'yes' if v['right_deploy'] else 'NO'}")
    print("(Read the report too: a right answer can still rest on wrong reasoning.)")


def main():
    argv = sys.argv[1:]
    mode = "fresh"
    if "--deploy" in argv:
        i = argv.index("--deploy")
        mode = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if a != "--force"]
    if len(args) != 1:
        sys.exit(__doc__)
    arg = args[0]
    if arg == "list":
        for name, (service, message, _) in FAILURES.items():
            print(f"{name:28} breaks {service:16} \"{message}\"")
    elif arg == "reset":
        cmd_reset()
    elif arg == "score":
        cmd_score()
    else:
        cmd_break(arg, force="--force" in argv, deploy_mode=mode)


if __name__ == "__main__":
    main()
