#!/usr/bin/env bash
#
# Restore a pre-migration database snapshot (#709).
#
# DESTRUCTIVE: replaces the current contents of the database with the dump.
# Run it via:  docker exec mkv-auto /app/db-restore.sh <dump>
# List backups: docker exec mkv-auto /app/db-restore.sh list
#
# Stop the app writers first so nothing mutates the DB mid-restore:
#   docker exec mkv-auto supervisorctl stop uvicorn celery-rip celery-default celery-preview
# then restore, then restart:
#   docker exec mkv-auto supervisorctl start uvicorn celery-rip celery-default celery-preview

set -uo pipefail

DB_NAME="${MKVAUTO_DB_NAME:-discs}"
BACKUP_DIR="${MKVAUTO_BACKUP_DIR:-/data/backups}"
# The role the app connects as (see /entrypoint.sh). Restored objects must be
# owned by it: restoring --no-owner AS POSTGRES flips every table to postgres
# ownership and locks the app out of its own database (#757).
APP_ROLE="${MKVAUTO_DB_ROLE:-mkvauto}"
PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
PG_RESTORE="${PGBIN:+$PGBIN/}pg_restore"

pg_as_postgres() {
  if [ "$(id -u)" = "0" ]; then su -s /bin/sh postgres -c "$*"; else sh -c "$*"; fi
}

arg="${1:-list}"

if [ "$arg" = "list" ] || [ "$arg" = "-h" ] || [ "$arg" = "--help" ]; then
  echo "Pre-migration snapshots in $BACKUP_DIR (newest first):"
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | sed 's/^/  /' || true
  [ -n "$(ls -A "$BACKUP_DIR"/*.dump 2>/dev/null)" ] || echo "  (none)"
  echo
  echo "Restore (DESTRUCTIVE — replaces '$DB_NAME'):"
  echo "  docker exec mkv-auto /app/db-restore.sh <path-to-.dump>"
  exit 0
fi

DUMP="$arg"
[ -f "$DUMP" ] || { echo "not found: $DUMP" >&2; exit 1; }

echo "Restoring '$DB_NAME' from $DUMP"
echo "  (this OVERWRITES existing data — Ctrl-C now if the app writers are still running)"

# --clean --if-exists drops+recreates objects before load; --no-owner so it
# works regardless of the dumping role; --role so the recreated objects are
# owned by the app role rather than postgres (we connect as postgres for peer
# auth, then SET ROLE). Errors during --clean of absent objects are tolerated
# by --if-exists; a genuine restore error still exits non-zero.
if pg_as_postgres "'$PG_RESTORE' --clean --if-exists --no-owner --role '$APP_ROLE' -d '$DB_NAME' '$DUMP'"; then
  echo "Restore complete. Restart writers: supervisorctl start uvicorn celery-rip celery-default celery-preview"
else
  rc=$?
  echo "Restore reported errors (rc=$rc). Inspect the DB before restarting writers." >&2
  exit "$rc"
fi
