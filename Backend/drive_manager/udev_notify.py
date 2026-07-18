#!/usr/bin/env python3
"""
UDS client script to notify drive manager of disc insertion/ejection events.
Replaces udev_rescan.sh for more reliable IPC via Unix Domain Sockets.
"""
import json
import os
import socket
import sys
import logging
from pathlib import Path

# Setup logging for this script
try:
    backend_path = Path(__file__).parent.parent
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))
    from core.logging_utils import get_logger
    logger = get_logger("drive_manager.udev_notify", "main")
except (ImportError, Exception):
    # Fallback to basic logging if core module not available
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("udev_notify")

# Get socket path - must match the server's get_mkvauto_tmp() logic
# Try to import from core.utils if possible (when run from backend context)
try:
    import sys
    backend_path = Path(__file__).parent.parent
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))
    from core.utils import get_mkvauto_tmp
    SOCKET_PATH = get_mkvauto_tmp() / "drive_manager.sock"
except (ImportError, Exception):
    # Fallback: replicate get_mkvauto_tmp() logic
    def _get_mkvauto_tmp():
        """Replicate core.utils.get_mkvauto_tmp() logic."""
        tmp_env = os.getenv("MKVAUTO_TMP_DIR")
        if tmp_env:
            return Path(tmp_env).expanduser()
        # Default: ~/MakeMKV-Auto/tmp (matching get_mkvauto_root() / "tmp")
        root_env = os.getenv("MKVAUTO_ROOT") or os.getenv("MAKEMKV_CONFIG_DIR")
        if root_env:
            root = Path(root_env).expanduser()
        else:
            root = Path.home() / "MakeMKV-Auto"
        tmp_dir = root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        return tmp_dir
    
    SOCKET_PATH = _get_mkvauto_tmp() / "drive_manager.sock"

# Allow override via environment
if "MKVAUTO_DRIVE_MANAGER_SOCK" in os.environ:
    SOCKET_PATH = Path(os.environ["MKVAUTO_DRIVE_MANAGER_SOCK"])


def get_media_state(device: str) -> str:
    """
    Return 'change' to indicate a media change event.
    
    The backend will determine if it's insert or eject based on its cache state.
    This avoids HTTP queries and circular dependencies.
    """
    return 'change'


# Debounce tracking
_last_notify: dict[str, float] = {}
_DEBOUNCE_SECONDS = 0.5


def main():
    """Send disc event to drive manager via UDS."""
    logger.debug("udev_notify.py called argv=%s", sys.argv)
    if len(sys.argv) < 2:
        logger.warning("Usage: udev_notify.py <device> [disc_num] OR udev_notify.py <action> <device> [disc_num]")
        sys.exit(1)
    
    # Support both old and new calling conventions:
    # Old: udev_notify.py insert /dev/sr1 1
    # New: udev_notify.py /dev/sr1 1
    
    # Detect format by checking if first arg is "insert" or "eject"
    if sys.argv[1] in ("insert", "eject"):
        # Old format: action is explicit
        if len(sys.argv) < 3:
            logger.warning("Usage: udev_notify.py <action> <device> [disc_num]")
            sys.exit(1)
        action = sys.argv[1]
        device = sys.argv[2]
        disc_num = sys.argv[3] if len(sys.argv) > 3 else None
        logger.info(f"Using legacy format: action={action}, device={device}, disc_num={disc_num}")
    else:
        # New format: query media state
        device = sys.argv[1]  # "/dev/sr1" or "sr1" (kernel name)
        disc_num = sys.argv[2] if len(sys.argv) > 2 else None
        
        # Normalize device path
        if not device.startswith("/dev/"):
            device = f"/dev/{device}"
        
        # Debounce: ignore rapid repeated events for same device
        # Optical drives emit multiple change events - we only want to act once
        import time
        now = time.time()
        if device in _last_notify:
            if now - _last_notify[device] < _DEBOUNCE_SECONDS:
                logger.debug(f"Debouncing {device} - too soon after last event ({now - _last_notify[device]:.2f}s)")
                return
        _last_notify[device] = now
        
        # Query actual media state (don't trust DISK_MEDIA_CHANGE value)
        # Optical drives send DISK_MEDIA_CHANGE=1 for BOTH insert and eject
        action = get_media_state(device)
        logger.info(f"Using new format with state query: device={device}, disc_num={disc_num}, queried_action={action}")
    
    # Normalize device path
    if not device.startswith("/dev/"):
        device = f"/dev/{device}"
    
    # Normalize disc_num: always extract number from kernel names like "sr0", "sr1"
    # This handles both cases: when disc_num is provided as "sr1" or when it needs extraction from device
    import re
    if disc_num and disc_num.startswith("sr"):
        # Extract number from kernel name (e.g., "sr1" -> "1")
        m = re.match(r"sr(\d+)", disc_num)
        if m:
            disc_num = m.group(1)
            logger.debug(f"Normalized disc_num from kernel name: {sys.argv[2] if len(sys.argv) > 2 else 'N/A'} -> {disc_num}")
    elif not disc_num:
        # Extract disc_num from device if not provided
        m = re.search(r"sr(\d+)$", device)
        if m:
            disc_num = m.group(1)
            logger.debug(f"Extracted disc_num from device path: {device} -> {disc_num}")
    
    # Log final normalized values for debugging
    logger.info(f"Sending UDS event: action={action}, device={device}, disc_num={disc_num}")
    
    # Build message
    message = {
        "action": action,
        "device": device,
        "disc_num": disc_num,
    }
    
    # Connect to UDS and send message
    logger.debug("Attempting UDS connection action=%s device=%s disc_num=%s socket_path=%s socket_exists=%s", 
                action, device, disc_num, str(SOCKET_PATH), SOCKET_PATH.exists())
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(SOCKET_PATH))
        logger.debug("UDS connection successful action=%s device=%s disc_num=%s", action, device, disc_num)
        
        # Send JSON message with newline delimiter
        message_json = json.dumps(message) + "\n"
        sock.sendall(message_json.encode("utf-8"))
        
        # Receive response
        response_data = b""
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            response_data += chunk
            if b"\n" in response_data:
                break
        
        sock.close()
        
        # Parse response
        if response_data:
            try:
                response = json.loads(response_data.decode("utf-8").strip())
                if response.get("status") == "error":
                    logger.error("Error: %s", response.get("message", "Unknown error"))
                    sys.exit(1)
            except json.JSONDecodeError:
                pass  # Ignore parse errors, assume success
        
        sys.exit(0)
    except FileNotFoundError:
        logger.debug("Socket file not found action=%s device=%s disc_num=%s socket_path=%s", 
                    action, device, disc_num, str(SOCKET_PATH))
        logger.error("Socket not found at %s. Drive manager may not be running.", SOCKET_PATH)
        sys.exit(1)
    except ConnectionRefusedError:
        logger.debug("Connection refused action=%s device=%s disc_num=%s socket_path=%s", 
                    action, device, disc_num, str(SOCKET_PATH))
        logger.error("Connection refused to %s. Drive manager may not be running.", SOCKET_PATH)
        sys.exit(1)
    except Exception as exc:
        logger.debug("Connection error action=%s device=%s disc_num=%s error=%s", 
                    action, device, disc_num, str(exc))
        logger.error("Error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

