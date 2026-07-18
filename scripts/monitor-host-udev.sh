#!/bin/bash
# Monitor host udev events for optical drives
# Run this ON THE HOST (outside Docker) to see if eject events are being generated

echo "=== Monitoring HOST udev events for optical drives ==="
echo "Press Ctrl+C to stop"
echo "Press the eject button on your drive to test..."
echo ""

if ! command -v udevadm &> /dev/null; then
    echo "ERROR: udevadm not found. This script must run on the host, not in Docker."
    exit 1
fi

# Monitor with timestamps if ts is available
if command -v ts &> /dev/null; then
    sudo udevadm monitor --environment --subsystem-match=block --property | \
        ts '[%Y-%m-%d %H:%M:%.S]' | \
        grep --line-buffered -E "DEVNAME=/dev/sr|DISK_MEDIA_CHANGE|ACTION="
else
    echo "Note: Install 'moreutils' package for timestamps (optional)"
    echo ""
    sudo udevadm monitor --environment --subsystem-match=block --property | \
        grep --line-buffered -E "DEVNAME=/dev/sr|DISK_MEDIA_CHANGE|ACTION="
fi
