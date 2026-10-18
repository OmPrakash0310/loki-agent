#!/usr/bin/env bash
# Daily compressed pg_dump of Loki's database; keeps LOKI_BACKUP_KEEP_DAYS days.
# Optional off-site copy: LOKI_BACKUP_RCLONE_REMOTE=remote:bucket/path (rclone on the host,
# configured with `rclone config` for any S3-compatible store).
# Cron (install.sh adds it): 17 3 * * * root /opt/loki/backup.sh >> /var/log/loki-backup.log 2>&1
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "$(readlink -f "$0")")/lib.sh"

dir="$(env_get LOKI_BACKUP_DIR)"; dir="${dir:-$LOKI_HOME/backups}"
keep="$(env_get LOKI_BACKUP_KEEP_DAYS)"; keep="${keep:-7}"
remote="$(env_get LOKI_BACKUP_RCLONE_REMOTE)"
mkdir -p "$dir"; chmod 700 "$dir"

out="$dir/loki-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
tmp="$out.partial"
umask 077
compose exec -T postgres pg_dump -U loki -d loki --clean --if-exists --no-owner | gzip -9 > "$tmp"
gzip -t "$tmp" || die "backup is corrupt: $tmp"
mv "$tmp" "$out"
log "backup written: $out ($(du -h "$out" | cut -f1))"

find "$dir" -name 'loki-*.sql.gz' -type f -mtime +"$((keep - 1))" -print -delete | sed 's/^/[loki] pruned /' >&2 || true

if [[ -n "$remote" ]]; then
  command -v rclone >/dev/null || die "LOKI_BACKUP_RCLONE_REMOTE set but rclone is not installed"
  rclone copy "$out" "$remote" && log "uploaded to $remote"
fi
