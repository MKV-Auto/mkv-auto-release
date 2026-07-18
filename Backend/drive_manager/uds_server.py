"""
Unix Domain Socket server for udev events.
Listens on a UDS socket and processes disc insertion/ejection events.
"""
import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Optional

from core.utils import get_mkvauto_tmp
from core.logging_utils import get_logger

log = get_logger("drive_manager.uds_server", "UDSServer")

# Socket path: {MKVAUTO_TMP}/drive_manager.sock
SOCKET_PATH = None


def get_socket_path() -> Path:
    """Get the path to the UDS socket file."""
    global SOCKET_PATH
    if SOCKET_PATH is None:
        tmp_dir = get_mkvauto_tmp()
        SOCKET_PATH = tmp_dir / "drive_manager.sock"
    return SOCKET_PATH


class UDSServer:
    """Unix Domain Socket server for processing udev events."""
    
    def __init__(self, event_handler):
        """
        Initialize UDS server.
        
        Args:
            event_handler: Callable that accepts (action: str, device: str, disc_num: Optional[str])
                          and returns {"status": "ok" | "error", "message": str}
        """
        self.event_handler = event_handler
        self.socket_path = get_socket_path()
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self.server_thread: Optional[threading.Thread] = None
    
    def start(self):
        """Start the UDS server in a background thread."""
        if self.running:
            log.warning("UDS server already running")
            return
        
        # Remove existing socket file if it exists
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception as exc:
                log.error(f"Failed to remove existing socket file: {exc}")
        
        self.running = True
        self.server_thread = threading.Thread(target=self._run_server, daemon=True)
        self.server_thread.start()
        log.info(f"UDS server started on {self.socket_path}")
    
    def stop(self):
        """Stop the UDS server."""
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass
        log.info("UDS server stopped")
    
    def _run_server(self):
        """Run the server loop."""
        try:
            # Create Unix Domain Socket
            self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_socket.bind(str(self.socket_path))
            self.server_socket.listen(5)
            
            # Set socket permissions so udev script can connect
            try:
                os.chmod(self.socket_path, 0o666)
            except Exception as exc:
                log.warning(f"Failed to set socket permissions: {exc}")
            
            log.info(f"UDS server listening on {self.socket_path}")
            
            while self.running:
                try:
                    # Accept connections with timeout to allow checking self.running
                    self.server_socket.settimeout(1.0)
                    client_socket, _ = self.server_socket.accept()
                    
                    # Handle client in a separate thread to allow concurrent requests
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client_socket,),
                        daemon=True
                    )
                    client_thread.start()
                except socket.timeout:
                    # Timeout is expected, continue loop to check self.running
                    continue
                except Exception as exc:
                    if self.running:
                        log.error(f"Error accepting connection: {exc}")
        except Exception as exc:
            log.error(f"UDS server error: {exc}")
        finally:
            if self.server_socket:
                try:
                    self.server_socket.close()
                except Exception:
                    pass
            if self.socket_path.exists():
                try:
                    self.socket_path.unlink()
                except Exception:
                    pass
    
    def _handle_client(self, client_socket: socket.socket):
        """Handle a client connection."""
        import json as json_module, traceback
        try:
            # Receive JSON message
            data = b""
            while True:
                chunk = client_socket.recv(1024)
                if not chunk:
                    break
                data += chunk
                # Messages are newline-delimited
                if b"\n" in data:
                    break
            
            if not data:
                return
            
            # Parse JSON message
            try:
                message = json_module.loads(data.decode("utf-8").strip())
            except json_module.JSONDecodeError as exc:
                log.error(f"Invalid JSON from client: {exc}")
                response = {"status": "error", "message": f"Invalid JSON: {exc}"}
                client_socket.sendall(json_module.dumps(response).encode("utf-8") + b"\n")
                return
            
            # Validate message
            action = message.get("action")
            device = message.get("device")
            disc_num = message.get("disc_num")
            
            if not action or action not in ("insert", "eject", "change"):
                response = {"status": "error", "message": "Invalid action (must be 'insert', 'eject', or 'change')"}
                client_socket.sendall(json_module.dumps(response).encode("utf-8") + b"\n")
                return
            
            if not device:
                response = {"status": "error", "message": "Missing 'device' field"}
                client_socket.sendall(json_module.dumps(response).encode("utf-8") + b"\n")
                return
            
            # Process event
            log.info(f"Processing UDS event: action={action}, device={device}, disc_num={disc_num}")
            try:
                result = self.event_handler(action, device, disc_num)
                response = result if isinstance(result, dict) else {"status": "ok"}
                log.info(f"UDS event processed successfully: action={action}, device={device}, result_status={response.get('status', 'unknown')}")
            except Exception as exc:
                log.error(f"Error processing event: {exc}")
                response = {"status": "error", "message": str(exc)}
            
            # Send response
            client_socket.sendall(json_module.dumps(response).encode("utf-8") + b"\n")
        except Exception as exc:
            log.error(f"Error handling client: {exc}")
            log.error(traceback.format_exc())
        finally:
            try:
                client_socket.close()
            except Exception:
                pass

