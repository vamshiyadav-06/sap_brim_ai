#!/usr/bin/env bash
# Verified database + uploaded-documents backup of the production stack.
#   scripts/production/backup.sh
# Settings (environment): BACKUP_DIR (default /opt/clyptus/backups), BACKUP_KEEP_DAYS (default 14),
# BACKUP_UPLOAD_CMD (optional; run as `bash -c "$BACKUP_UPLOAD_CMD" _ <file>` for each new file, e.g.
# 'rclone copy "$1" remote:clyptus-backups' or 'rsync -a "$1" backup@host:/srv/backups/'). Copies kept only
# on the server do not survive losing the server: set BACKUP_UPLOAD_CMD.
# Backups contain user data and chat history: the directory is created 0700 and files 0600.
set -euo pipefail
cd "$(dirname "$0")/../.."

BACKUP_DIR="${BACKUP_DIR:-/opt/clyptus/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
COMPOSE=(docker compose -f docker-compose.prod.yml)
stamp="$(date +%F-%H%M%S)"
dump="$BACKUP_DIR/sap_brim_ai-$stamp.dump"
docs="$BACKUP_DIR/documents-$stamp.tgz"

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

"${COMPOSE[@]}" exec -T postgres pg_dump -U sap_brim_ai -d sap_brim_ai -Fc > "$dump.partial"
[[ -s "$dump.partial" ]] || { rm -f "$dump.partial"; echo "Backup failed: the dump is empty." >&2; exit 1; }
# A readable table of contents proves the archive is a complete, valid custom-format dump.
"${COMPOSE[@]}" exec -T postgres pg_restore --list < "$dump.partial" > /dev/null \
  || { rm -f "$dump.partial"; echo "Backup failed: the dump cannot be read back." >&2; exit 1; }
mv "$dump.partial" "$dump"

if [[ -d knowledge_base/documents ]]; then
  tar -czf "$docs" knowledge_base/documents
fi

if [[ -n "${BACKUP_UPLOAD_CMD:-}" ]]; then
  for file in "$dump" "$docs"; do
    [[ -f "$file" ]] || continue
    bash -c "$BACKUP_UPLOAD_CMD" _ "$file" || { echo "Backup kept locally, but the upload of $file FAILED." >&2; exit 2; }
  done
else
  echo "WARN: BACKUP_UPLOAD_CMD is not set; backups exist only on this server." >&2
fi

find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'sap_brim_ai-*.dump' -o -name 'documents-*.tgz' \) -mtime +"$KEEP_DAYS" -delete
echo "Backup written: $dump ($(du -h "$dump" | cut -f1)); older than $KEEP_DAYS days pruned."
