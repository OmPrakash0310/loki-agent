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
| `main.py` | Receives alerts, groups an alert storm into one investigation |
| `context.py` | Builds the briefing: error patterns, before/after metrics, recent deploys |
| `tools.py` | Read-only tools Claude can call: logs, metrics, deploys, diffs, past incidents |
| `investigator.py` | The model loop: caches, budgets, step limit, cost logging, report |
| `telemetry.py` | Talks to Grafana Loki, Prometheus and the deploy history |
| `slack.py` | Slack reports with Confirm / Wrong / View trace buttons |
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

## Useful endpoints

- `localhost:8088/investigations`: all investigations, with status, steps and cost
- `localhost:8088/investigations/<id>/report`: the root-cause report
- `localhost:8088/investigations/<id>/trace`: every tool call Loki made
- `POST localhost:8088/investigations/<id>/reject`: mark a report wrong (optional body `{"reason": "..."}`)
- `localhost:8088/incidents`: alerts and which investigation they joined
- `localhost:3000`: Grafana (Explore → Loki for logs)

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
