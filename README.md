# Loki: AI on-call engineer

Loki investigates production incidents. When alerts fire, it reads the logs,
metrics and recent deploys, finds the root cause, and reports it with evidence.
It has read-only access; a human acts on its report.

## What's here

| Path | What it is |
|---|---|
| `services/` | Demo system: `shop-api` → `payments-svc` → Postgres, plus fake customers (`loadgen`) |
| `chaos.py` | Breaks payments on purpose: `bad_deploy`, `slow_db`, `timeout`, `none` |
| `state/deploys.json` | Deploy history (what a CI system would record) |
| `observability/` | Prometheus, Alertmanager, Grafana Loki, Promtail, Grafana |
| `agent/` | Loki itself |

Inside `agent/`:

| File | Job |
|---|---|
| `main.py` | Receives alerts, groups an alert storm into one investigation; `POST /backtest` |
| `backtest.py` | Replays a past incident with the clock set back (see Backtesting) |
| `context.py` | Builds the briefing: error patterns, before/after metrics, recent deploys |
| `tools.py` | Read-only tools Claude can call: logs, metrics, deploys, diffs, past incidents |
| `investigator.py` | The model loop: caches, budgets, step limit, cost logging, report |
| `telemetry.py` | Talks to Grafana Loki, Prometheus and the deploy history |
| `metrics_config.py` | Which metric and log label names to query (presets, `LOKI_METRICS_CONFIG`) |
| `slack.py` | Slack reports with Confirm / Wrong / View trace buttons |
| `verifier.py` | Proves a blamed deploy by reproducing the failure in a sandbox (see Verification) |
| `sources/` | Pluggable deploy and error sources: JSON file, Grafana Loki, GitHub, Sentry, Datadog logs |
| `redact.py` | Removes secrets and personal data before anything reaches the model |

## Deploying for a customer (pilot)

One Loki per customer on a small VPS, with HTTPS, auth, backups and upgrades: see
[`deploy/PILOT_RUNBOOK.md`](deploy/PILOT_RUNBOOK.md) (`deploy/install.sh`, `loki doctor`).

## Run it

1. Add your Anthropic API key:
   ```bash
   cp .env.example .env
   ```
   Then open `.env` and paste your key after `ANTHROPIC_API_KEY=`. `.env` is git-ignored.
2. Start everything:
   ```bash
   docker compose up -d --build
   ```
3. Wait a minute for normal traffic, then break something:
   ```bash
   python3 chaos.py bad_deploy
   ```
4. About 90 seconds later, read Loki's report:
   ```bash
   curl localhost:8088/investigations/latest/report
   ```
5. Put things back:
   ```bash
   python3 chaos.py none
   ```

Try `slow_db` and `timeout` too. Those failures have no deploy behind them, so
Loki has to find a different kind of cause.

## How Loki keeps costs down

1. **Answer cache:** once a human confirms a report (`POST /investigations/<id>/confirm`),
   the exact same incident (same error fingerprint and same suspect deploy) is answered
   from cache for $0 with no model calls. A new deploy never reuses an old answer.
2. **Hint cache:** a similar earlier incident is added to the briefing as a lead.
3. **Rule-based suspects:** plain code ranks the likely cause before the model starts,
   so the model verifies instead of searching.
4. **Tool-result cache:** identical log/metric queries within 45 seconds aren't repeated.
5. **Cheap model by default** (Claude Haiku 4.5), optional escalation to a stronger one.
6. **Hard limits:** cost per investigation, cost per day, and a step limit.
7. **Fake model:** `LOKI_MODEL=fake` runs the whole pipeline with no API calls.

`localhost:8088/stats` shows spend, cache hits and tokens per model.

## Self-improvement

Loki checks and learns, but only from things that are grounded. Self-critique
with nothing to check against mostly agrees with itself (the "coherence trap"),
and learning from your own unverified answers repeats your mistakes. So:

**Critic (per investigation).** After Loki submits a report, one cheap call
(`LOKI_CRITIC_MODEL`, Haiku by default) gets the report plus a numbered list of
every tool result (step 0 is the briefing). It must mark the root cause and each
evidence line *supported* (citing step numbers), *unsupported* or *contradicted*,
through a single forced, strict-schema tool call. Then:

- a "supported" claim that cites no real step counts as unsupported;
- unsupported or contradicted evidence is dropped from the report and kept under
  `report.critique.removed_evidence` for audit;
- a contradiction lowers confidence by at least 0.2; the critic can never raise it;
- on *revise*, the investigator gets one more round with the critic's notes (within
  the same step and cost limits), then one re-critique. Never more.

The report and Slack show one line, e.g. `Critic: 5/5 claims supported` or
`Critic: 2 claims removed`. Critic calls are traced as `tool = critic` and count
toward the investigation's cost. Cache hits skip it. Typical cost with Haiku:
about 3-6k input and 300-600 output tokens, roughly $0.005-0.01 per call, so
$0.005-0.02 per investigation including a revision round.

**Lessons notebook.** Short, general lessons in the `lessons` table, created only from:

- a human ❌ reject **with a reason** (`POST /investigations/<id>/reject {"reason": ...}`);
- an eval miss: `scripts/eval.py` posts each run graded wrong to `POST /lessons/eval-miss`,
  where a cheap model writes a 1-2 sentence lesson from the report vs. the ground truth.
  Deploy ids, scenario names and other ground-truth values are scrubbed; the truth is never stored
  (`--no-lessons` turns this off);
- `POST /lessons {"text", "services", "tags"}` by a human.

Loki's own unconfirmed reports never create lessons. Reject and eval-miss lessons
start inactive: review them with `GET /lessons`, then `POST /lessons/<id>/approve`
or `/disable`. Near-identical lessons are merged. Up to 3 active lessons matching
the incident's services, alert names and error keywords go into the briefing under
"Lessons from past mistakes (verified)". Backtests only see lessons created before
the replay time. All of these endpoints use the normal API keys and audit log.

## Backtesting

Replay a past incident and see what Loki would have said at the time: "give me
your last 5 incidents and I'll show you what Loki would have found."

```bash
# Replay the alerts Loki recorded around 21:03 UTC today
python3 scripts/backtest.py --at 21:03 --model fake
# Name the alerts yourself (any ISO timestamp works too)
python3 scripts/backtest.py --at 2026-10-16T21:03:00Z --alerts "HighErrorRate:payments-svc,HighErrorRate:shop-api"
# Several incidents, graded when the file says what the right answer was
python3 scripts/backtest.py --incidents-file past.jsonl
```

`past.jsonl` has one incident per line (`expected` is optional; `"deploy": ""` means no deploy should be blamed):

```json
{"name": "currency deploy", "at": "2026-10-16T21:03:00Z", "alerts": "HighErrorRate:payments-svc", "expected": {"service": "payments-svc", "cause": "deploy", "deploy": "552a2c7"}}
{"name": "slow database", "at": "2026-10-16T22:10:00Z", "expected": {"service": "payments-svc", "cause": "dependency|infrastructure", "deploy": ""}}
```

The script calls `POST /backtest {at, alerts, model, delay_s}`, which runs the investigation
synchronously and returns it. `--model fake` costs nothing; otherwise the agent's `LOKI_MODEL`
is used, and backtest spend counts toward the daily budget like any other.

**The honesty guarantee: nothing after the replay time reaches Loki.** Loki's clock is set to
the replay time, which is the alert time plus `--delay` (60s by default, about when a live
investigation would start). The clock is a per-investigation context variable, so a live
investigation running at the same time keeps the real clock. Then:

- Every log, metric and dependency query has its end capped at the replay time, and every
  result is filtered again so nothing stamped later gets through, even if a backend returns
  more than it was asked for.
- Deploys after the replay time don't exist, and neither do their diffs.
- No answer cache, no hint cache, and past-incident search only sees live incidents that were
  solved and confirmed before the replay time.
- The tool-result cache is keyed by the replay time, so live and replayed results never mix.

`tests/test_backtest.py` checks this with fake backends that return future data on purpose.

Backtests are stored with `mode = 'backtest'`. They never join a live alert storm, never post to
Slack, can't be confirmed, and are left out of `/stats` (shown under `backtests`), the answer
cache, past-incident search and `/investigations/latest/report`.

Limits: the replay can only see what Grafana Loki and Prometheus still keep (retention), plus
deploys in `state/deploys.json`. If an earlier incident just before the replayed one isn't
recorded in Loki's database, its leftover errors aren't cut off and can skew the rule-based suspects.

## Evals and chaos engineer

`python3 scripts/eval.py` still runs the original three `chaos.py` modes (bad_deploy, slow_db,
timeout) with their original graders and table. Those are easy and all on one service, so there
is also a larger, harder scenario set written by a "chaos engineer".

**Faults.** Both demo services have a hidden `POST /chaos` switch (never logged, never counted
in metrics, never held in a request handler's local variables, and never on the source line
that raises an error, since tracebacks print it). `{"mode": ...}` keeps `chaos.py` working;
`{"faults": {name: params}}` sets any combination of:

| Fault | Service | What happens |
|---|---|---|
| `slow_db` | payments-svc | Postgres queries wait `seconds` (on a `rate` share of requests) |
| `card_hang` | payments-svc | the card network hangs `seconds` |
| `card_errors` | payments-svc | the card network answers `status` (5xx) on a `rate` share |
| `risk_regression` | payments-svc | a code bug fails only charges >= `min_amount` (partial failure) |
| `mem_leak` | payments-svc | receipt cache stops evicting: memory and latency creep (HighMemory alert) |
| `amount_field` | shop-api | shop-api sends `amount_minor`; payments rejects with 422 (blame looks like payments) |
| `payments_timeout_config` | shop-api | a config value makes the payments timeout `timeout_s` |
| `bad_product` | shop-api | one product's catalog row has a NULL price: only its orders fail |
| `cpu_burn` | shop-api | recommendation scoring burns CPU on every request |
| `log_flood` | shop-api | unrelated error lines (analytics, recs cache) flood the logs |

Any fault also takes `flap_s` to switch itself on and off (intermittent faults).

**Scenarios.** `evals/scenarios/{train,heldout}/*.json` (format documented at the top of
`scripts/scenario_spec.py`): faults and their timing, deploy records with real-looking diffs
(culprits and innocent red herrings), extra load (steady, burst, ramp), a difficulty, and
ground truth: the root-cause service, cause type, accepted `trigger` values, the culprit deploy
(or none), acceptable answers, keywords, and what the trap is. They are generated:

```bash
python3 scripts/chaos_engineer.py            # 45 scenarios: 30 train, 15 held out (seeded, deterministic)
python3 scripts/chaos_engineer.py --check    # fails if the committed files are stale
LOKI_CHAOS_LLM=1 python3 scripts/chaos_engineer.py --llm 5   # optional: Claude proposes extra ones (costs money)
```

15 templates (payments bad deploy, partial deploy regression, shop-api deploy blamed on payments,
config deploy, slow DB, innocent deploy just before a DB slowdown, card errors, card hang,
DB slowness surfacing as upstream timeouts, memory leak, CPU pressure, bad record, flapping,
two independent faults, log-noise flood) x 3 variants. The last variant of each goes to the held-out
set, so every kind of incident is in both sets with different parameters; use train to tune Loki
and held-out as the no-regression gate.

**Running them.**

```bash
python3 scripts/eval.py --set train                       # 1 run each by default
python3 scripts/eval.py --set heldout --difficulty hard --runs 2
python3 scripts/eval.py --files evals/scenarios/train/s03-shop-bad-deploy-a.json
python3 scripts/eval.py --set all --dry-run               # validate and list only
```

Each scenario is played by `scripts/scenario_runner.py` (deploys recorded in `state/deploys.json`,
faults switched on schedule, culprit deploys reverted afterwards) and graded by one generic grader
driven by the ground truth: right service, right cause type, right deploy (or none blamed), names
the cause, right action where it matters, and "not fooled" by the trap. A scenario with two faults
accepts either. The table in `evals/latest.md` adds accuracy, average partial score, times fooled,
time and cost by difficulty, by fault type and by split.

**Trying a scenario without Loki.** A small separate copy of the app (5 containers, ports 182xx):

```bash
docker compose -p loki-chaos -f evals/live/docker-compose.chaos.yml up -d --build
python3 scripts/scenario_runner.py evals/scenarios/train/s13-card-flapping-a.json --hold 120 \
    --shop-url http://localhost:18201 --payments-url http://localhost:18202 \
    --deploys-file /tmp/chaos-deploys.json --ground-truth-file /tmp/chaos-truth.jsonl
# Prometheus (metrics, alerts) on http://localhost:18290; logs with docker logs
docker compose -p loki-chaos -f evals/live/docker-compose.chaos.yml down -v --rmi local
```

Limits: faults are simulated inside the services (the database is slowed with `pg_sleep`, the card
network is a `sleep`), the memory leak needs extra load and a few minutes to cross the alert, and
the grader matches services and keywords, so a right answer worded very differently can be
marked wrong.

## Verification

Loki doesn't stop at a guess. When it blames a deploy, the verifier tries to
prove it: it runs the version that deploy shipped next to the version it
replaced, sends both the request that failed in production, and marks the root
cause:

| Verdict | Meaning | Shown as |
|---|---|---|
| `CONFIRMED` | The new version fails with the production error; the previous version handles the same request | ✅ Verified by reproduction |
| `NOT_REPRODUCED` | No such difference: both versions work, both fail, or the new one fails differently | ⚠️ Not reproduced |
| `INCONCLUSIVE` | The sandbox couldn't answer: no image recorded, no failed request, a Docker error, or the time cap | Unverified, with the reason |

Causes that aren't deploys (a slow database, a hanging dependency) stay
*Unverified* with a short reason: there is no version to roll back to and run.

**How it works**

1. *Real versions.* Every deploy record names the image it shipped (`image` in
   `state/deploys.json`). The demo builds one payments-svc image per release:
   `scripts/build_payments_images.sh` makes `loki-demo/payments-svc:v2.4.0`,
   `:v2.5.0` (the multi-currency `KeyError` bug from the deploy's diff) and
   `:v2.5.1` (the revert). The live demo still breaks through `chaos.py`, which
   switches the same v2.5.0 code path on at runtime, so nothing is redeployed.
2. *A failed request to replay.* When payments-svc answers 5xx it logs one
   structured sample of the request, at most once every 30 seconds:
   `failed request sample: {"method": "POST", "path": "/charge", "status": 500, "error": "KeyError: 'currency'", "body": {...}}`.
   Fields that look secret (card, cvv, token, password, api key...) are masked
   and non-JSON bodies are left out. The verifier reads the newest sample logged
   since the deploy from Grafana Loki.
3. *A sandbox.* A new Docker network created with `--internal` (no internet, no
   route to the live stack), a throwaway Postgres seeded with `db/init.sql`, and
   the two images, each capped at 256 MB. No secrets go in: the only setting is
   the sandbox database's own URL. Images are never pulled. The request is sent
   from inside each container, so no ports are published. Everything is named
   `loki-verify-<run id>` and is removed afterwards, also on errors and timeouts.
   The whole run is capped at 120 seconds (10 to 15 seconds on a laptop).
4. *The verdict* goes into the report as `verification: {status, reason, evidence, seconds}`,
   is shown in the report and in Slack, and is recorded as a `verify` step in the trace.

Try it by hand (no API key needed):

```bash
scripts/build_payments_images.sh
python3 agent/verifier.py --bad loki-demo/payments-svc:v2.5.0 --previous loki-demo/payments-svc:v2.4.0 \
  --request '{"method": "POST", "path": "/charge", "body": {"order_id": 1, "amount": 250.0}, "error": "KeyError: '"'"'currency'"'"'"}'
```

**Turning it on.** Verification is off unless `LOKI_VERIFY=1`.

| Variable | Default | Meaning |
|---|---|---|
| `LOKI_VERIFY` | off | `1` runs the verifier after every report that blames a deploy |
| `LOKI_VERIFY_TIMEOUT_S` | `120` | Hard cap per verification; the sandbox is torn down either way |
| `LOKI_VERIFY_IMAGE_TEMPLATE` | | Image for deploy records that name none, e.g. `loki-demo/{service}:{version}` (older records in `state/deploys.json` have no `image`) |
| `LOKI_VERIFY_POSTGRES_IMAGE` | `postgres:16` | Database image for the sandbox (must already be on the host) |
| `LOKI_VERIFY_INIT_SQL` | `db/init.sql` | Schema to seed the sandbox database with |

The verifier drives Docker with the `docker` CLI. Run on your Mac, that just
works. **Inside the agent container** it needs, which the default compose
file deliberately doesn't give it:

- the Docker socket: `- /var/run/docker.sock:/var/run/docker.sock` under the agent's `volumes`;
- the docker CLI in the image, e.g. `COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker` in `agent/Dockerfile`;
- the seed schema: `- ./db/init.sql:/app/db/init.sql:ro` plus `LOKI_VERIFY_INIT_SQL=/app/db/init.sql`.

(This in-container setup is documented, not yet tested; the verifier itself
has been run for real from the host.)

**Security caveat.** Access to the Docker socket is root on the host: whoever
controls the agent can start any container, mount any host path and read every
other container's secrets. Loki's own code only creates `--internal`
`loki-verify-*` networks and containers from images that are already present,
but a bug in the agent or a compromise of it would not be limited by that. Only
turn it on where the agent is trusted like an admin, preferably on a separate
Docker host or VM used just for verification, or behind a socket proxy that
allows only the container, network, image and exec endpoints (exec and create
are still powerful, so this narrows the risk rather than removing it).

**Limits.** It proves code bugs that a single replayable request triggers.
It can't reproduce infrastructure or data problems (a slow database, a full
disk, a bad row in production, load, a third-party outage), bugs that need
state built up over many requests, or requests whose masked fields mattered.
It needs both images on the Docker host, the service's image to include
`python3` (the request is sent from inside the container) and, for now, a
service that needs only Postgres. Outside those limits expect `INCONCLUSIVE` or
`NOT_REPRODUCED`: a missing confirmation is not evidence that the deploy is innocent.

## Live probes

Logs say *that* `charge()` raised `KeyError: 'currency'`; a live probe shows
*what* `payload` held when it did. Loki places a non-breaking probe on one
`file:line` of a running service, the service records the local variables the
next few times that line runs, and Loki removes the probe again. Nothing is
paused, nothing is redeployed, and the request still runs exactly as before
(the same idea as Lightrun or Rookout snapshots).

```
capture_variables(service="payments-svc", file="main.py", line=68, variables=["payload"])
→ {"hit_count": 1, "captures": [{"time": "...", "thread": "AnyIO worker thread",
   "locals": {"payload": {"order_id": 4242, "amount": 499.0}}, "stack": ["main.py:68 in charge", ...]}]}
```

That is a real capture from payments-svc in `bad_deploy` mode: there's no
`currency` key, so line 68 fails.

**How it works.** The probe library (`probes/python/loki_probe`, no
dependencies beyond Starlette for the endpoint) runs inside the service. On
Python 3.12+ it uses `sys.monitoring` LINE events enabled only on the code
objects that contain the probed line; older Pythons fall back to
`sys.settrace`. The service mounts a small control API, `POST/GET/DELETE
/_loki/probes`, and Loki's `capture_variables` tool calls it: place the probe,
poll until it has hits (about 2s after the first hit) or the wait runs out,
then delete it, on errors too.

**Safety model.**
- Off unless you turn it on, twice: the service only mounts the endpoint when
  `LOKI_PROBE_TOKEN` is set (no token, no routes), and Loki only offers the tool
  when `LOKI_PROBES=1` and `LOKI_PROBE_ENDPOINTS` names the service. Every
  request needs the `X-Loki-Probe-Token` header (compared in constant time).
- Read-only. No expressions are ever evaluated and no variable is ever set.
  Values are rendered without calling the app's code: no custom `__repr__`,
  `__eq__` or properties; other objects show their type and instance attributes.
  The only filter is `{"variable": "order_id", "equals": 42}` on a local, compared
  only when both sides are plain strings, numbers, booleans or null.
- Bounded. 60s by default (300s cap), 20 hits by default (Loki asks for 5),
  4 KB per hit and 64 KB per probe, nested values 3 levels deep, 20 items per
  container, 200 characters per string, at most 5 probes at once. A timer removes
  the probe at expiry even if Loki never comes back.
- Never breaks the app. Every callback swallows its own errors and counts them;
  a probe that fails 10 times removes itself.
- Cheap. With no probe active nothing is hooked at all. With a probe active,
  only its function is hooked: other lines in it are switched off after their
  first call, and other functions pay nothing (benchmark: 74 ns per call with
  no probe, 74 ns with a probe in another function, about 1.4 µs when the
  probed line runs and a condition rejects it).
- Scoped. Only `.py` files under `LOKI_PROBE_ROOTS` (default: the service's
  working directory) can be probed; absolute paths, `../` escapes and
  `site-packages` are refused.
- Redacted twice. Variables and keys named like secrets (`password`, `token`,
  `card`, `cvv`, `auth`, ...) are dropped, and strings go through the same
  patterns as `agent/redact.py` (a test fails if the two copies drift). The
  agent redacts the tool output again before the model sees it.
- Accountable and budgeted. Every probe writes an `audit_log` row
  (`probe:<service>:<file>:<line>`, actor `loki-agent`) *before* it is placed:
  no audit row, no probe. The call and its output are in the investigation's
  trace. Each investigation may place 3 probes (`LOKI_PROBE_MAX_PER_INVESTIGATION`)
  and wait 120s in total, 60s per probe at most. Results are never cached.
- Never in backtests: a probe reads the present, not the replayed past, so the
  tool isn't offered (and refuses) during a replay.

**Enable it.**

```bash
# .env (or your shell) for the demo stack
LOKI_PROBE_TOKEN=<a long random string>   # payments-svc mounts /_loki/probes; the agent sends it
LOKI_PROBES=1                             # the agent offers capture_variables
# LOKI_PROBE_ENDPOINTS=payments-svc=http://payments-svc:8000   # the compose default
docker compose up -d --build payments-svc agent
```

In your own service: copy or `pip install probes/python`, then
`from loki_probe.http import mount; mount(app)` in a FastAPI/Starlette app. The
payments-svc image gets the library from the `probes` build context in
`docker-compose.yml`; a plain `docker build` (e.g.
`scripts/build_payments_images.sh`) builds without it and the service runs as
before.

**Supported runtimes.** Python services (FastAPI/Starlette for the control
endpoint; the core works in any Python process). Node is planned: the service
would open its own inspector session in-process (`node:inspector`'s
`Session.connect()`), call `Debugger.setBreakpointByUrl` with a condition that
calls a fixed, non-mutating capture function and returns `false` so execution
never pauses (a logpoint), serve results through the same `/_loki/probes` API and token, and remove the
breakpoint at expiry. Same limits, same redaction patterns.

**Limits.** The line must be inside a function that is already loaded
(module-level code has run already). Values are captured just *before* the
line runs, so a variable assigned on that line isn't there yet. Only traffic
that reaches the line during the wait is seen: an idle path returns no hits.
In a service with several worker processes a probe lives in the one process
that served the request, so it only sees that process's traffic. On Python
3.12+ the probe needs a free `sys.monitoring` tool slot and calls
`sys.monitoring.restart_events()`, which can briefly re-arm events another
tool had switched off. The settrace fallback only follows threads started after
the probe and the thread that placed it.

## Useful endpoints

- `localhost:8088/investigations`: all investigations, with status, steps and cost
- `localhost:8088/investigations/<id>/report`: the root-cause report
- `localhost:8088/investigations/<id>/trace`: every tool call Loki made
- `POST localhost:8088/investigations/<id>/reject`: mark a report wrong (optional body `{"reason": "..."}`)
- `POST localhost:8088/investigations/manual`: start an investigation by hand (see Connecting your alerts)
- `localhost:8088/incidents`: alerts and which investigation they joined
- `localhost:8088/deploys/<deploy_id>/diff`: the diff a deploy shipped (redacted)
- `POST localhost:8088/backtest`: replay a past incident (see Backtesting)
- `localhost:3000`: Grafana (Explore → Loki for logs)

## Use Loki from Claude Code / Cursor

`mcp/loki_mcp.py` is an MCP server (stdio) that lets a coding agent ask Loki what
broke and use the diagnosis to draft a fix: Loki finds the root cause, the coding
agent writes the fix and a regression test, a human reviews and ships it. Every
tool is a read-only GET against the agent API; nothing confirms, rejects or
changes an investigation.

| Tool | What it returns |
|---|---|
| `list_incidents(limit=10)` | Recent production investigations: status, summary, confidence, alerts, time |
| `get_root_cause(investigation_id=None)` | Root cause (latest finished one by default) split into evidence and hypothesis, the deploy and its diff summary, `file:line` hints, the error signature, confidence, and a fix brief |
| `get_evidence(investigation_id)` | Every step Loki took: briefing, tool calls and their (cut) outputs |
| `get_deploy_diff(deploy_id)` | The deploy's diff with per-file changed line numbers |

It also has a prompt, `fix_incident`, that walks the agent through: fetch the root
cause, read the diff and files, check the hypothesis against the code, write a
failing regression test and a minimal fix, and never deploy or touch production.

Install it in its own venv on your machine (not the agent container), from the repo root:

```bash
python3.12 -m venv mcp/.venv
mcp/.venv/bin/pip install -r mcp/requirements.txt
```

**Claude Code** (run from the repo root so `$(pwd)` expands to it):

```bash
claude mcp add loki --scope user -e LOKI_URL=http://localhost:8088 -- "$(pwd)/mcp/.venv/bin/python" "$(pwd)/mcp/loki_mcp.py"
```

**Cursor** (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in the project you're fixing; use absolute paths):

```json
{
  "mcpServers": {
    "loki": {
      "command": "/path/to/incident-agent/mcp/.venv/bin/python",
      "args": ["/path/to/incident-agent/mcp/loki_mcp.py"],
      "env": { "LOKI_URL": "http://localhost:8088" }
    }
  }
}
```

Then, in the repo of the service that broke:

> Use Loki to find what broke and draft a fix.

or run the `fix_incident` prompt (`/mcp__loki__fix_incident` in Claude Code).

`LOKI_URL` here is Loki's agent API (port 8088), not Grafana Loki (port 3100, which
the agent's own `LOKI_URL` points at). `LOKI_API_KEY`, if set, is sent as a bearer
token, for when the agent API gets auth. Loki's root cause is a hypothesis that has
not been verified by reproduction; the fix brief says so, and the agent should
reproduce it in a test before trusting it.

## Securing Loki

Traces contain log lines and confirm/reject change what Loki reuses, so **any
deployment reachable from outside your laptop (a tunnel for Slack included) must
set `LOKI_API_KEYS`**. Without it Loki runs open for the local demo and logs a
loud warning at startup. Set `LOKI_REQUIRE_AUTH=1` to make a missing key a
startup error instead.

| Variable | Meaning |
|---|---|
| `LOKI_API_KEYS` | Comma-separated `name:key[:scopes]`, e.g. `eval:3f9a...,mcp:b71c...:read`. Scopes: `read`, `write`, `read+write` (default). A bare `key` works too. |
| `LOKI_WEBHOOK_TOKEN` | Shared secret alert senders may pass as `?token=...` on any `/alerts` path. |
| `LOKI_LINK_SECRET` | HMAC key for the Slack "View trace" share links (`GET /share/trace/{id}?exp=&sig=`). Unset: derived from `LOKI_API_KEYS` with a warning (rotating a key then breaks old links) |
| `LOKI_LINK_TTL_S` | Share-link lifetime in seconds (default 604800, 7 days) |
| `LOKI_REQUIRE_AUTH` | `1` = refuse to start without keys. |
| `LOKI_API_KEY` | Client side: the key scripts and the MCP server send. |

Every endpoint needs `Authorization: Bearer <key>`, including endpoints added
later: one app-level dependency guards them all. GETs need `read`; confirm,
reject and backtest need `write`. The exceptions:

- `GET /health`: public, returns no data.
- `POST /slack/interactions`: checked with Slack's signature (`SLACK_SIGNING_SECRET`).
- `/alerts` and anything under it: the bearer key **or** `?token=<LOKI_WEBHOOK_TOKEN>`,
  for Alertmanager, Datadog and others that can't set headers. Point Alertmanager at
  `http://agent:8080/alerts?token=...` (see `observability/alertmanager.yml`).

Keys are compared in constant time and never logged (only key names, and
`?token=` is masked in access logs). Every confirm, reject and backtest goes to
the `audit_log` table with the key name (or `slack:<user>`) and time.

Clients: `scripts/eval.py`, `run_scenarios.py` and `backtest.py` and the MCP
server send `LOKI_API_KEY` from the environment. Note that the Slack
"View trace" link opens in a browser without a key, so it gets 401 when auth
is on.

## Settings

Set these in `.env` (see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Required for Claude models |
| `LOKI_MODEL` | `claude-haiku-4-5` | `claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5`, or `fake` |
| `LOKI_ESCALATE_MODEL` | off | Re-run with this model when confidence is low |
| `LOKI_ESCALATE_BELOW` | `0.6` | Confidence below which to escalate |
| `LOKI_MAX_COST_USD` | `0.10` | Spending cap per investigation |
| `LOKI_DAILY_BUDGET_USD` | `1.00` | Spending cap per day |
| `LOKI_MAX_STEPS` | `8` | Model calls before Loki must report |
| `LOKI_OLLAMA_URL` | | Point at Ollama to use a free local model |
| `LOKI_CRITIC` | on for Claude, off for fake/Ollama | `1`/`0`: grounded critic after each report |
| `LOKI_CRITIC_MODEL` | `claude-haiku-4-5` | Model for the critic (`fake` = free word-overlap critic) |
| `LOKI_LESSONS` | `1` | Lessons notebook on/off |
| `LOKI_LESSONS_MODEL` | `claude-haiku-4-5` | Phrases eval-miss lessons (`fake` = template) |
| `SLACK_BOT_TOKEN` | | Slack app bot token; with `SLACK_CHANNEL`, reports get buttons |
| `SLACK_CHANNEL` | | Channel to post reports in |
| `SLACK_SIGNING_SECRET` | | Verifies button clicks come from Slack |
| `LOKI_PUBLIC_URL` | | Agent's public base URL, for the "View trace" button |
| `SLACK_WEBHOOK_URL` | | Fallback: plain-text reports to an incoming webhook |

## Slack setup

Loki posts each report to Slack with three buttons: **✅ Root cause correct**
(same as `POST /investigations/<id>/confirm`), **❌ Wrong** (recorded in the
`feedback` table, and the answer is never reused from cache), and **View trace**.
The message then shows who confirmed or rejected it.

1. Create an app at https://api.slack.com/apps → *Create New App* → *From scratch*.
2. *OAuth & Permissions* → *Bot Token Scopes*: add `chat:write`. Install the app
   to your workspace and copy the *Bot User OAuth Token* (`xoxb-...`).
3. *Basic Information* → copy the *Signing Secret*.
4. *Interactivity & Shortcuts* → turn on, and set the *Request URL* to
   `https://<your-public-host>/slack/interactions`. Slack must reach the agent
   (port 8088) over HTTPS; for a laptop demo, a tunnel such as
   `ngrok http 8088` or `cloudflared tunnel --url http://localhost:8088` works.
5. Invite the app to your channel: `/invite @<app name>`.
6. In `.env`:
   ```bash
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_CHANNEL=C0123456789        # or #incidents
   SLACK_SIGNING_SECRET=...
   LOKI_PUBLIC_URL=https://<your-public-host>
   ```
   Then recreate the agent: `docker compose up -d agent`.

Without a bot token, `SLACK_WEBHOOK_URL` still works and posts plain text
reports (incoming webhooks can't receive button clicks). Requests to
`/slack/interactions` without a valid Slack signature, or older than 5 minutes,
are refused with 401.

Note: `LOKI_PUBLIC_URL` exposes the agent's read endpoints (traces include log
lines) to whoever can reach that URL. Put it behind your VPN or an auth proxy.
The "View trace" button is a signed, expiring link (`/share/trace/{id}?exp=&sig=`)
to a read-only HTML page for that one investigation, so it opens in a browser
without an API key; anyone holding the link can read that page until it expires.
A bad, tampered or expired signature gets 403.

## Supported languages

Loki's log condensing understands error output from **Python**, **Java/JVM**
(including Spring Boot), **Node.js/TypeScript** and **Go**, in plain text or as
JSON (pino, winston, zap, logrus). The Grafana Loki filter keeps the lines that
say what went wrong (exception summaries, `Caused by:` chains, Go panics, JSON
lines at error level) and drops stack frames. Stack traces are then folded back
into one event, grouped by their deepest cause, and the first frame in your own
code (not `java.*`, Spring, `node_modules`, the Go runtime or `site-packages`)
is shown as the location:

```
x312 in payments-svc: TypeError: Cannot read properties of undefined (reading 'currency') [at src/charge.ts:42]
```

Request ids, UUIDs, hex addresses, Java object hashes and durations don't split
patterns. Pattern keys for the Python demo are unchanged, so earlier confirmed
answers still match.

Limitations: the location needs the frames, so it appears when a trace reaches
Loki as one entry (JSON loggers, or Promtail/Alloy `multiline` stage) or when
`query_logs` reads full logs; with one entry per line, error-only queries show
the error without a location. Traces from two requests of the same service that
interleave line by line can be folded together. Other languages still work
as before, as plain error lines.

## Connecting your alerts

Any of these starts an investigation. Every source goes through the same flow:
an alert already open with the same fingerprint is ignored (dedup), alerts that
arrive within 5 minutes of each other join one investigation whatever tool sent
them (a Sentry alert and a Prometheus alert from the same failure are one
storm), and a resolved event closes the incident. A payload Loki can't read gets
a 400 that says what is missing; an unknown service is accepted (as given, or
`unknown`). Replace `loki.example.com` with where port 8088 is reachable.

With auth on (see Securing Loki), append `?token=<LOKI_WEBHOOK_TOKEN>` to each
`/alerts...` URL below, or send `Authorization: Bearer <key>` with a write-scoped key
if the tool can set headers. Sentry and PagerDuty signatures are checked on top of
that. `/investigations/manual` is not a webhook: it always needs a write-scoped key.

| Source | URL | Signature check |
|---|---|---|
| Prometheus Alertmanager | `POST /alerts` | none |
| Grafana alerting | `POST /alerts/grafana` | `GRAFANA_WEBHOOK_SECRET` (optional HMAC) |
| Sentry | `POST /alerts/sentry` | `SENTRY_WEBHOOK_SECRET` |
| Datadog | `POST /alerts/datadog` | none (Datadog webhooks are unsigned) |
| PagerDuty | `POST /alerts/pagerduty` | `PAGERDUTY_WEBHOOK_SECRET` |
| Anything else | `POST /alerts/generic` | none |
| A human | `POST /investigations/manual` | none |

| Variable | Meaning |
|---|---|
| `SENTRY_PROJECTS` | `project-slug=service,...` (shared with the Sentry integration). An unmapped project's slug is used as the service |
| `SENTRY_WEBHOOK_SECRET` | The internal integration's Client Secret. When set, a missing or wrong `Sentry-Hook-Signature` is rejected (401) |
| `PAGERDUTY_WEBHOOK_SECRET` | The V3 webhook's signing secret. When set, a wrong `X-PagerDuty-Signature` is rejected (401) |
| `GRAFANA_WEBHOOK_SECRET` | The webhook contact point's HMAC secret. When set, a wrong signature is rejected (401) |
| `GRAFANA_WEBHOOK_SIGNATURE_HEADER` | Default `X-Grafana-Alerting-Signature` (Grafana's default) |
| `GRAFANA_WEBHOOK_TIMESTAMP_HEADER` | Set to the contact point's timestamp header if you configured one; then `<ts>:<body>` is verified and requests older than 5 minutes are rejected |

Severity is normalized to `critical`, `warning` or `info`.

**Prometheus Alertmanager.** A webhook receiver pointing at `http://loki.example.com/alerts`
(see `observability/alertmanager.yml`). Labels `alertname`, `service` and `severity`
and the `summary` annotation are used.

**Grafana alerting.** Alerting → Contact points → New → Webhook, URL
`http://loki.example.com/alerts/grafana`. Give alert rules a `service` label
(else `service_name` or `job` is used, then `unknown`) and a `summary` annotation.
Optionally set an HMAC secret on the contact point and the same value in
`GRAFANA_WEBHOOK_SECRET`.

**Sentry.** Settings → Developer Settings → Custom Integration → Internal Integration.
Webhook URL `http://loki.example.com/alerts/sentry`, turn on **Alert Rule Action**,
save, and put its Client Secret in `SENTRY_WEBHOOK_SECRET`. Then in an issue alert
rule add the action "Send a notification via <integration>"; in a metric alert, add
it as the critical/warning action. Issue alerts open an incident per Sentry issue
(Sentry sends no resolve for issue alerts, so that incident stays open and repeats of
the same issue are deduplicated); metric alerts open on `critical`/`warning` and
close on `resolved`. The service comes from `SENTRY_PROJECTS`. Webhooks whose
`Sentry-Hook-Resource` is not `event_alert` or `metric_alert` (installation, issue,
error, comment...) are acknowledged and ignored.

**Datadog.** Integrations → Webhooks → New: URL `http://loki.example.com/alerts/datadog`
and this payload (Datadog fills in the `$` variables):

```json
{
  "id": "$ID",
  "alert_id": "$ALERT_ID",
  "title": "$EVENT_TITLE",
  "alert_transition": "$ALERT_TRANSITION",
  "alert_type": "$ALERT_TYPE",
  "alert_scope": "$ALERT_SCOPE",
  "priority": "$ALERT_PRIORITY",
  "tags": "$TAGS",
  "date": "$DATE",
  "body": "$EVENT_MSG"
}
```

Then add `@webhook-<name>` to each monitor's message. The service is the `service:` tag
(else `unknown`). `Recovered` resolves; `Warn`/`Re-Warn`, `No Data`/`Re-No Data` or
`$ALERT_TYPE` `warning` are warnings; `Triggered`/`Re-Triggered`/`Renotify` are
critical; a `severity:critical|warning|info` tag overrides. `$DATE` is epoch
milliseconds. One monitor alerting on several scopes is one incident per scope.
Variable names are from https://docs.datadoghq.com/integrations/webhooks/.

**PagerDuty.** Integrations → Generic Webhooks (v3) → New Webhook, URL
`http://loki.example.com/alerts/pagerduty`, events `incident.triggered`,
`incident.reopened` and `incident.resolved` (others are accepted and ignored). Copy the signing secret
into `PAGERDUTY_WEBHOOK_SECRET`. The PagerDuty service's name is used as the service,
so name them like your `service` labels. High urgency or P1/P2 is critical.

**Anything else.** POST JSON to `/alerts/generic`:

```json
{"service": "payments-svc", "alert_name": "CheckoutErrors", "severity": "critical",
 "summary": "checkout returns 500", "status": "firing"}
```

Only `alert_name` is required. `severity` is `critical|warning|info` (default `warning`),
`status` is `firing|resolved` (default `firing`). Optional `fingerprint` (default
`service:alert_name`) decides what counts as the same alert, and so what a `resolved` closes.

**By hand.** "Something's wrong, Loki, look":

```bash
LOKI_API_KEY=<write key> python3 scripts/investigate.py --service payments-svc --description "checkout failing"
# or
curl -X POST localhost:8088/investigations/manual -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $LOKI_API_KEY" \
  -d '{"service": "payments-svc", "description": "checkout failing", "severity": "critical"}'
```

It opens an incident at once and starts an investigation (or joins the one already
running for the current storm). Like any alert, the investigation waits about 25s for
related alerts before it begins.

## Integrations

By default Loki reads the demo stack: deploys from `state/deploys.json` and
errors from Grafana Loki. To investigate a real team's stack, point it at
GitHub (deploy history and diffs) and/or Sentry (errors) instead. Metrics
still come from Prometheus (`PROM_URL`; see "Using your own metrics"). Everything is read-only, and all text
from these sources goes through `redact.py` before the model sees it.

| Variable | Default | Meaning |
|---|---|---|
| `LOKI_DEPLOYS_SOURCE` | `file` | `file` (`DEPLOYS_FILE`) or `github` |
| `LOKI_ERRORS_SOURCE` | `loki` | `loki` (Grafana Loki at `LOKI_URL`) or `sentry` |
| `GITHUB_TOKEN` | | GitHub token (see scopes below) |
| `GITHUB_REPOS` | | `owner/repo=service,owner/repo2=service2`: which repo deploys which service. The service names should match the `service` label in Prometheus |
| `GITHUB_ENVIRONMENT` | all | Only count deployments to this environment, e.g. `production` |
| `GITHUB_API_URL` | `https://api.github.com` | For GitHub Enterprise Server: `https://<host>/api/v3` |
| `SENTRY_TOKEN` | | Sentry auth token (see scopes below) |
| `SENTRY_ORG` | | Organization slug |
| `SENTRY_PROJECTS` | | `project-slug=service,...`: which Sentry project is which service |
| `SENTRY_BASE_URL` | `https://sentry.io` | Self-hosted Sentry, or `https://de.sentry.io` for the EU region |

**Token scopes (read-only):**
- GitHub fine-grained token: repository permissions *Metadata*, *Contents*, *Deployments* and
  *Pull requests*, all **read-only**, on the listed repos. A classic token needs `repo`
  (which also grants write, so prefer fine-grained).
- Sentry: an internal integration or user auth token with `project:read` and `event:read`.

**How GitHub becomes deploy history.** Per repo, Loki uses the first of these that has
any data: the Deployments API (what Actions environments, Argo CD and others record),
then pull requests merged into the default branch, then commits on the default branch.
`git_diff` compares a deployment with the previous one to the same environment (or shows
the PR's changes, or the commit). Big diffs are cut down to the full file list plus the
most relevant hunks: source before tests and docs, lockfiles and generated files never.

**How Sentry becomes error patterns.** For each project, the issues seen in the window
(top 20 by frequency) and their events in the window become error lines, so the
briefing's patterns, first-error-per-service and suspect ranking work as they do with logs.

**Rate limits and failures.** If GitHub or Sentry rate-limits Loki (403/429), it does not
wait: it uses what it already fetched and tells the model the data is partial
(a `note` in the briefing and in tool results). A repo or project it cannot read is
skipped with a note.

**Limitations.**
- Deployment time is when the deployment was created, not when it finished.
- Sentry counts are events Loki listed (up to 200 per issue per query), so they are a
  lower bound on busy issues; a note says so.
- With `LOKI_ERRORS_SOURCE=sentry`, `query_logs` with `errors_only=false` still reads
  Grafana Loki, since Sentry has no non-error logs.
- Tested against recorded-style API payloads (`tests/test_sources.py`), not yet against a
  live GitHub or Sentry account.

Run the tests (no Docker, no network):

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest
```

`requirements-dev.txt` installs the agent, the MCP server and the test dependencies into
one venv; they resolve together (no hand-pinned `starlette` needed).

## Using your own metrics

The demo's services export `http_requests_total{service, status}`,
`http_request_duration_seconds` and `dependency_duration_seconds{service, dependency}`,
and Promtail labels their logs `service`. A real stack names things differently, so
every PromQL and LogQL query Loki sends is built from a small config
(`agent/metrics_config.py`). With nothing set, the queries are exactly the demo's.

| Variable | Default | Meaning |
|---|---|---|
| `LOKI_METRICS_PRESET` | `default` | `default`, `otel`, `spring` or `express-prom` (below) |
| `LOKI_METRICS_CONFIG` | | Path to a JSON file (YAML if PyYAML is installed) overriding single fields |
| `LOKI_PROM_URL` | `PROM_URL` | Prometheus to query. Set it to empty (or `off`) when there is no Prometheus |

| Preset | Service label | Status label | Requests | Latency histogram | Dependency histogram | Log label |
|---|---|---|---|---|---|---|
| `default` | `service` | `status` | `http_requests_total` | `http_request_duration_seconds` | `dependency_duration_seconds` (`dependency`) | `service` |
| `otel` | `service_name` | `http_response_status_code` | `http_server_request_duration_seconds_count` | `http_server_request_duration_seconds` | `http_client_request_duration_seconds` (`server_address`) | `service_name` |
| `spring` | `application` | `status` | `http_server_requests_seconds_count` | `http_server_requests_seconds` | `http_client_requests_seconds` (`client_name`) | `application` |
| `express-prom` | `job` | `status_code` | `http_request_duration_seconds_count` | `http_request_duration_seconds` | none | `job` |

- `otel`: OpenTelemetry semantic conventions as exported to Prometheus with resource
  attributes turned into labels (`service_name`). Grafana Loki 3 sets `service_name` on logs.
- `spring`: Micrometer. Latency needs `management.metrics.distribution.percentiles-histogram.http.server.requests=true`;
  `application` comes from `management.metrics.tags.application`. Use `"service_label": "job"` if you don't set it.
- `express-prom`: prom-client through `express-prom-bundle`, which has no service label of
  its own, so the scrape `job` names the service.

A config file overrides a preset field by field (`null` turns a metric off):

```json
{"preset": "spring", "service_label": "job", "status_5xx": "5..", "dependency_histogram": null,
 "log_service_label": "app"}
```

Fields: `service_label`, `status_label`, `status_5xx` (regex), `requests` (a counter or a
histogram's `_count`), `latency_histogram` and `dependency_histogram` (base names, without
`_bucket`), `dependency_label`, `log_service_label`, `self_service` (Loki's own logs, never
investigated), `services_from_requests` (list services from the request metric only, so
unrelated scrape jobs don't show up; on in every preset except `default`). In Docker,
mount the file into the agent container and give its path there.

**Find your names.** Point the discovery helper at your Prometheus. It only sends GET
requests, lists candidate request counters, latency histograms and their labels, names
the closest preset and prints a config to start from:

```bash
.venv/bin/python scripts/discover_metrics.py --prom http://localhost:9090
.venv/bin/python scripts/discover_metrics.py --prom http://prom:9090 --json > metrics.json
```

**Missing metrics.** Loki asks Prometheus once which metric names exist (cached for 5
minutes). A metric that isn't configured or isn't there is skipped in the briefing with a
note (for example `p95_latency_seconds skipped: no latency metric found`), and
`query_metrics` / `query_dependencies` answer with that note instead of failing. If the
name lookup itself fails, Loki assumes the metrics exist and queries as before.

**No Prometheus.** With `LOKI_PROM_URL` set to empty, the briefing is built from logs,
errors and deploys only, and says so.

**Limitations.** One service label for every metric (no per-metric relabeling); the
status regex must match how your label spells codes (`5..` fits `500`, not `5xx`; use
`"status_5xx": "5.*"` for that); the presets are written from the projects' documented
defaults and checked with mocked Prometheus responses, not yet against live
OpenTelemetry, Spring or Express services.
