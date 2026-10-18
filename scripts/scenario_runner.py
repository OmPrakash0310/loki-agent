"""Play one scenario file against the demo services: record its deploys, switch its
faults on and off on schedule, add its extra load, then put everything back.

Library use (scripts/eval.py):
    run = ScenarioRun(spec).start()   # steps play in a background thread
    ...wait for Loki's report...
    run.stop()                        # faults off, culprit deploys reverted

CLI (live proof, no Loki needed):
    python3 scripts/scenario_runner.py evals/scenarios/train/<id>.json --hold 150 \\
        --payments-url http://localhost:18202 --shop-url http://localhost:18201 \\
        --deploys-file /tmp/deploys.json

Fault switches go over each service's hidden POST /chaos (never logged, never in metrics).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario_spec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URLS = {
    "payments-svc": os.environ.get("LOKI_CHAOS_PAYMENTS_URL", "http://localhost:8002"),
    "shop-api": os.environ.get("LOKI_CHAOS_SHOP_URL", "http://localhost:8001"),
}
IMAGES = {"payments-svc": "loki-demo/payments-svc", "shop-api": "loki-demo/shop-api"}


def http_post(url: str, body: dict, timeout: float = 5) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def revert_diff(diff: str) -> str:
    return "\n".join(
        ("-" + l[1:]) if l.startswith("+") and not l.startswith("+++")
        else ("+" + l[1:]) if l.startswith("-") and not l.startswith("---")
        else l
        for l in diff.splitlines()
    ) + "\n"


class ScenarioRun:
    def __init__(self, spec: dict, urls: dict | None = None, deploys_file: Path | None = None,
                 ground_truth_file: Path | None = None, post=http_post, sleep=time.sleep, log=print):
        errs = scenario_spec.validate(spec)
        if errs:
            raise ValueError(f"invalid scenario {spec.get('id')}: " + "; ".join(errs))
        self.spec, self.urls = spec, {**DEFAULT_URLS, **(urls or {})}
        self.deploys_file = Path(deploys_file or ROOT / "state" / "deploys.json")
        self.ground_truth_file = Path(ground_truth_file or ROOT / "ground_truth.jsonl")
        self.post, self.sleep, self.log = post, sleep, log
        self.deploy_ids: dict[str, str] = {}
        self.active: dict[str, dict] = {s: {} for s in scenario_spec.SERVICES}
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started_at: float | None = None

    # ---- deploy history ----
    def _append_deploy(self, record: dict) -> str:
        deploys = json.loads(self.deploys_file.read_text()) if self.deploys_file.exists() else []
        deploy_id = secrets.token_hex(4)[:7]
        deploys.append({"id": deploy_id, **record, "deployed_at": datetime.now(timezone.utc).isoformat()})
        self.deploys_file.parent.mkdir(parents=True, exist_ok=True)
        self.deploys_file.write_text(json.dumps(deploys, indent=2) + "\n")
        return deploy_id

    def _deploy(self, ref: str):
        d = next(d for d in self.spec["deploys"] if d["ref"] == ref)
        self.deploy_ids[ref] = self._append_deploy({
            "service": d["service"], "version": d["version"], "author": d["author"], "message": d["message"],
            "image": f"{IMAGES[d['service']]}:{d['version']}", "diff": d["diff"],
        })
        self._event("deploy", ref=ref, id=self.deploy_ids[ref], service=d["service"], version=d["version"])

    # ---- faults ----
    def _push(self, service: str):
        self.post(f"{self.urls[service]}/chaos", {"faults": self.active[service]})

    def _step(self, step: dict):
        action = step["action"]
        if action == "deploy":
            self._deploy(step["ref"])
        elif action == "fault":
            self.active[step["service"]][step["fault"]] = dict(step.get("params") or {})
            self._push(step["service"])
            self._event("fault", service=step["service"], fault=step["fault"])
        elif action == "clear":
            self.active[step["service"]].pop(step["fault"], None)
            self._push(step["service"])
            self._event("clear", service=step["service"], fault=step["fault"])
        elif action == "mode":
            self.post(f"{self.urls['payments-svc']}/chaos", {"mode": step["mode"]})
            self._event("mode", mode=step["mode"])

    def _event(self, kind, **kw):
        at = round(time.monotonic() - (self.started_at or time.monotonic()), 1)
        self.events.append({"t": at, "event": kind, **kw})
        self.log(f"[{self.spec['id']} +{at:>5}s] {kind} " + " ".join(f"{k}={v}" for k, v in kw.items()))

    def _play(self):
        start = self.started_at
        for step in self.spec["steps"]:
            wait = start + step["at_s"] - time.monotonic()
            if wait > 0 and self._stop.wait(wait):
                return
            if self._stop.is_set():
                return
            self._step(step)

    # ---- extra load ----
    def _load(self):
        load = self.spec["load"]
        rps, duration = load["extra_rps"], load["duration_s"]
        if not rps:
            return
        shop = self.urls["shop-api"]
        start = time.monotonic()
        n = 0
        while not self._stop.is_set() and time.monotonic() - start < duration:
            elapsed = time.monotonic() - start
            rate = (rps * min(1.0, elapsed / max(duration / 2, 1)) if load["shape"] == "ramp"
                    else rps if load["shape"] == "steady" or (elapsed % 60) < 30  # burst: 30s on, 30s off
                    else 0)
            if rate <= 0:
                self._stop.wait(0.5)
                continue
            n += 1
            path = "/orders" if n % 2 else "/products"
            threading.Thread(target=self._hit, args=(shop + path, path == "/orders"), daemon=True).start()
            self._stop.wait(1 / rate)

    @staticmethod
    def _hit(url, post):
        try:
            if post:
                http_post(url, {}, timeout=5)
            else:
                urllib.request.urlopen(url, timeout=5).read()
        except Exception:
            pass  # failures are the point

    # ---- lifecycle ----
    def start(self, background: bool = True) -> "ScenarioRun":
        self.started_at = time.monotonic()
        self._write_truth()
        for target in (self._play, self._load):
            t = threading.Thread(target=target, daemon=True)
            self._threads.append(t)
            t.start()
        if not background:
            self._threads[0].join()
        return self

    def _write_truth(self):
        # Written up front so the eval has it even if the run is cut short. Deploy ids are
        # filled in by stop(); eval.py grades with run.deploy_ids directly.
        self._truth = {"at": datetime.now(timezone.utc).isoformat(), "mode": f"scenario:{self.spec['id']}",
                       "scenario": self.spec["id"]}

    def stop(self):
        """Faults off on every service, culprit deploys reverted (like chaos.py none)."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        for service in self.urls:
            try:
                self.post(f"{self.urls[service]}/chaos", {"mode": "none"})
            except Exception as e:  # a service that is down has no faults to clear
                self.log(f"could not reset {service}: {e}")
            self.active[service] = {}
        for d in self.spec["deploys"]:
            if d["culprit"] and d["ref"] in self.deploy_ids:
                major, minor, patch = (d["version"].lstrip("v").split(".") + ["0", "0"])[:3]
                version = f"v{major}.{minor}.{int(patch) + 1 if patch.isdigit() else 1}"
                self._append_deploy({
                    "service": d["service"], "version": version,
                    "author": d["author"], "message": f"Revert \"{d['message']}\" ({self.deploy_ids[d['ref']]})",
                    "image": f"{IMAGES[d['service']]}:{version}", "diff": revert_diff(d["diff"]),
                })
        self._truth["deploy_ids"] = dict(self.deploy_ids)
        with self.ground_truth_file.open("a") as f:
            f.write(json.dumps(self._truth) + "\n")
        self._event("reset")


def load_spec(path) -> dict:
    return json.loads(Path(path).read_text())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario")
    p.add_argument("--hold", type=float, default=120, help="seconds to keep the scenario running before reset")
    p.add_argument("--payments-url", default=DEFAULT_URLS["payments-svc"])
    p.add_argument("--shop-url", default=DEFAULT_URLS["shop-api"])
    p.add_argument("--deploys-file", type=Path)
    p.add_argument("--ground-truth-file", type=Path)
    a = p.parse_args()
    run = ScenarioRun(load_spec(a.scenario), {"payments-svc": a.payments_url, "shop-api": a.shop_url},
                      a.deploys_file, a.ground_truth_file).start()
    try:
        time.sleep(a.hold)
    finally:
        run.stop()
    print(json.dumps({"scenario": run.spec["id"], "deploy_ids": run.deploy_ids, "events": run.events}))


if __name__ == "__main__":
    main()
