#!/bin/bash
# dev.sh - Local development environment functions
# Extracted from manage.sh for modular mkv command

#--------------------------
# Configuration
#--------------------------
POSTGRES_CONTAINER="mkv_postgres"
REDIS_CONTAINER="mkv_redis"
POSTGRES_DB="discs"
POSTGRES_USER="postgres"
POSTGRES_PASSWORD="ripper_pass"

# Dev seed: default repo devseed/ (used by devseed-backup / devseed-reload)
MKVAUTO_DEVSEED_ROOT="${MKVAUTO_DEVSEED_ROOT:-${PROJECT_ROOT}/devseed}"

# Repo paths
FRONTEND_DIR="${FRONTEND_DIR:-${PROJECT_ROOT}/Frontend}"
BACKEND_DIR="${BACKEND_DIR:-${PROJECT_ROOT}/Backend}"
VENV="${VENV:-${BACKEND_DIR}/.venv}"

BACKEND_API_URL="${MKVAUTO_BACKEND_URL:-http://127.0.0.1:8000}"

# App/data roots (override with MKVAUTO_ROOT / MKVAUTO_DATA)
MKVAUTO_ROOT="${MKVAUTO_ROOT:-$HOME/MakeMKV-Auto}"
MKVAUTO_DATA="${MKVAUTO_DATA:-${MKVAUTO_ROOT}/data}"
MKVAUTO_TMP_DIR="${MKVAUTO_TMP_DIR:-${MKVAUTO_ROOT}/tmp}"

# Local state/logs (outside repo by default)
MKVAUTO_PIDS_DIR="${MKVAUTO_PIDS_DIR:-${PIDS_DIR:-${MKVAUTO_ROOT}/.pids}}"
MKVAUTO_LOG_DIR="${MKVAUTO_LOG_DIR:-${LOG_DIR:-${MKVAUTO_ROOT}/logs}}"
PIDS_DIR="$MKVAUTO_PIDS_DIR"
LOG_DIR="$MKVAUTO_LOG_DIR"

# Data/storage dirs
DATA_ROOT="${MKVAUTO_DATA}"
POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-${MKVAUTO_ROOT}/backend/postgres}"
REDIS_DATA_DIR="${REDIS_DATA_DIR:-${MKVAUTO_ROOT}/backend/redis}"
MAKEMKV_DATA_DIR_DEFAULT="${MAKEMKV_DATA_DIR_DEFAULT:-${MKVAUTO_TMP_DIR}}"

ROOT_HELPER="${BACKEND_DIR}/root_update_helper.py"
ROOT_HELPER_SOCK="${MAKEMKV_ROOT_HELPER_SOCK:-/tmp/makemkv_auto.sock}"
UDEV_RULE_TEMPLATE="${PROJECT_ROOT}/Deploy/udev/99-mkva-disc.rules.template"
UDEV_RULE_DEST="/etc/udev/rules.d/99-mkva-disc.rules"
RESCAN_SERVICE_UNIT="/etc/systemd/system/mkva-rescan@.service"
RESCAN_EJECT_SERVICE_UNIT="/etc/systemd/system/mkva-rescan-eject@.service"

mkdir -p "$MKVAUTO_PIDS_DIR"
mkdir -p "$MKVAUTO_LOG_DIR"
mkdir -p "$POSTGRES_DATA_DIR" "$REDIS_DATA_DIR" "$MAKEMKV_DATA_DIR_DEFAULT"

#--------------------------
# System Dependencies
#--------------------------
ensure_system_dependencies() {
  # Check for samba-client (needed for anonymous SMB access)
  if ! command -v smbclient >/dev/null 2>&1; then
    log "smbclient not found. Attempting to install samba-client..."
    
    if command -v apt-get >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq samba-client || {
          log "Warning: Failed to install samba-client. Install manually with: sudo apt-get install samba-client"
        }
      else
        log "Warning: smbclient not found and sudo access required to install."
      fi
    elif command -v yum >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo yum install -y -q samba-client || {
          log "Warning: Failed to install samba-client."
        }
      else
        log "Warning: smbclient not found and sudo access required."
      fi
    elif command -v dnf >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo dnf install -y -q samba-client || {
          log "Warning: Failed to install samba-client."
        }
      else
        log "Warning: smbclient not found and sudo access required."
      fi
    elif command -v pacman >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo pacman -Sy --noconfirm samba >/dev/null 2>&1 || {
          log "Warning: Failed to install samba."
        }
      else
        log "Warning: smbclient not found and sudo access required."
      fi
    elif command -v brew >/dev/null 2>&1; then
      brew install samba >/dev/null 2>&1 || {
        log "Warning: Failed to install samba."
      }
    else
      log "Warning: Could not detect package manager. smbclient not found."
    fi
  fi
  
  # Check for cifs-utils (needed for SMB/CIFS mount support)
  if ! command -v mount.cifs >/dev/null 2>&1 && [ ! -f /sbin/mount.cifs ] && [ ! -f /usr/sbin/mount.cifs ]; then
    log "mount.cifs not found. Attempting to install cifs-utils..."
    
    if command -v apt-get >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq cifs-utils || {
          log "Warning: Failed to install cifs-utils."
        }
      else
        log "Warning: mount.cifs not found and sudo access required."
      fi
    elif command -v yum >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo yum install -y -q cifs-utils || {
          log "Warning: Failed to install cifs-utils."
        }
      else
        log "Warning: mount.cifs not found and sudo access required."
      fi
    elif command -v dnf >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo dnf install -y -q cifs-utils || {
          log "Warning: Failed to install cifs-utils."
        }
      else
        log "Warning: mount.cifs not found and sudo access required."
      fi
    elif command -v pacman >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo pacman -Sy --noconfirm cifs-utils >/dev/null 2>&1 || {
          log "Warning: Failed to install cifs-utils."
        }
      else
        log "Warning: mount.cifs not found and sudo access required."
      fi
    elif command -v brew >/dev/null 2>&1; then
      log "Note: macOS has built-in SMB support, cifs-utils not needed."
    else
      log "Warning: Could not detect package manager. mount.cifs not found."
    fi
  fi
  
  # Check for nfs-common (needed for NFS mount support)
  if ! command -v mount.nfs >/dev/null 2>&1 && [ ! -f /sbin/mount.nfs ] && [ ! -f /usr/sbin/mount.nfs ]; then
    log "mount.nfs not found. Attempting to install nfs-common/nfs-utils..."
    
    if command -v apt-get >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq nfs-common || {
          log "Warning: Failed to install nfs-common."
        }
      else
        log "Warning: mount.nfs not found and sudo access required."
      fi
    elif command -v yum >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo yum install -y -q nfs-utils || {
          log "Warning: Failed to install nfs-utils."
        }
      else
        log "Warning: mount.nfs not found and sudo access required."
      fi
    elif command -v dnf >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo dnf install -y -q nfs-utils || {
          log "Warning: Failed to install nfs-utils."
        }
      else
        log "Warning: mount.nfs not found and sudo access required."
      fi
    elif command -v pacman >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo pacman -Sy --noconfirm nfs-utils >/dev/null 2>&1 || {
          log "Warning: Failed to install nfs-utils."
        }
      else
        log "Warning: mount.nfs not found and sudo access required."
      fi
    elif command -v brew >/dev/null 2>&1; then
      log "Note: macOS has built-in NFS support."
    else
      log "Warning: Could not detect package manager. mount.nfs not found."
    fi
  fi
  
  # Check for Java Runtime Environment (JRE)
  if ! command -v java >/dev/null 2>&1; then
    log "Java Runtime Environment (JRE) not found. Attempting to install..."
    
    if command -v apt-get >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq default-jre || {
          log "Warning: Failed to install default-jre. Some discs may fail to rip."
        }
      else
        log "Warning: java not found and sudo access required to install."
      fi
    elif command -v yum >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo yum install -y -q java-1.8.0-openjdk || {
          log "Warning: Failed to install java-1.8.0-openjdk."
        }
      else
        log "Warning: java not found and sudo access required."
      fi
    elif command -v dnf >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo dnf install -y -q java-11-openjdk || {
          log "Warning: Failed to install java-11-openjdk."
        }
      else
        log "Warning: java not found and sudo access required."
      fi
    elif command -v pacman >/dev/null 2>&1; then
      if sudo -n true 2>/dev/null; then
        sudo pacman -Sy --noconfirm jre-openjdk >/dev/null 2>&1 || {
          log "Warning: Failed to install jre-openjdk."
        }
      else
        log "Warning: java not found and sudo access required."
      fi
    elif command -v brew >/dev/null 2>&1; then
      brew install openjdk >/dev/null 2>&1 || {
        log "Warning: Failed to install openjdk."
      }
    else
      log "Warning: Could not detect package manager. java not found."
      log "See http://www.makemkv.com/bdjava/ for details."
    fi
  else
    if java -version >/dev/null 2>&1; then
      log "Java Runtime Environment (JRE) is installed and working."
    else
      log "Warning: java command found but not working correctly."
    fi
  fi
}

#--------------------------
# Virtual Environment
#--------------------------
ensure_virtualenv() {
  PYTHON_BIN="${PYTHON:-python3.13}"
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  fi
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python interpreter not found; please install python3."
    exit 1
  fi

  mkdir -p "$MKVAUTO_LOG_DIR"

  recreate=false
  if [ ! -d "$VENV" ] || [ ! -x "$VENV/bin/pip" ]; then
    recreate=true
  else
    if [ -f "$VENV/bin/uvicorn" ]; then
      first_line="$(head -n1 "$VENV/bin/uvicorn" || true)"
      case "$first_line" in
        "#!$VENV"*) : ;;
        *) recreate=true ;;
      esac
    fi
  fi
  
  if [ "$recreate" = true ]; then
    log "Creating fresh virtualenv at $VENV..."
    rm -rf "$VENV"
    "$PYTHON_BIN" -m venv "$VENV"
  fi
  
  log "Upgrading pip, setuptools, and wheel..."
  "$VENV/bin/python" -m pip install --upgrade pip setuptools wheel >"$MKVAUTO_LOG_DIR/pip-upgrade.log" 2>&1 || {
    log "Warning: pip upgrade failed, continuing anyway..."
  }
  
  log "Installing backend requirements..."
  if ! "$VENV/bin/python" -m pip install -r "$BACKEND_DIR/requirements.txt" >>"$MKVAUTO_LOG_DIR/pip-install.log" 2>&1; then
    log "Error: Failed to install requirements. Check $MKVAUTO_LOG_DIR/pip-install.log for details."
    exit 1
  fi
  
  log "Verifying critical dependencies..."
  for pkg in fastapi uvicorn sqlalchemy cryptography smbprotocol; do
    if ! "$VENV/bin/python" -m pip show "$pkg" >/dev/null 2>&1; then
      log "Warning: Critical package $pkg is not installed."
    fi
  done
  
  log "Backend dependencies installed successfully."
}

#--------------------------
# Migrations
#--------------------------
run_migrations() {
  ensure_virtualenv
  if [ ! -f "$BACKEND_DIR/alembic.ini" ]; then
    log "alembic.ini not found under $BACKEND_DIR; skipping migrations."
    return
  fi
  log "Ensuring Alembic is installed..."
  "$VENV/bin/python" -m pip show alembic >/dev/null 2>&1 || "$VENV/bin/python" -m pip install alembic >/dev/null

  log "Running Alembic migrations..."
  (
    cd "$BACKEND_DIR"
    if ! source "$VENV/bin/activate" >/dev/null 2>&1; then
      log "Failed to activate virtualenv; skipping migrations."
      return
    fi
    "$VENV/bin/python" -m alembic upgrade head
  )
}

#--------------------------
# Root Helper
#--------------------------
start_root_helper() {
  if [ -S "$ROOT_HELPER_SOCK" ]; then
    log "Root helper socket found at $ROOT_HELPER_SOCK; assuming helper is running."
    return
  fi

  log "Starting root update helper (requires sudo)…"
  if sudo -n true 2>/dev/null; then
    sudo -E PYTHONPATH="$BACKEND_DIR" MAKEMKV_ROOT_HELPER_SOCK="$ROOT_HELPER_SOCK" "$VENV/bin/python" "$ROOT_HELPER" >>"$MKVAUTO_LOG_DIR/root_helper.log" 2>&1 &
  else
    log "Sudo password may be required to start root helper."
    sudo -E PYTHONPATH="$BACKEND_DIR" MAKEMKV_ROOT_HELPER_SOCK="$ROOT_HELPER_SOCK" "$VENV/bin/python" "$ROOT_HELPER" >>"$MKVAUTO_LOG_DIR/root_helper.log" 2>&1 &
  fi
  echo $! > "$MKVAUTO_PIDS_DIR/root_helper.pid"
}

#--------------------------
# Systemd Services
#--------------------------
install_systemd_services() {
  log "Installing systemd service files for drive rescan triggers..."
  
  RESCAN_SERVICE_SRC="${PROJECT_ROOT}/Deploy/systemd/mkva-rescan@.service"
  RESCAN_EJECT_SERVICE_SRC="${PROJECT_ROOT}/Deploy/systemd/mkva-rescan-eject@.service"
  
  if [ ! -f "$RESCAN_SERVICE_SRC" ] || [ ! -f "$RESCAN_EJECT_SERVICE_SRC" ]; then
    log "WARNING: Systemd service templates not found. Skipping."
    return 1
  fi
  
  tmp_rescan="$(mktemp)"
  tmp_eject="$(mktemp)"
  
  sed "s|@MKVAUTO_ROOT@|${PROJECT_ROOT}|g; s|@MKVAUTO_LOG_DIR@|${MKVAUTO_LOG_DIR}|g; s|@MKVAUTO_BACKEND_URL@|${BACKEND_API_URL}|g" \
    "$RESCAN_SERVICE_SRC" > "$tmp_rescan"
  sed "s|@MKVAUTO_ROOT@|${PROJECT_ROOT}|g; s|@MKVAUTO_LOG_DIR@|${MKVAUTO_LOG_DIR}|g; s|@MKVAUTO_BACKEND_URL@|${BACKEND_API_URL}|g" \
    "$RESCAN_EJECT_SERVICE_SRC" > "$tmp_eject"
  
  if command -v sudo >/dev/null 2>&1; then
    sudo cp "$tmp_rescan" "$RESCAN_SERVICE_UNIT" || {
      log "WARNING: Failed to install rescan service unit."
      rm -f "$tmp_rescan" "$tmp_eject"
      return 1
    }
    sudo cp "$tmp_eject" "$RESCAN_EJECT_SERVICE_UNIT" || {
      log "WARNING: Failed to install eject service unit."
      rm -f "$tmp_rescan" "$tmp_eject"
      return 1
    }
    sudo systemctl daemon-reload || true
    log "Systemd service files installed successfully"
  else
    cp "$tmp_rescan" "$RESCAN_SERVICE_UNIT" || {
      log "WARNING: Failed to install rescan service unit."
      rm -f "$tmp_rescan" "$tmp_eject"
      return 1
    }
    cp "$tmp_eject" "$RESCAN_EJECT_SERVICE_UNIT" || {
      log "WARNING: Failed to install eject service unit."
      rm -f "$tmp_rescan" "$tmp_eject"
      return 1
    }
    systemctl daemon-reload || true
    log "Systemd service files installed successfully"
  fi
  rm -f "$tmp_rescan" "$tmp_eject"
}

#--------------------------
# Udev Rules
#--------------------------
install_udev_rule() {
  log "Installing udev rule for drive rescan on media insert and eject..."
  
  install_systemd_services || {
    log "WARNING: Systemd service installation failed, but continuing."
  }
  
  TEMPLATE_FILE="$PROJECT_ROOT/Deploy/udev/99-mkva-disc.rules.template"
  
  if [ ! -f "$TEMPLATE_FILE" ]; then
    log "ERROR: Template file not found: $TEMPLATE_FILE"
    return 1
  fi
  
  UDEV_ROOT="${MKVAUTO_ROOT:-$PROJECT_ROOT}"
  UDEV_TMP_DIR="${MKVAUTO_TMP_DIR:-$UDEV_ROOT/tmp}"
  UDEV_NOTIFY_SCRIPT="$PROJECT_ROOT/Backend/drive_manager/udev_notify.py"
  PYTHON_BIN="/usr/bin/python3"
  
  tmp_rule="$(mktemp)"
  sed -e "s|__MKVAUTO_ROOT__|${UDEV_ROOT}|g" \
      -e "s|__MKVAUTO_TMP_DIR__|${UDEV_TMP_DIR}|g" \
      -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
      -e "s|__UDEV_NOTIFY_SCRIPT__|${UDEV_NOTIFY_SCRIPT}|g" \
      "$TEMPLATE_FILE" > "$tmp_rule"

  if command -v sudo >/dev/null 2>&1; then
    sudo cp "$tmp_rule" "$UDEV_RULE_DEST" || {
      log "WARNING: Failed to install udev rule."
      rm -f "$tmp_rule"
      return 1
    }
    sudo udevadm control --reload-rules || true
    sudo udevadm trigger --subsystem-match=block --action=change || true
    log "Udev rule installed successfully"
  else
    cp "$tmp_rule" "$UDEV_RULE_DEST" || {
      log "WARNING: Failed to install udev rule."
      rm -f "$tmp_rule"
      return 1
    }
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=block --action=change || true
    log "Udev rule installed successfully"
  fi
  rm -f "$tmp_rule"
}

cleanup_udev_rule() {
  log "Removing udev rule..."
  if command -v sudo >/dev/null 2>&1; then
    sudo rm -f "$RESCAN_SERVICE_UNIT" "$RESCAN_EJECT_SERVICE_UNIT" "$UDEV_RULE_DEST" || true
    sudo systemctl daemon-reload 2>/dev/null || true
    sudo udevadm control --reload-rules || true
  else
    rm -f "$RESCAN_SERVICE_UNIT" "$RESCAN_EJECT_SERVICE_UNIT" "$UDEV_RULE_DEST" || true
    systemctl daemon-reload 2>/dev/null || true
    udevadm control --reload-rules || true
  fi
}

#--------------------------
# Docker Containers
#--------------------------
start_postgres() {
  log "Ensuring Postgres container is running..."
  if container_exists "$POSTGRES_CONTAINER"; then
    if container_running "$POSTGRES_CONTAINER"; then
      log "Postgres container already running."
    else
      docker start "$POSTGRES_CONTAINER"
      log "Started existing Postgres container."
    fi
  else
    docker run -d --name "$POSTGRES_CONTAINER" \
      -e POSTGRES_DB="$POSTGRES_DB" \
      -e POSTGRES_USER="$POSTGRES_USER" \
      -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
      -v "$POSTGRES_DATA_DIR":/var/lib/postgresql/data \
      -p 5432:5432 \
      postgres:17
    log "Created new Postgres container."
  fi

  if wait_for_postgres "$POSTGRES_CONTAINER" "$POSTGRES_USER"; then
    run_migrations
  fi

  if container_running "$POSTGRES_CONTAINER"; then
    docker logs -f "$POSTGRES_CONTAINER" >>"$MKVAUTO_LOG_DIR/postgres.log" 2>&1 &
    echo $! > "$MKVAUTO_PIDS_DIR/postgres-logs.pid"
  fi
}

start_redis() {
  log "Ensuring Redis container is running..."
  if container_exists "$REDIS_CONTAINER"; then
    if container_running "$REDIS_CONTAINER"; then
      log "Redis container already running."
    else
      docker start "$REDIS_CONTAINER"
      log "Started existing Redis container."
    fi
  else
    docker run -d --name "$REDIS_CONTAINER" \
      -v "$REDIS_DATA_DIR":/data \
      -p 6379:6379 \
      redis:8
    log "Created new Redis container."
  fi

  if container_running "$REDIS_CONTAINER"; then
    docker logs -f "$REDIS_CONTAINER" >>"$MKVAUTO_LOG_DIR/redis.log" 2>&1 &
    echo $! > "$MKVAUTO_PIDS_DIR/redis-logs.pid"
  fi
}

stop_containers() {
  echo "⏹  Stopping Docker containers (preserving data volumes)..."
  docker stop "$POSTGRES_CONTAINER" "$REDIS_CONTAINER" >/dev/null 2>&1 || true
}

#--------------------------
# Local Services
#--------------------------
start_backend() {
  ensure_virtualenv
  log "Activating Python virtualenv and starting Uvicorn..."
  (
    cd "$BACKEND_DIR"
    export MAKEMKV_DATA_DIR="${MAKEMKV_DATA_DIR:-$MAKEMKV_DATA_DIR_DEFAULT}"
    export MKVAUTO_DEBUG_LEVEL="${MKVAUTO_DEBUG_LEVEL:-INFO}"
    export PYTHONPATH="${BACKEND_DIR}${PYTHONPATH:+:$PYTHONPATH}"
    mkdir -p "$MAKEMKV_DATA_DIR"
    mkdir -p "$MKVAUTO_LOG_DIR"
    source "$VENV/bin/activate"
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000 2>>"$MKVAUTO_LOG_DIR/uvicorn.log" &
    echo $! > "$MKVAUTO_PIDS_DIR/uvicorn.pid"

    log "Starting Celery workers (log level: ${MKVAUTO_DEBUG_LEVEL})..."
    celery -A workers.tasks worker -Q rip -c 5 --loglevel=warning >>"$MKVAUTO_LOG_DIR/celery_rip.log" 2>&1 &
    echo $! > "$MKVAUTO_PIDS_DIR/celery_rip.pid"
    celery -A workers.tasks worker -Q postprocess -c 5 --loglevel=warning >>"$MKVAUTO_LOG_DIR/celery_postprocess.log" 2>&1 &
    echo $! > "$MKVAUTO_PIDS_DIR/celery_postprocess.pid"
    celery -A workers.tasks worker -Q transfer -c 5 --loglevel=warning >>"$MKVAUTO_LOG_DIR/celery_transfer.log" 2>&1 &
    echo $! > "$MKVAUTO_PIDS_DIR/celery_transfer.pid"
    celery -A workers.tasks worker -Q preview -c 5 --loglevel=warning >>"$MKVAUTO_LOG_DIR/celery_preview.log" 2>&1 &
    echo $! > "$MKVAUTO_PIDS_DIR/celery_preview.pid"
    celery -A workers.tasks worker -Q celery -c 2 --loglevel=warning >>"$MKVAUTO_LOG_DIR/celery.log" 2>&1 &
    echo $! > "$MKVAUTO_PIDS_DIR/celery.pid"

    start_root_helper
  )
}

start_frontend() {
  echo "▶️  Starting Angular dev server..."
  (
    cd "$FRONTEND_DIR"
    npm install
    npx ng serve --host 0.0.0.0 >>"$MKVAUTO_LOG_DIR/frontend.log" 2>&1 &
    echo $! > "$MKVAUTO_PIDS_DIR/frontend.pid"
  )
}

stop_backend() {
  echo "⏹  Stopping backend services..."
  for pidfile in "$MKVAUTO_PIDS_DIR"/uvicorn.pid "$MKVAUTO_PIDS_DIR"/celery.pid "$MKVAUTO_PIDS_DIR"/celery_rip.pid "$MKVAUTO_PIDS_DIR"/celery_postprocess.pid "$MKVAUTO_PIDS_DIR"/celery_transfer.pid "$MKVAUTO_PIDS_DIR"/celery_preview.pid "$MKVAUTO_PIDS_DIR"/root_helper.pid; do
    [ -f "$pidfile" ] || continue
    pid=$(<"$pidfile")
    echo "    → Killing PID $pid"
    if [[ "$pidfile" == *root_helper.pid ]]; then
      sudo kill "$pid" || true
    else
      kill "$pid" || true
    fi
    rm -f "$pidfile"
  done
  if [ -S "$ROOT_HELPER_SOCK" ]; then
    sudo rm -f "$ROOT_HELPER_SOCK" || true
  fi
  if command -v pkill >/dev/null 2>&1; then
    sudo pkill -9 celery || true
    sudo pkill -9 uvicorn || true
  fi
}

stop_frontend() {
  echo "⏹  Stopping frontend service..."
  if [ -f "$MKVAUTO_PIDS_DIR/frontend.pid" ]; then
    pid=$(<"$MKVAUTO_PIDS_DIR/frontend.pid")
    echo "    → Killing PID $pid"
    kill "$pid" || true
    rm -f "$MKVAUTO_PIDS_DIR/frontend.pid"
  fi
  if command -v pkill >/dev/null 2>&1; then
    sudo pkill -9 ng || true
  fi
}

stop_services() {
  echo "⏹  Stopping local processes..."
  for pidfile in "$MKVAUTO_PIDS_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    pid=$(<"$pidfile")
    echo "    → Killing PID $pid"
    if [[ "$pidfile" == *root_helper.pid ]]; then
      sudo kill "$pid" || true
    else
      kill "$pid" || true
    fi
    rm -f "$pidfile"
  done
  if [ -S "$ROOT_HELPER_SOCK" ]; then
    sudo rm -f "$ROOT_HELPER_SOCK" || true
  fi
  if command -v pkill >/dev/null 2>&1; then
    sudo pkill -9 ng || true
    sudo pkill -9 mkv || true
    sudo pkill -9 python || true
    sudo pkill -9 celery || true
    sudo pkill -9 uvicorn || true
  fi
}

restart_backend() {
  echo "🔄 Restarting backend services..."
  stop_backend
  start_backend
  echo "✅ Backend services restarted."
}

restart_frontend() {
  echo "🔄 Restarting frontend service..."
  stop_frontend
  start_frontend
  echo "✅ Frontend service restarted."
}

#--------------------------
# Database Operations
#--------------------------
reset_db() {
  echo "🔄 Resetting Postgres database..."
  docker exec -u postgres "$POSTGRES_CONTAINER" psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$POSTGRES_DB' AND pid <> pg_backend_pid();"
  docker exec -u postgres "$POSTGRES_CONTAINER" psql -c "DROP DATABASE IF EXISTS $POSTGRES_DB;"
  docker exec -u postgres "$POSTGRES_CONTAINER" psql -c "CREATE DATABASE $POSTGRES_DB;"

  echo "🔄 Flushing Redis..."
  docker exec "$REDIS_CONTAINER" redis-cli FLUSHALL
}

reset_all() {
  echo "⚠️ Performing hard reset: stopping services, removing containers, deleting data/logs."
  stop_services
  cleanup_udev_rule
  stop_containers
  echo "🗑️  Removing containers..."
  docker rm -f "$POSTGRES_CONTAINER" "$REDIS_CONTAINER" >/dev/null 2>&1 || true

  echo "🗑️  Deleting data/logs/pids..."
  rm -rf "$POSTGRES_DATA_DIR" "$REDIS_DATA_DIR" "$MAKEMKV_DATA_DIR_DEFAULT" "$DATA_ROOT" "$MKVAUTO_LOG_DIR" "$MKVAUTO_PIDS_DIR" "$MKVAUTO_ROOT" >/dev/null 2>&1 || true
  sudo rm -rf "$POSTGRES_DATA_DIR" "$REDIS_DATA_DIR" "$MAKEMKV_DATA_DIR_DEFAULT" "$DATA_ROOT" "$MKVAUTO_LOG_DIR" "$MKVAUTO_PIDS_DIR" "$MKVAUTO_ROOT" >/dev/null 2>&1 || true

  echo "✅ Hard reset complete. Services are stopped."
  echo "   Run 'mkv dev start' to launch fresh."
  eject_tray
}

#--------------------------
# Dev Seed Operations
#--------------------------
run_devseed_backup() {
  local force_arg=""
  [ "${1:-}" = "force" ] && force_arg="--force"
  log "Running dev seed backup (create)..."
  ensure_virtualenv
  (
    cd "$BACKEND_DIR"
    export MKVAUTO_DEVSEED_ROOT
    export POSTGRES_CONTAINER POSTGRES_DB POSTGRES_USER
    export MKVAUTO_DATA
    source "$VENV/bin/activate"
    [[ -f scripts/create_devseed.py ]] || { echo "devseed tooling is not included in release snapshots" >&2; exit 1; }
    PYTHONPATH="${BACKEND_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" scripts/create_devseed.py --seed-root "$MKVAUTO_DEVSEED_ROOT" $force_arg
  ) || return 1
  log "Dev seed backup done."
}

run_devseed_reload() {
  local full_arg=""
  [ "$1" = "--full" ] || [ "$1" = "full" ] && full_arg="--full"
  log "Running dev seed reload..."
  start_redis
  log "Flushing Redis..."
  docker exec "$REDIS_CONTAINER" redis-cli FLUSHALL
  ensure_virtualenv
  (
    cd "$BACKEND_DIR"
    export MKVAUTO_DEVSEED_ROOT
    export POSTGRES_CONTAINER POSTGRES_DB POSTGRES_USER
    export MKVAUTO_DATA
    source "$VENV/bin/activate"
    [[ -f scripts/reload_devseed.py ]] || { echo "devseed tooling is not included in release snapshots" >&2; exit 1; }
    PYTHONPATH="${BACKEND_DIR}${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" scripts/reload_devseed.py --seed-root "$MKVAUTO_DEVSEED_ROOT" $full_arg
  ) || return 1
  log "Dev seed reload done."
}

devseed_reload_if_requested() {
  if [ "${ENABLE_DEVMODE:-0}" != "1" ] && [ "${ENABLE_DEVMODE:-0}" != "true" ]; then
    return 0
  fi
  if [ "${MKVAUTO_DEVSEED_RELOAD:-0}" != "1" ] && [ "${MKVAUTO_DEVSEED_RELOAD:-0}" != "true" ]; then
    return 0
  fi
  if [ ! -f "${MKVAUTO_DEVSEED_ROOT}/database.sql" ]; then
    log "Dev seed reload skipped (seed database.sql missing)."
    return 0
  fi
  log "ENABLE_DEVMODE=1 and MKVAUTO_DEVSEED_RELOAD=1: reloading from dev seed..."
  run_devseed_reload || { log "Dev seed reload failed."; return 1; }
}

#--------------------------
# Main Dev Commands
#--------------------------
dev_start() {
  ensure_system_dependencies
  install_udev_rule
  start_postgres
  start_redis
  devseed_reload_if_requested || exit 1
  start_backend
  start_frontend
  echo "✅ All services started."
  close_tray
}

dev_stop() {
  stop_services
  cleanup_udev_rule
  stop_containers
  eject_tray
  echo "✅ All services stopped."
}

dev_restart() {
  local service="${1:-all}"
  case "$service" in
    frontend)
      restart_frontend
      ;;
    backend)
      restart_backend
      ;;
    all|"")
      echo "🔄 Rotating logs and restarting services..."
      rotate_logs "$MKVAUTO_LOG_DIR"
      dev_stop
      dev_start
      ;;
    *)
      echo "Error: Unknown service '$service'. Use 'frontend', 'backend', or 'all'"
      exit 1
      ;;
  esac
}

dev_status() {
  echo "ℹ️  Service status:"
  printf "  %-18s %s\n" "Postgres container:" "$(container_running "$POSTGRES_CONTAINER" && echo running || echo stopped)"
  printf "  %-18s %s\n" "Redis container:"    "$(container_running "$REDIS_CONTAINER" && echo running || echo stopped)"
  if [ -f "$MKVAUTO_PIDS_DIR/uvicorn.pid" ] && ps -p "$(cat "$MKVAUTO_PIDS_DIR/uvicorn.pid")" >/dev/null 2>&1; then
    printf "  %-18s %s\n" "Backend (uvicorn):" "running (pid $(cat "$MKVAUTO_PIDS_DIR/uvicorn.pid"))"
  else
    printf "  %-18s %s\n" "Backend (uvicorn):" "stopped"
  fi
  if [ -f "$MKVAUTO_PIDS_DIR/celery_rip.pid" ] && ps -p "$(cat "$MKVAUTO_PIDS_DIR/celery_rip.pid")" >/dev/null 2>&1; then
    printf "  %-18s %s\n" "Celery (rip):" "running (pid $(cat "$MKVAUTO_PIDS_DIR/celery_rip.pid"))"
  else
    printf "  %-18s %s\n" "Celery (rip):" "stopped"
  fi
  if [ -f "$MKVAUTO_PIDS_DIR/celery_postprocess.pid" ] && ps -p "$(cat "$MKVAUTO_PIDS_DIR/celery_postprocess.pid")" >/dev/null 2>&1; then
    printf "  %-18s %s\n" "Celery (post):" "running (pid $(cat "$MKVAUTO_PIDS_DIR/celery_postprocess.pid"))"
  else
    printf "  %-18s %s\n" "Celery (post):" "stopped"
  fi
  if [ -f "$MKVAUTO_PIDS_DIR/celery_transfer.pid" ] && ps -p "$(cat "$MKVAUTO_PIDS_DIR/celery_transfer.pid")" >/dev/null 2>&1; then
    printf "  %-18s %s\n" "Celery (xfer):" "running (pid $(cat "$MKVAUTO_PIDS_DIR/celery_transfer.pid"))"
  else
    printf "  %-18s %s\n" "Celery (xfer):" "stopped"
  fi
  if [ -f "$MKVAUTO_PIDS_DIR/celery_preview.pid" ] && ps -p "$(cat "$MKVAUTO_PIDS_DIR/celery_preview.pid")" >/dev/null 2>&1; then
    printf "  %-18s %s\n" "Celery (preview):" "running (pid $(cat "$MKVAUTO_PIDS_DIR/celery_preview.pid"))"
  else
    printf "  %-18s %s\n" "Celery (preview):" "stopped"
  fi
  if [ -f "$MKVAUTO_PIDS_DIR/celery.pid" ] && ps -p "$(cat "$MKVAUTO_PIDS_DIR/celery.pid")" >/dev/null 2>&1; then
    printf "  %-18s %s\n" "Celery (maint):" "running (pid $(cat "$MKVAUTO_PIDS_DIR/celery.pid"))"
  else
    printf "  %-18s %s\n" "Celery (maint):" "stopped"
  fi
  if [ -f "$MKVAUTO_PIDS_DIR/frontend.pid" ] && ps -p "$(cat "$MKVAUTO_PIDS_DIR/frontend.pid")" >/dev/null 2>&1; then
    printf "  %-18s %s\n" "Frontend (ng):" "running (pid $(cat "$MKVAUTO_PIDS_DIR/frontend.pid"))"
  else
    printf "  %-18s %s\n" "Frontend (ng):" "stopped"
  fi
  if [ -S "$ROOT_HELPER_SOCK" ]; then
    printf "  %-18s %s\n" "Root helper sock:" "$ROOT_HELPER_SOCK"
  else
    printf "  %-18s %s\n" "Root helper sock:" "missing"
  fi
}

dev_reset() {
  reset_db
  echo "✅ Database and cache reset."
}

dev_reset_all() {
  reset_all
}

dev_seed() {
  local subcmd="${1:-}"
  shift || true
  
  case "$subcmd" in
    backup|create)
      start_postgres 2>/dev/null || true
      run_devseed_backup "${1:-}" || exit 1
      echo "✅ Dev seed backup done."
      ;;
    reload)
      if [ "${ENABLE_DEVMODE:-0}" != "1" ] && [ "${ENABLE_DEVMODE:-0}" != "true" ]; then
        log "Dev seed reload is only allowed when ENABLE_DEVMODE=1."
        exit 1
      fi
      start_postgres 2>/dev/null || true
      run_devseed_reload "${1:-}" || exit 1
      echo "✅ Dev seed reloaded."
      ;;
    *)
      echo "Error: Unknown seed command '$subcmd'"
      echo "Available: backup, create, reload"
      exit 1
      ;;
  esac
}
