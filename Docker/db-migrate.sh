#!/usr/bin/env bash
#
# Guarded database migration for the all-in-one container (#709).
#
# Replaces the old `sleep 15 && alembic upgrade head` supervisor command with a
# safety-first sequence, because migrations run UNATTENDED at every container
# start against the user's live data:
#
#   1. Wait for Postgres to accept connections.
#   2. If the DB is already at head → nothing to do (no backup, no churn).
#   3. If migrations are pending on an EXISTING database → pg_dump a
#      point-in-time snapshot to /data/backups BEFORE touching anything.
#      Fail closed: if the backup can't be written, do NOT migrate.
#   4. Run `alembic upgrade head`.
#   5. On failure → write a sentinel that the API reads to refuse serving a
#      half-migrated DB (see api/main.py readiness gate). On success → clear it.
#
# A fresh install (no prior Alembic revision, i.e. empty DB) skips the backup —
# there is no user data to lose, and we must not brick first boot on a dump hiccup.
#
# Env overrides (defaults suit the container):
#   MKVAUTO_DB_NAME             database name (default: discs)
#   MKVAUTO_BACKUP_DIR          backup directory (default: /data/backups)
#   MKVAUTO_MIGRATION_SENTINEL  failure sentinel path (default: /data/.mkvauto-migration-failed)
#   MKVAUTO_BACKUP_KEEP         how many pre-migrate dumps to retain (default: 10)
#   MKVAUTO_BACKEND_DIR         backend dir with alembic.ini (default: /app/backend)
#   MKVAUTO_ALEMBIC             alembic binary (default: /app/venv/bin/alembic)

set -uo pipefail

DB_NAME="${MKVAUTO_DB_NAME:-discs}"
BACKUP_DIR="${MKVAUTO_BACKUP_DIR:-/data/backups}"
SENTINEL="${MKVAUTO_MIGRATION_SENTINEL:-/data/.mkvauto-migration-failed}"
KEEP="${MKVAUTO_BACKUP_KEEP:-10}"
BACKEND_DIR="${MKVAUTO_BACKEND_DIR:-/app/backend}"
ALEMBIC="${MKVAUTO_ALEMBIC:-/app/venv/bin/alembic}"

log() { echo "[db-migrate] $*"; }

# Resolve the pg client tools (postgresql-client-15 in the image).
PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
PG_DUMP="${PGBIN:+$PGBIN/}pg_dump"
PG_ISREADY="${PGBIN:+$PGBIN/}pg_isready"

cd "$BACKEND_DIR" || { log "backend dir $BACKEND_DIR missing"; exit 1; }

# Run pg_dump/pg_restore as the postgres OS user (local-socket peer auth, no
# password) when we're root; otherwise run directly (already postgres, or dev).
pg_as_postgres() {
  if [ "$(id -u)" = "0" ]; then
    su -s /bin/sh postgres -c "$*"
  else
    sh -c "$*"
  fi
}

# A prior boot's sentinel must not persist across a now-healthy start; a fresh
# failure below re-writes it.
rm -f "$SENTINEL" 2>/dev/null || true

# 1) Wait for Postgres (was a blind `sleep 15`).
ready=0
for _ in $(seq 1 60); do
  if "$PG_ISREADY" -q -h 127.0.0.1 -p 5432 2>/dev/null; then ready=1; break; fi
  sleep 1
done
[ "$ready" = "1" ] || log "pg_isready never succeeded; attempting migration anyway"

# 2) Determine current vs head revision.
#
# "alembic current" exits 0 with EMPTY output on a genuinely fresh database,
# and non-zero when it cannot read the revision at all (auth failure, missing
# table privileges, Postgres mid-start). Those two must never be conflated:
# treating "unreadable" as "fresh" skips the backup and migrates a database we
# could not even read — found live in the 1.2.0-rc.1 rehearsal (#757).
rev_token() { grep -viE 'INFO|WARNING|ERROR|^[[:space:]]*$' | tail -1 | awk '{print $1}'; }
CUR=""
CUR_OK=0
for _ in 1 2 3 4 5; do
  if CUR_RAW="$("$ALEMBIC" current 2>/tmp/db-migrate-current.err)"; then
    CUR="$(printf '%s\n' "$CUR_RAW" | rev_token)"
    CUR_OK=1
    break
  fi
  sleep 2
done
if [ "$CUR_OK" != "1" ]; then
  log "ERROR: cannot read the current DB revision — refusing to guess:"
  tail -3 /tmp/db-migrate-current.err 2>/dev/null | sed 's/^/[db-migrate]   /'
  printf 'could not read the current DB revision; refused to migrate.\nSee /tmp/db-migrate-current.err inside the container.\n' > "$SENTINEL"
  exit 1
fi
HEAD="$("$ALEMBIC" heads 2>/dev/null | rev_token)"
log "current=${CUR:-<none>} head=${HEAD:-<unknown>}"

if [ -n "$CUR" ] && [ "$CUR" = "$HEAD" ]; then
  log "already at head — no migration needed"
  exit 0
fi

# 3) Back up before migrating — but only for an EXISTING database (CUR set).
if [ -n "$CUR" ]; then
  mkdir -p "$BACKUP_DIR"
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  DEST="$BACKUP_DIR/pre-migrate_${TS}_${CUR}_to_${HEAD:-head}.dump"
  log "backing up '$DB_NAME' -> $DEST"
  if pg_as_postgres "'$PG_DUMP' -Fc -d '$DB_NAME'" > "$DEST" 2>/tmp/db-migrate-pgdump.err; then
    log "backup ok ($(du -h "$DEST" 2>/dev/null | cut -f1))"
    # Rotate: keep newest $KEEP pre-migrate dumps.
    # shellcheck disable=SC2012
    ls -1t "$BACKUP_DIR"/pre-migrate_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
  else
    log "ERROR: pre-migration backup failed:"; tail -3 /tmp/db-migrate-pgdump.err 2>/dev/null | sed 's/^/[db-migrate]   /'
    rm -f "$DEST" 2>/dev/null || true
    # Fail closed — never migrate live data without a recovery point.
    printf 'pre-migration backup failed at %s (from=%s to=%s); refused to migrate.\nSee /tmp/db-migrate-pgdump.err inside the container.\n' \
      "$TS" "$CUR" "${HEAD:-head}" > "$SENTINEL"
    exit 1
  fi
else
  log "fresh database (no prior revision) — migrating without backup"
fi

# 4) Migrate. Capture the exit status directly: `rc=$?` after an `if` whose
# body did not run is 0 by definition, which turned every failure into a clean
# supervisor exit (#757). Only the API sentinel gate caught it.
"$ALEMBIC" upgrade head
rc=$?
if [ "$rc" -eq 0 ]; then
  log "migration succeeded (${CUR:-base} -> ${HEAD:-head})"
  rm -f "$SENTINEL" 2>/dev/null || true
  exit 0
fi
log "MIGRATION FAILED (rc=$rc)."
{
  printf 'alembic upgrade head FAILED (rc=%s) from=%s to=%s.\n' "$rc" "${CUR:-base}" "${HEAD:-head}"
  if [ -n "${DEST:-}" ]; then
    printf 'A pre-migration snapshot was saved at: %s\n' "$DEST"
    printf 'Restore it with:  /app/db-restore.sh %s\n' "$DEST"
  fi
  printf 'The API will refuse to serve until this is resolved and the container is restarted.\n'
} > "$SENTINEL"
exit "$rc"
