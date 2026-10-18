"""Loki as an MCP server: coding agents (Claude Code, Cursor, Codex) ask Loki what
broke in production and use the diagnosis to draft a fix. A human approves it.

Read-only by design: every tool is a GET against Loki's HTTP API. Nothing here
confirms, rejects or changes an investigation.

Run over stdio:  python mcp/loki_mcp.py
Env: LOKI_URL (default http://localhost:8088), LOKI_API_KEY (optional bearer token).

This folder has no __init__.py on purpose: a package named `mcp` would shadow
the MCP SDK it imports.
"""
import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every request to the client's stderr

DEFAULT_URL = "http://localhost:8088"
TIMEOUT_S = 15
MAX_DIFF_CHARS = 6000  # enough for a deploy's diff; the full one is a get_deploy_diff call away
START_HINT = (
    "Start it from the incident-agent repo with `docker compose up -d`, "
    "or set LOKI_URL to where Loki's agent API runs."
)


# ---------- HTTP ----------

class Loki:
    """A thin GET-only client for Loki's agent API."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, transport=None):
        self.base_url = (base_url or os.environ.get("LOKI_URL") or DEFAULT_URL).rstrip("/")
        key = api_key if api_key is not None else os.environ.get("LOKI_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.client = httpx.Client(base_url=self.base_url, headers=headers, timeout=TIMEOUT_S, transport=transport)

    def get(self, path: str, not_found: str | None = None):
        try:
            r = self.client.get(path)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ToolError(f"Loki is not reachable at {self.base_url}. {START_HINT}") from None
        except httpx.HTTPError as e:
            raise ToolError(f"Request to Loki at {self.base_url}{path} failed: {type(e).__name__}: {e}") from None
        if r.status_code == 404:
            if _detail(r) == "Not Found":  # FastAPI's own 404: the route itself is missing
                raise ToolError(f"Loki at {self.base_url} has no route for {path}: it is an older build. "
                                "Rebuild the agent (`docker compose up -d --build agent`) to get it.")
            raise ToolError(not_found or f"Loki has nothing at {path}.")
        if r.status_code in (401, 403):
            raise ToolError(f"Loki refused the request ({r.status_code}). Check LOKI_API_KEY.")
        if r.status_code >= 400:
            raise ToolError(f"Loki returned {r.status_code} for {path}: {_detail(r)}")
        return r.json()


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    return str(body.get("detail", body) if isinstance(body, dict) else body)[:300]


loki = Loki()  # tests swap this for one on an httpx.MockTransport


def _investigation(inv_id: int) -> dict[str, Any]:
    return loki.get(f"/investigations/{int(inv_id)}", not_found=f"Loki has no investigation #{inv_id}.")


def _diff(deploy_id: str) -> dict[str, Any]:
    return loki.get(f"/deploys/{quote(deploy_id, safe='')}/diff", not_found=f"Loki knows no deploy {deploy_id!r}.")


# ---------- extracting hints for the coding agent ----------

_EXT = r"(?:py|js|jsx|ts|tsx|go|java|kt|rb|rs|php|cs|c|cc|cpp|h|scala|swift|ex|exs)"
_FILE_LINE = re.compile(rf"(?<![\w/.-])([\w./-]+\.{_EXT}):(\d+)\b")
_PY_FRAME = re.compile(rf'File "([^"]+\.{_EXT})", line (\d+)')
# The message after "KeyError:" is a quoted value or the rest of the clause, not the whole sentence.
_EXCEPTION = re.compile(r"""\b([A-Z]\w*(?:Error|Exception|Exit|Interrupt|Timeout))\b(?::\s*('[^'\n]*'|"[^"\n]*"|[^\n"\\,;()]{1,80}))?""")


def file_line_hints(texts: list[str], source: str) -> list[dict[str, Any]]:
    """`charge.py:42` and Python `File "charge.py", line 42` mentions, in order, deduplicated."""
    seen, hints = set(), []
    for text in texts:
        for m in [*_FILE_LINE.finditer(text), *_PY_FRAME.finditer(text)]:
            key = (m.group(1), int(m.group(2)))
            if key not in seen:
                seen.add(key)
                hints.append({"file": key[0], "line": key[1], "source": source})
    return hints


def error_signature(texts: list[str]) -> str | None:
    """The first exception named in the evidence, e.g. "KeyError: 'currency'"."""
    for text in texts:
        m = _EXCEPTION.search(text)
        if m:
            return f"{m.group(1)}: {m.group(2).strip()}" if m.group(2) else m.group(1)
    return None


def diff_files(diff: str) -> list[dict[str, Any]]:
    """Per file in a unified diff: lines added/removed and the new-side line numbers that changed."""
    files, cur, new_line = [], None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            cur = {"file": path.removeprefix("b/"), "added": 0, "removed": 0, "changed_lines": []}
            files.append(cur)
        elif line.startswith("--- ") or cur is None:
            continue
        elif m := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line):
            new_line = int(m.group(1))
        elif line.startswith("+"):
            cur["added"] += 1
            cur["changed_lines"].append(new_line)
            new_line += 1
        elif line.startswith("-"):
            cur["removed"] += 1
        else:
            new_line += 1
    for f in files:
        f["changed_lines"] = _ranges(f["changed_lines"])
    return files


def _ranges(nums: list[int]) -> str:
    """[41, 42, 43, 47] -> "41-43, 47"."""
    out = []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in out)


# ---------- the fix brief ----------

def _verification(inv: dict) -> str:
    """Loki doesn't reproduce incidents yet; say so unless the report carries a verdict."""
    r = inv.get("report") or {}
    v = r.get("verification") or r.get("verified")
    if isinstance(v, dict):
        return str(v.get("summary") or v.get("status") or v)
    if v is True:
        return "verified"
    return str(v) if v else "not verified by reproduction"


def build_root_cause(inv: dict, deploy: dict | None, deploy_error: str | None, trace_texts: list[str]) -> dict[str, Any]:
    r = inv["report"]
    evidence, ruled_out = r.get("evidence") or [], r.get("ruled_out") or []
    diff = (deploy or {}).get("diff") or ""
    files = diff_files(diff) if diff else []
    observed = [*evidence, r.get("root_cause", "")]
    hints = file_line_hints(observed, "evidence") or file_line_hints(trace_texts, "trace")
    signature = error_signature(observed) or error_signature(trace_texts)
    result = {
        "investigation_id": inv["id"],
        "finished_at": inv.get("finished_at"),
        "model": inv.get("model"),
        "confidence": r.get("confidence"),
        "verification": _verification(inv),
        # What Loki observed in logs, metrics and deploys: facts, quoted from tool results.
        "evidence": {"observed": evidence, "ruled_out": ruled_out},
        # Loki's reading of that evidence: the diagnosis a fix must test, not a fact.
        "hypothesis": {
            "summary": r.get("summary"),
            "root_cause": r.get("root_cause"),
            "service": r.get("root_cause_service"),
            "trigger": r.get("trigger"),
            "deploy_id": r.get("deploy_id") or None,
            "affected_services": r.get("affected_services") or [],
            "suggested_action": r.get("suggested_action"),
            "action_detail": r.get("action_detail"),
        },
        "error_signature": signature,
        "file_line_hints": hints,
        "deploy": None,
    }
    if r.get("deploy_id"):
        result["deploy"] = {
            "deploy_id": r["deploy_id"],
            "service": (deploy or {}).get("service"),
            "version": (deploy or {}).get("version"),
            "files_changed": files,
            "diff": diff[:MAX_DIFF_CHARS] + ("\n... [truncated: call get_deploy_diff]" if len(diff) > MAX_DIFF_CHARS else ""),
            **({"source": deploy["source"]} if deploy and deploy.get("source") else {}),
            **({"error": deploy_error} if deploy_error else {}),
        }
    if (inv.get("cache_hit") or "").startswith("exact:"):
        result["note"] = "This report was reused from an earlier, human-confirmed investigation of the same incident."
    result["fix_brief"] = fix_brief(result)
    return result


def fix_brief(rc: dict) -> str:
    h, d = rc["hypothesis"], rc["deploy"]
    conf = rc["confidence"]
    lines = [
        f"Loki investigation #{rc['investigation_id']}: confidence {conf:.0%}, {rc['verification']}."
        if isinstance(conf, (int, float)) else f"Loki investigation #{rc['investigation_id']}: {rc['verification']}.",
        f"Suspected service: {h['service'] or 'unknown'} (trigger: {h['trigger'] or 'unknown'}).",
        f"Hypothesis (Loki's diagnosis, not a fact): {h['root_cause']}",
    ]
    if rc["error_signature"]:
        lines.append(f"Error signature (observed): {rc['error_signature']}")
    if d:
        changed = "; ".join(f"{f['file']} +{f['added']}/-{f['removed']} (new lines {f['changed_lines'] or 'none'})"
                            for f in d["files_changed"])
        lines.append(f"Deploy {d['deploy_id']}" + (f" ({d['version']})" if d.get("version") else "") +
                     f": {changed or d.get('error') or 'no diff available'}")
    if rc["file_line_hints"]:
        lines.append("File/line hints (from Loki's evidence and trace): " + ", ".join(f"{x['file']}:{x['line']}" for x in rc["file_line_hints"]))
    lines += ["Evidence (observed):", *[f"- {e}" for e in rc["evidence"]["observed"]]]
    if rc["evidence"]["ruled_out"]:
        lines += ["Ruled out:", *[f"- {e}" for e in rc["evidence"]["ruled_out"]]]
    lines += [
        f"Loki's suggested action for on-call: {h['suggested_action']}. {h['action_detail']}",
        "What a fix must achieve: " + _goal(rc),
        ("Before trusting the hypothesis, reproduce it locally (ideally as a failing test). "
         "Do not deploy or touch production; a human reviews the change."),
    ]
    return "\n".join(lines)


def _goal(rc: dict) -> str:
    h = rc["hypothesis"]
    sig = f" no longer raises {rc['error_signature']}" if rc["error_signature"] else " stops failing"
    goal = f"{h['service'] or 'the affected service'}{sig} on the inputs seen in production"
    if rc["deploy"]:
        goal += f", keeping what deploy {rc['deploy']['deploy_id']} meant to add where it is safe"
    return goal + ", with a regression test that fails before the fix and passes after."


# ---------- MCP tools ----------

mcp = FastMCP(
    "loki",
    instructions=(
        "Loki investigates production incidents and reports a root cause with evidence. "
        "Use get_root_cause for a diagnosis and fix brief, get_evidence to see how Loki got there, "
        "and get_deploy_diff for the suspect change. All tools are read-only. Loki's root cause is "
        "a hypothesis: verify it in the code before fixing, and never deploy or change production."
    ),
)


@mcp.tool()
def list_incidents(limit: int = 10) -> list[dict[str, Any]]:
    """Recent production investigations by Loki (newest first): id, status, summary,
    confidence, alerts and time. Backtests (replays) are left out."""
    limit = max(1, min(int(limit), 20))  # Loki's API returns the latest 20
    invs = [i for i in loki.get("/investigations") if i.get("mode", "live") == "live"][:limit]
    alerts: dict[int, list[str]] = {}
    for inc in loki.get("/incidents"):
        alerts.setdefault(inc.get("investigation_id"), []).append(f"{inc['alert_name']} on {inc['service']}")
    out = []
    for i in invs:
        # Confidence lives in the full report; only finished investigations have one.
        conf = (_investigation(i["id"]).get("report") or {}).get("confidence") if i["status"] == "done" else None
        out.append({
            "id": i["id"], "status": i["status"], "summary": i.get("summary"), "confidence": conf,
            "alerts": alerts.get(i["id"], []), "created_at": i.get("created_at"),
            "finished_at": i.get("finished_at"), "error": i.get("error"),
        })
    return out


@mcp.tool()
def get_root_cause(investigation_id: int | None = None) -> dict[str, Any]:
    """Loki's root cause for an incident, with a fix brief for a coding agent: suspected
    service, deploy and diff summary, file:line hints, error signature, evidence vs
    hypothesis, confidence. Omit the id for the latest finished production incident."""
    note = None
    if investigation_id is None:
        invs = [i for i in loki.get("/investigations") if i.get("mode", "live") == "live"]
        done = next((i for i in invs if i["status"] == "done"), None)
        if done is None:
            raise ToolError("Loki has no finished investigation yet.")
        if invs[0]["id"] != done["id"]:
            note = f"Investigation #{invs[0]['id']} is {invs[0]['status']}; showing the latest finished one."
        investigation_id = done["id"]
    inv = _investigation(investigation_id)
    if inv["status"] != "done" or not inv.get("report"):
        return {"investigation_id": inv["id"], "status": inv["status"], "error": inv.get("error"),
                "message": f"Investigation #{inv['id']} is {inv['status']}; no root cause yet."}
    trace = loki.get(f"/investigations/{inv['id']}/trace")
    deploy, deploy_error = None, None
    if inv["report"].get("deploy_id"):
        try:
            deploy = _diff(inv["report"]["deploy_id"])
        except ToolError as e:  # still worth returning the diagnosis, with the diff Loki saw if any
            deploy = _diff_from_trace(trace, inv["report"]["deploy_id"])
            deploy_error = None if deploy else str(e)
    result = build_root_cause(inv, deploy, deploy_error, [s.get("output") or "" for s in trace])
    if note:
        result["note"] = note + (" " + result["note"] if result.get("note") else "")
    return result


def _diff_from_trace(trace: list[dict], deploy_id: str) -> dict | None:
    """The diff Loki itself fetched during the investigation. The trace cuts outputs
    at 1500 chars, so a long diff won't parse and we return None."""
    for s in trace:
        if s["tool"] == "git_diff" and (s.get("input") or {}).get("deploy_id") == deploy_id:
            try:
                return {**json.loads(s["output"]), "source": "investigation trace"}
            except (TypeError, ValueError):
                return None
    return None


@mcp.tool()
def get_evidence(investigation_id: int) -> list[dict[str, Any]]:
    """Every step Loki took in an investigation: the briefing it started from, each
    tool call (logs, metrics, deploys, diffs) with its input and output, and the report.
    Outputs are cut at 1500 characters."""
    trace = loki.get(f"/investigations/{int(investigation_id)}/trace",
                     not_found=f"Loki has no investigation #{investigation_id}.")
    # 'llm' rows only record a stop reason; the tool calls carry the evidence.
    return [{"step": s["step"], "tool": s["tool"], "input": s.get("input"), "output": s.get("output")}
            for s in trace if s["tool"] != "llm"]


@mcp.tool()
def get_deploy_diff(deploy_id: str) -> dict[str, Any]:
    """The code diff a deploy shipped (secrets redacted), with per-file added/removed
    counts and the changed line numbers."""
    d = _diff(deploy_id)
    return {**d, "files_changed": diff_files(d.get("diff") or "")}


@mcp.prompt()
def fix_incident(investigation_id: str = "") -> str:
    """Find what broke in production with Loki and draft a minimal, reviewed fix."""
    which = f"investigation #{investigation_id}" if investigation_id else "the latest finished incident"
    arg = f"investigation_id={investigation_id}" if investigation_id else "no arguments"
    return f"""Use Loki to find what broke in production and draft a fix for {which}.

1. Call the Loki tool get_root_cause with {arg}. Read the fix brief. Treat "evidence" as
   observed facts and "hypothesis" as Loki's diagnosis, which may be wrong. Note the confidence
   and whether it was verified by reproduction.
2. If a deploy is named, call get_deploy_diff and read the change. Open the files and lines
   it touched and any file:line hints in this repository. Call get_evidence if you need to see
   how Loki reached its conclusion.
3. Check the hypothesis against the code. If the code does not support it, stop and say why
   instead of forcing a fix.
4. Write a regression test that reproduces the failure (it should fail on the current code),
   then propose the smallest fix that makes it pass without dropping what the deploy intended.
5. Run the tests. Summarise the root cause, the fix, the test, and anything still uncertain.

Rules: do not deploy, roll back, restart or change production or any shared environment, and
do not confirm or reject the investigation in Loki. Leave the change for a human to review."""


if __name__ == "__main__":
    mcp.run()  # stdio
