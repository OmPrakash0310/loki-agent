"""Loki's critic: one cheap, grounded review of a finished report.

Ungrounded self-critique ("does this look right?") mostly confirms whatever the
model already believed. This critic is only allowed to judge each claim against
the numbered tool results of this investigation: a claim is supported only if it
cites a step that exists. It can lower confidence and drop unsupported evidence;
it can never raise confidence or add claims of its own.

Env:
  LOKI_CRITIC        1/0. Default: on for Claude models, off for the fake model and Ollama.
  LOKI_CRITIC_MODEL  default claude-haiku-4-5. "fake" = a free word-overlap critic (tests, demos).
"""
import json
import os
import re
from types import SimpleNamespace

CRITIC_SYSTEM = """You review an incident report written by another engineer. You did not investigate; \
you only check whether the report is backed by the evidence it had.

You get the report and a numbered list of every tool result (step 0 is the briefing). For the root-cause \
claim and each evidence item, decide:
- supported: a numbered result shows it. Cite those step numbers.
- unsupported: nothing in the results shows it (a plausible guess is still unsupported).
- contradicted: a result shows the opposite. Cite the step.
Judge only against the numbered results. Don't use outside knowledge and don't suggest new causes.
Verdict "revise" only if the root cause is unsupported or contradicted, or most evidence is unsupported. \
confidence_adjustment is 0 or negative (how much to lower confidence); never positive."""

CRITIQUE_TOOL = {
    "name": "submit_critique",
    "description": "Submit your review of the report. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": ["root_cause", "evidence"]},
                        "index": {"type": "integer", "description": "Evidence item number (0-based); 0 for the root cause."},
                        "status": {"type": "string", "enum": ["supported", "unsupported", "contradicted"]},
                        "steps": {"type": "array", "items": {"type": "integer"}},
                        "note": {"type": "string"},
                    },
                    "required": ["kind", "index", "status", "steps", "note"],
                },
            },
            "verdict": {"type": "string", "enum": ["accept", "revise"]},
            "confidence_adjustment": {"type": "number"},
            "notes": {"type": "string", "description": "For revise: what the investigator should check, citing steps."},
        },
        "required": ["claims", "verdict", "confidence_adjustment", "notes"],
    },
}

MAX_OBS_CHARS = 1500     # per tool result
MAX_TOTAL_CHARS = 24000  # whole evidence list
CONTRADICTION_PENALTY = 0.2


def model() -> str:
    return os.environ.get("LOKI_CRITIC_MODEL", "claude-haiku-4-5")


def enabled(investigator_model: str, local: bool) -> bool:
    flag = os.environ.get("LOKI_CRITIC", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return investigator_model != "fake" and not local


# ---------- inputs ----------

def format_observations(observations: list[dict]) -> str:
    """observations: [{"step": n, "tool": name, "input": {...}, "output": str}], step 0 = briefing."""
    parts, total = [], 0
    for o in observations:
        out = o["output"]
        if len(out) > MAX_OBS_CHARS:
            out = out[:MAX_OBS_CHARS] + f"… [{len(o['output']) - MAX_OBS_CHARS} chars cut]"
        part = f"[{o['step']}] {o['tool']}({json.dumps(o.get('input') or {})[:200]})\n{out}"
        if total + len(part) > MAX_TOTAL_CHARS:
            parts.append(f"[…{len(observations) - len(parts)} more results cut for length]")
            break
        parts.append(part)
        total += len(part)
    return "\n\n".join(parts)


def build_prompt(report: dict, observations: list[dict]) -> str:
    ev = "\n".join(f"  {i}. {e}" for i, e in enumerate(report.get("evidence", [])))
    return (
        f"## Report\nRoot cause ({report.get('root_cause_service', '?')}, trigger {report.get('trigger', '?')}"
        + (f", deploy {report['deploy_id']}" if report.get("deploy_id") else "")
        + f"): {report.get('root_cause', '')}\nEvidence:\n{ev or '  (none)'}\n"
        f"Confidence: {report.get('confidence', 0)}\n\n## Numbered tool results\n{format_observations(observations)}"
    )


# ---------- the call ----------

def call(client, critic_model: str, report: dict, observations: list[dict]):
    """One forced-structure call. Returns (critique_dict, usage)."""
    if critic_model == "fake":
        return fake_critique(report, observations), SimpleNamespace(
            input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    resp = client.messages.create(
        model=critic_model, max_tokens=2000, system=CRITIC_SYSTEM, tools=[CRITIQUE_TOOL],
        tool_choice={"type": "tool", "name": "submit_critique"},
        messages=[{"role": "user", "content": build_prompt(report, observations)}],
    )
    block = next((b for b in resp.content if b.type == "tool_use" and b.name == "submit_critique"), None)
    if block is None:
        raise RuntimeError("critic returned no critique")
    return dict(block.input), resp.usage


_WORD = re.compile(r"[a-z0-9_.%'-]{4,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def fake_critique(report: dict, observations: list[dict]) -> dict:
    """Free stand-in: a claim is supported when most of its words appear in one result."""
    def judge(text):
        w = _words(text)
        best, step = 0.0, None
        for o in observations:
            score = len(w & _words(o["output"])) / max(1, len(w))
            if score > best:
                best, step = score, o["step"]
        return ("supported", [step]) if best >= 0.5 else ("unsupported", [])

    claims = []
    status, steps = judge(report.get("root_cause", ""))
    claims.append({"kind": "root_cause", "index": 0, "status": status, "steps": steps, "note": "fake critic"})
    for i, e in enumerate(report.get("evidence", [])):
        s, st = judge(e)
        claims.append({"kind": "evidence", "index": i, "status": s, "steps": st, "note": "fake critic"})
    bad_root = claims[0]["status"] != "supported"
    return {"claims": claims, "verdict": "revise" if bad_root else "accept",
            "confidence_adjustment": -0.1 if bad_root else 0.0,
            "notes": "Root cause not found in any tool result." if bad_root else ""}


# ---------- applying it ----------

def normalize(raw: dict, report: dict, observations: list[dict]) -> dict:
    """Ground the critic itself: 'supported' must cite a step that exists, or it isn't supported."""
    valid_steps = {o["step"] for o in observations}
    n_ev = len(report.get("evidence", []))
    claims, seen = [], set()
    for c in raw.get("claims", []):
        kind = c.get("kind")
        idx = 0 if kind == "root_cause" else c.get("index")
        if kind not in ("root_cause", "evidence") or not isinstance(idx, int) or (kind == "evidence" and not 0 <= idx < n_ev):
            continue
        if (kind, idx) in seen:
            continue
        seen.add((kind, idx))
        steps = sorted({s for s in c.get("steps", []) if isinstance(s, int) and s in valid_steps})
        status = c.get("status") if c.get("status") in ("supported", "unsupported", "contradicted") else "unsupported"
        note = str(c.get("note", ""))[:300]
        if status in ("supported", "contradicted") and not steps:
            note = (note + " " if note else "") + "(no valid step cited)"
            status = "unsupported"
        claims.append({"kind": kind, "index": idx, "status": status, "steps": steps, "note": note})
    # Evidence the critic skipped is not judged: keep it, but say so.
    for i in range(n_ev):
        if ("evidence", i) not in seen:
            claims.append({"kind": "evidence", "index": i, "status": "unjudged", "steps": [], "note": ""})
    claims.sort(key=lambda c: (c["kind"] != "root_cause", c["index"]))
    try:
        adj = float(raw.get("confidence_adjustment", 0) or 0)
    except (TypeError, ValueError):
        adj = 0.0
    root = next((c for c in claims if c["kind"] == "root_cause"), None)
    verdict = raw.get("verdict") if raw.get("verdict") in ("accept", "revise") else "accept"
    if root and root["status"] != "supported":
        verdict = "revise"
    return {"claims": claims, "verdict": verdict, "confidence_adjustment": max(-1.0, min(0.0, adj)),
            "notes": str(raw.get("notes", ""))[:1500]}


def apply(report: dict, crit: dict, extra: dict | None = None) -> dict:
    """Return a new report with unsupported evidence removed and confidence lowered."""
    out = dict(report)
    evidence = list(report.get("evidence", []))
    drop = {c["index"] for c in crit["claims"] if c["kind"] == "evidence" and c["status"] in ("unsupported", "contradicted")}
    out["evidence"] = [e for i, e in enumerate(evidence) if i not in drop]
    contradicted = any(c["status"] == "contradicted" for c in crit["claims"])
    before = float(report.get("confidence", 0) or 0)
    after = before + crit["confidence_adjustment"]
    if contradicted:
        after = min(after, before - CONTRADICTION_PENALTY)
    out["confidence"] = round(max(0.0, min(before, after)), 2)
    judged = [c for c in crit["claims"] if c["status"] != "unjudged"]
    out["critique"] = {
        **(extra or {}),
        "verdict": crit["verdict"],
        "claims": [{**c, "text": report.get("root_cause", "") if c["kind"] == "root_cause" else evidence[c["index"]]}
                   for c in crit["claims"]],
        "supported": sum(c["status"] == "supported" for c in judged),
        "total": len(judged),
        "removed_evidence": [evidence[i] for i in sorted(drop)],
        "contradicted": contradicted,
        "confidence_before": before,
        "confidence_after": out["confidence"],
        "notes": crit["notes"],
    }
    return out


def revision_prompt(crit: dict) -> str:
    bad = [c for c in crit["claims"] if c["status"] in ("unsupported", "contradicted")]
    lines = [f"- {c['kind']}{'' if c['kind'] == 'root_cause' else ' #' + str(c['index'])}: {c['status']}"
             + (f" (steps {c['steps']})" if c["steps"] else "") + (f". {c['note']}" if c["note"] else "") for c in bad]
    return ("A reviewer checked your report against the tool results and found claims the evidence doesn't back:\n"
            + "\n".join(lines) + (f"\nReviewer notes: {crit['notes']}" if crit["notes"] else "")
            + "\nUse tools to check these claims if needed, then call submit_report again. Keep only evidence you "
            "saw in a tool result, and lower confidence if the cause is still uncertain.")


def headline(c: dict | None) -> str:
    if not c:
        return ""
    removed = len(c.get("removed_evidence", []))
    text = f"{removed} claim{'s' if removed != 1 else ''} removed" if removed else f"{c['supported']}/{c['total']} claims supported"
    if c.get("contradicted"):
        text += ", contradiction found, confidence lowered"
    elif any(cl["kind"] == "root_cause" and cl["status"] == "unsupported" for cl in c.get("claims", [])):
        text += ", root cause unsupported"
    if c.get("rounds", 1) > 1:
        text += " (after 1 revision)"
    return f"Critic: {text}"
