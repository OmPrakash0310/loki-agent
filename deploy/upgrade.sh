#!/usr/bin/env bash
# Upgrade Loki to a new image tag with automatic rollback.
#   upgrade.sh <new-version>      e.g. upgrade.sh 0.2.0
# Steps: backup -> pull -> restart on the new tag (schema migrates at startup:
# CREATE/ALTER ... IF NOT EXISTS) -> readiness + schema check -> roll back on failure.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"
here="$(dirname "$(readlink -f "$0")")"

new="${1:-}"
[[ -n "$new" ]] || die "usage: upgrade.sh <new-version>"
old="$(env_get LOKI_VERSION)"
[[ "$new" != "$old" ]] || die "already on $old"
image="$(env_get LOKI_IMAGE)"

log "upgrade $image: $old -> $new"
"$here/backup.sh"

if [[ "${LOKI_SKIP_PULL:-0}" != "1" ]]; then
  docker pull "$image:$new" || die "cannot pull $image:$new (nothing changed)"
fi

schema_ok() {
  local n
  n="$(compose exec -T postgres psql -U loki -d loki -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('incidents','investigations','trace_steps','report_cache','feedback','audit_log','lessons')")"
  [[ "${n//[[:space:]]/}" == "7" ]]
}

env_set LOKI_VERSION "$new"
compose up -d agent
if wait_ready "${LOKI_UPGRADE_TIMEOUT:-150}" && schema_ok; then
  log "upgrade to $new OK"
  exit 0
fi

log "new version failed readiness or schema check; rolling back to $old"
compose logs --tail 50 agent >&2 || true
env_set LOKI_VERSION "$old"
compose up -d agent
wait_ready 150 || die "ROLLBACK ALSO FAILED: restore the backup with restore.sh"
die "upgrade failed, rolled back to $old (backup kept in $(env_get LOKI_BACKUP_DIR))"
