"""The chaos engineer: writes Loki's eval scenarios.

Deterministic by default. Templates (one per kind of incident) are filled in with
seeded random choices (fault parameters, timings, versions, authors, red herrings),
so the same seed always gives byte-identical files:

    python3 scripts/chaos_engineer.py                    # write evals/scenarios/{train,heldout}/
    python3 scripts/chaos_engineer.py --seed 7 --variants 4 --out /tmp/scen
    python3 scripts/chaos_engineer.py --check            # exit 1 if the committed files are stale

Every template makes `--variants` scenarios. The last variant of each template goes to the
held-out set, so every kind of incident is in both sets and held-out scenarios are never
copies of training ones (different parameters, timings and red herrings).

Optional: --llm asks Claude to propose extra, novel combinations. It needs
LOKI_CHAOS_LLM=1 and ANTHROPIC_API_KEY, costs money, and is never used by tests. Every
proposal must pass the same validator as the templates; invalid ones are dropped.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario_spec as ss  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evals" / "scenarios"
AUTHORS = ["ravi", "meera", "arjun", "lena", "tomas", "aisha", "kenji"]
PRODUCTS = {1: "Coffee", 2: "Headphones", 3: "Notebook"}
CARD = ["card-network", {"service": "payments-svc", "if_text_any": ["card"]}]
POSTGRES = ["postgres", {"service": "payments-svc", "if_text_any": ["postgres", "database", " db"]}]


# ---------- building blocks ----------

def _version(rng, svc):
    major = 2 if svc == "payments-svc" else 1
    return f"v{major}.{rng.randint(6, 19)}.{rng.randint(0, 4)}"


def deploy(rng, ref, svc, message, diff, culprit, kind="code"):
    return {"ref": ref, "service": svc, "version": _version(rng, svc), "author": rng.choice(AUTHORS),
            "message": message, "kind": kind, "diff": diff, "culprit": culprit}


def fault(at, svc, name, **params):
    return {"at_s": at, "action": "fault", "service": svc, "fault": name, "params": params}


def truth(service, cause, triggers, acceptable, deploy_ref=None, keywords=None, actions=None,
          trap="", trap_services=None, trap_deploy_refs=None, alternatives=None):
    t = {"root_cause_service": service, "cause_type": cause, "triggers": triggers, "deploy_ref": deploy_ref,
         "acceptable_services": acceptable, "trap": trap}
    if keywords:
        t["keywords_any"] = keywords
    if actions:
        t["actions_any"] = actions
    if trap_services:
        t["trap_services"] = trap_services
    if trap_deploy_refs:
        t["trap_deploy_refs"] = trap_deploy_refs
    if alternatives:
        t["alternatives"] = alternatives
    return t


def steady(rng):
    return {"shape": "steady", "extra_rps": 0, "duration_s": 0}


# Innocent changes: realistic, noisy, and unrelated to the fault.
INNOCENT = [
    ("payments-svc", "Bump structlog and tidy log formatting",
     "--- a/payments-svc/requirements.txt\n+++ b/payments-svc/requirements.txt\n@@ -3,4 +3,4 @@\n"
     " psycopg[binary]==3.2.3\n-structlog==24.1.0\n+structlog==24.4.0\n prometheus-client==0.21.0\n"),
    ("payments-svc", "Add payment_method to the receipts email template",
     "--- a/payments-svc/templates/receipt.txt\n+++ b/payments-svc/templates/receipt.txt\n@@ -4,3 +4,4 @@\n"
     " Order: {{ order_id }}\n Amount: {{ amount }}\n+Paid with: {{ payment_method | default('card') }}\n"),
    ("shop-api", "Update product descriptions copy",
     "--- a/shop-api/catalog/copy.yml\n+++ b/shop-api/catalog/copy.yml\n@@ -1,4 +1,4 @@\n coffee:\n"
     "-  tagline: Fresh roasted\n+  tagline: Freshly roasted, single origin\n headphones:\n"),
    ("shop-api", "Log request ids on every line (observability)",
     "--- a/shop-api/main.py\n+++ b/shop-api/main.py\n@@ -12,7 +12,7 @@\n-logging.basicConfig(level=logging.INFO, "
     "format=\"%(asctime)s %(levelname)s shop-api %(message)s\")\n+logging.basicConfig(level=logging.INFO, "
     "format=\"%(asctime)s %(levelname)s shop-api [%(request_id)s] %(message)s\")\n"),
    ("shop-api", "Raise analytics batch size to 500",
     "--- a/shop-api/config/analytics.env\n+++ b/shop-api/config/analytics.env\n@@ -1,3 +1,3 @@\n"
     " ANALYTICS_URL=http://analytics-collector:4317\n-ANALYTICS_BATCH=100\n+ANALYTICS_BATCH=500\n"),
]


def innocent(rng, ref, svc=None):
    pool = [i for i in INNOCENT if svc is None or i[0] == svc]
    service, message, diff = rng.choice(pool)
    return deploy(rng, ref, service, message, diff, culprit=False, kind="config" if ".env" in diff else "code")


# ---------- templates: each returns (difficulty, fault_types, description, deploys, steps, load, truth) ----------

def t_payments_bad_deploy(rng, v):
    msg = rng.choice(["Support multi-currency charges", "Charge in the customer's currency",
                      "Multi-currency: convert with FX rates"])
    diff = ("--- a/payments-svc/charge.py\n+++ b/payments-svc/charge.py\n@@ -38,7 +38,10 @@ def charge(body: Charge):\n"
            "     payload = body.model_dump()\n+    currency = payload[\"currency\"]\n"
            "+    amount = payload[\"amount\"] * FX_RATES[currency]\n-    amount = payload[\"amount\"]\n")
    deploys = [deploy(rng, "d1", "payments-svc", msg, diff, True)]
    steps = [{"at_s": 0, "action": "deploy", "ref": "d1"}, {"at_s": 0, "action": "mode", "mode": "bad_deploy"}]
    types = ["bad_deploy"]
    trap = ""
    trap_refs = []
    if v % 2:  # a harmless shop-api deploy a minute earlier
        deploys.insert(0, innocent(rng, "d0", "shop-api"))
        steps.insert(0, {"at_s": 0, "action": "deploy", "ref": "d0"})
        for s in steps[1:]:
            s["at_s"] = rng.choice([40, 60, 75])
        trap, trap_refs = "a harmless shop-api deploy shipped shortly before", ["d0"]
        types.append("timing_trap")
    elif v:  # unrelated shop-api error noise running since before the deploy
        steps.insert(0, fault(0, "shop-api", "log_flood", lines_per_request=rng.choice([3, 8, 15])))
        trap = "shop-api is logging unrelated analytics / recs-cache errors"
        types.append("log_noise")
    return ("easy" if not v else "medium", types,
            f"payments-svc deploy '{msg}' reads a currency field the shop never sends: every charge 500s.",
            deploys, steps, steady(rng),
            truth("payments-svc", "bad_deploy", ["deploy"], ["payments-svc"], "d1", ["currency", "deploy"],
                  ["rollback"], trap, ["shop-api"] if "log_noise" in types else None, trap_refs))


def t_partial_deploy(rng, v):
    min_amount = rng.choice([500, 1000, 2000, 2500])
    msg = rng.choice(["Add risk scoring for large charges", "Fraud: score charges before authorizing"])
    diff = ("--- a/payments-svc/charge.py\n+++ b/payments-svc/charge.py\n@@ -44,6 +44,10 @@ def charge(body: Charge):\n"
            f"+    if amount >= {min_amount}:\n+        score = amount / 1000\n+        if score > 1:\n"
            "+            raise ValueError(f\"risk score out of range: {score:.3f}\")\n")
    return ("medium", ["partial_deploy_regression", "partial_failure"],
            f"payments-svc '{msg}' fails only charges >= {min_amount} (the Headphones): a fraction of orders fail.",
            [deploy(rng, "d1", "payments-svc", msg, diff, True)],
            [{"at_s": 0, "action": "deploy", "ref": "d1"},
             fault(rng.choice([0, 10, 20]), "payments-svc", "risk_regression", min_amount=min_amount)],
            steady(rng),
            truth("payments-svc", "partial_deploy_regression", ["deploy"], ["payments-svc"], "d1",
                  ["risk", "score", "large", "amount"], ["rollback", "fix_forward"],
                  "only some orders fail, so the error rate is modest and looks like noise"))


def t_shop_bad_deploy(rng, v):
    msg = rng.choice(["Send payment amounts in minor units", "Payments: use integer paise amounts"])
    diff = ("--- a/shop-api/main.py\n+++ b/shop-api/main.py\n@@ -52,7 +52,7 @@ def create_order():\n"
            "-            json={\"order_id\": order_id, \"amount\": product[\"price\"]},\n"
            "+            json={\"order_id\": order_id, \"amount_minor\": int(product[\"price\"] * 100)},\n")
    deploys = [deploy(rng, "d1", "shop-api", msg, diff, True)]
    steps = [{"at_s": 0, "action": "deploy", "ref": "d1"}, fault(rng.choice([0, 5, 15]), "shop-api", "amount_field")]
    if v % 2:  # payments also shipped something harmless: tempting
        deploys.insert(0, innocent(rng, "d0", "payments-svc"))
        steps.insert(0, {"at_s": 0, "action": "deploy", "ref": "d0"})
    return ("hard", ["bad_deploy", "upstream_blame_trap"],
            f"shop-api '{msg}' sends a field payments rejects (422). shop-api's errors all name payments-svc.",
            deploys, steps, steady(rng),
            truth("shop-api", "bad_deploy", ["deploy"], ["shop-api"], "d1", ["amount", "422", "field"],
                  ["rollback"], "every error line says 'payments-svc returned 422', pointing at the victim",
                  ["payments-svc"], ["d0"] if v % 2 else None))


def t_config_error(rng, v):
    timeout = rng.choice([0.005, 0.01, 0.02, 0.05])
    diff = ("--- a/shop-api/config/production.env\n+++ b/shop-api/config/production.env\n@@ -2,4 +2,4 @@\n"
            " PAYMENTS_URL=http://payments-svc:8000\n-PAYMENTS_TIMEOUT_S=2.0\n"
            f"+PAYMENTS_TIMEOUT_S={timeout}\n LOG_LEVEL=info\n")
    msg = rng.choice(["Tune payments client timeout", "config: align timeouts with SLO doc"])
    return ("hard", ["config_error", "upstream_blame_trap"],
            f"Config deploy sets shop-api's payments timeout to {timeout}s (meant ms vs s): orders time out.",
            [deploy(rng, "d1", "shop-api", msg, diff, True, kind="config")],
            [{"at_s": 0, "action": "deploy", "ref": "d1"},
             fault(rng.choice([0, 5]), "shop-api", "payments_timeout_config", timeout_s=timeout)],
            steady(rng),
            truth("shop-api", "config_error", ["deploy"], ["shop-api"], "d1", ["timeout", "config"],
                  ["rollback", "fix_forward"], "errors read 'timeout calling payments-svc' though payments is healthy",
                  ["payments-svc", "card-network"]))


def t_slow_db(rng, v):
    secs = rng.choice([1.2, 1.5, 1.8])
    rate = 1.0 if v % 2 == 0 else rng.choice([0.5, 0.7])
    return ("easy" if rate == 1.0 else "medium", ["slow_db"] + (["partial_failure"] if rate < 1 else []),
            f"Postgres answers payments-svc slowly ({secs}s on {rate:.0%} of queries); no deploy.",
            [], [fault(0, "payments-svc", "slow_db", seconds=secs, rate=rate)], steady(rng),
            truth("postgres", "slow_db", ["infrastructure", "dependency"], POSTGRES, None,
                  ["postgres", "database", "db"]))


def t_timing_trap(rng, v):
    secs = rng.choice([1.5, 2.0, 3.0])
    at = rng.choice([45, 60, 90])
    d0 = innocent(rng, "d0", "payments-svc")
    return ("hard", ["slow_db", "timing_trap"],
            f"A harmless payments-svc deploy ('{d0['message']}'), then {at}s later Postgres slows down ({secs}s).",
            [d0], [{"at_s": 0, "action": "deploy", "ref": "d0"},
                   fault(at, "payments-svc", "slow_db", seconds=secs)], steady(rng),
            truth("postgres", "slow_db", ["infrastructure", "dependency"], POSTGRES, None,
                  ["postgres", "database", "db"], None,
                  "a deploy to the failing service just before the slowdown; the ranking rules score it highest",
                  None, ["d0"]))


def t_card_errors(rng, v):
    rate = rng.choice([0.3, 0.5, 1.0])
    status = rng.choice([502, 503])
    return ("medium", ["dependency_errors"] + (["partial_failure"] if rate < 1 else []),
            f"The card network returns {status} on {rate:.0%} of authorizations; no deploy.",
            [], [fault(0, "payments-svc", "card_errors", rate=rate, status=status)], steady(rng),
            truth("card-network", "dependency_errors", ["dependency"], CARD, None, ["card"]))


def t_card_hang(rng, v):
    secs = rng.choice([3, 5, 8])
    return ("easy" if v == 0 else "medium", ["dependency_hang", "downstream_timeout"],
            f"The card network hangs {secs}s: shop-api times out calling payments-svc.",
            [], [fault(0, "payments-svc", "card_hang", seconds=secs)], steady(rng),
            truth("card-network", "dependency_hang", ["dependency", "infrastructure"], CARD, None, ["card"],
                  None, "shop-api's timeouts name payments-svc; the hang is one hop further down",
                  ["shop-api"]))


def t_downstream_db(rng, v):
    secs = rng.choice([2.5, 3.0, 4.0])
    return ("medium", ["slow_db", "downstream_timeout"],
            f"Postgres takes {secs}s, longer than shop-api's 2s payments timeout: shop-api reports timeouts.",
            [], [fault(0, "payments-svc", "slow_db", seconds=secs)], steady(rng),
            truth("postgres", "slow_db", ["infrastructure", "dependency"], POSTGRES, None,
                  ["postgres", "database", "db"], None, "the only error lines are shop-api timeouts",
                  ["shop-api"]))


def t_memory_leak(rng, v):
    kb = rng.choice([256, 384, 512])
    diff = ("--- a/payments-svc/idempotency.py\n+++ b/payments-svc/idempotency.py\n@@ -20,6 +20,5 @@ def remember(receipt):\n"
            "     _receipts.append(receipt)\n-    del _receipts[:-100]\n")
    msg = rng.choice(["Keep receipts for idempotent retries", "Idempotency: cache receipts longer"])
    return ("hard", ["memory_leak"],
            f"payments-svc '{msg}' stops evicting receipts ({kb}KB each): memory climbs, latency creeps up.",
            [deploy(rng, "d1", "payments-svc", msg, diff, True)],
            [{"at_s": 0, "action": "deploy", "ref": "d1"},
             fault(0, "payments-svc", "mem_leak", kb_per_request=kb, max_mb=600)],
            {"shape": "ramp", "extra_rps": rng.choice([6, 8, 10]), "duration_s": 300},
            truth("payments-svc", "memory_leak", ["deploy"], ["payments-svc"], "d1",
                  ["memory", "leak", "cache", "receipt"], ["rollback", "restart"],
                  "no errors at first; the extra load looks like the cause", None))


def t_cpu_pressure(rng, v):
    rounds = rng.choice([1_000_000, 1_500_000, 2_000_000])
    diff = ("--- a/shop-api/recommend.py\n+++ b/shop-api/recommend.py\n@@ -8,5 +8,5 @@ def score(product):\n"
            "-    for _ in range(10):\n+    for _ in range(" + f"{rounds}" + "):  # deeper personalization\n"
            "         digest = hashlib.sha256(digest).digest()\n")
    msg = rng.choice(["Deeper personalized recommendations", "Recs v2: more hashing rounds"])
    return ("medium" if v == 0 else "hard", ["cpu_pressure"],
            f"shop-api '{msg}' burns CPU on every request: shop-api latency rises, nothing errors.",
            [deploy(rng, "d1", "shop-api", msg, diff, True)],
            [{"at_s": 0, "action": "deploy", "ref": "d1"}, fault(0, "shop-api", "cpu_burn", rounds=rounds)],
            {"shape": "burst", "extra_rps": rng.choice([2, 4]), "duration_s": 240},
            truth("shop-api", "cpu_pressure", ["deploy"], ["shop-api"], "d1",
                  ["cpu", "recommend", "hash", "personaliz"], ["rollback"],
                  "a load burst arrives at the same time", ["payments-svc"]))


def t_bad_record(rng, v):
    pid = rng.choice([1, 2, 3])
    name = PRODUCTS[pid]
    return ("medium", ["data_issue", "partial_failure"],
            f"The catalog row for {name} (id {pid}) has a NULL price: only {name} orders fail, inside shop-api.",
            [], [fault(0, "shop-api", "bad_product", product_id=pid)], steady(rng),
            truth("shop-api", "data_issue", ["unknown", "infrastructure"], ["shop-api"], None,
                  [name.lower(), "price", "null", "none", "product"], ["fix_forward", "escalate"],
                  "about a third of orders fail with a TypeError; there is no deploy to blame"))


def t_flapping(rng, v):
    flap = rng.choice([15, 20, 30])
    return ("hard", ["dependency_errors", "flapping"],
            f"Card network 503s that come and go every {flap}s.",
            [], [fault(0, "payments-svc", "card_errors", rate=1.0, status=503, flap_s=flap)], steady(rng),
            truth("card-network", "dependency_errors", ["dependency"], CARD, None, ["card"], None,
                  "the error rate keeps recovering, so it looks like it fixed itself"))


def t_two_faults(rng, v):
    pid = rng.choice([1, 3])
    combos = [
        ("bad_product + card 503s",
         [fault(0, "shop-api", "bad_product", product_id=pid), fault(rng.choice([0, 20]), "payments-svc", "card_errors", rate=0.5)],
         truth("card-network", "dependency_errors", ["dependency"], CARD, None, ["card"]),
         truth("shop-api", "data_issue", ["unknown", "infrastructure"], ["shop-api"], None,
               [PRODUCTS[pid].lower(), "price", "null", "none"])),
        ("slow database + NULL-price product",
         [fault(0, "payments-svc", "slow_db", seconds=1.5), fault(rng.choice([10, 30]), "shop-api", "bad_product", product_id=pid)],
         truth("postgres", "slow_db", ["infrastructure", "dependency"], POSTGRES, None, ["postgres", "database", "db"]),
         truth("shop-api", "data_issue", ["unknown", "infrastructure"], ["shop-api"], None,
               [PRODUCTS[pid].lower(), "price", "null", "none"])),
    ]
    label, steps, first, second = combos[v % 2]
    first["alternatives"] = [second]
    first["trap"] = "two independent faults at once; naming either one (with its evidence) counts"
    return ("hard", ["multi_fault", first["cause_type"], second["cause_type"]],
            f"Two independent faults: {label}.", [], steps, steady(rng), first)


def t_log_noise(rng, v):
    lines = rng.choice([5, 10, 20])
    real = v % 2
    steps = [fault(0, "shop-api", "log_flood", lines_per_request=lines)]
    if real == 0:
        steps.append(fault(rng.choice([20, 40]), "payments-svc", "card_errors", rate=1.0, status=503))
        t = truth("card-network", "dependency_errors", ["dependency"], CARD, None, ["card"], None,
                  "shop-api floods unrelated analytics / recs-cache errors", ["shop-api"])
        types = ["dependency_errors", "log_noise"]
    else:
        steps.append(fault(rng.choice([20, 40]), "payments-svc", "slow_db", seconds=2.5))
        t = truth("postgres", "slow_db", ["infrastructure", "dependency"], POSTGRES, None,
                  ["postgres", "database", "db"], None,
                  "shop-api floods unrelated analytics / recs-cache errors", ["shop-api"])
        types = ["slow_db", "log_noise", "downstream_timeout"]
    return ("hard", types, f"{lines} unrelated error lines per request in shop-api hide a real payments-side fault.",
            [], steps, steady(rng), t)


TEMPLATES = [
    ("payments-bad-deploy", t_payments_bad_deploy), ("partial-deploy", t_partial_deploy),
    ("shop-bad-deploy", t_shop_bad_deploy), ("shop-config-timeout", t_config_error), ("slow-db", t_slow_db),
    ("deploy-then-slow-db", t_timing_trap), ("card-errors", t_card_errors), ("card-hang", t_card_hang),
    ("db-upstream-timeouts", t_downstream_db), ("memory-leak", t_memory_leak), ("cpu-pressure", t_cpu_pressure),
    ("bad-record", t_bad_record), ("card-flapping", t_flapping), ("two-faults", t_two_faults),
    ("log-noise", t_log_noise),
]


def generate(seed: int = 1, variants: int = 3) -> list[dict]:
    """Deterministic: same seed and variants -> same scenarios."""
    out = []
    for n, (name, template) in enumerate(TEMPLATES):
        seen = set()
        for v in range(variants):
            # Re-roll until this variant differs from the template's earlier ones (faults,
            # timings or answer), so held-out scenarios are never copies of training ones.
            for attempt in range(50):
                rng = random.Random(f"{seed}:{name}:{v}" + (f":{attempt}" if attempt else ""))
                difficulty, types, desc, deploys, steps, load, gt = template(rng, v)
                key = json.dumps([steps, gt], sort_keys=True)
                if key not in seen:
                    break
            else:
                raise AssertionError(f"template {name} can't make {variants} distinct variants")
            seen.add(key)
            spec = {
                "id": f"s{n + 1:02d}-{name}-{'abcdefghij'[v]}",
                "description": desc, "difficulty": difficulty,
                "split": "heldout" if variants > 1 and v == variants - 1 else "train",
                "fault_types": sorted(dict.fromkeys(types)), "load": load,
                "deploys": deploys, "steps": steps, "ground_truth": gt,
                "generator": {"mode": "template", "template": name, "seed": seed, "variant": v},
            }
            errs = ss.validate(spec)
            if errs:
                raise AssertionError(f"template {name} made an invalid scenario: {errs}")
            out.append(spec)
    return out


# ---------- optional: LLM proposals (never used in tests without a mock client) ----------

LLM_PROMPT = """You design incident scenarios for evaluating an AI on-call agent.
Services: shop-api (front door, calls payments-svc), payments-svc (calls a card network, stores in Postgres).
Fault catalog (name: service, params[min,max]): {catalog}
Every fault also takes flap_s (5-300) to make it intermittent.
Propose {n} NOVEL scenarios as a JSON array. Each must follow this example's exact shape:
{example}
Rules: ids start with "llm-"; split "train"; deploy diffs are unified diffs; mark the culprit deploy;
ground_truth names the real root cause and any trap. Reply with only the JSON array."""


def llm_generate(n: int, seed_examples: list[dict], client=None, model: str | None = None) -> list[dict]:
    if client is None:
        if os.environ.get("LOKI_CHAOS_LLM") != "1":
            raise SystemExit("--llm needs LOKI_CHAOS_LLM=1 (and ANTHROPIC_API_KEY). It costs money.")
        import anthropic
        client = anthropic.Anthropic()
    catalog = {k: {"service": f["service"], "params": {p: [v[1], v[2]] for p, v in f["params"].items()}}
               for k, f in ss.FAULTS.items()}
    example = {k: v for k, v in seed_examples[0].items() if k != "generator"}
    msg = client.messages.create(
        model=model or os.environ.get("LOKI_CHAOS_MODEL", "claude-haiku-4-5"), max_tokens=8000,
        messages=[{"role": "user", "content": LLM_PROMPT.format(catalog=json.dumps(catalog), n=n,
                                                                 example=json.dumps(example))}])
    text = "".join(getattr(b, "text", "") for b in msg.content)
    try:
        proposals = json.loads(text[text.index("["): text.rindex("]") + 1])
    except ValueError:
        return []
    good = []
    for p in proposals if isinstance(proposals, list) else []:
        if not isinstance(p, dict):
            continue
        p["split"] = "train"
        p["generator"] = {"mode": "llm"}
        if not str(p.get("id", "")).startswith("llm-"):
            p["id"] = f"llm-{p.get('id', len(good))}"
        if not ss.validate(p):
            good.append(p)
    return good


# ---------- files ----------

def render(spec: dict) -> str:
    return json.dumps(spec, indent=2) + "\n"


def write(specs: list[dict], out: Path) -> None:
    for split in ss.SPLITS:
        d = out / split
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.json"):
            if not old.name.startswith("llm-"):
                old.unlink()
    for s in specs:
        (out / s["split"] / f"{s['id']}.json").write_text(render(s))
    index = {"count": len(specs),
             "train": sorted(s["id"] for s in specs if s["split"] == "train"),
             "heldout": sorted(s["id"] for s in specs if s["split"] == "heldout")}
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")


def load_all(out: Path = OUT, split: str = "all") -> list[dict]:
    splits = sorted(ss.SPLITS) if split == "all" else [split]
    return [json.loads(p.read_text()) for s in splits for p in sorted((out / s).glob("*.json"))]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--variants", type=int, default=3, help="scenarios per template (the last one is held out)")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--check", action="store_true", help="fail if the files on disk differ from a fresh generation")
    p.add_argument("--llm", type=int, metavar="N", default=0, help="also ask Claude for N novel scenarios (costs money)")
    a = p.parse_args()
    specs = generate(a.seed, a.variants)
    if a.check:
        stale = [s["id"] for s in specs
                 if not (a.out / s["split"] / f"{s['id']}.json").exists()
                 or (a.out / s["split"] / f"{s['id']}.json").read_text() != render(s)]
        if stale:
            sys.exit(f"stale scenario files: {', '.join(stale)} (run scripts/chaos_engineer.py)")
        print(f"{len(specs)} scenarios up to date")
        return
    write(specs, a.out)
    if a.llm:
        extra = llm_generate(a.llm, specs)
        for s in extra:
            (a.out / "train" / f"{s['id']}.json").write_text(render(s))
        print(f"LLM proposed {len(extra)} valid scenario(s)")
    by = {}
    for s in specs:
        by.setdefault(s["split"], 0)
        by[s["split"]] += 1
    print(f"wrote {len(specs)} scenarios to {a.out}: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))


if __name__ == "__main__":
    main()
