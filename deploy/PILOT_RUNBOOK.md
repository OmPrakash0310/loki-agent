# Loki pilot runbook (operator)

One Loki per customer, on a small VPS **you** run. Loki only reads the customer's
telemetry (metrics, logs, errors, deploys) and posts reports. It never changes
their systems.

```
customer tools ──HTTPS webhook──▶ Caddy (80/443, TLS) ──▶ agent (no host port) ──▶ Postgres (internal network only)
                                                          └──egress──▶ customer Prometheus / Grafana Loki / GitHub / Sentry, Anthropic API, Slack
```

Files in `/opt/loki`: `docker-compose.yml`, `loki.env` (secrets, mode 600), `caddy/`,
`backup.sh`, `restore.sh`, `upgrade.sh`, `lib.sh`, `loki` (CLI, linked as `/usr/local/bin/loki`), `backups/`.

## 1. Provision a VPS

| Size | Fits | Hetzner Cloud | DigitalOcean |
|---|---|---|---|
| 2 vCPU / 4 GB / 40 GB (recommended) | 1 customer, comfortable headroom | CX22 / CAX11, about €4–6 per month | Basic 4 GB, about $24 per month |
| 1–2 vCPU / 2 GB (minimum) | 1 customer with low alert volume | CX11-class, about €4 per month | Basic 2 GB, about $12 per month |

These are list prices as of writing, before VAT and backups. Check the provider's page.
The containers are capped at 768 MB (agent), 512 MB (Postgres) and 128 MB (Caddy), so
2 GB works. 4 GB leaves room for upgrades, where two images briefly coexist, and for `pg_dump`.
The model is the bigger cost: with `claude-haiku-4-5` and the default caps
(`LOKI_MAX_COST_USD=0.10` per investigation, `LOKI_DAILY_BUDGET_USD`), budget a few dollars per day at most.

- Ubuntu 22.04 or 24.04, x86_64 or arm64 (the image workflow builds both).
- Use the provider firewall or `ufw` to allow only 22, 80 and 443. Ports published by Docker
  bypass `ufw`. That is fine here because only Caddy publishes ports.
- Use SSH keys only and turn on unattended-upgrades. `cloud-init.yaml` does both, apart from the SSH keys, which you add in the provider UI.

## 2. DNS

Create an `A` (and `AAAA` if the VPS has IPv6) record, `loki.<customer>.com` → VPS IP, or a
subdomain of yours such as `acme.loki.yourco.com`. It must resolve **before** the install starts,
or Let's Encrypt fails. Caddy then retries and you get the cert later. Check with `dig +short <domain>`.

## 3. Install

**Option A: cloud-init (self-installs).** Paste `deploy/cloud-init.yaml` as user data, after filling in
domain, e-mail, version and repo. The repo must be readable from the VPS (public, a deploy key,
or a release tarball). Watch `/var/log/loki-install.log` (mode 600, it contains the webhook token).

**Option B: by hand.**
```bash
git clone --branch v0.1.0 <repo> /opt/loki-src
sudo /opt/loki-src/deploy/install.sh --domain loki.acme.com --email ops@yourco.com
# private image: first `docker login ghcr.io` with a read:packages token
# no registry: add --build to build from the checkout
```
`install.sh` is idempotent. Re-run it to repair files. It never regenerates existing secrets and
only adds missing ones. It installs Docker from Docker's apt repository if missing, writes
`/opt/loki/loki.env` (chmod 600) and generates these values:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD` | 48 hex chars |
| `LOKI_API_KEYS` | `admin:<64 hex>` (read+write), `mcp:<64 hex>:read` |
| `LOKI_WEBHOOK_TOKEN` | 64 hex, used as `?token=` on `/alerts*` |
| `LOKI_LINK_SECRET` | signs Slack "View trace" links |
| `LOKI_PROBE_TOKEN` | unused while probes are off |

It also adds a daily backup cron entry (`/etc/cron.d/loki-backup`, 03:17), starts the stack,
waits for `/health/ready` and prints the webhook URLs.

Then fill in the integrations in `loki.env` (sections 4–6), and run:
```bash
loki restart      # docker compose up -d: recreates the agent with the new env
loki doctor       # ✅ / ⚠️ / ❌ per check; exits 1 on any ❌
loki urls         # webhook URLs with the full token
```

### Secrets

- `loki.env` is the only secret store. It is root-owned, mode 600, and never goes in git. Do not paste it into tickets.
  Rotate a key by editing it and running `loki restart`. The old key stops working at once.
- **Docker secrets (optional).** Compose can mount files from `secrets:` at `/run/secrets/<name>`.
  Loki reads env vars, not files, so this needs a small entrypoint wrapper that exports
  `ANTHROPIC_API_KEY=$(cat /run/secrets/anthropic)`. That wrapper is not shipped. Use it if your
  policy forbids secrets in environment variables.
- **SOPS (optional).** Keep an encrypted `loki.env.sops` per customer in your ops repo
  (`sops -e --age <key> loki.env > loki.env.sops`), and decrypt it on the box
  (`sops -d loki.env.sops > /opt/loki/loki.env && chmod 600 /opt/loki/loki.env`). This gives you
  audit history for config without plaintext in git.

## 4. Connect the customer's alerts

Each URL is `https://<LOKI_DOMAIN><path>?token=<LOKI_WEBHOOK_TOKEN>`. Get the URLs from `loki urls`.

| Tool | Path | Where to set it |
|---|---|---|
| Prometheus Alertmanager | `/alerts` | `receivers: - webhook_configs: - url: ...`, with `send_resolved: true` |
| Grafana alerting | `/alerts/grafana` | Contact point → Webhook |
| Sentry | `/alerts/sentry` | Internal Integration → Webhook URL. Set `SENTRY_WEBHOOK_SECRET` = Client Secret (signature checked) |
| Datadog | `/alerts/datadog` | Integrations → Webhooks (default payload) |
| PagerDuty | `/alerts/pagerduty` | V3 webhook subscription. Set `PAGERDUTY_WEBHOOK_SECRET` (signature checked) |
| Anything else | `/alerts/generic` | JSON with `alert_name`, `service`, `summary`, `severity`, `status` |

Test one without waiting for a real incident:
```bash
curl -X POST "https://<domain>/alerts/generic?token=<token>" -H 'content-type: application/json' \
  -d '{"alert_name":"PilotSmokeTest","service":"checkout","summary":"test","severity":"warning","status":"firing"}'
```

## 5. Connect telemetry (read-only)

All of these live in `loki.env`. Run `loki restart && loki doctor` after each change.

- **Metrics.** Set `LOKI_PROM_URL` to the customer's Prometheus-compatible query URL. Mimir and Thanos
  work if they serve `/api/v1/query`. Find the right preset from your laptop:
  `python scripts/discover_metrics.py --prom <url>`. Then set `LOKI_METRICS_PRESET`
  (`default|otel|spring|express-prom`). For a custom JSON, mount it into the agent and set `LOKI_METRICS_CONFIG` to its path in the container.
  **Auth:** if the endpoint needs credentials, set `LOKI_PROM_BEARER_TOKEN`, or `LOKI_PROM_BASIC_USER` +
  `LOKI_PROM_BASIC_PASSWORD` (Grafana Cloud), and `LOKI_PROM_ORG_ID` for multi-tenant Mimir. Grafana Loki
  uses the same names with `LOKI_LOGS_`. Prefer read-only credentials; `loki doctor` uses them.
- **Logs.** Set `LOKI_URL` to the customer's Grafana Loki base URL. The name is historical:
  `LOKI_URL` means Grafana Loki. Empty means no log source.
- **Errors.** For Sentry, create an internal integration with `project:read` and `event:read`, then set
  `SENTRY_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECTS=slug=service,...` and `LOKI_ERRORS_SOURCE=sentry`.
  Only set the source once the token works. A source set without its token makes every investigation fail, and doctor shows ❌.
- **Deploys.** For GitHub, create a **fine-grained** token on the customer's org, limited to the listed repos.
  It needs read-only *Metadata*, *Contents*, *Deployments* and *Pull requests*, with an expiry date
  (set a reminder). Set `GITHUB_TOKEN`, `GITHUB_REPOS=owner/repo=service,...`, optionally
  `GITHUB_ENVIRONMENT=production`, and `LOKI_DEPLOYS_SOURCE=github`. Doctor warns on a classic token with `repo` scope, which grants write.
- **Datadog logs.** In Datadog: Organization Settings → API Keys (copy one) and Application Keys → New Key with the
  `logs_read_data` permission only. Set `DATADOG_API_KEY`, `DATADOG_APP_KEY`, `DATADOG_SITE` (e.g. `datadoghq.eu` if
  their URL isn't `app.datadoghq.com`) and `LOKI_ERRORS_SOURCE=datadog`. Optional `DATADOG_LOG_QUERY=env:production`.
  Loki reads error logs and searches full logs through the Logs Search API. **Not yet tested against a live Datadog
  account**: run `loki doctor` and the smoke test below before relying on it.
- **Datadog metrics and PagerDuty** are alert intake only. Loki does not read Datadog metrics.
- **Model.** Set `ANTHROPIC_API_KEY` and keep `LOKI_MODEL=claude-haiku-4-5` to start. Doctor checks the
  key with the free models list.

- **Similar past incidents.** `LOKI_EMBEDDINGS=local` (the default in `docker-compose.yml` and the template)
  finds confirmed past incidents by meaning with a local model baked into the image: no extra service, no
  data leaves the VPS, about 190 MB of memory once loaded. Installs from before 0.1.2 have a compose file
  without this line: add `LOKI_EMBEDDINGS=local` to `loki.env`. `off` falls back to keyword search.

## 6. Slack, in shadow mode for the first 2 weeks

1. api.slack.com/apps → *Create New App* → *From a manifest* → paste `deploy/slack-app-manifest.yml`
   after replacing the domain. Install it in the customer's workspace.
2. Copy the *Bot User OAuth Token* into `SLACK_BOT_TOKEN` and the *Signing Secret* into `SLACK_SIGNING_SECRET`.
3. **Shadow mode.** Create a **private** channel such as `#loki-shadow` with only you and 1–2 customer
   engineers, `/invite @loki`, and set `SLACK_CHANNEL=<its ID>`. Don't post in the on-call channel yet.
4. For two weeks, compare every Loki report with what the humans found. Use ✅/❌ on each report. A ❌ with a
   reason becomes a lesson after approval, and the answers feed `/stats`.
5. Go wider only when the customer agrees the hit rate is useful. Then move `SLACK_CHANNEL` and run `loki restart`.

`LOKI_OPERATOR_SLACK_WEBHOOK` is **your** incoming webhook, not the customer's. Loki posts there when
`LOKI_FAILURE_ALERT_THRESHOLD` investigations (default 3) fail within `LOKI_FAILURE_ALERT_WINDOW_MIN`
(30), at most once per `LOKI_FAILURE_ALERT_COOLDOWN_MIN` (60).

## 7. Backtest a past incident

Show value on day one by replaying an incident the customer already resolved:
```bash
LOKI_API_KEY=<admin key> python scripts/backtest.py --agent https://<domain> \
  --at 2026-10-16T21:03:00Z --alerts "HighErrorRate:checkout" --model fake   # pipeline check, $0
# then without --model fake to use LOKI_MODEL (counts toward the daily budget)
```
This only works while the customer's telemetry still holds data for that time, which depends on their
retention. Use `--incidents-file past.jsonl` with `expected` answers to grade several incidents.

## 8. Self-monitoring

- `GET /health` is liveness. `GET /health/ready` returns 200 when the database is reachable and the
  background worker is alive, and 503 otherwise. Both are public and return only booleans.
- **External uptime check.** In UptimeRobot (free, 5-minute interval) or Better Stack, add an HTTP(S) monitor on
  `https://<domain>/health/ready` that expects 200 and alerts your on-call. It also catches TLS expiry
  and DNS mistakes. Add a keyword monitor for `"ok":true` if you want.
- Container logs are rotated by Docker at 10 MB × 5 files per container. Run `loki logs agent` to read them.
  Caddy's access log masks `?token=` and drops `Authorization`.
- Container health: `loki status`. The agent healthcheck uses `/health/ready`.

## 9. Backups and restore

- Backups run daily at 03:17 through cron. Each run writes `pg_dump | gzip -9` to `/opt/loki/backups/loki-<UTC>.sql.gz` (mode 600).
  Backups older than `LOKI_BACKUP_KEEP_DAYS` (7) are deleted.
- Run one by hand with `loki backup`.
- **Off-site (recommended).** Install rclone (`apt install rclone`), then run `rclone config` and add an S3-compatible remote
  (Hetzner Object Storage, DO Spaces, Backblaze B2, AWS S3). Set
  `LOKI_BACKUP_RCLONE_REMOTE=myremote:bucket/customer-a`. Give the remote's key write-only access to that prefix if
  the provider supports it, and turn on bucket lifecycle expiry.
  To use the aws CLI instead, replace the `rclone copy` line in `backup.sh` with `aws s3 cp "$out" s3://...`.
- **Restore.** `loki restore /opt/loki/backups/loki-....sql.gz` stops the agent, replays the dump (it drops
  and recreates objects), starts the agent and waits for ready.
- Test a restore once per pilot. The backup includes traces, which contain the customer's log lines. Treat them as customer data.

## 10. Upgrades

```bash
loki upgrade 0.2.0
```
The upgrade takes a backup, pulls `LOKI_IMAGE:0.2.0`, sets `LOKI_VERSION`, recreates the agent and waits
for `/health/ready`. It then checks that all 7 tables exist. The schema migrates at startup with
`CREATE ... IF NOT EXISTS` and `ALTER ... ADD COLUMN IF NOT EXISTS`, so it only ever adds. On any failure it
sets `LOKI_VERSION` back and restarts the old image. Because migrations only add, the old image
runs on the newer schema. If even the rollback fails, restore the backup the upgrade just made.

## 10b. Several customers on one server

One Loki can serve several customers (tenants). `loki.env` configures the first one, the
**default tenant**, exactly as above. Each other customer gets its own webhook token, API key,
integrations and budget, and Postgres keeps their data apart with row-level security: a
connection acting for one customer can't read or write another's rows.

`install.sh` generates `LOKI_SECRET_KEY`, which encrypts customers' secrets in the database.
**Back it up with `loki.env`**: without it, stored customer secrets can't be read.

Add a customer (with a server key from `LOKI_API_KEYS`):
```bash
ADMIN=<admin key>
curl -s -X POST https://<domain>/tenants -H "Authorization: Bearer $ADMIN" \
  -H 'content-type: application/json' -d '{"slug": "acme", "name": "Acme Inc"}'
```
The answer holds its `webhook_token` and `api_key`, **shown once**. Store them now. Its alert
webhooks are the URLs from `loki urls` with `?token=<its webhook_token>`.

Configure it (names as in `.env.template`; `""` removes one; secrets are encrypted):
```bash
curl -s -X PUT https://<domain>/tenants/2/settings -H "Authorization: Bearer $ADMIN" \
  -H 'content-type: application/json' \
  -d '{"SLACK_BOT_TOKEN": "xoxb-...", "SLACK_CHANNEL": "C0...", "SLACK_TEAM_ID": "T0...",
       "LOKI_DEPLOYS_SOURCE": "github", "GITHUB_TOKEN": "github_pat_...", "GITHUB_REPOS": "acme/shop=shop",
       "LOKI_ERRORS_SOURCE": "sentry", "SENTRY_TOKEN": "...", "SENTRY_ORG": "acme", "SENTRY_PROJECTS": "shop=shop",
       "LOKI_DAILY_BUDGET_USD": "3"}'
```
- `GET /tenants` lists customers and the *names* of their settings, never values.
- `POST /tenants/<id>/rotate/webhook_token` or `/rotate/api_key` replaces a credential at once.
- A server key can act for a customer with the header `X-Loki-Tenant: acme` (reading its reports, for example).
- `LOKI_TOTAL_DAILY_BUDGET_USD` caps the Claude spend of all customers together; each also has `LOKI_DAILY_BUDGET_USD`.

What stays with the default tenant only: live probes and verification by reproduction (they reach
into the customer's services or run its images on this server), and the demo deploy file. Slack's
buttons route by workspace (`SLACK_TEAM_ID`). If the database user can't create the restricted
role `loki_app` (some managed Postgres plans), Loki runs as a single tenant and refuses `POST /tenants`.

## 11. Offboarding and data deletion

1. Ask the customer to remove the webhooks and uninstall the Slack app, and to revoke the GitHub token,
   the Sentry integration and any Prometheus allowlist entry.
2. If the customer wants a copy, run `loki backup` and hand the file over through an agreed channel.
3. On the VPS:
   ```bash
   cd /opt/loki && docker compose -p loki --env-file loki.env down -v   # removes Postgres and Caddy volumes
   shred -u loki.env && rm -rf /opt/loki /opt/loki-src /etc/cron.d/loki-backup /usr/local/bin/loki /var/log/loki-*.log
   ```
4. Delete the off-site backups under the customer's prefix, then delete the VPS and its provider snapshots.
5. Rotate or delete the Anthropic key if it was dedicated to this customer.
6. Write down the date and what was deleted, and send the customer confirmation.

## 12. Security checklist

- [ ] `loki doctor` shows no ❌. `LOKI_REQUIRE_AUTH=1` is forced by compose.
- [ ] Only ports 22, 80 and 443 are open. SSH uses keys only. Unattended-upgrades is on.
- [ ] `loki.env` is mode 600 and owned by root. It is not in git or tickets (use SOPS if you keep it anywhere).
- [ ] The GitHub token is fine-grained, read-only, limited to repos and expires. Sentry has `project:read` and `event:read` only.
- [ ] The Prometheus and Grafana Loki URLs are reachable only from the VPS (IP allowlist or VPN), not public.
- [ ] Sentry and PagerDuty webhook secrets are set, so signatures are checked.
- [ ] Slack is in shadow mode in a private channel. The signing secret is set.
- [ ] `LOKI_VERIFY=0` and `LOKI_PROBES=0` (the defaults). The agent has no Docker socket.
- [ ] Off-site backups are configured, and one restore has been tested.
- [ ] The uptime monitor is on `/health/ready` and the operator failure webhook is set.
- [ ] The customer has agreed in writing which data Loki reads and how long it is kept (backups: 7 days).
- [ ] The image is pinned to a version tag, never `latest`.

What the image and compose do for you: the agent runs as uid 10001 with a read-only root filesystem, all
capabilities dropped and `no-new-privileges`. It has no compilers or pip, and the base image is pinned by digest.
Postgres sits only on an internal network. Caddy proxies only Loki's routes (everything else is 404), caps
request bodies at 1 MB and sets HSTS, CSP, nosniff, frame-deny and no-referrer headers.

## 13. Known limits

- **No HA.** One server, possibly for several customers (section 10b). A VPS outage means missed alerts until it is back.
  Alertmanager retries, but most SaaS webhooks do not.
- Datadog metrics and PagerDuty are intake only. Datadog logs are read through the Logs Search API (untested live).
- Investigations run in-process. After a restart, live ones started in the last 30 minutes are run again
  from scratch (once); older ones are marked failed.
- Backups are plain `pg_dump` (no point-in-time recovery), so up to 24 hours of reports can be lost.
- `install.sh` and `cloud-init.yaml` target Ubuntu. They were exercised on macOS with `--no-docker` and a
  local compose project, **not yet on a real Ubuntu VPS**.
- `upgrade.sh` rolls back the image, not the data. That is fine while migrations only add.
- The Slack "View trace" link is a signed, expiring public URL to one trace. Anyone holding the link can read it.
- The client-side tools (`mcp/loki_mcp.py`) use `LOKI_URL` to mean *Loki's own API URL*, while the agent uses it
  for Grafana Loki. Don't copy one into the other.
