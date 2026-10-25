"""Loki's investigator: a Claude tool-use loop that finds the root cause of an incident.

Built to spend as little as possible:
1. Exact cache: a confirmed answer for the same incident (same fingerprint and
   suspect deploy) is reused for $0.
2. Hint cache: an earlier answer for a similar incident goes into the briefing,
   so the model can confirm it in a step or two.
3. The briefing already ranks suspects with plain code, so the model verifies
   instead of searching.
4. A cheap model by default, hard budgets per investigation and per day, and a
   step limit. Every model call's tokens and cost are logged.
"""
import json
import logging
import os
import re
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import anthropic

import context
import critic
import db
import lessons
import slack
import telemetry as tm
import tenant
import tools
import verifier

log = logging.getLogger("agent")

MODEL = os.environ.get("LOKI_MODEL", "claude-haiku-4-5")
# Optional: re-run with a stronger model when the cheap one is unsure. Off unless set.
ESCALATE_MODEL = os.environ.get("LOKI_ESCALATE_MODEL", "")
ESCALATE_BELOW = float(os.environ.get("LOKI_ESCALATE_BELOW", "0.6"))
MAX_STEPS = int(os.environ.get("LOKI_MAX_STEPS", "8"))
MAX_COST_USD = float(os.environ.get("LOKI_MAX_COST_USD", "0.10"))  # per investigation
DAILY_BUDGET_USD = float(os.environ.get("LOKI_DAILY_BUDGET_USD", "1.00"))
CACHE_TTL_DAYS = 7
# Set LOKI_OLLAMA_URL to a local Ollama server (it speaks the same API) to run
# open-source models for free. Claude-only features are switched off there.
OLLAMA_URL = os.environ.get("LOKI_OLLAMA_URL", "")
LOCAL = bool(OLLAMA_URL)

# USD per million tokens: input, output, cache write, cache read.
PRICES = {
    "claude-opus-5": (5.0, 25.0, 6.25, 0.5),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.2),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.1),
}

SYSTEM = """You are Loki, an on-call engineer investigating a live production incident.

You get a briefing with the alerts, a before-and-after of key metrics, the most common error log \
patterns, recent deploys, and suspects ranked by simple rules. Find the root cause: the one change \
or failure that started everything, not the symptoms it caused downstream.

How to work:
- Start from the top suspect and try to confirm or rule it out with the fewest tool calls. The \
ranking is a heuristic; overturn it when the evidence says so.
- Alerts on several services often share a single cause. A service reporting failures calling \
another service is usually a victim, not the cause.
- A deploy is only the cause if its change explains the errors. Read its diff and match the change \
to the error lines. Timing alone is not proof.
- Not every incident is a deploy. Slow responses with no matching code change point to a database, \
a dependency, or load; check dependency latency.
- Every link in your causal chain needs evidence from a tool result or the briefing. Don't propose a \
mechanism (connection pools, memory leaks, cache effects) that no evidence shows.
- Evidence must come from this incident's time window. Errors that stopped before this incident began, \
and deploys that were already replaced or reverted, belong to earlier incidents and are not the cause.
- If the briefing includes a similar past incident, check whether the same cause applies now \
before reusing its conclusion.
- If you have capture_variables, use it only to confirm a hypothesis about a bad value at a specific \
line (e.g. a KeyError in a diff you read). Never use it when logs or the diff already answer the question.
- You have read-only access. A human acts on your report.

When you have enough evidence, call submit_report once. Only state facts you saw in the briefing or \
a tool result, and quote specific numbers and log lines. Set confidence honestly: lower it when the \
evidence is thin or two causes fit equally well. Most incidents need 2 to 5 tool calls."""


# Briefing hint from a past incident matched by meaning: only when it is clearly close.
HINT_MIN_SCORE = float(os.environ.get("LOKI_HINT_MIN_SIMILARITY", "0.75"))


class BudgetExceeded(Exception):
    pass


# ---------- model calls ----------

def _price(model: str):
    if LOCAL or model == "fake":
        return (0, 0, 0, 0)
    return PRICES.get(model, PRICES["claude-opus-5"])


def _cost(model: str, usage) -> tuple[float, int, int]:
    p_in, p_out, p_write, p_read = _price(model)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (usage.input_tokens * p_in + usage.output_tokens * p_out + cache_write * p_write + cache_read * p_read) / 1e6
    return cost, cache_read, cache_write


def _call(client, model: str, messages: list):
    if model == "fake":
        return _fake_response(messages)
    if LOCAL:
        # Local open-source model through Ollama: plain request, no Claude-only extras.
        return client.messages.create(model=model, max_tokens=8000, system=SYSTEM, tools=tools.available_tools(), messages=messages)
    if model.startswith("claude-haiku"):
        # Haiku 4.5: no effort setting and no server-side fallbacks. Caching kicks in
        # once the prompt passes Haiku's 4,096-token minimum (later steps).
        return client.messages.create(
            model=model, max_tokens=8000, system=SYSTEM, tools=tools.available_tools(), messages=messages,
            cache_control={"type": "ephemeral"},
        )
    # Opus/Sonnet: adaptive thinking is on by default. fallbacks="default" re-runs a
    # request on Anthropic's recommended model if the safety classifiers decline it.
    return client.beta.messages.create(
        model=model, max_tokens=16000, system=SYSTEM, tools=tools.available_tools(), messages=messages,
        cache_control={"type": "ephemeral"},
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


# ---------- fake model: tests the whole pipeline for $0 ----------

_fake_state: dict[int, dict] = {}


def _fake_response(messages: list):
    """A scripted 'model' that follows the rule-based suspects. No API calls."""
    key = id(messages)
    state = _fake_state.setdefault(key, {"step": 0, "suspects": _fake_state.get("next_suspects", [])})
    state["step"] += 1
    top = state["suspects"][0] if state["suspects"] else {"kind": "non-deploy", "service": "unknown"}

    def tool_use(name, args):
        return SimpleNamespace(type="tool_use", id=f"fake_{state['step']}_{name}", name=name, input=args)

    if state["step"] == 1:
        block = tool_use("recent_deploys", {"minutes": 60})
    elif state["step"] == 2 and top["kind"] == "deploy":
        block = tool_use("git_diff", {"deploy_id": top["deploy_id"]})
    else:
        is_deploy = top["kind"] == "deploy"
        block = tool_use("submit_report", {
            "summary": f"[fake model] {top['service']} looks like the source" + (f" after deploy {top['deploy_id']}" if is_deploy else ""),
            "root_cause": "Fake-model report built from the rule-based suspects; for pipeline testing only.",
            "root_cause_service": top["service"],
            "trigger": "deploy" if is_deploy else "unknown",
            "deploy_id": top.get("deploy_id", ""),
            "affected_services": [top["service"]],
            "evidence": top.get("reasons", []),
            "ruled_out": [],
            "confidence": 0.5,
            "suggested_action": "rollback" if is_deploy else "escalate",
            "action_detail": "Pipeline test only.",
        })
    usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(content=[block], stop_reason="tool_use", stop_details=None, usage=usage)


# ---------- the loop ----------

def _trace(conn, inv_id: int, step: int, tool: str, args, output: str, model=None, usage=None, cost=None):
    cost_info = _cost(model, usage) if usage is not None else (None, None, None)
    conn.execute(
        "INSERT INTO trace_steps (investigation_id, step, tool, input, output, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (inv_id, step, tool, json.dumps(args), output[:20000], model,
         getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
         cost_info[1], cost_info[2], cost),
    )


# All tenants together, paid by the operator. Unset: no cap across tenants.
TOTAL_DAILY_BUDGET_USD = float(os.environ.get("LOKI_TOTAL_DAILY_BUDGET_USD", "0") or 0)


def _max_cost() -> float:
    """Per-investigation cap: the tenant's own, else the server's."""
    return MAX_COST_USD if tenant.current().is_default else float(tenant.setting("LOKI_MAX_COST_USD") or MAX_COST_USD)


def _daily_budget() -> float:
    return DAILY_BUDGET_USD if tenant.current().is_default else float(tenant.setting("LOKI_DAILY_BUDGET_USD") or DAILY_BUDGET_USD)


def _spent_today_all_tenants() -> float:
    with db.connect(admin=True) as conn:
        row = conn.execute("SELECT coalesce(sum(cost_usd), 0) AS spent FROM investigations "
                           "WHERE created_at >= date_trunc('day', now())").fetchone()
    return float(row["spent"])


def _spent_today(conn) -> float:
    row = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) AS spent FROM investigations WHERE created_at >= date_trunc('day', now())"
    ).fetchone()
    return float(row["spent"])


def clean_service(value, known: set[str]) -> str:
    """The report's service field must be a name, not prose: models sometimes write
    'cart (due to Redis infrastructure failure'. Keep a known name the text starts
    with (or contains, earliest first); otherwise its first word."""
    text = str(value or "").strip()
    if not text or text in known:
        return text
    found = [(m.start(), -len(name), name) for name in known
             for m in [re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", text)] if m]
    if found:
        return min(found)[2]
    first = re.match(r"[\w.:/-]+", text)
    return first.group(0) if first else text


def _run_loop(conn, client, inv_id: int, model: str, briefing: context.Briefing, budget: float,
              session: SimpleNamespace | None = None, max_steps: int | None = None):
    """Investigate with one model. Returns (report, steps, tokens_in, tokens_out, cost).

    session (optional) collects the conversation and every tool result, so the critic can
    check the report and the investigator can continue the same conversation for one revision.
    Pass a session that already has messages to continue it; max_steps then bounds the extra steps.
    """
    if model == "fake":
        _fake_state["next_suspects"] = briefing.suspects
    if session is None:
        session = SimpleNamespace(messages=None, observations=[], submit_id=None, step_base=0)
    if session.messages is None:
        session.messages = [{"role": "user", "content": briefing.text}]
        session.observations = [{"step": 0, "tool": "briefing", "input": {}, "output": briefing.text}]
    messages = session.messages
    limit = MAX_STEPS if max_steps is None else max_steps
    report, steps, tokens_in, tokens_out, cost = None, 0, 0, 0, 0.0

    while report is None:
        steps += 1
        last_chance = steps >= limit or cost >= budget * 0.7
        if steps > limit or cost >= budget:
            raise BudgetExceeded(f"stopped at step {steps - 1}, ${cost:.4f} spent (limit ${budget:.2f}, {limit} steps)")
        if last_chance:
            messages.append({"role": "user", "content": "You're at your step or budget limit. Call submit_report now "
                             "with what you have, and lower confidence to match the evidence."})

        resp = _call(client, model, messages)
        call_cost = _cost(model, resp.usage)[0]
        tokens_in += resp.usage.input_tokens
        tokens_out += resp.usage.output_tokens
        cost += call_cost
        step_no = session.step_base + steps
        _trace(conn, inv_id, step_no, "llm", {}, f"stop_reason={resp.stop_reason}", model, resp.usage, round(call_cost, 5))

        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model declined: {resp.stop_details}")
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "Call submit_report with your conclusion."})
            continue

        results = []
        for tu in tool_uses:
            if tu.name == "submit_report":
                report = dict(tu.input)
                report["root_cause_service"] = clean_service(report.get("root_cause_service"), briefing.services)
                session.submit_id = tu.id
                _trace(conn, inv_id, step_no, tu.name, tu.input, "report submitted")
                continue
            output, is_error = tools.run(tu.name, tu.input)
            _trace(conn, inv_id, step_no, tu.name, tu.input, output)
            session.observations.append({"step": len(session.observations), "tool": tu.name, "input": tu.input,
                                         "output": ("ERROR: " if is_error else "") + output})
            log.info("investigation #%s step %d: %s(%s)", inv_id, step_no, tu.name, json.dumps(tu.input))
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output, "is_error": is_error})
        if report is None:
            messages.append({"role": "user", "content": results})
        else:
            session.pending_results = results  # other tool calls made alongside submit_report

    session.step_base += steps
    _fake_state.pop(id(messages), None)
    return report, steps, tokens_in, tokens_out, cost


def investigate(inv_id: int, model: str | None = None) -> None:
    """model: override LOKI_MODEL for this investigation (backtests use it to run the free fake model)."""
    with db.connect() as conn:
        inv = conn.execute("SELECT mode, replay_at FROM investigations WHERE id = %s", (inv_id,)).fetchone()
    backtest = bool(inv) and inv["mode"] == "backtest"
    # A backtest runs with the clock set back to the replay time, so every query treats it as the present.
    with tm.replay(inv["replay_at"]) if backtest else nullcontext():
        _investigate(inv_id, model or MODEL, backtest)


def _investigate(inv_id: int, model: str, backtest: bool) -> None:
    with db.connect() as conn:
        over = None
        if _spent_today(conn) >= _daily_budget():
            over = f"daily budget of ${_daily_budget():.2f} reached"
        elif TOTAL_DAILY_BUDGET_USD and _spent_today_all_tenants() >= TOTAL_DAILY_BUDGET_USD:
            over = "the server's daily budget across all customers is reached"
        if over:
            conn.execute(
                "UPDATE investigations SET status = 'skipped', finished_at = now(), error = %s WHERE id = %s",
                (over, inv_id),
            )
            log.warning("investigation #%s skipped: %s", inv_id, over)
            return

        conn.execute("UPDATE investigations SET status = 'running', model = %s WHERE id = %s", (model, inv_id))
        incidents = conn.execute(
            "SELECT * FROM incidents WHERE investigation_id = %s ORDER BY started_at", (inv_id,)
        ).fetchall()
        # Scope to this incident: start after the previous incident was resolved (but never
        # later than 90s before this alert, since errors begin before an alert fires).
        first_alert = min(i["started_at"] for i in incidents)
        prev = conn.execute(
            "SELECT max(resolved_at) AS t FROM incidents WHERE resolved_at <= %s "
            "AND investigation_id IS DISTINCT FROM %s",
            (first_alert, inv_id),
        ).fetchone()["t"]
        floor = min(prev, first_alert - timedelta(seconds=90)) if prev else None
        tools.set_window_floor(floor)
        tools.set_investigation(inv_id)
        briefing = context.build(incidents, floor)
        conn.execute(
            "UPDATE investigations SET fingerprint = %s, cache_key = %s WHERE id = %s",
            (briefing.fingerprint, briefing.exact_key, inv_id),
        )
        log.warning("investigation #%s: %d alert(s), fingerprint %s", inv_id, len(incidents), briefing.exact_key)

        # 1. Exact cache: a confirmed answer for this same incident costs nothing.
        # Backtests skip both caches: a cached answer may come from after the replay
        # time, and the point of a backtest is what Loki would have worked out then.
        cached = None if backtest else conn.execute(
            "SELECT * FROM report_cache WHERE exact_key = %s AND confirmed AND expires_at > now()",
            (briefing.exact_key,),
        ).fetchone()
        if cached:
            conn.execute("UPDATE report_cache SET hits = hits + 1 WHERE exact_key = %s", (briefing.exact_key,))
            report = dict(cached["report"])
            report["summary"] = f"[Matched confirmed incident #{cached['investigation_id']}] {report['summary']}"
            conn.execute(
                "UPDATE investigations SET status = 'done', finished_at = now(), report = %s, steps = 0, "
                "cache_hit = %s, model = 'cache' WHERE id = %s",
                (json.dumps(report), f"exact:{cached['investigation_id']}", inv_id),
            )
            _trace(conn, inv_id, 0, "cache", {"exact_key": briefing.exact_key}, f"reused confirmed #{cached['investigation_id']}")
            _finish(inv_id, report, 0, 0.0)
            return

        # 2. Hint cache: a similar earlier incident becomes a lead in the briefing.
        similar = None if backtest else conn.execute(
            # Only human-confirmed answers become hints: an unconfirmed (possibly wrong)
            # answer would otherwise reinforce its own mistake next time.
            "SELECT * FROM report_cache WHERE fingerprint = %s AND confirmed AND expires_at > now() "
            "ORDER BY created_at DESC LIMIT 1",
            (briefing.fingerprint,),
        ).fetchone()
        cache_hit = None
        if similar:
            r = similar["report"]
            briefing.text += (
                f"\n\n## Similar past incident #{similar['investigation_id']} (confirmed by a human)\n"
                f"- Root cause then: {r['root_cause']}\n- Action then: {r['suggested_action']}: {r['action_detail']}\n"
                "Check whether the same cause applies now; don't assume it."
            )
            cache_hit = f"hint:{similar['investigation_id']}"
        elif not backtest:
            # No exact fingerprint match: look for a confirmed incident that *means* the same
            # thing (embeddings). A stricter bar than the search tool, since nobody asked for it.
            try:
                close = tools.similar_incidents(conn, briefing.search_text, limit=1, min_score=HINT_MIN_SCORE)
            except Exception:  # noqa: BLE001 - a hint is optional; never fail the investigation over it
                log.exception("investigation #%s: similar-incident lookup failed", inv_id)
                close = None
            if close:
                score, row = close[0]
                r = row["report"]
                briefing.text += (
                    f"\n\n## Possibly similar past incident #{row['id']} (confirmed by a human; "
                    f"similarity {score:.2f}, matched by meaning)\n"
                    f"- What it was: {r.get('summary', '')}\n- Root cause then: {r.get('root_cause', '')}\n"
                    f"- Action then: {r.get('suggested_action', '')}: {r.get('action_detail', '')}\n"
                    "It may be a different problem that looks alike. Check the evidence now; don't assume it."
                )
                cache_hit = f"hint:{row['id']}"
        # Lessons learned from verified mistakes (human rejects, eval misses, manual).
        # A backtest only sees lessons that existed at its replay time.
        if lessons.enabled():
            section = lessons.briefing_section(conn, incidents, briefing.text,
                                               before=tm.now() if backtest else None, count_use=not backtest)
            if section:
                briefing.text += "\n\n" + section
        _trace(conn, inv_id, 0, "briefing", {"fingerprint": briefing.exact_key, "cache": cache_hit}, briefing.text)

        # 3. Investigate with the cheap model; escalate only if it's unsure and budget allows.
        # Ollama ignores the key but the SDK needs one.
        client = (anthropic.Anthropic(api_key="ollama", base_url=OLLAMA_URL) if LOCAL
                  else None if model == "fake" else anthropic.Anthropic())
        session = SimpleNamespace(messages=None, observations=[], submit_id=None, step_base=0)
        report, steps, t_in, t_out, cost = _run_loop(conn, client, inv_id, model, briefing, _max_cost(), session)
        model_used = model
        # The fake model never escalates: a free run must stay free.
        if ESCALATE_MODEL and model != "fake" and report["confidence"] < ESCALATE_BELOW and cost < _max_cost():
            log.warning("investigation #%s: confidence %.2f, escalating to %s", inv_id, report["confidence"], ESCALATE_MODEL)
            strong = anthropic.Anthropic()
            session = SimpleNamespace(messages=None, observations=[], submit_id=None, step_base=steps)
            report, s2, i2, o2, c2 = _run_loop(conn, strong, inv_id, ESCALATE_MODEL, briefing, _max_cost() - cost, session)
            steps, t_in, t_out, cost, model_used = steps + s2, t_in + i2, t_out + o2, cost + c2, f"{model}+{ESCALATE_MODEL}"
            client, model = strong, ESCALATE_MODEL

        # 3b. Critic (LOKI_CRITIC): one cheap check of every claim against the tool results.
        if critic.enabled(model, LOCAL):
            report, s3, i3, o3, c3 = _critique(conn, client, inv_id, model, briefing, session, report, steps, cost)
            steps, t_in, t_out, cost = steps + s3, t_in + i3, t_out + o3, cost + c3

        # 4. Prove it (LOKI_VERIFY=1): reproduce a blamed deploy in a sandbox. Backtests
        # skip this: they replay what Loki would have said, not what it can rerun now.
        if not backtest:
            _verify(conn, inv_id, steps, report)

        conn.execute(
            "UPDATE investigations SET status = 'done', finished_at = now(), report = %s, steps = %s, input_tokens = %s, "
            "output_tokens = %s, cost_usd = %s, model = %s, cache_hit = %s WHERE id = %s",
            (json.dumps(report), steps, t_in, t_out, round(cost, 4), model_used, cache_hit, inv_id),
        )
        if backtest:
            log.warning("backtest #%s (as of %s) done\n%s", inv_id, tm.now().isoformat(), render(inv_id, report, steps, cost))
            return  # never cached and never posted to Slack: it's not a live incident
        # Remember the answer. It's reused as-is only after a human confirms it (POST /investigations/<id>/confirm).
        conn.execute(
            "INSERT INTO report_cache (exact_key, fingerprint, investigation_id, report, expires_at) "
            "VALUES (%s, %s, %s, %s, now() + make_interval(days => %s)) "
            # Never overwrite an answer a human confirmed.
            "ON CONFLICT (tenant_id, exact_key) DO UPDATE SET investigation_id = EXCLUDED.investigation_id, report = EXCLUDED.report, "
            "created_at = now(), expires_at = EXCLUDED.expires_at WHERE NOT report_cache.confirmed",
            (briefing.exact_key, briefing.fingerprint, inv_id, json.dumps(report), CACHE_TTL_DAYS),
        )
    _finish(inv_id, report, steps, cost)


def _critic_pass(conn, inv_id: int, step: int, report: dict, session) -> tuple[dict, float, int, int]:
    """One critic call. Returns (normalized critique, cost, tokens in, tokens out)."""
    cm = critic.model()
    client = None if cm == "fake" else anthropic.Anthropic()
    raw, usage = critic.call(client, cm, report, session.observations)
    c = _cost(cm, usage)[0]
    crit = critic.normalize(raw, report, session.observations)
    _trace(conn, inv_id, step, "critic", {"model": cm, "verdict": crit["verdict"]}, json.dumps(crit), cm, usage, round(c, 5))
    return crit, c, usage.input_tokens, usage.output_tokens


def _critique(conn, client, inv_id, model, briefing, session, report, steps, cost):
    """Critic, then at most one revision round and one re-critique. Never fails the investigation.
    Returns (report, extra steps, tokens in, tokens out, extra cost)."""
    extra_steps, t_in, t_out, spent = 0, 0, 0, 0.0
    if cost >= _max_cost():
        return report, 0, 0, 0, 0.0
    try:
        crit, c, i, o = _critic_pass(conn, inv_id, steps, report, session)
    except Exception as e:
        log.warning("investigation #%s: critic failed, report kept as is: %s", inv_id, e)
        return report, 0, 0, 0, 0.0
    spent, t_in, t_out = c, i, o
    final_report, final_crit, rounds, first = report, crit, 1, None
    steps_left = MAX_STEPS - steps
    budget_left = _max_cost() - cost - spent
    if crit["verdict"] == "revise" and steps_left > 0 and budget_left > 0 and session.submit_id:
        # The API needs a tool_result for the submit_report call: the critic's notes go there.
        session.messages.append({"role": "user", "content": [
            *getattr(session, "pending_results", []),
            {"type": "tool_result", "tool_use_id": session.submit_id, "content": critic.revision_prompt(crit)},
        ]})
        try:
            revised, s2, i2, o2, c2 = _run_loop(conn, client, inv_id, model, briefing, budget_left, session, steps_left)
            extra_steps, t_in, t_out, spent = s2, t_in + i2, t_out + o2, spent + c2
            crit2, c3, i3, o3 = _critic_pass(conn, inv_id, steps + s2, revised, session)
            spent, t_in, t_out = spent + c3, t_in + i3, t_out + o3
            first = {"verdict": crit["verdict"], "notes": crit["notes"],
                     "unsupported": sum(c_["status"] != "supported" for c_ in crit["claims"] if c_["status"] != "unjudged")}
            final_report, final_crit, rounds = revised, crit2, 2
        except Exception as e:  # out of steps/budget mid-revision: keep the first report and its critique
            log.warning("investigation #%s: revision stopped (%s); keeping the first report", inv_id, e)
    out = critic.apply(final_report, final_crit, {"model": critic.model(), "rounds": rounds,
                                                  **({"first_pass": first} if first else {})})
    log.warning("investigation #%s: %s", inv_id, critic.headline(out["critique"]))
    return out, extra_steps, t_in, t_out, spent


def _verify(conn, inv_id: int, steps: int, report: dict) -> None:
    """Attach the verifier's verdict to the report (in place). Never raises."""
    verification = verifier.verify_report(report)
    if verification is None:
        return  # verification is off
    report["verification"] = verification
    _trace(conn, inv_id, steps, "verify", {"deploy_id": report.get("deploy_id", "")}, json.dumps(verification))
    log.warning("investigation #%s: verification %s (%s)", inv_id, verification["status"], verification["reason"])


def _finish(inv_id: int, report: dict, steps: int, cost: float):
    text = render(inv_id, report, steps, cost)
    log.warning("investigation #%s done\n%s", inv_id, text)
    slack.post_report(inv_id, report, steps, cost, text)


def render(inv_id: int, r: dict, steps: int, cost: float) -> str:
    lines = [
        f"*Loki · Incident #{inv_id}*  ({r['trigger']}, confidence {r['confidence']:.0%})",
        f"*{r['summary']}*",
        "",
        f"*Root cause* ({r['root_cause_service']}" + (f", deploy {r['deploy_id']}" if r.get("deploy_id") else "") + f"): {r['root_cause']}",
        "",
        "*Evidence*",
        *[f"• {e}" for e in r["evidence"]],
    ]
    if r.get("ruled_out"):
        lines += ["", "*Ruled out*", *[f"• {e}" for e in r["ruled_out"]]]
    if r.get("critique"):
        lines += ["", f"_{critic.headline(r['critique'])}_"]
    v = r.get("verification")
    if v:
        lines += ["", f"*Verification:* {verifier.headline(v)}", *[f"• {e}" for e in v.get("evidence", [])]]
    lines += [
        "",
        f"*Suggested action: {r['suggested_action']}*. {r['action_detail']}",
        f"_Affected: {', '.join(r['affected_services'])} · {steps} steps · ${cost:.4f} · {verifier.label(v)}_",
    ]
    return "\n".join(lines)

