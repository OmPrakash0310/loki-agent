#!/usr/bin/env bash
# Shared helpers for the Loki pilot scripts. Sourced, not run.
# LOKI_HOME            install directory (default /opt/loki)
# LOKI_PROJECT         compose project name (default loki)
# LOKI_COMPOSE_EXTRA   extra compose file, e.g. a local test override (optional)

LOKI_HOME="${LOKI_HOME:-/opt/loki}"
LOKI_PROJECT="${LOKI_PROJECT:-loki}"
LOKI_ENV_FILE="${LOKI_ENV_FILE:-$LOKI_HOME/loki.env}"

log() { printf '[loki] %s\n' "$*" >&2; }
die() { printf '[loki] ERROR: %s\n' "$*" >&2; exit 1; }

compose() {
  local files=(-f "$LOKI_HOME/docker-compose.yml")
  if [[ -n "${LOKI_COMPOSE_EXTRA:-}" ]]; then files+=(-f "$LOKI_COMPOSE_EXTRA"); fi
  docker compose -p "$LOKI_PROJECT" --project-directory "$LOKI_HOME" "${files[@]}" \
    --env-file "$LOKI_ENV_FILE" "$@"
}

# env_get KEY: value of KEY from loki.env (last match wins), without sourcing the file.
env_get() {
  local line
  line="$(grep -E "^$1=" "$LOKI_ENV_FILE" 2>/dev/null | tail -n 1)" || true
  printf '%s' "${line#*=}"
}

# env_set KEY VALUE: replace or append KEY in loki.env, keeping mode 600.
env_set() {
  local tmp
  tmp="$(mktemp "$LOKI_ENV_FILE.XXXXXX")"
  chmod 600 "$tmp"
  if grep -qE "^$1=" "$LOKI_ENV_FILE"; then
    awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$LOKI_ENV_FILE" > "$tmp"
  else
    cat "$LOKI_ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
  fi
  mv "$tmp" "$LOKI_ENV_FILE"
}

# wait_ready [seconds]: poll /health/ready inside the agent container.
wait_ready() {
  local deadline=$((SECONDS + ${1:-120}))
  while (( SECONDS < deadline )); do
    if compose exec -T agent python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=4).status == 200 else 1)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

rand_hex() { openssl rand -hex "${1:-32}"; }
