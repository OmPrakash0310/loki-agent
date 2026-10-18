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
| `slack.py` | Slack reports with Confirm / Wrong / View trace buttons |
| `verifier.py` | Proves a blamed deploy by reproducing the failure in a sandbox (see Verification) |
| `sources/` | Pluggable deploy and error sources: JSON file, Grafana Loki, GitHub, Sentry |
| `redact.py` | Removes secrets and personal data before anything reaches the model |

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
| Grafana alerting | `POST /alerts/grafana` | none |
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

Severity is normalized to `critical`, `warning` or `info`.

**Prometheus Alertmanager.** A webhook receiver pointing at `http://loki.example.com/alerts`
(see `observability/alertmanager.yml`). Labels `alertname`, `service` and `severity`
and the `summary` annotation are used.

**Grafana alerting.** Alerting → Contact points → New → Webhook, URL
`http://loki.example.com/alerts/grafana`. Give alert rules a `service` label
(else `service_name` or `job` is used, then `unknown`) and a `summary` annotation.

**Sentry.** Settings → Developer Settings → Custom Integration → Internal Integration.
Webhook URL `http://loki.example.com/alerts/sentry`, turn on **Alert Rule Action**,
save, and put its Client Secret in `SENTRY_WEBHOOK_SECRET`. Then in an issue alert
rule add the action "Send a notification via <integration>"; in a metric alert, add
it as the critical/warning action. Issue alerts open an incident per Sentry issue
(Sentry sends no resolve for issue alerts, so that incident stays open and repeats of
the same issue are deduplicated); metric alerts open on `critical`/`warning` and
close on `resolved`. The service comes from `SENTRY_PROJECTS`.

**Datadog.** Integrations → Webhooks → New: URL `http://loki.example.com/alerts/datadog`
and this payload (Datadog fills in the `$` variables):

```json
{
  "id": "$ID",
  "alert_id": "$ALERT_ID",
  "title": "$EVENT_TITLE",
  "alert_transition": "$ALERT_TRANSITION",
  "alert_scope": "$ALERT_SCOPE",
  "priority": "$ALERT_PRIORITY",
  "tags": "$TAGS",
  "date": "$DATE",
  "body": "$EVENT_MSG"
}
```

Then add `@webhook-<name>` to each monitor's message. The service is the `service:` tag
(else `unknown`); `Recovered` resolves, `Warn` is a warning, anything else critical
(or a `severity:` tag). One monitor alerting on several scopes is one incident per scope.

**PagerDuty.** Integrations → Generic Webhooks (v3) → New Webhook, URL
`http://loki.example.com/alerts/pagerduty`, events `incident.triggered` and
`incident.resolved` (others are accepted and ignored). Copy the signing secret
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
still come from Prometheus (`PROM_URL`). Everything is read-only, and all text
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
python3.12 -m venv .venv && .venv/bin/pip install httpx==0.27.2 pytest
.venv/bin/pytest tests
```
