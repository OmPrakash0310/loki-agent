#!/usr/bin/env python3
"""Break the OTel Demo on purpose, and write down what really broke.

    python3 otel-demo/break.py list               the failures you can cause
    python3 otel-demo/break.py paymentFailure     turn one on
    python3 otel-demo/break.py reset              turn everything off
    python3 otel-demo/break.py score              compare Loki's last report with the truth

Turning a failure on does three things:
  1. flips the demo's feature flag (flagd reads the file and applies it within seconds)
  2. records a matching "bad deploy" in state/deploys.json, plus an innocent one on
     another service, so Loki has to pick the right one
  3. appends the true answer to runs.jsonl, so results can be scored later

Set DEMO_DIR if the demo isn't cloned at ~/opentelemetry-demo.
"""
import json
import os
import random
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO_DIR = Path(os.environ.get("DEMO_DIR", Path.home() / "opentelemetry-demo"))
FLAGS_FILE = DEMO_DIR / "src" / "flagd" / "demo.flagd.json"
DEPLOYS_FILE = HERE / "state" / "deploys.json"
RUNS_FILE = HERE / "runs.jsonl"
LOKI_URL = os.environ.get("LOKI_AGENT_URL", "http://localhost:8088")

# flag -> (service at fault, what the "bad deploy" changed, short diff)
FAILURES = {
    "paymentFailure": ("payment", "Retry card charges with new gateway client",
                       "-  const result = await gateway.charge(card, amount)\n+  const result = await newGateway.charge(card, amount)  // no fallback"),
    "paymentUnreachable": ("payment", "Move payment service to new port",
                           "-  PAYMENT_PORT: 50051\n+  PAYMENT_PORT: 50052"),
    "adHighCpu": ("ad", "Rank ads with full scoring pass",
                  "-  return ads.subList(0, 2);\n+  for (Ad a : ads) for (Ad b : ads) score(a, b);  // O(n^2) per request"),
    "adManualGc": ("ad", "Free memory after each ad request",
                   "+  System.gc();  // after every request"),
    "adFailure": ("ad", "Validate ad request context",
                  "+  if (random.nextInt(10) == 0) throw new StatusRuntimeException(Status.UNAVAILABLE);"),
    "cartFailure": ("cart", "Switch cart store connection settings",
                    "-  valkey: valkey-cart:6379\n+  valkey: valkey-cart-new:6379"),
    "productCatalogFailure": ("product-catalog", "Add product lookup validation",
                              "+  if id == \"OLJCESPC7Z\" { return nil, status.Errorf(codes.Internal, \"invalid product\") }"),
    "recommendationCacheFailure": ("recommendation", "Cache product list in memory",
                                   "+  cached_ids.extend(product_ids)  # never cleared"),
    "emailMemoryLeak": ("email", "Keep sent emails for audit",
                        "+  $sent_emails << email  # grows forever"),
    "imageSlowLoad": ("image-provider", "Serve images through new resize path",
                      "+  sleep(rand(1..5))  # resize on the fly"),
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


def cmd_break(name):
    if name not in FAILURES:
        sys.exit(f"Unknown failure {name!r}. Run: python3 otel-demo/break.py list")
    service, message, diff = FAILURES[name]
    bad = _deploy(service, message, diff, minutes_ago=1)
    innocent = _deploy(*random.choice([i for i in INNOCENT if i[0] != service]), minutes_ago=random.randint(3, 20))
    deploys = _load(DEPLOYS_FILE, [])
    deploys += [innocent, bad]
    _save(DEPLOYS_FILE, deploys)
    _set_flag(name, True)
    with RUNS_FILE.open("a") as f:
        f.write(json.dumps({"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "flag": name, "service": service, "deploy_id": bad["id"]}) + "\n")
    print(f"Broke {service} ({name}). Bad deploy {bad['id']}: \"{message}\".")
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
    try:
        with urllib.request.urlopen(f"{LOKI_URL}/investigations/latest/report", timeout=10) as r:
            report = r.read().decode()
    except Exception as e:
        sys.exit(f"Couldn't read Loki's report from {LOKI_URL}: {e}")
    text = report.lower()
    got_service = truth["service"].lower() in text
    got_deploy = truth["deploy_id"].lower() in text
    print(report)
    print("\n" + "-" * 60)
    print(f"Truth: {truth['flag']} broke {truth['service']}, bad deploy {truth['deploy_id']}")
    print(f"Named the right service: {'yes' if got_service else 'NO'}")
    print(f"Named the right deploy:  {'yes' if got_deploy else 'NO'}")
    print("(A quick text check. Read the report to judge whether the reasoning is right.)")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    arg = sys.argv[1]
    if arg == "list":
        for name, (service, message, _) in FAILURES.items():
            print(f"{name:28} breaks {service:16} \"{message}\"")
    elif arg == "reset":
        cmd_reset()
    elif arg == "score":
        cmd_score()
    else:
        cmd_break(arg)


if __name__ == "__main__":
    main()
