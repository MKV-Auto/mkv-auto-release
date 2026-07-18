#!/bin/bash
# Monitor container udev events for optical drives  
# Run this on the host to see what the Docker container receives

echo "=== Monitoring CONTAINER udev events for optical drives ==="
echo "Press Ctrl+C to stop"
echo "Press the eject button on your drive to test..."
echo ""

CONTAINER_NAME="mkv-auto"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "ERROR: Container '$CONTAINER_NAME' is not running"
    echo "Available containers:"
    docker ps --format '{{.Names}}'
    exit 1
fi

# Monitor with timestamps if ts is available
if command -v ts &> /dev/null; then
    docker exec "$CONTAINER_NAME" udevadm monitor --environment --subsystem-match=block --property | \
        ts '[%Y-%m-%d %H:%M:%.S]' | \
        grep --line-buffered -E "DEVNAME=/dev/sr|DISK_MEDIA_CHANGE|ACTION="
else
    echo "Note: Install 'moreutils' package for timestamps (optional)"
    echo ""
    docker exec "$CONTAINER_NAME" udevadm monitor --environment --subsystem-match=block --property | \
        grep --line-buffered -E "DEVNAME=/dev/sr|DISK_MEDIA_CHANGE|ACTION="
fi
