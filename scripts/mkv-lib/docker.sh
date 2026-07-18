#!/bin/bash
# docker.sh - Docker container development environment functions
# Extracted from mkv-docker-dev for modular mkv command

#--------------------------
# Frontend Build
#--------------------------
build_frontend() {
  echo "🔨 Building frontend..."
  cd "$PROJECT_ROOT/Frontend"
  npm run build
}

#--------------------------
# Container Management
#--------------------------
stop_container() {
  echo "🧹 Stopping and removing existing container..."
  docker stop mkv-auto 2>/dev/null || true
  docker rm mkv-auto 2>/dev/null || true
}

#--------------------------
# Rip-in-progress Guard (#495)
#--------------------------

# Populated by check_rip_in_progress with one tab-separated line per active
# rip: `<job_id>\t<mount_point>\t<disc_num>\t<rip_state>`. Used by the guard
# to display what's running.
RIP_GUARD_DETAILS=""

# Returns 0 (success) if a rip is currently in progress, 1 otherwise.
# A "rip in progress" means a job with job_status='running' AND
# rip_state IN ('running', 'pending') — pending here is the brief window
# between celery picking up the task and MakeMKV emitting first progress.
check_rip_in_progress() {
  RIP_GUARD_DETAILS=""
  # If the container isn't up, nothing can be ripping.
  if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^mkv-auto$'; then
    return 1
  fi
  local rows
  rows=$(docker exec -u postgres mkv-auto psql -d discs -A -t -F$'\t' -c \
    "SELECT id, mount_point, COALESCE(disc_num::text, '?'), rip_state
     FROM jobs
     WHERE job_status='running' AND rip_state IN ('running','pending');" \
    2>/dev/null) || rows=""
  if [ -n "$rows" ]; then
    RIP_GUARD_DETAILS="$rows"
    return 0
  fi
  return 1
}

# Block container-mutating commands while a rip is in flight.
# Reads --force and --no-wait from the caller's argv (passes the rest through).
#
#   default      poll until rip completes (10s interval), then continue
#   --force      print warning, continue immediately (rip will likely die)
#   --no-wait    refuse with a clear message, exit 1 (for CI / scripts)
guard_rip_in_progress() {
  local force=0
  local no_wait=0
  for arg in "$@"; do
    case "$arg" in
      --force) force=1 ;;
      --no-wait) no_wait=1 ;;
    esac
  done

  if ! check_rip_in_progress; then
    return 0
  fi

  if [ "$force" = "1" ]; then
    echo "⚠️  Rip in progress — proceeding anyway (--force):"
    printf '%s\n' "$RIP_GUARD_DETAILS" | sed 's/^/    /'
    return 0
  fi

  if [ "$no_wait" = "1" ]; then
    echo "❌ Rip in progress — refusing to continue (--no-wait was set)."
    echo "    Active rip(s):"
    printf '%s\n' "$RIP_GUARD_DETAILS" | sed 's/^/      /'
    echo "    Pass --force to stop anyway, or omit --no-wait to wait."
    return 1
  fi

  echo "🎞️  Rip in progress — waiting for it to complete."
  echo "    Active rip(s):"
  printf '%s\n' "$RIP_GUARD_DETAILS" | sed 's/^/      /'
  echo "    Pass --force in a new shell to override and stop the rip."
  local poll_interval="${MKVAUTO_RIP_GUARD_POLL_SECONDS:-10}"
  while check_rip_in_progress; do
    sleep "$poll_interval"
    echo "    ...still ripping (checked $(date +%T))"
  done
  echo "✅ Rip complete — proceeding."
  return 0
}

remove_data_volume() {
  echo "🗑️  Removing data volume for clean state..."
  docker volume rm mkv-auto-data 2>/dev/null || true
}

clear_docker_data() {
  local DOCKER_DATA="${MKVAUTO_DOCKER_DATA:-$PROJECT_ROOT/docker-data}"
  if [ -z "$DOCKER_DATA" ] || [ "$DOCKER_DATA" = "/" ]; then
    echo "⚠️  Refusing to clear empty or root DOCKER_DATA"
    return 1
  fi
  echo "🗑️  Clearing mounted storage folder ($DOCKER_DATA)..."
  rm -rf "${DOCKER_DATA:?}"
  mkdir -p "$DOCKER_DATA"
  echo "✅ Mounted storage cleared"
}

clear_python_cache() {
  echo "🧹 Clearing Python bytecode cache..."
  find "$PROJECT_ROOT/Backend" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
  find "$PROJECT_ROOT/Backend" -type f -name "*.pyc" -delete 2>/dev/null || true
  echo "✅ Python cache cleared from host"
}

clear_python_cache_in_container() {
  echo "🧹 Clearing Python bytecode cache in container..."
  docker exec mkv-auto find /app/backend -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
  docker exec mkv-auto find /app/backend -type f -name "*.pyc" -delete 2>/dev/null || true
  echo "✅ Python cache cleared in container"
}

start_container() {
  # App data (logs, jobs, tmp, makemkv) lives on host for persistence across restarts
  local DOCKER_DATA="${MKVAUTO_DOCKER_DATA:-$PROJECT_ROOT/docker-data}"
  mkdir -p "$DOCKER_DATA"

  echo "🚀 Starting container with volume mounts..."
  # ENABLE_DEVMODE is forwarded so `ENABLE_DEVMODE=1 mkv docker start` flips
  # backend dev mode on (DEV badge, dev menu, /setup?force=1 preview).
  # (Note: MKVAUTO_RENAME_DIRECT_TO_DEST forwarding was dropped in 5d —
  # the transient/-drop direct-to-destination flow is now the only
  # production behaviour for local mode and the env var is ignored.)
  docker run -d \
    --name mkv-auto \
    -p 0.0.0.0:80:80 \
    -p 0.0.0.0:5432:5432 \
    -p 0.0.0.0:6379:6379 \
    -p 0.0.0.0:8000:8000 \
    -e ENABLE_DEVMODE="${ENABLE_DEVMODE:-}" \
    -v "$PROJECT_ROOT/Backend:/app/backend" \
    -v "$PROJECT_ROOT/Frontend/dist/disc-ripper-ui:/app/frontend/dist/disc-ripper-ui" \
    -v mkv-auto-data:/data \
    -v "$DOCKER_DATA:/data/mkvauto" \
    -v /dev:/dev \
    --privileged \
    mkv-auto:latest

  # Dev container device discovery — bind-mounts host /dev so the container
  # picks up optical drives dynamically. The previous approach (#570 +
  # static --device flags) had two friction points:
  #
  #   1. --device=/dev/sr0 + --device=/dev/sg0 are evaluated at container
  #      start. If the host's optical drive lands at sr1 (because another
  #      drive holds sr0), the container can't see it without a restart.
  #   2. After a hot-plug or USB reset, kernel renumbering of /dev/srN
  #      meant the running container's /dev pinned to a dead node.
  #
  # The bind covers /dev/disk/by-id (was the separate :ro mount from #570),
  # /dev/sr*, /dev/sg*, and any future device node the host kernel creates.
  # The container is already --privileged, so this is equivalent to the
  # previous cgroup posture; the only change is that device-node creation
  # is dynamic instead of frozen at container-start time.

  echo "⏳ Waiting for services to start..."
  sleep 15

  echo "✅ Container started! Checking status..."
  docker exec mkv-auto supervisorctl status

  # Get host IP for external access info
  local host_ip=$(hostname -I | awk '{print $1}')
  
  echo ""
  echo "🌐 Services (accessible externally):"
  echo "   Frontend:  http://localhost or http://${host_ip}"
  echo "   Backend:   http://localhost:8000 or http://${host_ip}:8000"
  echo "   Postgres:  localhost:5432 or ${host_ip}:5432"
  echo "   Redis:     localhost:6379 or ${host_ip}:6379"
  echo ""
  echo "📋 All logs: docker logs -f mkv-auto"
  echo ""
  echo "💡 Backend changes are live via volume mount!"
  echo "💡 For frontend changes, run: mkv docker rebuild frontend"
}

check_container_running() {
  if ! docker ps --filter "name=mkv-auto" --format "{{.Names}}" | grep -q "mkv-auto"; then
    echo "❌ Container is not running. Start it first with: mkv docker start"
    exit 1
  fi
}

#--------------------------
# Log Management
#--------------------------
resolve_log_path() {
  local log_name="$1"
  local log_file=""
  
  if [[ "$log_name" == *.log ]]; then
    log_file="$log_name"
    log_name="${log_name%.log}"
  else
    log_file="${log_name}.log"
  fi
  
  local app_log="/data/mkvauto/logs/${log_file}"
  if docker exec mkv-auto test -f "$app_log" 2>/dev/null; then
    echo "$app_log"
    return 0
  fi
  
  local supervisor_log="/var/log/supervisor/${log_file}"
  if docker exec mkv-auto test -f "$supervisor_log" 2>/dev/null; then
    echo "$supervisor_log"
    return 0
  fi
  
  local supervisor_err_log="/var/log/supervisor/${log_name}_err.log"
  if docker exec mkv-auto test -f "$supervisor_err_log" 2>/dev/null; then
    echo "$supervisor_err_log"
    return 0
  fi
  
  return 1
}

# Celery worker programs in Supervisor (keep in sync with Docker/supervisord.conf).
MKV_DOCKER_CELERY_PROGRAMS="celery celery-rip celery-postprocess celery-transfer celery-preview"

# Returns app log paths under /data/mkvauto/logs for Celery virtual targets (one per line).
# Usage: mapfile -t paths < <(get_celery_log_paths "celery-rip" "stdout")
get_celery_log_paths() {
  local target="${1:?}"
  local stream="${2:-}"
  local base="/data/mkvauto/logs"
  case "$target" in
    celery)
      # All worker logs merged (Supervisor writes each program to its own file).
      case "$stream" in
        stdout|stderr)
          echo "$base/celery_rip.log"
          echo "$base/celery_postprocess.log"
          echo "$base/celery_transfer.log"
          echo "$base/celery_preview.log"
          echo "$base/celery.log"
          ;;
        *)
          echo "$base/celery_rip.log"
          echo "$base/celery_postprocess.log"
          echo "$base/celery_transfer.log"
          echo "$base/celery_preview.log"
          echo "$base/celery.log"
          ;;
      esac
      ;;
    celery-rip)
      echo "$base/celery_rip.log"
      ;;
    celery-postprocess)
      echo "$base/celery_postprocess.log"
      ;;
    celery-transfer)
      echo "$base/celery_transfer.log"
      ;;
    celery-preview)
      echo "$base/celery_preview.log"
      ;;
    celery-extra)
      # Alias: default-queue (maintenance) worker only — same as log name celery.log
      echo "$base/celery.log"
      ;;
    *)
      return 1
      ;;
  esac
}

list_logs() {
  check_container_running
  
  echo "📋 Available logs:"
  echo ""
  echo "Application logs (/data/mkvauto/logs/):"
  docker exec mkv-auto find /data/mkvauto/logs -type f -name "*.log" -exec stat -c '%s %n' {} \; 2>/dev/null | while read -r size log; do
    local basename=$(basename "$log")
    local name="${basename%.log}"
    local size_mb=$(echo "scale=2; $size/1024/1024" | bc)
    echo "  • $name (${size_mb}MB)"
  done
  
  echo ""
  echo "Supervisor logs (/var/log/supervisor/):"
  docker exec mkv-auto find /var/log/supervisor -type f -name "*.log" -exec stat -c '%s %n' {} \; 2>/dev/null | while read -r size log; do
    local basename=$(basename "$log")
    local name="${basename%.log}"
    local size_mb=$(echo "scale=2; $size/1024/1024" | bc)
    echo "  • $name (${size_mb}MB)"
  done
  
  echo ""
  echo "Usage:"
  echo "  mkv docker logs <name>     # View log"
  echo "  mkv docker logs <name> -f  # Follow log in real-time"
  echo ""
  echo "Celery: celery (all workers), celery-rip, celery-postprocess, celery-transfer, celery-preview, celery-extra (maintenance log)"
  echo "  Append stdout or stderr to filter stream; -f to follow."
}

view_log() {
  local log_name="$1"
  local stream="${2:-}"
  check_container_running

  if [[ "$log_name" == "celery" || "$log_name" == "celery-rip" || "$log_name" == "celery-postprocess" || "$log_name" == "celery-transfer" || "$log_name" == "celery-preview" || "$log_name" == "celery-extra" ]]; then
    local paths
    mapfile -t paths < <(get_celery_log_paths "$log_name" "$stream")
    if [[ ${#paths[@]} -eq 0 ]]; then
      echo "❌ No paths for '$log_name'"
      exit 1
    fi
    echo "📄 Viewing: ${paths[*]}"
    echo "   (Press 'q' to quit, 'G' to go to end, 'g' to go to start)"
    echo ""
    docker exec -it mkv-auto sh -c 'cat "$@" | less -R +G' _ "${paths[@]}"
    return
  fi

  local log_path
  if ! log_path=$(resolve_log_path "$log_name"); then
    echo "❌ Log '$log_name' not found"
    echo ""
    echo "Available logs:"
    docker exec mkv-auto sh -c 'ls -1 /data/mkvauto/logs/*.log 2>/dev/null | xargs -n1 basename | sed "s/.log$//"' 2>/dev/null | sed 's/^/  • /'
    echo ""
    docker exec mkv-auto sh -c 'ls -1 /var/log/supervisor/*.log 2>/dev/null | xargs -n1 basename | sed "s/.log$//"' 2>/dev/null | sed 's/^/  • /'
    exit 1
  fi

  echo "📄 Viewing: $log_path"
  echo "   (Press 'q' to quit, 'G' to go to end, 'g' to go to start)"
  echo ""
  docker exec -it mkv-auto less -R +G "$log_path"
}

follow_log() {
  local log_name="$1"
  local stream="${2:-}"
  check_container_running

  if [[ "$log_name" == "celery" || "$log_name" == "celery-rip" || "$log_name" == "celery-postprocess" || "$log_name" == "celery-transfer" || "$log_name" == "celery-preview" || "$log_name" == "celery-extra" ]]; then
    local paths
    mapfile -t paths < <(get_celery_log_paths "$log_name" "$stream")
    if [[ ${#paths[@]} -eq 0 ]]; then
      echo "❌ No paths for '$log_name'"
      exit 1
    fi
    echo "📄 Following: ${paths[*]}"
    echo "   (Press Ctrl+C to stop)"
    echo ""
    docker exec -it mkv-auto tail -f "${paths[@]}"
    return
  fi

  local log_path
  if ! log_path=$(resolve_log_path "$log_name"); then
    echo "❌ Log '$log_name' not found"
    echo ""
    echo "Available logs:"
    docker exec mkv-auto sh -c 'ls -1 /data/mkvauto/logs/*.log 2>/dev/null | xargs -n1 basename | sed "s/.log$//"' 2>/dev/null | sed 's/^/  • /'
    echo ""
    docker exec mkv-auto sh -c 'ls -1 /var/log/supervisor/*.log 2>/dev/null | xargs -n1 basename | sed "s/.log$//"' 2>/dev/null | sed 's/^/  • /'
    exit 1
  fi

  echo "📄 Following: $log_path"
  echo "   (Press Ctrl+C to stop)"
  echo ""
  docker exec -it mkv-auto tail -f "$log_path"
}

#--------------------------
# Health Checks
#--------------------------
check_makemkv() {
  check_container_running
  
  echo "🔍 Checking MakeMKV installation..."
  echo ""
  
  local has_errors=false
  
  echo "📁 Data directory:"
  if docker exec mkv-auto test -f /data/mkvauto/makemkv/bin/makemkvcon 2>/dev/null; then
    echo "  ✅ Binary exists: /data/mkvauto/makemkv/bin/makemkvcon"
    if docker exec mkv-auto test -x /data/mkvauto/makemkv/bin/makemkvcon 2>/dev/null; then
      echo "     (executable)"
    else
      echo "     ❌ Not executable!"
      has_errors=true
    fi
  else
    echo "  ❌ Binary missing: /data/mkvauto/makemkv/bin/makemkvcon"
    echo "     Install MakeMKV via Settings → MakeMKV in the web UI"
    has_errors=true
  fi
  
  if docker exec mkv-auto test -f /data/mkvauto/makemkv/lib/libmakemkv.so.1 2>/dev/null; then
    echo "  ✅ Library exists: libmakemkv.so.1"
  else
    echo "  ⚠️  Library missing: libmakemkv.so.1 (optional)"
  fi
  
  if docker exec mkv-auto test -f /data/mkvauto/makemkv/lib/libdriveio.so.0 2>/dev/null; then
    echo "  ✅ Library exists: libdriveio.so.0"
  else
    echo "  ⚠️  Library missing: libdriveio.so.0 (optional)"
  fi
  
  echo ""
  echo "🔗 System symlinks:"
  
  if docker exec mkv-auto test -L /usr/bin/makemkvcon 2>/dev/null; then
    local target=$(docker exec mkv-auto readlink /usr/bin/makemkvcon 2>/dev/null)
    echo "  ✅ Symlink exists: /usr/bin/makemkvcon → $target"
    
    if docker exec mkv-auto test -f /usr/bin/makemkvcon 2>/dev/null; then
      echo "     (target is accessible)"
    else
      echo "     ❌ Target is not accessible!"
      has_errors=true
    fi
  elif docker exec mkv-auto test -f /usr/bin/makemkvcon 2>/dev/null; then
    echo "  ✅ Binary exists: /usr/bin/makemkvcon (not a symlink)"
  else
    echo "  ❌ Binary missing: /usr/bin/makemkvcon"
    has_errors=true
  fi
  
  if docker exec mkv-auto test -L /usr/lib/libmakemkv.so.1 2>/dev/null; then
    local target=$(docker exec mkv-auto readlink /usr/lib/libmakemkv.so.1 2>/dev/null)
    echo "  ✅ Library symlink: /usr/lib/libmakemkv.so.1 → $target"
  else
    echo "  ⚠️  Library symlink missing: /usr/lib/libmakemkv.so.1 (optional)"
  fi
  
  if docker exec mkv-auto test -L /usr/lib/libdriveio.so.0 2>/dev/null; then
    local target=$(docker exec mkv-auto readlink /usr/lib/libdriveio.so.0 2>/dev/null)
    echo "  ✅ Library symlink: /usr/lib/libdriveio.so.0 → $target"
  else
    echo "  ⚠️  Library symlink missing: /usr/lib/libdriveio.so.0 (optional)"
  fi
  
  echo ""
  echo "🧪 Functionality test:"
  
  if docker exec mkv-auto test -x /usr/bin/makemkvcon 2>/dev/null; then
    if version=$(docker exec mkv-auto sh -c 'strings /usr/bin/makemkvcon 2>/dev/null | grep -E "^v[0-9]+\.[0-9]+\.[0-9]+" | head -1'); then
      echo "  ✅ MakeMKV is functional"
      echo "     Version: $version"
    else
      echo "  ✅ MakeMKV is functional"
      echo "     Version: Unable to determine"
    fi
  else
    echo "  ❌ Cannot execute makemkvcon"
    has_errors=true
  fi
  
  echo ""
  if [ "$has_errors" = true ]; then
    echo "❌ MakeMKV installation has errors"
    echo ""
    echo "To fix:"
    echo "  1. Restart container: mkv docker restart"
    echo "  2. If still failing, install via web UI: http://localhost/settings"
    exit 1
  else
    echo "✅ MakeMKV installation is valid"
  fi
}

#--------------------------
# Watch Command
#--------------------------
docker_watch() {
  check_container_running
  
  local interval=1
  local command=""
  
  if [[ "${1:-}" == "-n"* ]]; then
    if [[ "$1" == "-n" ]]; then
      interval="$2"
      shift 2
    else
      interval="${1#-n}"
      shift
    fi
  fi
  
  command="$*"
  
  if [ -z "$command" ]; then
    echo "Usage: mkv docker watch [-n<seconds>] <command>"
    echo ""
    echo "Examples:"
    echo "  mkv docker watch 'ps -elf'"
    echo "  mkv docker watch -n5 'df -h'"
    echo "  mkv docker watch 'supervisorctl status'"
    exit 1
  fi
  
  echo "👁️  Watching: $command (interval: ${interval}s)"
  echo "   Press Ctrl+C to stop"
  echo ""
  
  docker exec -it mkv-auto watch -n "$interval" "$command"
}

#--------------------------
# Main Docker Commands
#--------------------------
docker_start() {
  echo "Starting MKV-Auto container (keeping data)..."
  build_frontend
  stop_container
  if [[ "${MKVAUTO_DOCKER_TRAY_CYCLE:-}" == "1" ]]; then
    eject_tray
  fi
  clear_python_cache
  start_container
  if [[ "${MKVAUTO_DOCKER_TRAY_CYCLE:-}" == "1" ]]; then
    close_tray
  fi
}

docker_restart() {
  guard_rip_in_progress "$@" || exit 1
  echo "Restarting MKV-Auto (keeping data volume)..."
  stop_container
  if [[ "${MKVAUTO_DOCKER_TRAY_CYCLE:-}" == "1" ]]; then
    eject_tray
  fi
  clear_python_cache
  start_container
  if [[ "${MKVAUTO_DOCKER_TRAY_CYCLE:-}" == "1" ]]; then
    close_tray
  fi
}

docker_rebuild() {
  # Strip guard flags before deriving target so callers can mix them:
  # `mkv docker rebuild backend --force` etc.
  local target=""
  local args=()
  for arg in "$@"; do
    case "$arg" in
      --force|--no-wait) ;;  # consumed by the guard
      *) args+=("$arg") ;;
    esac
  done
  target="${args[0]:-all}"
  guard_rip_in_progress "$@" || exit 1
  check_container_running
  
  case "$target" in
    frontend)
      echo "Rebuilding frontend only..."
      build_frontend
      echo "🔄 Restarting frontend service..."
      docker exec mkv-auto supervisorctl restart nginx
      echo "⏳ Waiting for service to stabilize..."
      sleep 3
      echo "✅ Frontend rebuilt and restarted!"
      docker exec mkv-auto supervisorctl status nginx
      ;;
    backend)
      echo "Rebuilding backend only..."
      clear_python_cache_in_container
      echo "🔄 Restarting backend services..."
      docker exec mkv-auto supervisorctl restart uvicorn $MKV_DOCKER_CELERY_PROGRAMS
      echo "⏳ Waiting for services to stabilize..."
      sleep 5
      echo "✅ Backend rebuilt and restarted!"
      docker exec mkv-auto supervisorctl status uvicorn $MKV_DOCKER_CELERY_PROGRAMS
      ;;
    all|"")
      echo "Rebuilding frontend and backend..."
      build_frontend
      clear_python_cache_in_container
      echo "🔄 Restarting all services..."
      docker exec mkv-auto supervisorctl restart all
      echo "⏳ Waiting for services to stabilize..."
      sleep 5
      echo "✅ All services rebuilt and restarted!"
      docker exec mkv-auto supervisorctl status
      ;;
    *)
      echo "Error: Unknown rebuild target '$target'"
      echo "Usage: mkv docker rebuild [frontend|backend]"
      echo "  frontend  - Rebuild frontend only"
      echo "  backend   - Clear Python cache and restart backend services"
      echo "  (none)    - Rebuild both frontend and backend"
      exit 1
      ;;
  esac
}

docker_reset() {
  local target="${1:-all}"
  case "$target" in
    all|"")
      echo "Resetting MKV-Auto (clean state - removing data volume and clearing docker-data)..."
      build_frontend
      stop_container
      if [[ "${MKVAUTO_DOCKER_TRAY_CYCLE:-}" == "1" ]]; then
        eject_tray
      fi
      remove_data_volume
      clear_docker_data
      start_container
      if [[ "${MKVAUTO_DOCKER_TRAY_CYCLE:-}" == "1" ]]; then
        close_tray
      fi
      ;;
    db)
      check_container_running
      echo "Resetting database and Redis (keeping settings, MakeMKV, job data)..."
      # Use default Unix socket (peer auth) so postgres does not prompt for password
      docker exec mkv-auto su - postgres -c "psql -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'discs' AND pid <> pg_backend_pid();\"" 2>/dev/null || true
      docker exec mkv-auto su - postgres -c "psql -c \"DROP DATABASE IF EXISTS discs;\"" || exit 1
      docker exec mkv-auto su - postgres -c "psql -c \"CREATE DATABASE discs;\"" || exit 1
      docker exec mkv-auto su - postgres -c "psql -d discs -c \"GRANT ALL ON SCHEMA public TO mkvauto;\"" 2>/dev/null || true
      docker exec mkv-auto redis-cli FLUSHALL >/dev/null 2>&1 || true
      echo "Running database migrations on fresh DB..."
      # Use embedded DB URL so Alembic connects as mkvauto (container default may be unset or point to external)
      if ! docker exec -e DATABASE_URL="postgresql://mkvauto:changeme@127.0.0.1:5432/discs" mkv-auto bash -c 'cd /app/backend && /app/venv/bin/alembic upgrade head'; then
        echo "⚠️  Migrations failed. If using external DB, run: docker exec -e DATABASE_URL=\"<your-url>\" mkv-auto bash -c 'cd /app/backend && /app/venv/bin/alembic upgrade head'"
        exit 1
      fi
      echo "Restarting backend..."
      docker exec mkv-auto supervisorctl restart uvicorn $MKV_DOCKER_CELERY_PROGRAMS
      echo "✅ Database and Redis reset; migrations applied."
      ;;
    settings)
      echo "Removing app settings (keeping DB, MakeMKV, job data)..."
      if docker ps --filter "name=mkv-auto" --format "{{.Names}}" | grep -q "mkv-auto"; then
        docker exec mkv-auto rm -rf /data/mkvauto/backend
      else
        docker run --rm -v mkv-auto-data:/data alpine rm -rf /data/mkvauto/backend
      fi
      echo "✅ Settings reset."
      ;;
    makemkv)
      echo "Removing MakeMKV install (keeping DB, settings, job data)..."
      if docker ps --filter "name=mkv-auto" --format "{{.Names}}" | grep -q "mkv-auto"; then
        docker exec mkv-auto rm -rf /data/mkvauto/makemkv
      else
        docker run --rm -v mkv-auto-data:/data alpine rm -rf /data/mkvauto/makemkv
      fi
      echo "✅ MakeMKV reset. Restart container to re-run symlinks; reinstall MakeMKV via Settings → MakeMKV if needed."
      ;;
    *)
      echo "Error: Unknown reset target '$target'"
      echo "Usage: mkv docker reset [TARGET]"
      echo "  TARGET: db, settings, makemkv, or all (omit for full volume reset)"
      exit 1
      ;;
  esac
}

docker_stop() {
  guard_rip_in_progress "$@" || exit 1
  echo "Stopping MKV-Auto..."
  stop_container
  if [[ "${MKVAUTO_DOCKER_TRAY_CYCLE:-}" == "1" ]]; then
    eject_tray
  fi
  echo "✅ Container stopped and removed"
}

docker_status() {
  echo "Checking MKV-Auto container status..."
  if docker ps --filter "name=mkv-auto" --format "{{.Names}}" | grep -q "mkv-auto"; then
    echo "✅ Container is running"
    echo ""
    docker ps --filter "name=mkv-auto" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    echo ""
    echo "Service status:"
    docker exec mkv-auto supervisorctl status || true
  else
    echo "❌ Container is not running"
    if docker ps -a --filter "name=mkv-auto" --format "{{.Names}}" | grep -q "mkv-auto"; then
      echo "⚠️  Container exists but is stopped"
    else
      echo "⚠️  Container does not exist"
    fi
  fi
}

docker_logs() {
  local subcommand="${1:-list}"
  shift || true

  case "$subcommand" in
    list)
      list_logs
      return
      ;;
  esac

  local log_name="$subcommand"
  local stream=""
  local follow_mode=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -f|--follow)
        follow_mode=true
        shift
        ;;
      stdout|stderr)
        stream="$1"
        shift
        ;;
      *)
        echo "❌ Unknown option: $1"
        echo "Usage: mkv docker logs <name> [stdout|stderr] [-f]"
        exit 1
        ;;
    esac
  done

  if [[ "$follow_mode" == true ]]; then
    follow_log "$log_name" "$stream"
  else
    view_log "$log_name" "$stream"
  fi
}

docker_check() {
  local subcommand="${1:-}"
  
  case "$subcommand" in
    mkv)
      check_makemkv
      ;;
    "")
      echo "Usage: mkv docker check <target>"
      echo ""
      echo "Available checks:"
      echo "  mkv    Check MakeMKV installation"
      ;;
    *)
      echo "Error: Unknown check target '$subcommand'"
      echo "Available: mkv"
      exit 1
      ;;
  esac
}
