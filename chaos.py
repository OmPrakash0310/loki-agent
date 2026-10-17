"""Break payments-svc on purpose, and record the ground truth for evals.

Usage: python3 chaos.py bad_deploy | slow_db | timeout | none

bad_deploy also records a deploy in state/deploys.json, the way a real CI
system would, so Loki can find it in the deploy history. slow_db and timeout
leave no deploy: those failures come from the database and a dependency.
"""
import json
import secrets
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEPLOYS = Path(__file__).parent / "state" / "deploys.json"

BAD_DIFF = """--- a/payments-svc/charge.py
+++ b/payments-svc/charge.py
@@ -38,7 +38,10 @@ def charge(body: Charge):
     log.debug("charging order %s", body.order_id)
     payload = body.model_dump()
+    # Multi-currency: convert using the currency the shop sends
+    currency = payload["currency"]
+    amount = payload["amount"] * FX_RATES[currency]
-    amount = payload["amount"]
     with psycopg.connect(DB_URL) as conn:
         conn.execute(INSERT_PAYMENT, (body.order_id, amount))
"""


def record_deploy(version: str, message: str, diff: str) -> str:
    deploys = json.loads(DEPLOYS.read_text()) if DEPLOYS.exists() else []
    deploy_id = secrets.token_hex(4)[:7]
    deploys.append({
        "id": deploy_id,
        "service": "payments-svc",
        "version": version,
        "author": "ravi",
        "message": message,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "diff": diff,
    })
    DEPLOYS.write_text(json.dumps(deploys, indent=2) + "\n")
    return deploy_id

MODES = ["none", "bad_deploy", "slow_db", "timeout"]
mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode not in MODES:
    sys.exit(f"usage: python3 chaos.py {{{'|'.join(MODES)}}}")

req = urllib.request.Request(
    "http://localhost:8002/chaos",
    data=json.dumps({"mode": mode}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req, timeout=5).read()

if mode == "bad_deploy":
    deploy_id = record_deploy("v2.5.0", "Support multi-currency charges", BAD_DIFF)
    print(f"recorded deploy {deploy_id} (payments-svc v2.5.0)")
elif mode == "none":
    deploys = json.loads(DEPLOYS.read_text()) if DEPLOYS.exists() else []
    last = next((d for d in reversed(deploys) if d["service"] == "payments-svc"), None)
    if last and last["version"] == "v2.5.0":
        reverted = "\n".join(
            ("-" + l[1:]) if l.startswith("+") and not l.startswith("+++")
            else ("+" + l[1:]) if l.startswith("-") and not l.startswith("---")
            else l
            for l in BAD_DIFF.splitlines()
        )
        deploy_id = record_deploy("v2.5.1", f"Revert \"Support multi-currency charges\" ({last['id']})", reverted + "\n")
        print(f"recorded rollback deploy {deploy_id}")

# Ground truth lives only on your Mac; the agent never sees it.
with open(Path(__file__).parent / "ground_truth.jsonl", "a") as f:
    truth = {"at": datetime.now(timezone.utc).isoformat(), "mode": mode}
    if mode == "bad_deploy":
        truth["deploy_id"] = deploy_id  # the eval checks Loki names this exact deploy
    f.write(json.dumps(truth) + "\n")
print(f"payments-svc chaos mode -> {mode}")
