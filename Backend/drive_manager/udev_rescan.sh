#!/usr/bin/env bash
# Trigger a backend drive rescan when udev reports media change.
# Intended to be called from a udev rule. Idempotent and rate‑limited via flock.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${MKVAUTO_ROOT:-/home/user/MakeMKV-Auto}"
API_BASE="${MKVAUTO_BACKEND_URL:-http://127.0.0.1:8000}"
TMPDIR="${MKVAUTO_TMP_DIR:-${ROOT}/tmp}"
LOCK="${TMPDIR}/udev-rescan.lock"
LOGDIR="${MKVAUTO_LOG_DIR:-${ROOT}/logs}"
LOGFILE="${LOGDIR}/udev_rescan.log"
CURL_BIN="${CURL_BIN:-$(command -v curl || echo /usr/bin/curl)}"
# Short timeout for quick connection - backend returns immediately and handles work asynchronously
# Use 5 seconds for connect timeout, 5 seconds max time to avoid blocking udev
CURL_CONNECT_TIMEOUT="${MKVAUTO_CURL_CONNECT_TIMEOUT:-5}"
CURL_MAX_TIME="${MKVAUTO_CURL_MAX_TIME:-5}"

mkdir -p "$TMPDIR"
# If the configured log dir is not writable (common under udev), fall back to tmp so we still emit diagnostics.
if ! (mkdir -p "$LOGDIR" 2>/dev/null && touch "$LOGDIR/.writable" 2>/dev/null); then
  LOGDIR="$TMPDIR"
  LOGFILE="${LOGDIR}/udev_rescan.log"
  mkdir -p "$LOGDIR"
fi

# Serialize invocations so multiple udev events don't hammer the backend.
(
  flock -n 9 || exit 0
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[$ts] udev_rescan: dev=$1 change=$2 -> POST ${API_BASE}/events/drive/rescan (curl=$CURL_BIN)" >>"$LOGFILE" || true
  echo "[$ts] udev_rescan env: MKVAUTO_ROOT=${MKVAUTO_ROOT:-} MKVAUTO_LOG_DIR=${MKVAUTO_LOG_DIR:-} MKVAUTO_BACKEND_URL=${MKVAUTO_BACKEND_URL:-} LOGDIR=${LOGDIR}" >>"$LOGFILE" || true
  
  # Also log to drive_manager.log if it exists and is writable
  DRIVE_MANAGER_LOG="${MKVAUTO_LOG_DIR:-${ROOT}/logs}/drive_manager.log"
  if [ -w "$(dirname "$DRIVE_MANAGER_LOG")" ] 2>/dev/null; then
    echo "[$ts] UDEV trigger: dev=$1 change=$2" >>"$DRIVE_MANAGER_LOG" || true
  fi
  if [ ! -x "$CURL_BIN" ]; then
    echo "[$ts] udev_rescan: curl not found/executable" >>"$LOGFILE" || true
    exit 0
  fi
  # Keep it quick; ignore errors to avoid blocking udev, but log the exit code.
  set +e
  endpoint="/events/drive/rescan"
  # DISK_MEDIA_CHANGE: 1=insert, 2=eject
  if [ "${2:-}" = "2" ] || [ "${2:-}" = "eject" ]; then
    endpoint="/events/drive/eject"
  fi
  
  # Log which endpoint we're calling
  full_url="${API_BASE}${endpoint}"
  echo "[$ts] udev_rescan: calling endpoint ${endpoint} for dev=$1 change=$2" >>"$LOGFILE" || true
  echo "[$ts] udev_rescan: full URL=${full_url}" >>"$LOGFILE" || true
  
  # Use systemd-run to execute curl in a proper systemd context with network access
  # udev runs in a restricted environment without network access, but systemd-run provides it
  if command -v systemd-run >/dev/null 2>&1; then
    echo "[$ts] udev_rescan: Using systemd-run to execute curl with network access" >>"$LOGFILE" || true
    
    # Try user scope first (if systemd user session exists)
    if systemctl --user is-system-running >/dev/null 2>&1; then
      # User scope - runs as current user with network access
      systemd-run --user --no-block --quiet -- \
        env MKVAUTO_BACKEND_URL="${API_BASE}" \
        $CURL_BIN -s --connect-timeout "$CURL_CONNECT_TIMEOUT" -m "$CURL_MAX_TIME" \
        -o /dev/null -w '%{http_code}' \
        -X POST \
        -d "device=$1" \
        -d "change=${2:-}" \
        "$full_url" >>"$LOGFILE" 2>&1 || true
      rc=0
      http_code="systemd_user_triggered"
    else
      # System scope - requires proper permissions but has network access
      systemd-run --no-block --quiet -- \
        env MKVAUTO_BACKEND_URL="${API_BASE}" \
        $CURL_BIN -s --connect-timeout "$CURL_CONNECT_TIMEOUT" -m "$CURL_MAX_TIME" \
        -o /dev/null -w '%{http_code}' \
        -X POST \
        -d "device=$1" \
        -d "change=${2:-}" \
        "$full_url" >>"$LOGFILE" 2>&1 || true
      rc=0
      http_code="systemd_system_triggered"
    fi
    echo "[$ts] udev_rescan: systemd-run triggered (non-blocking), curl executing in background with network access" >>"$LOGFILE" || true
  else
    # Fallback: direct curl (may fail in udev environment, but try anyway)
    echo "[$ts] udev_rescan: systemd-run not available, trying direct curl (may fail)" >>"$LOGFILE" || true
    curl_stderr="${TMPDIR}/udev_curl_$$.err"
    http_code=$($CURL_BIN -s --connect-timeout "$CURL_CONNECT_TIMEOUT" -m "$CURL_MAX_TIME" -o /dev/null -w '%{http_code}' \
      -X POST \
      -d "device=$1" \
      -d "change=${2:-}" \
      "$full_url" 2>"$curl_stderr")
    rc=$?
    curl_error=$(cat "$curl_stderr" 2>/dev/null || echo "")
    rm -f "$curl_stderr"
    
    if [ $rc -ne 0 ] || [ -z "$http_code" ] || [ "$http_code" = "000" ]; then
      echo "[$ts] udev_rescan: Direct curl FAILED rc=$rc http=${http_code:-"-"} error='${curl_error:0:100}'" >>"$LOGFILE" || true
    else
      echo "[$ts] udev_rescan: Direct curl SUCCESS rc=$rc http=${http_code}" >>"$LOGFILE" || true
    fi
  fi
  
  # Also log result to drive_manager.log
  if [ -w "$(dirname "$DRIVE_MANAGER_LOG")" ] 2>/dev/null; then
    echo "[$ts] UDEV result: curl rc=$rc http=${http_code:-"-"} for ${endpoint}" >>"$DRIVE_MANAGER_LOG" || true
  fi
  
  set -e
) 9>"$LOCK"
