#!/usr/bin/env bash
# Install (or replace) the daily 02:00 backup job for the current user. Safe to run again.
#   BACKUP_UPLOAD_CMD='rclone copy "$1" remote:clyptus-backups' scripts/production/install-backup-cron.sh
# The cron line is tagged so only that one line is ever replaced; other cron jobs are left alone.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
tag="# clyptus-backup"
env_part=""
[[ -n "${BACKUP_DIR:-}" ]] && env_part+="BACKUP_DIR='$BACKUP_DIR' "
[[ -n "${BACKUP_UPLOAD_CMD:-}" ]] && env_part+="BACKUP_UPLOAD_CMD='$BACKUP_UPLOAD_CMD' "
line="0 2 * * * cd '$repo' && ${env_part}scripts/production/backup.sh >> '$repo/.deploy/backup.log' 2>&1 $tag"

mkdir -p "$repo/.deploy"
( crontab -l 2>/dev/null | grep -v -F "$tag" || true; echo "$line" ) | crontab -
echo "Installed: daily backup at 02:00 (log: $repo/.deploy/backup.log)."
[[ -n "${BACKUP_UPLOAD_CMD:-}" ]] || echo "WARN: no BACKUP_UPLOAD_CMD given, so backups stay on this server only." >&2
