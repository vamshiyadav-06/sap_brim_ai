#!/usr/bin/env bash
# Rehearse a restore: load the newest (or the given) dump into a SCRATCH database next to the live one,
# count what came back, and drop only that scratch database. The live database is never touched.
#   scripts/production/restore-check.sh [path/to/sap_brim_ai-DATE.dump]
# Run it after the first backup and then regularly (e.g. monthly): a backup is only proven by a restore.
set -euo pipefail
cd "$(dirname "$0")/../.."

BACKUP_DIR="${BACKUP_DIR:-/opt/clyptus/backups}"
dump="${1:-$(ls -1t "$BACKUP_DIR"/sap_brim_ai-*.dump 2>/dev/null | head -n 1)}"
[[ -n "$dump" && -s "$dump" ]] || { echo "No backup found (looked in $BACKUP_DIR)." >&2; exit 1; }
COMPOSE=(docker compose -f docker-compose.prod.yml)
scratch="restore_check_$(date +%s)"   # the drop below only ever targets a name with this prefix

pg() { "${COMPOSE[@]}" exec -T postgres sh -c "$1"; }
cleanup() { pg "dropdb -U \"\$POSTGRES_USER\" --if-exists $scratch" > /dev/null 2>&1 || true; }
trap cleanup EXIT

pg "createdb -U \"\$POSTGRES_USER\" -O sap_brim_ai $scratch"
"${COMPOSE[@]}" exec -T postgres sh -c "pg_restore -U \"\$POSTGRES_USER\" -d $scratch --no-owner --role=sap_brim_ai --no-comments --exit-on-error" < "$dump"

tables=$(pg "psql -U \"\$POSTGRES_USER\" -d $scratch -Atc \"select count(*) from information_schema.tables where table_schema='public'\"")
users=$(pg "psql -U \"\$POSTGRES_USER\" -d $scratch -Atc \"select count(*) from users\"" 2>/dev/null || echo "n/a")
echo "Restore rehearsal OK: $(basename "$dump") -> $tables tables, $users users in scratch database $scratch (now dropped)."
