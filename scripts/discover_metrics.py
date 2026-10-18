"""Find the request and latency metrics in a real Prometheus and suggest a Loki metrics config.

Read-only: it only sends GET requests to the Prometheus HTTP API.

Usage:
    python3 scripts/discover_metrics.py --prom http://localhost:9090
    python3 scripts/discover_metrics.py --prom http://prom:9090 --json > metrics.json   # LOKI_METRICS_CONFIG=metrics.json
"""
import argparse
import json
import os
import re
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
import metrics_config  # noqa: E402

SERVICE_LABELS = ["service", "service_name", "app", "application", "app_kubernetes_io_name", "k8s_app",
                  "kubernetes_name", "container", "job"]
STATUS_LABELS = ["status", "status_code", "http_status_code", "http_response_status_code", "code", "http_status"]
DEPENDENCY_LABELS = ["dependency", "server_address", "client_name", "peer_service", "net_peer_name", "target", "host"]

_HTTPISH = re.compile(r"(http|request|rpc|server)", re.I)


def _get(client: httpx.Client, path: str, **params):
    resp = client.get(path, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]


def candidates(names: list[str]) -> dict[str, list[str]]:
    """Metric names that look like request counters, latency histograms and outgoing-call histograms."""
    names = set(names)
    hist = sorted(n[: -len("_bucket")] for n in names if n.endswith("_bucket")
                  and re.search(r"(duration|latency|seconds|_time)", n))
    latency = [h for h in hist if _HTTPISH.search(h) and not re.search(r"client|dependency|outgoing|db_", h)]
    dependency = [h for h in hist if re.search(r"client|dependency|outgoing|db_", h)]
    counters = sorted(n for n in names if n.endswith("_total") and _HTTPISH.search(n)
                      and not re.search(r"(bytes|size|client|process_|go_|python_)", n))
    return {"requests": counters, "latency": latency, "dependency": dependency}


def labels_of(client: httpx.Client, metric: str) -> list[str]:
    """Label names on a few recent series of `metric`."""
    now = time.time()
    series = _get(client, "/api/v1/series", **{"match[]": metric, "start": now - 3600, "end": now})[:200]
    return sorted({k for s in series for k in s if k != "__name__"})


def _pick(preferred: list[str], present: list[str]) -> str | None:
    return next((p for p in preferred if p in present), None)


def suggest(client: httpx.Client) -> dict:
    names = _get(client, "/api/v1/label/__name__/values")
    found = candidates(names)
    cfg: dict = {}
    report = {"candidates": found, "labels": {}}

    latency = found["latency"][0] if found["latency"] else None
    requests = found["requests"][0] if found["requests"] else (f"{latency}_count" if latency else None)
    probe = requests or (f"{latency}_count" if latency else None)
    if probe:
        labels = labels_of(client, probe)
        report["labels"][probe] = labels
        cfg["service_label"] = _pick(SERVICE_LABELS, labels) or "job"
        cfg["status_label"] = _pick(STATUS_LABELS, labels)
    cfg["requests"] = requests
    cfg["latency_histogram"] = latency
    dep = found["dependency"][0] if found["dependency"] else None
    cfg["dependency_histogram"] = dep
    if dep:
        dlabels = labels_of(client, f"{dep}_count")
        report["labels"][f"{dep}_count"] = dlabels
        cfg["dependency_label"] = _pick(DEPENDENCY_LABELS, dlabels) or "dependency"
    cfg["services_from_requests"] = True
    report["preset"] = closest_preset(cfg)
    report["config"] = cfg
    return report


def closest_preset(cfg: dict) -> str:
    """The preset sharing the most fields with the suggestion."""
    keys = ("service_label", "status_label", "requests", "latency_histogram", "dependency_histogram")
    score = {name: sum(getattr(p, k) == cfg.get(k) for k in keys) for name, p in metrics_config.PRESETS.items()}
    return max(score, key=score.get)


def main(argv=None, client: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prom", required=True, help="Prometheus base URL, e.g. http://localhost:9090")
    ap.add_argument("--json", action="store_true", help="print only the suggested config as JSON")
    args = ap.parse_args(argv)
    client = client or httpx.Client(base_url=args.prom.rstrip("/"))
    try:
        report = suggest(client)
    except httpx.HTTPError as e:
        print(f"could not read {args.prom}: {e}", file=sys.stderr)
        return 1
    cfg = report["config"]
    try:
        metrics_config.MetricsConfig(**cfg)
    except ValueError as e:
        print(f"warning: suggested config is incomplete: {e}", file=sys.stderr)
    if args.json:
        print(json.dumps(cfg, indent=2))
        return 0
    c = report["candidates"]
    print("Request counters:   ", ", ".join(c["requests"]) or "none found")
    print("Latency histograms: ", ", ".join(c["latency"]) or "none found")
    print("Dependency/outgoing:", ", ".join(c["dependency"]) or "none found")
    for metric, labels in report["labels"].items():
        print(f"Labels on {metric}: {', '.join(labels) or 'none'}")
    print(f"\nClosest preset: {report['preset']}  (LOKI_METRICS_PRESET={report['preset']})")
    print("Suggested LOKI_METRICS_CONFIG file (check the names, then save it as JSON):")
    print(json.dumps(cfg, indent=2))
    if not cfg.get("status_label"):
        print("\nNo status-code label found: error_rate will be skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
