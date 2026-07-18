#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[MKV-Auto]${NC} $1"
}

error() {
    echo -e "${RED}[MKV-Auto ERROR]${NC} $1" >&2
}

warn() {
    echo -e "${YELLOW}[MKV-Auto WARN]${NC} $1"
}

info() {
    echo -e "${BLUE}[MKV-Auto INFO]${NC} $1"
}

log "Starting MKV-Auto container initialization..."

# Create necessary directories
log "Creating data directories..."
mkdir -p /data/postgres /data/redis /data/mkvauto/data /data/mkvauto/logs /data/mkvauto/tmp
mkdir -p /var/log/supervisor /var/log/nginx

# Check MakeMKV installation status
check_makemkv() {
    # Create MakeMKV data directory
    mkdir -p /data/mkvauto/.MakeMKV
    export HOME=/data/mkvauto
    
    # Check if MakeMKV is installed in /data and create symlinks if needed
    if [ -f "/data/mkvauto/makemkv/bin/makemkvcon" ]; then
        log "MakeMKV found in /data, creating system symlinks..."
        
        # Create symlinks to standard locations
        ln -sf /data/mkvauto/makemkv/bin/makemkvcon /usr/bin/makemkvcon 2>/dev/null || true
        
        # Link libraries if they exist
        if [ -f "/data/mkvauto/makemkv/lib/libmakemkv.so.1" ]; then
            ln -sf /data/mkvauto/makemkv/lib/libmakemkv.so.1 /usr/lib/libmakemkv.so.1 2>/dev/null || true
        fi
        if [ -f "/data/mkvauto/makemkv/lib/libdriveio.so.0" ]; then
            ln -sf /data/mkvauto/makemkv/lib/libdriveio.so.0 /usr/lib/libdriveio.so.0 2>/dev/null || true
        fi
        
        # Update library cache
        ldconfig 2>/dev/null || true
        
        log "✅ MakeMKV symlinks created"
    fi
    
    # Check if MakeMKV is installed
    if command -v makemkvcon >/dev/null 2>&1; then
        local version=$(makemkvcon --version 2>/dev/null | head -n1 || echo "unknown")
        log "✅ MakeMKV installed: $version"
        return 0
    else
        log "============================================"
        warn "⚠️  MakeMKV not installed"
        log "============================================"
        info "MakeMKV is required for disc ripping."
        info ""
        info "To install MakeMKV:"
        info "  1. Access the web UI on container port 80 (e.g. http://localhost:8080 with the default -p 8080:80 mapping)"
        info "  2. Go to Settings → MakeMKV"
        info "  3. Click 'Install/Update MakeMKV'"
        info ""
        info "The built-in updater will download and compile"
        info "MakeMKV automatically (takes 5-10 minutes)."
        log "============================================"
        return 1
    fi
}

# Check MakeMKV status
log ""
check_makemkv || true
log ""

# Set permissions
chown -R postgres:postgres /data/postgres 2>/dev/null || warn "Could not set postgres ownership"
chown -R redis:redis /data/redis 2>/dev/null || warn "Could not set redis ownership"
chmod 700 /data/postgres 2>/dev/null || true

# Detect if using external or embedded databases
EXTERNAL_POSTGRES=false
EXTERNAL_REDIS=false

if [ -n "$DATABASE_URL" ]; then
    if [[ "$DATABASE_URL" != *"127.0.0.1"* ]] && [[ "$DATABASE_URL" != *"localhost"* ]]; then
        info "Using external PostgreSQL database"
        EXTERNAL_POSTGRES=true
    fi
fi

if [ -n "$REDIS_URL" ]; then
    if [[ "$REDIS_URL" != *"127.0.0.1"* ]] && [[ "$REDIS_URL" != *"localhost"* ]]; then
        info "Using external Redis cache"
        EXTERNAL_REDIS=true
    fi
fi

# Configure environment variables
if [ "$EXTERNAL_POSTGRES" = false ]; then
    log "Using embedded PostgreSQL database"
    export DATABASE_URL="${DATABASE_URL:-postgresql://mkvauto:changeme@127.0.0.1:5432/discs}"
    
    # Initialize PostgreSQL if needed
    if [ ! -f "/data/postgres/PG_VERSION" ]; then
        log "Initializing PostgreSQL database cluster..."
        su - postgres -c "/usr/lib/postgresql/15/bin/initdb -D /data/postgres" || error "Failed to initialize PostgreSQL"
        
        # Start PostgreSQL temporarily to create database
        log "Starting PostgreSQL temporarily for initialization..."
        su - postgres -c "/usr/lib/postgresql/15/bin/pg_ctl -D /data/postgres -l /tmp/postgres-init.log -o '-c listen_addresses=127.0.0.1' start"
        sleep 5
        
        # Wait for PostgreSQL to be ready
        for i in {1..30}; do
            if su - postgres -c "pg_isready -h 127.0.0.1" > /dev/null 2>&1; then
                break
            fi
            if [ $i -eq 30 ]; then
                error "PostgreSQL failed to start"
                cat /tmp/postgres-init.log
                exit 1
            fi
            sleep 1
        done
        
        # Create database and user
        log "Creating MKV-Auto database and user..."
        su - postgres -c "psql -h 127.0.0.1 -c \"CREATE DATABASE discs;\"" || warn "Database might already exist"
        su - postgres -c "psql -h 127.0.0.1 -c \"CREATE USER mkvauto WITH PASSWORD 'changeme';\"" || warn "User might already exist"
        su - postgres -c "psql -h 127.0.0.1 -c \"GRANT ALL PRIVILEGES ON DATABASE discs TO mkvauto;\"" || warn "Privileges might already be granted"
        # Grant schema permissions (required for PostgreSQL 15+)
        su - postgres -c "psql -h 127.0.0.1 -d discs -c \"GRANT ALL ON SCHEMA public TO mkvauto;\"" || warn "Schema privileges might already be granted"
        
        # Stop temporary PostgreSQL
        log "Stopping temporary PostgreSQL instance..."
        su - postgres -c "/usr/lib/postgresql/15/bin/pg_ctl -D /data/postgres stop -m fast"
        sleep 2
        
        log "PostgreSQL initialization complete"
    else
        info "PostgreSQL already initialized"
    fi
else
    # Disable embedded PostgreSQL in supervisor
    log "Disabling embedded PostgreSQL (using external)"
    sed -i '/\[program:postgresql\]/,/^$/s/autostart=true/autostart=false/' /etc/supervisor/conf.d/mkvauto.conf || true
    
    # Wait for external PostgreSQL
    info "Waiting for external PostgreSQL to be ready..."
    DB_HOST=$(echo "$DATABASE_URL" | sed -n 's/.*@\([^:\/]*\).*/\1/p')
    for i in {1..30}; do
        if pg_isready -h "$DB_HOST" > /dev/null 2>&1; then
            log "External PostgreSQL is ready"
            break
        fi
        if [ $i -eq 30 ]; then
            error "External PostgreSQL not available after 30 seconds"
            exit 1
        fi
        sleep 1
    done
fi

if [ "$EXTERNAL_REDIS" = false ]; then
    log "Using embedded Redis cache"
    export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
else
    # Disable embedded Redis in supervisor
    log "Disabling embedded Redis (using external)"
    sed -i '/\[program:redis\]/,/^$/s/autostart=true/autostart=false/' /etc/supervisor/conf.d/mkvauto.conf || true
    
    # Wait for external Redis
    info "Waiting for external Redis to be ready..."
    REDIS_HOST=$(echo "$REDIS_URL" | sed -n 's/.*\/\/\([^:\/]*\).*/\1/p')
    REDIS_PORT=$(echo "$REDIS_URL" | sed -n 's/.*:\([0-9]*\).*/\1/p')
    REDIS_PORT=${REDIS_PORT:-6379}
    for i in {1..30}; do
        if redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping > /dev/null 2>&1; then
            log "External Redis is ready"
            break
        fi
        if [ $i -eq 30 ]; then
            error "External Redis not available after 30 seconds"
            exit 1
        fi
        sleep 1
    done
fi

# Export environment variables for supervisor
export DATABASE_URL
export REDIS_URL

# Migrations will be run by supervisor after PostgreSQL starts
log "Database migrations will be run automatically after PostgreSQL starts"

# Signal handling is managed by tini (PID 1)
# Tini forwards signals to supervisord and reaps zombie processes automatically
# supervisord.conf settings (stopasgroup=true, killasgroup=true) ensure clean shutdown

# Display startup information
log "============================================"
log "MKV-Auto Container Started"
log "============================================"
info "Database: $([ "$EXTERNAL_POSTGRES" = true ] && echo "External" || echo "Embedded") PostgreSQL"
info "Cache: $([ "$EXTERNAL_REDIS" = true ] && echo "External" || echo "Embedded") Redis"
info "Data directory: /data"
info "Access the web interface on container port 80 (e.g. http://localhost:8080 with the default -p 8080:80 mapping)"
log "============================================"

# Configure optical drive behavior to prevent auto-reinsertion
log "Configuring optical drive behavior..."
if [ -w /proc/sys/dev/cdrom/autoclose ]; then
    if echo 0 > /proc/sys/dev/cdrom/autoclose 2>/dev/null; then
        log "✅ Disabled CD-ROM autoclose (prevents auto-reinsertion)"
    else
        warn "Could not disable autoclose (may require host configuration)"
        warn "If discs auto-reingest after ejection, run: sudo scripts/setup-host-optical.sh"
    fi
else
    warn "Cannot access /proc/sys/dev/cdrom/autoclose"
    warn "If discs auto-reingest after ejection, run: sudo scripts/setup-host-optical.sh on the host"
fi

# Report optical drive configuration status
AUTOCLOSE=$(cat /proc/sys/dev/cdrom/autoclose 2>/dev/null || echo "unknown")
if [ "$AUTOCLOSE" = "0" ]; then
    info "Optical drives configured correctly (autoclose=$AUTOCLOSE)"
else
    warn "Optical drive autoclose not disabled (value: $AUTOCLOSE)"
    warn "Discs may auto-reingest after ejection"
fi

# Initialize udev for disc detection
log "Initializing udev for disc detection..."
mkdir -p /run/udev
# Trigger initial device scan to populate udev database
udevadm trigger --subsystem-match=block --action=change 2>/dev/null || true
log "✅ Udev initialized"

# Start supervisor with all services
log "Starting all services via supervisor..."

# Execute supervisor (or passed command)
if [ "$1" = "supervisord" ] || [ $# -eq 0 ]; then
    exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
else
    exec "$@"
fi
