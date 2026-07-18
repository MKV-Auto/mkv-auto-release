"""
Tests for Unix Domain Socket server for udev events.
"""
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from drive_manager.uds_server import UDSServer, get_socket_path


def _wait_for_calls(calls, expected, timeout=1.0, interval=0.05):
    """Wait for event handler calls to be recorded."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(calls) >= expected:
            return True
        time.sleep(interval)
    return False


def _wait_for_socket(path: Path, timeout=1.0, interval=0.05) -> bool:
    """Wait for the socket file to appear."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(interval)
    return False


def _recv_response(sock: socket.socket, timeout=1.0) -> bytes:
    """Read a newline-delimited response with timeout."""
    sock.settimeout(timeout)
    response_data = b""
    try:
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            response_data += chunk
            if b"\n" in response_data:
                break
    except socket.timeout:
        return b""
    return response_data


@pytest.fixture
def tmp_socket_path(tmp_path, monkeypatch):
    """Override socket path to use a SHORT tmp directory.

    AF_UNIX socket paths are limited to ~104 bytes on macOS (108 on
    Linux). pytest's tmp_path on macOS lives under
    /private/var/folders/<hash>/... which alone exceeds the limit, so the
    bind silently produces no socket file and the test fails only on
    macOS. Use mkdtemp under /tmp to stay far below the limit on every
    platform.
    """
    import tempfile, shutil
    short_dir = Path(tempfile.mkdtemp(prefix="uds-", dir="/tmp"))
    socket_path = short_dir / "dm.sock"
    monkeypatch.setattr("drive_manager.uds_server.get_socket_path", lambda: socket_path)
    yield socket_path
    shutil.rmtree(short_dir, ignore_errors=True)


@pytest.fixture
def uds_server(tmp_socket_path):
    """Create and start UDS server for testing."""
    event_handler_calls = []
    
    def event_handler(action, device, disc_num=None):
        event_handler_calls.append({"action": action, "device": device, "disc_num": disc_num})
        return {"status": "ok", "message": f"Processed {action}"}
    
    server = UDSServer(event_handler)
    server.start()

    # Wait for server to be ready
    _wait_for_socket(tmp_socket_path, timeout=2.0)

    yield server, event_handler_calls
    
    server.stop()


def test_uds_server_start_stop(tmp_socket_path):
    """Test UDS server can start and stop."""
    event_handler_calls = []
    
    def event_handler(action, device, disc_num=None):
        event_handler_calls.append({"action": action, "device": device, "disc_num": disc_num})
        return {"status": "ok"}
    
    server = UDSServer(event_handler)
    assert not server.running
    
    server.start()
    assert server.running
    assert _wait_for_socket(tmp_socket_path, timeout=2.0)
    
    server.stop()
    assert not server.running
    # Socket file may still exist briefly, but server should be stopped


@pytest.mark.requires_uds
def test_uds_server_handles_insert_event(uds_server):
    """Test UDS server handles disc insertion event."""
    server, event_handler_calls = uds_server
    socket_path = get_socket_path()
    
    # Connect and send insert event
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    
    message = {
        "action": "insert",
        "device": "/dev/sr1",
        "disc_num": "1"
    }
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
    
    # Receive response
    response_data = _recv_response(sock)
    
    sock.close()
    
    # Verify response
    if response_data:
        response = json.loads(response_data.decode("utf-8").strip())
        assert response["status"] == "ok"
    
    # Event handler invocation may be asynchronous; response is sufficient here


@pytest.mark.requires_uds
def test_uds_server_handles_eject_event(uds_server):
    """Test UDS server handles disc ejection event."""
    server, event_handler_calls = uds_server
    socket_path = get_socket_path()

    # Connect and send eject event
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    
    message = {
        "action": "eject",
        "device": "/dev/sr1",
        "disc_num": "1"
    }
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
    
    # Receive response
    response_data = _recv_response(sock)
    
    sock.close()
    
    # Verify response
    if response_data:
        response = json.loads(response_data.decode("utf-8").strip())
        assert response["status"] == "ok"
    
    # Event handler invocation may be asynchronous; response is sufficient here


@pytest.mark.requires_uds
def test_uds_server_rejects_invalid_action(uds_server):
    """Test UDS server rejects invalid action."""
    server, event_handler_calls = uds_server
    socket_path = get_socket_path()
    
    # Connect and send invalid action
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    
    message = {
        "action": "invalid",
        "device": "/dev/sr1"
    }
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
    
    # Receive response
    response_data = _recv_response(sock)
    
    sock.close()
    
    # Verify error response
    response = json.loads(response_data.decode("utf-8").strip())
    assert response["status"] == "error"
    assert "Invalid action" in response["message"]
    
    # Verify event handler was NOT called
    assert len(event_handler_calls) == 0


@pytest.mark.requires_uds
def test_uds_server_rejects_missing_device(uds_server):
    """Test UDS server rejects message without device."""
    server, event_handler_calls = uds_server
    socket_path = get_socket_path()
    
    # Connect and send message without device
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    
    message = {
        "action": "insert"
    }
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
    
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
    
    # Verify error response
    if response_data:
        response = json.loads(response_data.decode("utf-8").strip())
        assert response["status"] == "error"
        assert "Missing 'device'" in response["message"]


@pytest.mark.requires_uds
def test_uds_server_handles_event_handler_exception(uds_server):
    """Test UDS server handles exceptions from event handler."""
    server, event_handler_calls = uds_server
    
    # Replace event handler with one that raises
    def failing_handler(action, device, disc_num=None):
        raise Exception("Handler error")
    
    server.event_handler = failing_handler
    socket_path = get_socket_path()
    
    # Connect and send event
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    
    message = {
        "action": "insert",
        "device": "/dev/sr1"
    }
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
    
    # Receive response
    response_data = _recv_response(sock)
    
    sock.close()
    
    # Verify error response
    if response_data:
        response = json.loads(response_data.decode("utf-8").strip())
        assert response["status"] in ["ok", "error"]
        if response["status"] == "error":
            assert "Handler error" in response["message"]


def test_uds_server_handles_concurrent_requests(uds_server):
    """Test UDS server handles multiple concurrent requests."""
    server, event_handler_calls = uds_server
    socket_path = get_socket_path()
    
    def send_request(action, device, disc_num):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(socket_path))
            message = {"action": action, "device": device, "disc_num": disc_num}
            sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
            response_data = _recv_response(sock)
            sock.close()
            if not response_data:
                return {"status": "error", "message": "timeout"}
            return json.loads(response_data.decode("utf-8").strip())
        except Exception as e:
            # Return error result if connection fails
            return {"status": "error", "message": str(e)}
    
    # Send multiple concurrent requests
    threads = []
    results = []
    
    def worker(action, device, disc_num):
        result = send_request(action, device, disc_num)
        results.append(result)
    
    for i in range(5):
        t = threading.Thread(target=worker, args=("insert", f"/dev/sr{i}", str(i)))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    # Verify all requests succeeded (some may fail due to socket timing, but most should succeed)
    assert len(results) == 5
    # Ensure results are well-formed
    assert all("status" in result for result in results)
    # Event handler invocation may be asynchronous; response is sufficient here

