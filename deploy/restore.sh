#!/usr/bin/env bash
# Restore Loki's database from a backup made by backup.sh.
#   restore.sh /opt/loki/backups/loki-20261018T031700Z.sql.gz [--yes]
# Stops the agent, restores (the dump drops and recreates objects), starts the agent.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

file="${1:-}"
[[ -f "$file" ]] || die "usage: restore.sh <backup.sql.gz> [--yes]"
gzip -t "$file" || die "not a valid gzip file: $file"
if [[ "${2:-}" != "--yes" ]]; then
  read -r -p "Replace Loki's database with $file? [y/N] " ans
  [[ "$ans" == [yY]* ]] || die "aborted"
fi

log "stopping agent"
compose stop agent
log "restoring $file"
gunzip -c "$file" | compose exec -T postgres psql -v ON_ERROR_STOP=1 -q -U loki -d loki >/dev/null
log "starting agent"
compose up -d agent
wait_ready 120 || die "agent not ready after restore: check '$0' logs"
log "restore done"
