#!/usr/bin/env bash
# Install (or re-run to repair) a Loki pilot on Ubuntu 22.04 / 24.04. Idempotent:
# an existing loki.env is kept (secrets are never regenerated); missing secrets are added.
#
#   sudo ./install.sh --domain loki.acme.com --email ops@acme.com [options]
#
# Options (each also prompted for on a TTY when missing):
#   --domain D          public hostname (DNS must point at this VPS)
#   --email E           Let's Encrypt e-mail
#   --anthropic-key K   model API key (or set ANTHROPIC_API_KEY in the environment)
#   --model M           default claude-haiku-4-5 (fake = testing only)
#   --image I           default ghcr.io/turbotrade-tt/loki-agent
#   --version V         image tag, default 0.1.0
#   --build             build the image from ../agent instead of pulling
#   --dir DIR           install directory, default /opt/loki
#   --project P         compose project name, default loki
#   --no-docker         don't install Docker (must already be present)
#   --no-start          write files only
#   --no-cron           don't install the daily backup cron job
#   --yes               non-interactive: never prompt
set -euo pipefail
src="$(dirname "$(readlink -f "$0")")"

domain="" email="" akey="${ANTHROPIC_API_KEY:-}" model="claude-haiku-4-5"
image="ghcr.io/turbotrade-tt/loki-agent" version="0.1.0"
build=0 install_docker=1 start=1 cron=1 yes=0
dir="/opt/loki" project="loki"

while (($#)); do
  case "$1" in
    --domain) domain="$2"; shift 2 ;;
    --email) email="$2"; shift 2 ;;
    --anthropic-key) akey="$2"; shift 2 ;;
    --model) model="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --version) version="$2"; shift 2 ;;
    --dir) dir="$2"; shift 2 ;;
    --project) project="$2"; shift 2 ;;
    --build) build=1; shift ;;
    --no-docker) install_docker=0; shift ;;
    --no-start) start=0; shift ;;
    --no-cron) cron=0; shift ;;
    --yes) yes=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

export LOKI_HOME="$dir" LOKI_PROJECT="$project"
# shellcheck source=deploy/lib.sh
source "$src/lib.sh"

ask() {  # ask VAR "prompt" [secret]
  local __v="$1" __p="$2" __s="${3:-}" __a=""
  [[ -n "${!__v}" ]] && return 0
  if [[ "$yes" == 1 || ! -t 0 ]]; then return 0; fi
  if [[ -n "$__s" ]]; then read -r -s -p "$__p: " __a; echo; else read -r -p "$__p: " __a; fi
  printf -v "$__v" '%s' "$__a"
}

# ---- 1. Docker ----
if ! command -v docker >/dev/null 2>&1; then
  [[ "$install_docker" == 1 ]] || die "docker not found and --no-docker given"
  [[ "$(id -u)" == 0 ]] || die "run as root (sudo) to install Docker"
  . /etc/os-release
  [[ "$ID" == ubuntu ]] || die "automatic Docker install supports Ubuntu only (found $ID)"
  log "installing Docker from Docker's apt repository"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl openssl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing"
command -v openssl >/dev/null || die "openssl missing"

# ---- 2. Files ----
mkdir -p "$dir/caddy" "$dir/backups"
chmod 700 "$dir/backups"
for f in docker-compose.yml lib.sh backup.sh restore.sh upgrade.sh loki .env.template PILOT_RUNBOOK.md slack-app-manifest.yml; do
  if [[ -f "$src/$f" && "$(readlink -f "$src/$f")" != "$(readlink -f "$dir/$f" 2>/dev/null || true)" ]]; then
    install -m 0644 "$src/$f" "$dir/$f"
  fi
done
chmod 0755 "$dir"/*.sh "$dir/loki"
install -m 0644 "$src"/caddy/* "$dir/caddy/"

# ---- 3. loki.env (chmod 600) ----
if [[ ! -f "$LOKI_ENV_FILE" ]]; then
  ask domain "Public hostname for Loki (e.g. loki.acme.com)"
  ask email "E-mail for Let's Encrypt"
  if [[ "$model" != fake ]]; then ask akey "Anthropic API key (empty = set later)" secret; fi
  [[ -n "$domain" && -n "$email" ]] || die "--domain and --email are required"
  log "writing $LOKI_ENV_FILE"
  ( umask 077; cp "$src/.env.template" "$LOKI_ENV_FILE" )
  chmod 600 "$LOKI_ENV_FILE"
  env_set LOKI_DOMAIN "$domain"
  env_set LOKI_ACME_EMAIL "$email"
  env_set LOKI_PUBLIC_URL "https://$domain"
  env_set LOKI_IMAGE "$image"
  env_set LOKI_VERSION "$version"
  env_set LOKI_MODEL "$model"
  env_set ANTHROPIC_API_KEY "$akey"
  env_set LOKI_BACKUP_DIR "$dir/backups"
else
  log "keeping existing $LOKI_ENV_FILE"
  chmod 600 "$LOKI_ENV_FILE"
fi
# Secrets: generated once, never overwritten.
[[ -n "$(env_get POSTGRES_PASSWORD)" ]] || env_set POSTGRES_PASSWORD "$(rand_hex 24)"
[[ -n "$(env_get LOKI_API_KEYS)" ]] || env_set LOKI_API_KEYS "admin:$(rand_hex 32),mcp:$(rand_hex 32):read"
[[ -n "$(env_get LOKI_WEBHOOK_TOKEN)" ]] || env_set LOKI_WEBHOOK_TOKEN "$(rand_hex 32)"
[[ -n "$(env_get LOKI_PROBE_TOKEN)" ]] || env_set LOKI_PROBE_TOKEN "$(rand_hex 32)"

# ---- 4. CLI + cron ----
if [[ "$(id -u)" == 0 ]]; then
  ln -sf "$dir/loki" /usr/local/bin/loki
  if [[ "$cron" == 1 ]]; then
    printf 'LOKI_HOME=%s\nLOKI_PROJECT=%s\n17 3 * * * root %s/backup.sh >> /var/log/loki-backup.log 2>&1\n' \
      "$dir" "$project" "$dir" > /etc/cron.d/loki-backup
    chmod 644 /etc/cron.d/loki-backup
  fi
fi

[[ "$start" == 1 ]] || { log "files written; not starting (--no-start)"; exit 0; }

# ---- 5. Image ----
img="$(env_get LOKI_IMAGE):$(env_get LOKI_VERSION)"
if [[ "$build" == 1 ]]; then
  [[ -d "$src/../agent" ]] || die "--build needs the repository checkout (../agent)"
  log "building $img"
  docker build -t "$img" "$src/../agent"
elif ! docker image inspect "$img" >/dev/null 2>&1; then
  log "pulling $img"
  docker pull "$img" || die "cannot pull $img (private registry? run: docker login ghcr.io)"
fi

# ---- 6. Start + wait ----
log "starting stack"
compose up -d
wait_ready 180 || { compose logs --tail 60 agent >&2; die "agent did not become ready"; }

base="$(env_get LOKI_PUBLIC_URL)"; tok="$(env_get LOKI_WEBHOOK_TOKEN)"
cat <<MSG

Loki is running.        $base/health   $base/health/ready
Secrets live in $LOKI_ENV_FILE (mode 600). API keys: LOKI_API_KEYS there
(admin = read+write, mcp = read-only for Claude Code / Cursor: LOKI_API_KEY=<mcp key>).

Alert webhook URLs (give these to the customer's tools):
  Alertmanager  $base/alerts?token=$tok
  Grafana       $base/alerts/grafana?token=$tok
  Sentry        $base/alerts/sentry?token=$tok
  Datadog       $base/alerts/datadog?token=$tok
  PagerDuty     $base/alerts/pagerduty?token=$tok
  Generic       $base/alerts/generic?token=$tok
Slack interactivity URL: $base/slack/interactions

Next: edit $LOKI_ENV_FILE (integrations), 'loki restart', then 'loki doctor'.
MSG
