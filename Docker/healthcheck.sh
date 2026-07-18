#!/bin/bash
# Health check script for MKV-Auto container

set -e

# Check NGINX is serving
if ! curl -sf http://localhost:80/ > /dev/null 2>&1; then
    echo "NGINX not responding"
    exit 1
fi

# Check backend API health endpoint
if ! curl -sf http://localhost:80/api/system/health > /dev/null 2>&1; then
    echo "Backend API not healthy"
    exit 1
fi

# Check PostgreSQL (only if using embedded)
if [ -z "$DATABASE_URL" ] || [[ "$DATABASE_URL" == *"127.0.0.1"* ]] || [[ "$DATABASE_URL" == *"localhost"* ]]; then
    if ! pg_isready -h 127.0.0.1 -U postgres > /dev/null 2>&1; then
        echo "PostgreSQL not ready"
        exit 1
    fi
fi

# Check Redis (only if using embedded)
if [ -z "$REDIS_URL" ] || [[ "$REDIS_URL" == *"127.0.0.1"* ]] || [[ "$REDIS_URL" == *"localhost"* ]]; then
    if ! redis-cli -h 127.0.0.1 ping > /dev/null 2>&1; then
        echo "Redis not responding"
        exit 1
    fi
fi

# All checks passed
exit 0
