"""The context builder: turns an alert storm into a short briefing for the investigator.

Millions of log lines become a handful of error patterns, metrics become a
before-and-after per service, recent deploys are listed, and plain code ranks
the likely suspects, so the model starts from a strong lead instead of a blank
page. All of it is free: no model calls happen here.
"""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import telemetry as tm

LOOKBACK = timedelta(minutes=15)
# A deploy this long before the first error is still a suspect.
DEPLOY_SUSPECT_WINDOW = timedelta(minutes=30)


@dataclass
class Briefing:
    text: str
    fingerprint: str  # what the incident looks like (service + error patterns + deploy or not)
    exact_key: str  # fingerprint + the suspect deploy: the key for reusing a confirmed answer
    suspects: list[dict] = field(default_factory=list)
    onset: dict[str, datetime] = field(default_factory=dict)


def _error_onset(lines) -> dict[str, datetime]:
    """When each service logged its first error in the window."""
    onset: dict[str, datetime] = {}
    for t, svc, _ in lines:
        onset.setdefault(svc, t)
    return dict(sorted(onset.items(), key=lambda kv: kv[1]))


def source_service(onset: dict[str, datetime], clusters: list[dict]) -> tuple[str | None, str]:
    """Which service the failure started in. Errors often start within the same
    second everywhere, so timing alone can't tell. Error lines that name another
    service ("payments-svc returned 500") mark the speaker as the victim."""
    if not onset:
        return None, ""
    earliest = next(iter(onset.values()))
    candidates = [svc for svc, t in onset.items() if (t - earliest).total_seconds() <= 5]
    blamed = {svc: 0 for svc in onset}
    for c in clusters:
        for speaker in c["services"]:
            for other in onset:
                if other != speaker and other in c["example"]:
                    blamed[other] += c["count"]
    source = max(candidates, key=lambda svc: (blamed[svc], -onset[svc].timestamp()))
    if blamed[source]:
        return source, f"other services' errors point at {source} ({blamed[source]} lines)"
    return source, f"{source} logged errors first (at {tm._hms(onset[source])})"


def latest_deploys(deploys: list[dict], before: datetime) -> list[dict]:
    """Only each service's most recent deploy before the failure can be running.
    Older ones were already replaced (or reverted), so they can't be the cause."""
    latest: dict[str, dict] = {}
    for d in deploys:
        if d["deployed_at_dt"] <= before:
            latest[d["service"]] = d
    return list(latest.values())


def rank_suspects(onset: dict[str, datetime], clusters: list[dict], deploys: list[dict], incidents: list[dict],
                  dep_spikes: list[dict], floor: datetime | None = None) -> list[dict]:
    """Score likely causes with simple rules. The model verifies; it doesn't start from scratch."""
    first_service, why_source = source_service(onset, clusters)
    first_error = min(onset.values()) if onset else min(i["started_at"] for i in incidents)
    suspects = []
    for spike in dep_spikes:
        suspects.append({
            "kind": "non-deploy", "service": spike["service"], "dependency": spike["dependency"], "score": 4, "gap": 0,
            "reasons": [f"{spike['service']}'s calls to {spike['dependency']} slowed from p95 {spike['before']}s to {spike['now']}s"],
        })
    for d in latest_deploys(deploys, first_error + timedelta(seconds=60)):
        if floor and d["deployed_at_dt"] < floor:
            continue  # the system was healthy again after this deploy, so it isn't the cause
        gap = (first_error - d["deployed_at_dt"]).total_seconds()
        if not (-60 <= gap <= DEPLOY_SUSPECT_WINDOW.total_seconds()):
            continue
        score, reasons = 0, []
        if d["service"] == first_service:
            score += 3
            reasons.append(f"deployed to {first_service}, where the failure started ({why_source})")
        if gap <= 600:
            score += 2
            reasons.append(f"shipped {max(gap, 0):.0f}s before the first error")
        else:
            reasons.append(f"shipped {gap / 60:.0f} min before the first error")
        suspects.append({"kind": "deploy", "deploy_id": d["id"], "service": d["service"], "score": score,
                         "gap": max(gap, 0), "reasons": reasons})
    if first_service and not dep_spikes:
        suspects.append({
            "kind": "non-deploy",
            "service": first_service,
            "score": 1 if suspects else 3,
            "gap": 0,
            "reasons": [why_source
                        + ("" if suspects else "; no deploy shortly before, so look at its dependencies, database or load")],
        })
    elif not suspects and not first_service:
        # No error logs at all: a pure slowness incident. The alerting services are the leads.
        for svc in sorted({i["service"] for i in incidents}):
            suspects.append({"kind": "non-deploy", "service": svc, "score": 1, "gap": 0,
                             "reasons": [f"{svc} alerted with no error logs and no recent deploy: check latency and dependencies"]})
    # Higher score first; on a tie, the deploy closest to the first error.
    return sorted(suspects, key=lambda s: (-s["score"], s["gap"]))


def _fingerprint(first_service: str | None, clusters: list[dict], suspects: list[dict], incidents: list[dict]) -> tuple[str, str]:
    patterns = [tm.normalize(c["example"]) for c in clusters if first_service in c["services"]][:3]
    if not patterns:  # slowness-only incidents: fall back to which alerts fired
        patterns = sorted({f"{i['alert_name']}:{i['service']}" for i in incidents})
    top = suspects[0] if suspects else {}
    basis = "|".join([first_service or "-", top.get("kind", "unknown"), top.get("dependency", "-"), *patterns])
    fingerprint = hashlib.sha1(basis.encode()).hexdigest()[:16]
    exact_key = f"{fingerprint}:{top.get('deploy_id', 'none')}"
    return fingerprint, exact_key


def build(incidents: list[dict], floor: datetime | None = None) -> Briefing:
    """floor: when the previous incident was resolved. Nothing before it is looked at,
    so an earlier incident's leftover errors can't be mistaken for this one's cause."""
    first_alert = min(i["started_at"] for i in incidents)
    start = max(first_alert - LOOKBACK, floor) if floor else first_alert - LOOKBACK
    end = tm.now()
    before = first_alert - timedelta(minutes=1)  # errors start ~40s before an alert fires

    out = ["# Incident briefing", "", "## Alerts (grouped into this one incident)"]
    for i in sorted(incidents, key=lambda i: i["started_at"]):
        out.append(f"- {tm._hms(i['started_at'])} {i['alert_name']} on {i['service']} ({i['severity']}): {i['summary']}")

    out += ["", "## Metrics: typical value before the first alert -> now"]
    for svc in tm.services():
        row = []
        for metric in ("error_rate", "p95_latency_seconds", "request_rate"):
            b, n = tm.metric_before(metric, svc, before), tm.metric_at(metric, svc, end)
            row.append(f"{metric} {'no data' if b is None else b} -> {'no data' if n is None else n}")
        out.append(f"- {svc}: " + "; ".join(row))

    # Dependencies: is a service slow because something it calls is slow?
    dep_spikes = []
    dep_lines = []
    for svc in tm.services_with_dependencies():
        b, n = tm.dependency_p95_at(svc, before - timedelta(minutes=2)), tm.dependency_p95_at(svc, end)
        for dep in sorted(set(b) | set(n)):
            bv, nv = b.get(dep), n.get(dep)
            dep_lines.append(f"- {svc} -> {dep}: p95 {bv if bv is not None else 'no data'}s -> {nv if nv is not None else 'no data'}s")
            # A spike: 4x slower than before, or over 1s when there's no baseline to compare with.
            if nv is not None and ((bv is not None and nv > 0.25 and nv > bv * 4) or (bv is None and nv > 1.0)):
                dep_spikes.append({"service": svc, "dependency": dep, "before": bv, "now": nv})
    if dep_lines:
        out += ["", "## Dependency latency (calls from a service to its database / external APIs): before -> now", *dep_lines]

    lines = tm.fetch_logs(None, start, end, errors_only=True)
    onset = _error_onset(lines)
    clusters = tm.cluster_logs(lines, top=6)
    out += ["", f"## Error log patterns since {tm._hms(start)} ({len(lines)} error lines in total)"]
    if not clusters:
        # No error lines can mean "no errors" or "no logs at all". Tell the model which.
        any_logs = tm.fetch_logs(None, start, end, errors_only=False, limit=1)
        out.append("- none" if any_logs else
                   "- WARNING: no logs at all were received in this window. The log pipeline may be broken; "
                   "treat log evidence as missing, not as 'no errors'.")
    for c in clusters:
        out.append(f"- x{c['count']} in {', '.join(c['services'])} ({c['first_seen']} to {c['last_seen']}): {c['example']}")
    if onset:
        out.append("- first error per service: " + ", ".join(f"{s} {tm._hms(t)}" for s, t in onset.items()))
    if floor and floor > first_alert - LOOKBACK:
        out.append(f"- (window starts at {tm._hms(floor)}, when the previous incident was resolved; "
                   "earlier errors belong to that incident)")
        out += ["", "## System health before this incident",
                f"- A previous incident was resolved by {tm._hms(floor)}: its alerts cleared and the system was healthy "
                f"again. Anything that happened before {tm._hms(floor)} (its errors, its deploys and reverts) did not "
                "cause this incident."]

    deploys = tm.deploys_between(first_alert - timedelta(hours=2), end)
    out += ["", "## Deploys in the 2 hours before the first alert"]
    if not deploys:
        out.append("- none")
    running = {d["id"] for d in latest_deploys(deploys, end)}
    for d in deploys:
        note = "" if d["id"] in running else " (superseded by a later deploy to this service)"
        out.append(f"- {d['id']} {d['service']} {d['version']} at {tm._hms(d['deployed_at_dt'])} by {d['author']}: {d['message']}{note}")

    suspects = rank_suspects(onset, clusters, deploys, incidents, dep_spikes, floor)
    out += ["", "## Suspects ranked by simple rules (a starting point: verify, don't trust blindly)"]
    for n, s in enumerate(suspects, 1):
        name = (f"deploy {s['deploy_id']} to {s['service']}" if s["kind"] == "deploy"
                else f"slow dependency {s['dependency']} of {s['service']}" if s.get("dependency")
                else f"a non-deploy cause in {s['service']}")
        out.append(f"{n}. {name} (score {s['score']}): {'; '.join(s['reasons'])}")

    out += ["", f"Current time: {tm._hms(end)} UTC."]
    fingerprint, exact_key = _fingerprint(source_service(onset, clusters)[0], clusters, suspects, incidents)
    return Briefing("\n".join(out), fingerprint, exact_key, suspects, onset)
