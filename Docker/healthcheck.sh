#!/bin/bash
# Health check script for MKV-Auto container.
#
# EVERY probe below must be self-limiting. Docker's `--timeout` only stops
# WAITING for the healthcheck — it does not kill the process. On 2026-09-09 a
# wedged optical drive hung the API; each 30s healthcheck blocked forever on
# its first curl, and ~1750 healthcheck+curl pairs accumulated over 20 hours
# until the host was at load 16 and the container could not even be killed
# (its unkillable D-state children blocked Docker's own teardown). A hung
# dependency must make this script FAIL FAST, never queue.
#
# Rules for anything added here:
#   * network probe  -> curl --max-time
#   * any other tool -> wrap in `timeout`
# The whole script is also bounded by HEALTHCHECK_MAX_SECONDS as a backstop.

set -e

# Per-probe budget. Kept well under the Dockerfile's HEALTHCHECK --timeout so
# a failure is reported as unhealthy rather than abandoned mid-flight.
PROBE_TIMEOUT="${HEALTHCHECK_PROBE_TIMEOUT:-3}"
MAX_SECONDS="${HEALTHCHECK_MAX_SECONDS:-8}"

# Backstop: self-terminate if the sum of probes somehow still runs long.
# Runs in a subshell so it cannot outlive this script's own exit.
( sleep "$MAX_SECONDS"; kill -TERM $$ 2>/dev/null ) &
watchdog_pid=$!
trap 'kill "$watchdog_pid" 2>/dev/null || true' EXIT

# Check NGINX is serving
if ! curl -sf --max-time "$PROBE_TIMEOUT" http://localhost:80/ > /dev/null 2>&1; then
    echo "NGINX not responding"
    exit 1
fi

# Check the backend is ready to serve. Deliberately /api/readyz, NOT
# /api/system/health: readyz is the real readiness probe (DB reachable, WAL
# recovery fenced) and answers in ~0.02s, while system/health is an
# operator DIAGNOSTIC that shells out to makemkvcon and broadcasts to every
# Celery worker — 3-5s per call, and running that every 30s pokes the
# optical drive for no reason. Liveness wants cheap and deterministic.
if ! curl -sf --max-time "$PROBE_TIMEOUT" http://localhost:80/api/readyz > /dev/null 2>&1; then
    echo "Backend API not ready"
    exit 1
fi

# Check PostgreSQL (only if using embedded)
if [ -z "$DATABASE_URL" ] || [[ "$DATABASE_URL" == *"127.0.0.1"* ]] || [[ "$DATABASE_URL" == *"localhost"* ]]; then
    if ! timeout "$PROBE_TIMEOUT" pg_isready -h 127.0.0.1 -U postgres > /dev/null 2>&1; then
        echo "PostgreSQL not ready"
        exit 1
    fi
fi

# Check Redis (only if using embedded)
if [ -z "$REDIS_URL" ] || [[ "$REDIS_URL" == *"127.0.0.1"* ]] || [[ "$REDIS_URL" == *"localhost"* ]]; then
    if ! timeout "$PROBE_TIMEOUT" redis-cli -h 127.0.0.1 ping > /dev/null 2>&1; then
        echo "Redis not responding"
        exit 1
    fi
fi

# All checks passed
exit 0
