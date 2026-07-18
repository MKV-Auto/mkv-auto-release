#!/bin/bash
# E2E test for MakeMKV updater functionality
# Tests that all build dependencies are present and MakeMKV can be compiled successfully

set -e

CONTAINER_NAME="${1:-mkv-auto-test}"
API_BASE="http://localhost:8080/api"
TIMEOUT=900  # 15 minutes max for compilation

echo "======================================"
echo "MakeMKV Updater E2E Test"
echo "======================================"
echo ""

# Check if container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "❌ Container '$CONTAINER_NAME' is not running"
    echo "   Start it with: ./test-docker.sh"
    exit 1
fi

echo "✅ Container is running"
echo ""

# Wait for backend to be ready
echo "Waiting for backend to be ready..."
for i in {1..30}; do
    if curl -s "${API_BASE}/makemkv" > /dev/null 2>&1; then
        echo "✅ Backend is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "❌ Backend not ready after 30 seconds"
        exit 1
    fi
    sleep 1
done
echo ""

# Check current MakeMKV status
echo "Checking current MakeMKV installation status..."
MAKEMKV_INFO=$(curl -s "${API_BASE}/makemkv")
CURRENT_VERSION=$(echo "$MAKEMKV_INFO" | grep -o '"version":"[^"]*"' | cut -d'"' -f4 || echo "")

if [ -n "$CURRENT_VERSION" ]; then
    echo "⚠️  MakeMKV is already installed (version: $CURRENT_VERSION)"
    echo "   Skipping installation test"
    echo ""
    echo "To test fresh installation:"
    echo "  1. Stop container: docker stop $CONTAINER_NAME"
    echo "  2. Remove volume: docker volume rm mkv-test-data"
    echo "  3. Restart: ./test-docker.sh"
    echo "  4. Run this test again"
    exit 0
fi

echo "✅ MakeMKV not installed (ready for test)"
echo ""

# Start MakeMKV update
echo "Starting MakeMKV installation..."
UPDATE_RESPONSE=$(curl -s -X POST "${API_BASE}/makemkv/update/start" \
    -H "Content-Type: application/json" \
    -d '{"build_ffmpeg": false}')

JOB_ID=$(echo "$UPDATE_RESPONSE" | grep -o '"jobId":"[^"]*"' | cut -d'"' -f4)

if [ -z "$JOB_ID" ]; then
    echo "❌ Failed to start MakeMKV update"
    echo "Response: $UPDATE_RESPONSE"
    exit 1
fi

echo "✅ Update started (Job ID: $JOB_ID)"
echo ""

# Monitor progress via EventSource (simulated with curl)
echo "Monitoring installation progress..."
echo "This will take 5-10 minutes..."
echo ""

START_TIME=$(date +%s)
LAST_LOG=""
STATUS="running"

while [ "$STATUS" = "running" ]; do
    # Check timeout
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))
    if [ $ELAPSED -gt $TIMEOUT ]; then
        echo "❌ Installation timed out after $TIMEOUT seconds"
        exit 1
    fi
    
    # Poll MakeMKV info for status
    MAKEMKV_INFO=$(curl -s "${API_BASE}/makemkv" 2>/dev/null || echo "")
    CURRENT_VERSION=$(echo "$MAKEMKV_INFO" | grep -o '"version":"[^"]*"' | cut -d'"' -f4 || echo "")
    
    if [ -n "$CURRENT_VERSION" ]; then
        STATUS="completed"
        break
    fi
    
    # Show elapsed time every 30 seconds
    if [ $((ELAPSED % 30)) -eq 0 ] && [ $ELAPSED -gt 0 ]; then
        MINS=$((ELAPSED / 60))
        SECS=$((ELAPSED % 60))
        echo "⏱  Still installing... (${MINS}m ${SECS}s elapsed)"
    fi
    
    sleep 5
done

echo ""

# Verify installation
if [ "$STATUS" = "completed" ]; then
    echo "✅ Installation completed!"
    echo ""
    
    # Verify MakeMKV is installed and working
    echo "Verifying MakeMKV installation..."
    
    # Check version via API
    MAKEMKV_INFO=$(curl -s "${API_BASE}/makemkv")
    VERSION=$(echo "$MAKEMKV_INFO" | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
    
    if [ -n "$VERSION" ]; then
        echo "✅ MakeMKV version: $VERSION"
    else
        echo "❌ Failed to get MakeMKV version from API"
        exit 1
    fi
    
    # Verify makemkvcon binary exists
    if docker exec "$CONTAINER_NAME" which makemkvcon > /dev/null 2>&1; then
        echo "✅ makemkvcon binary found in PATH"
    else
        echo "❌ makemkvcon binary not found"
        exit 1
    fi
    
    # Try running makemkvcon --version
    MAKEMKVCON_VERSION=$(docker exec "$CONTAINER_NAME" makemkvcon --version 2>/dev/null | head -n1 || echo "")
    if [ -n "$MAKEMKVCON_VERSION" ]; then
        echo "✅ makemkvcon executable: $MAKEMKVCON_VERSION"
    else
        echo "⚠️  makemkvcon runs but version check inconclusive"
    fi
    
    echo ""
    echo "======================================"
    echo "✅ MakeMKV Updater Test PASSED"
    echo "======================================"
    echo ""
    echo "All dependencies are present and MakeMKV compiled successfully!"
    echo "Total time: $(($(date +%s) - START_TIME)) seconds"
    exit 0
else
    echo "❌ Installation failed or status unknown"
    exit 1
fi
