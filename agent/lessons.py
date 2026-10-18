"""Loki's lessons notebook: short, general lessons learned from verified mistakes.

Grounding rule: a lesson is only ever created from a verified signal:
  - 'reject'    a human rejected a report and said why,
  - 'eval_miss' scripts/eval.py graded a report wrong against the ground truth,
  - 'manual'    a human wrote it (POST /lessons).
Never from Loki's own unconfirmed reports (that is how it used to repeat its mistakes).
'reject' and 'eval_miss' lessons start inactive until a human approves them.

Env:
  LOKI_LESSONS        1/0, default 1.
  LOKI_LESSONS_MODEL  model that phrases eval-miss lessons, default claude-haiku-4-5; "fake" = template, $0.
"""
import json
import os
import re

SOURCES = ("reject", "eval_miss", "manual")
MAX_IN_BRIEFING = 3
DUPLICATE_SIMILARITY = 0.75
MAX_TEXT = 400

STOP = set("""the and that this with from have were when then than into only also about after before because
which while what your their there they them been being does doesn't loki loki's report said blamed check
should would could other more most some just like over under same each every incident cause root""".split())

_TOKEN = re.compile(r"[a-z][a-z0-9_-]{2,}")


def enabled() -> bool:
    return os.environ.get("LOKI_LESSONS", "1").strip().lower() not in ("0", "false", "no", "off")


def model() -> str:
    return os.environ.get("LOKI_LESSONS_MODEL", "claude-haiku-4-5")


def tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 1.0 if (a or "").strip().lower() == (b or "").strip().lower() else 0.0
    return len(ta & tb) / len(ta | tb)


def _norm_list(items) -> list[str]:
    return sorted({str(i).strip().lower() for i in (items or []) if str(i).strip()})


# ---------- creating ----------

def create(conn, text: str, created_from: str, services=None, tags=None, source_investigation: int | None = None,
           confirmed_by: str | None = None) -> dict:
    """Insert a lesson unless a near-identical one exists. Returns {"id", "duplicate_of"?, "active"}."""
    if created_from not in SOURCES:
        raise ValueError(f"lessons come only from verified sources {SOURCES}, not {created_from!r}")
    text = " ".join((text or "").split())[:MAX_TEXT]
    if len(text) < 10:
        raise ValueError("lesson text is too short")
    services, tags = _norm_list(services), _norm_list(tags)
    for row in conn.execute("SELECT id, text, services, tags, active FROM lessons ORDER BY id DESC LIMIT 500").fetchall():
        if similarity(text, row["text"]) >= DUPLICATE_SIMILARITY:
            # Same lesson again: widen where it applies instead of adding a copy.
            merged_s, merged_t = sorted(set(row["services"] or []) | set(services)), sorted(set(row["tags"] or []) | set(tags))
            conn.execute("UPDATE lessons SET services = %s, tags = %s WHERE id = %s", (merged_s, merged_t, row["id"]))
            return {"id": row["id"], "duplicate_of": row["id"], "active": row["active"]}
    active = created_from == "manual"
    row = conn.execute(
        "INSERT INTO lessons (text, services, tags, source_investigation, created_from, confirmed_by, active) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (text, services, tags, source_investigation, created_from, confirmed_by if active else None, active),
    ).fetchone()
    return {"id": row["id"], "active": active}


def _scope(conn, inv_id: int, report: dict) -> tuple[list[str], list[str]]:
    """Services and alert names an investigation was about: where its lesson applies."""
    rows = conn.execute("SELECT alert_name, service FROM incidents WHERE investigation_id = %s", (inv_id,)).fetchall()
    services = {r["service"] for r in rows if r.get("service")} | set(report.get("affected_services") or [])
    if report.get("root_cause_service"):
        services.add(report["root_cause_service"])
    tags = {r["alert_name"] for r in rows if r.get("alert_name")}
    return sorted(services), sorted(tags)


def from_reject(conn, inv: dict, reason: str | None, who: str | None) -> dict | None:
    """A human said the report was wrong and why. No reason, no lesson: we'd only be guessing."""
    reason = " ".join((reason or "").split())
    if not reason:
        return None
    r = inv.get("report") or {}
    blamed = r.get("trigger") or "cause"
    where = f" in {r['root_cause_service']}" if r.get("root_cause_service") else ""
    text = f"A report blaming a {blamed}{where} was rejected by a human: {reason}"
    services, tags = _scope(conn, inv["id"], r)
    return create(conn, text, "reject", services, tags, inv["id"], who)


# ---------- eval misses ----------

EVAL_SYSTEM = """You write one lesson for an AI incident investigator that got an incident wrong.
You get its report, the checks it failed, and the right answer. Write 1-2 sentences of general advice \
that would have avoided this mistake on a similar incident: what to check, or what not to assume. \
Do not mention deploy ids, commit hashes, versions, scenario names, investigation numbers or the \
specific right answer; describe the pattern instead."""


def _truth_strings(truth: dict, scenario: str | None) -> set[str]:
    out = {scenario} if scenario else set()
    stack = [truth]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack += list(v.values())
        elif isinstance(v, list):
            stack += v
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out.add(str(v))
    # Short or generic values (e.g. "payments-svc", "deploy") are fine in a lesson; ids aren't.
    return {s for s in out if len(s) >= 4}


_ID_LIKE = re.compile(r"\b(?=[a-z0-9_-]*\d)[a-z0-9_-]{6,}\b", re.I)  # hashes, ids, versions


def scrub(text: str, truth: dict, scenario: str | None, keep: set[str] = frozenset()) -> str:
    """Remove anything from the ground truth that is specific, plus anything id-like."""
    for s in sorted(_truth_strings(truth, scenario), key=len, reverse=True):
        if s.lower() in {k.lower() for k in keep}:
            continue
        text = re.sub(re.escape(s), "[specific]", text, flags=re.I)
        # scenario names also appear with spaces instead of underscores
        text = re.sub(re.escape(s.replace("_", " ")), "[specific]", text, flags=re.I)
    text = _ID_LIKE.sub(lambda m: m.group(0) if m.group(0).lower() in {k.lower() for k in keep} else "[id]", text)
    return " ".join(text.split())


def _fake_eval_lesson(report: dict, failed: list[str]) -> str:
    hints = {
        "right_deploy": "When blaming a deploy, match the diff to the error lines before naming it.",
        "no_deploy_blamed": "A recent deploy is not proof: if its diff doesn't explain the errors, check dependencies and the database.",
        "right_cause_type": "Check dependency latency before calling an incident a deploy problem.",
        "right_service": "The service raising errors is often a victim; follow the calls to the service that started it.",
        "right_action": "Pick the action that undoes the actual cause.",
    }
    picked = [hints.get(f, "") for f in failed if hints.get(f)]
    return " ".join(picked[:2]) or "Confirm every link of the causal chain with a tool result before reporting."


def write_eval_lesson(report: dict, truth: dict, checks: dict, scenario: str | None, client=None) -> tuple[str, float]:
    """Ask a cheap model for a general lesson from report vs truth. Returns (text, cost_usd)."""
    failed = [k for k, v in (checks or {}).items() if not v]
    m = model()
    cost = 0.0
    if m == "fake":
        text = _fake_eval_lesson(report, failed)
    else:
        import anthropic
        import investigator
        client = client or anthropic.Anthropic()
        prompt = (f"## Its report\n{json.dumps({k: report.get(k) for k in ('summary', 'root_cause', 'root_cause_service', 'trigger', 'evidence')})}\n\n"
                  f"## Failed checks\n{', '.join(failed) or 'none listed'}\n\n## Right answer\n{json.dumps(truth)}")
        resp = client.messages.create(model=m, max_tokens=300, system=EVAL_SYSTEM,
                                      messages=[{"role": "user", "content": prompt}])
        text = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        cost = investigator._cost(m, resp.usage)[0]
    keep = set(report.get("affected_services") or []) | {report.get("root_cause_service") or ""}
    return scrub(text, truth, scenario, keep - {""}), cost


# ---------- retrieval ----------

def score(lesson: dict, services: set[str], alerts: set[str], text_tokens: set[str]) -> int:
    s = 3 * len(set(lesson.get("services") or []) & services)
    s += 2 * len(set(lesson.get("tags") or []) & alerts)
    s += min(3, len(tokens(lesson["text"]) & text_tokens))
    return s


def relevant(conn, incidents: list[dict], briefing_text: str, before=None, limit: int = MAX_IN_BRIEFING) -> list[dict]:
    if before is None:
        rows = conn.execute("SELECT id, text, services, tags FROM lessons WHERE active").fetchall()
    else:  # backtests: only lessons that existed at the replay time
        rows = conn.execute("SELECT id, text, services, tags FROM lessons WHERE active AND created_at <= %s",
                            (before,)).fetchall()
    services = {(i.get("service") or "").lower() for i in incidents} - {""}
    alerts = {(i.get("alert_name") or "").lower() for i in incidents} - {""}
    text_tokens = tokens(briefing_text)
    scored = [(score(r, services, alerts, text_tokens), r) for r in rows]
    # A lesson needs a service or alert match, or strong keyword overlap, to be shown.
    scored = [(s, r) for s, r in scored if s >= 3]
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return [r for _, r in scored[:limit]]


def briefing_section(conn, incidents: list[dict], briefing_text: str, before=None, count_use: bool = True) -> str:
    picked = relevant(conn, incidents, briefing_text, before)
    if not picked:
        return ""
    if count_use:
        conn.execute("UPDATE lessons SET uses = uses + 1 WHERE id = ANY(%s)", ([r["id"] for r in picked],))
    return "## Lessons from past mistakes (verified)\n" + "\n".join(f"- {r['text']}" for r in picked) + \
        "\nThese come from past wrong answers. Use them as things to check, not as conclusions."
