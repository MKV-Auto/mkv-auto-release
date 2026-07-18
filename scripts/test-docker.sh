#!/bin/bash
# Test script for MKV-Auto Docker image

set -e

CONTAINER_NAME="mkv-auto-test"
VOLUME_NAME="mkv-test-data"
PORT=8080
KEEP=0
CONTAINER_STARTED=0

# Parse args: optional IMAGE and/or --keep
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    *)      IMAGE="${IMAGE:-$arg}" ;;
  esac
done
IMAGE=${IMAGE:-mkv-auto:latest}

teardown() {
  if [ "$CONTAINER_STARTED" -eq 1 ]; then
    echo ""
    echo "Tearing down test container and volume..."
    docker stop $CONTAINER_NAME 2>/dev/null || true
    docker rm $CONTAINER_NAME 2>/dev/null || true
    docker volume rm $VOLUME_NAME 2>/dev/null || true
  fi
}

if [ "$KEEP" -eq 0 ]; then
  trap teardown EXIT
fi

echo "Testing MKV-Auto Docker image: $IMAGE"
echo ""

# Clean up any existing test container
echo "Cleaning up existing test container..."
docker stop $CONTAINER_NAME 2>/dev/null || true
docker rm $CONTAINER_NAME 2>/dev/null || true

# Start the container
echo "Starting container..."
docker run -d \
  --name $CONTAINER_NAME \
  -p $PORT:80 \
  -v $VOLUME_NAME:/data \
  $IMAGE
CONTAINER_STARTED=1

echo ""
echo "Waiting for container to start (30 seconds)..."
sleep 30

# Check container status
echo ""
echo "Container status:"
docker ps -f name=$CONTAINER_NAME

echo ""
echo "Container logs (last 20 lines):"
docker logs --tail 20 $CONTAINER_NAME

# Test health endpoint
echo ""
echo "Testing health endpoint..."
if curl -sf http://localhost:$PORT/api/system/health > /dev/null; then
    echo "✅ Health check passed"
else
    echo "⚠️  Health check failed (this is normal if backend is still starting)"
fi

# Test frontend
echo ""
echo "Testing frontend..."
if curl -sf http://localhost:$PORT/ | grep -qi "<!doctype html>"; then
    echo "✅ Frontend is serving"
else
    echo "❌ Frontend test failed"
fi

echo ""
echo "============================================"
if [ "$KEEP" -eq 1 ]; then
  echo "Test container is running!"
  echo "============================================"
  echo "Access at: http://localhost:$PORT"
  echo ""
  echo "To view logs:"
  echo "  docker logs -f $CONTAINER_NAME"
  echo ""
  echo "To stop and remove:"
  echo "  docker stop $CONTAINER_NAME && docker rm $CONTAINER_NAME"
  echo ""
  echo "To clean up test data:"
  echo "  docker volume rm $VOLUME_NAME"
else
  echo "Tests complete. Container and volume will be cleaned up on exit."
  echo "============================================"
fi
