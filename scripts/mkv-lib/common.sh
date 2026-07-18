#!/bin/bash
# common.sh - Shared utilities for mkv command
# This file is sourced by the main mkv script and other library files

# Logging function
log() {
  printf '[mkv] %s %s\n' "$(date '+%H:%M:%S')" "$*"
}

# Check if a Docker container exists
container_exists() {
  docker ps -aq --filter "name=^${1}$" | grep -q .
}

# Check if a Docker container is running
container_running() {
  local state
  state=$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null) || return 1
  [ "$state" = "true" ]
}

# Wait for Postgres to be ready
wait_for_postgres() {
  local container="$1"
  local user="$2"
  log "Waiting for Postgres to accept connections..."
  for _ in {1..20}; do
    if docker exec "$container" pg_isready -U "$user" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  log "Postgres did not become ready in time."
  return 1
}

# List optical devices
list_optical_devices() {
  ls /dev/sr* 2>/dev/null || true
}

# Eject optical disc tray
eject_tray() {
  local devs
  devs=$(list_optical_devices)
  [ -z "$devs" ] && return
  for dev in $devs; do
    log "Ejecting tray $dev..."
    if command -v sudo >/dev/null 2>&1; then
      sudo eject "$dev" >/dev/null 2>&1 || true
    else
      eject "$dev" >/dev/null 2>&1 || true
    fi
  done
}

# Close optical disc tray
close_tray() {
  local devs
  devs=$(list_optical_devices)
  [ -z "$devs" ] && return
  for dev in $devs; do
    log "Closing tray $dev..."
    if command -v sudo >/dev/null 2>&1; then
      sudo eject -t "$dev" >/dev/null 2>&1 || true
    else
      eject -t "$dev" >/dev/null 2>&1 || true
    fi
  done
}

# Rotate log files
rotate_logs() {
  local log_dir="$1"
  mkdir -p "$log_dir"
  local ts
  ts="$(date '+%Y%m%d-%H%M%S')"
  for f in "$log_dir"/*.log; do
    [ -f "$f" ] || continue
    mv "$f" "${f}.${ts}" || true
  done
}
