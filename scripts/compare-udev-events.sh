#!/bin/bash
# Compare host vs container udev events side-by-side
# This helps diagnose if Docker is filtering/delaying events

echo "=== Comparing HOST vs CONTAINER udev events ==="
echo "This will open two monitoring windows side-by-side"
echo ""
echo "Instructions:"
echo "1. Press the eject button on your drive"
echo "2. Observe which window shows events first"
echo "3. Check if both windows show DISK_MEDIA_CHANGE=2 (eject)"
echo "4. Press Ctrl+C in each window to stop"
echo ""

CONTAINER_NAME="mkv-auto"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "ERROR: Container '$CONTAINER_NAME' is not running"
    exit 1
fi

# Check if tmux is available
if command -v tmux &> /dev/null; then
    echo "Using tmux for split view..."
    echo "Use Ctrl+B then arrow keys to switch panes"
    echo "Press Ctrl+C in each pane to stop monitoring"
    echo ""
    read -p "Press Enter to start monitoring..."
    
    # Create tmux session with split panes
    tmux new-session -d -s udev-monitor
    tmux split-window -h -t udev-monitor
    
    # Left pane: host monitoring
    tmux send-keys -t udev-monitor:0.0 "echo '=== HOST UDEV ===' && sudo udevadm monitor --environment --subsystem-match=block --property | grep --line-buffered -E 'DEVNAME=/dev/sr|DISK_MEDIA_CHANGE|ACTION='" C-m
    
    # Right pane: container monitoring
    tmux send-keys -t udev-monitor:0.1 "echo '=== CONTAINER UDEV ===' && docker exec ${CONTAINER_NAME} udevadm monitor --environment --subsystem-match=block --property | grep --line-buffered -E 'DEVNAME=/dev/sr|DISK_MEDIA_CHANGE|ACTION='" C-m
    
    # Attach to session
    tmux attach -t udev-monitor
else
    echo "WARNING: tmux not installed, running sequentially instead"
    echo "For split view, install tmux: sudo apt-get install tmux"
    echo ""
    echo "Running host monitoring for 30 seconds..."
    timeout 30 sudo udevadm monitor --environment --subsystem-match=block --property | \
        grep --line-buffered -E "DEVNAME=/dev/sr|DISK_MEDIA_CHANGE|ACTION=" &
    
    echo "Running container monitoring for 30 seconds..."
    timeout 30 docker exec "$CONTAINER_NAME" udevadm monitor --environment --subsystem-match=block --property | \
        grep --line-buffered -E "DEVNAME=/dev/sr|DISK_MEDIA_CHANGE|ACTION="
    
    wait
fi
