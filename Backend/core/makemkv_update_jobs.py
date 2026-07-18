import asyncio
import json
import os
import queue as stdlib_queue
import socket
import threading
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Dict, Tuple, Any, Optional, List, AsyncIterator

from core.makemkv_updater import update_makemkv, MakeMKVUpdateError
from core import makemkv_state
import logging

log = logging.getLogger(__name__)


class UpdateJob:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
        self.status = "pending"  # pending, running, completed, failed
        self.error: Optional[str] = None
        self.version: Optional[str] = None
        self.logs: List[str] = []  # Persist all logs
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

jobs: Dict[str, UpdateJob] = {}

# Configuration for job cleanup
MAX_LOG_LINES = 10000  # Maximum logs per job
JOB_RETENTION_HOURS = 24  # Remove jobs older than 24 hours


def cleanup_old_jobs() -> int:
    """
    Remove jobs older than JOB_RETENTION_HOURS.
    Returns the number of jobs removed.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=JOB_RETENTION_HOURS)
    
    to_remove = []
    for job_id, job in jobs.items():
        if job.created_at < cutoff:
            to_remove.append(job_id)
    
    for job_id in to_remove:
        del jobs[job_id]
    
    return len(to_remove)


def start_update_job(
    version: Optional[str],
    build_ffmpeg: bool,
    ffmpeg_advanced_features: bool,
    install_prefix: Optional[str],
    work_dir: Optional[str],
    loop: asyncio.AbstractEventLoop,
) -> str:
    job_id = str(uuid4())
    queue: asyncio.Queue[Tuple[str, Any]] = asyncio.Queue()
    jobs[job_id] = UpdateJob(queue)
    
    # Clean up old jobs before starting a new one
    cleanup_old_jobs()

    async def runner():
        jobs[job_id].status = "running"
        jobs[job_id].updated_at = datetime.utcnow()
        
        # Import here to avoid circular dependency
        try:
            from api.routers.websockets import _emit_to_coordinator
            websocket_available = True
        except ImportError:
            log.warning("WebSocket emit not available, skipping real-time updates")
            websocket_available = False
        
        try:
            async def emit_ws_log(msg: str):
                """Emit log via WebSocket for real-time updates."""
                if websocket_available:
                    try:
                        await _emit_to_coordinator("makemkv_update_log", {
                            "job_id": job_id,
                            "line": msg,
                        })
                    except Exception as e:
                        pass
                        pass
            
            async def emit_ws_status(status: str, payload: dict):
                """Emit status via WebSocket for real-time updates."""
                if websocket_available:
                    try:
                        # Filter out 'type' from payload to avoid overriding our event type
                        filtered_payload = {k: v for k, v in payload.items() if k != "type"}
                        await _emit_to_coordinator("makemkv_update_status", {
                            "job_id": job_id,
                            "status": status,
                            **filtered_payload,
                        })
                    except Exception as e:
                        pass
                        pass
            
            def store_log(msg: str):
                """Store log in job and internal queue (sync)."""
                if len(jobs[job_id].logs) >= MAX_LOG_LINES:
                    jobs[job_id].logs.pop(0)
                jobs[job_id].logs.append(msg)
                jobs[job_id].updated_at = datetime.utcnow()
                loop.call_soon_threadsafe(queue.put_nowait, ("log", msg))

            # Read from root helper in a dedicated thread so event loop never starves the socket
            candidate_socks = [
                os.getenv("MAKEMKV_ROOT_HELPER_SOCK", "/run/makemkv_updater.sock"),
                "/run/makemkv_helper.sock",
                "/tmp/makemkv_auto.sock",
                "/tmp/makemkv_updater.sock",
            ]
            stream_payload = {
                "version": version,
                "build_ffmpeg": build_ffmpeg,
                "ffmpeg_advanced_features": ffmpeg_advanced_features,
                "install_prefix": install_prefix,
                "work_dir": work_dir,
            }
            sync_stream_queue: stdlib_queue.Queue = stdlib_queue.Queue()
            stream_thread = threading.Thread(
                target=_sync_stream_reader,
                args=(stream_payload, sync_stream_queue, candidate_socks),
                daemon=True,
            )
            stream_thread.start()

            # Log lines go to ws_queue; a dedicated task sends them so we never block on WebSocket
            ws_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

            async def ws_sender():
                while True:
                    line = await ws_queue.get()
                    if line is None:
                        break
                    try:
                        await emit_ws_log(line)
                    except Exception:
                        pass

            ws_sender_task = asyncio.create_task(ws_sender())

            try:
                while True:
                    kind, payload = await loop.run_in_executor(None, sync_stream_queue.get)
                    if kind == "_done":
                        break
                    if kind == "_error":
                        raise MakeMKVUpdateError(payload.get("error", "Stream error"))
                    if kind == "log":
                        line = payload.get("line", "")
                        store_log(line)
                        await ws_queue.put(line)
                    elif kind == "status":
                        # Flush log queue so frontend gets all lines before status (avoids cleanup hiding later logs)
                        while not ws_queue.empty():
                            await asyncio.sleep(0.05)
                        if payload.get("status") == "completed":
                            jobs[job_id].status = "completed"
                            jobs[job_id].version = payload.get("version")
                            jobs[job_id].updated_at = datetime.utcnow()
                            makemkv_state.clear_disabled()
                        else:
                            jobs[job_id].status = "failed"
                            jobs[job_id].error = payload.get("error")
                            jobs[job_id].updated_at = datetime.utcnow()
                        loop.call_soon_threadsafe(queue.put_nowait, ("status", payload))
                        await emit_ws_status(jobs[job_id].status, payload)
                        if payload.get("status") == "completed":
                            try:
                                from core.startup_discs import (
                                    run_startup_drive_warmup_if_makemkv_ready,
                                )

                                drives_snapshot = await loop.run_in_executor(
                                    None, run_startup_drive_warmup_if_makemkv_ready
                                )
                                # #613: emit so the frontend coordinator refetches
                                # /drives/drives + the disc list. Without this the
                                # carousel stays empty / "Insert Disc" even though
                                # the backend cache now has any loaded discs.
                                try:
                                    from api.routers.websockets import _emit_to_coordinator

                                    await _emit_to_coordinator(
                                        "makemkv_drives_ready",
                                        {
                                            "drives_count": len(drives_snapshot or []),
                                            "source": "post-install",
                                            "job_id": job_id,
                                        },
                                    )
                                except Exception as emit_exc:
                                    log.warning(
                                        "Failed to emit makemkv_drives_ready after install: %s",
                                        emit_exc,
                                    )
                            except Exception as wexc:
                                log.warning(
                                    "Post-install drive warmup failed: %s",
                                    wexc,
                                    exc_info=True,
                                )
            finally:
                await ws_queue.put(None)
                await ws_sender_task
            if jobs[job_id].status != "completed":
                raise MakeMKVUpdateError(jobs[job_id].error or "Update failed via root helper")
        except Exception as exc:
            jobs[job_id].status = "failed"
            jobs[job_id].error = str(exc)
            jobs[job_id].updated_at = datetime.utcnow()
            loop.call_soon_threadsafe(queue.put_nowait, ("status", {"status": "failed", "error": str(exc)}))
            # Emit failure status via WebSocket
            if websocket_available:
                try:
                    await _emit_to_coordinator("makemkv_update_status", {
                        "job_id": job_id,
                        "status": "failed",
                        "error": str(exc),
                    })
                except Exception:
                    pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

    loop.create_task(runner())
    return job_id


def get_job(job_id: str) -> Optional[UpdateJob]:
    return jobs.get(job_id)


def get_active_job() -> Optional[Tuple[str, UpdateJob]]:
    """Return (job_id, job) for the first job with status pending or running, or None."""
    for job_id, job in jobs.items():
        if job.status in ("pending", "running"):
            return (job_id, job)
    return None


def _sync_stream_reader(
    payload: dict,
    out_queue: stdlib_queue.Queue,
    candidate_socks: List[str],
) -> None:
    """
    Run in a dedicated thread: connect to root helper via Unix socket, send request,
    read JSON lines and put (kind, data) into out_queue. Never blocks the event loop.
    """
    sock = None
    try:
        for path in candidate_socks:
            if not path:
                continue
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(30.0)
                sock.connect(path)
                break
            except (FileNotFoundError, PermissionError, ConnectionRefusedError, OSError):
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None
                continue
        if sock is None:
            out_queue.put(("_error", {"error": "Root helper socket not reachable"}))
            return
        sock.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    data = json.loads(line.decode().strip())
                    kind = data.get("type")
                    out_queue.put((kind, data))
                    if kind == "status" and data.get("status") in ("completed", "failed"):
                        return
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        out_queue.put(("_error", {"error": str(e)}))
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        out_queue.put(("_done", None))


async def _stream_from_root_helper(
    version: Optional[str],
    build_ffmpeg: bool,
    ffmpeg_advanced_features: bool,
    install_prefix: Optional[str],
    work_dir: Optional[str],
) -> AsyncIterator[tuple[str, dict]]:
    """
    Connect to the root helper via Unix socket and stream log/status events.
    Protocol: client sends one JSON line; server streams JSON lines with type fields.
    """
    primary_sock = os.getenv("MAKEMKV_ROOT_HELPER_SOCK", "/run/makemkv_updater.sock")
    # Try multiple well-known paths to avoid env mismatches.
    candidate_socks = [
        primary_sock,
        "/run/makemkv_helper.sock",
        "/tmp/makemkv_auto.sock",
        "/tmp/makemkv_updater.sock",
    ]

    async def _connect(path: str):
        return await asyncio.open_unix_connection(path, limit=1024*1024)  # allow large log lines

    last_err: Exception | None = None
    for path in candidate_socks:
        if not path:
            continue
        try:
            reader, writer = await _connect(path)
            break
        except (FileNotFoundError, PermissionError, ConnectionRefusedError) as exc:
            last_err = exc
            continue
        except Exception as exc:
            last_err = exc
            continue
    else:
        detail = (
            f"Root helper socket not reachable at any of: {', '.join(candidate_socks)}. "
            "Start it as root (sudo python3 Backend/root_update_helper.py) or run ./manage.sh start."
        )
        if last_err:
            detail += f" Last error: {last_err}"
        raise MakeMKVUpdateError(detail)

    payload = {
        "version": version,
        "build_ffmpeg": build_ffmpeg,
        "ffmpeg_advanced_features": ffmpeg_advanced_features,
        "install_prefix": install_prefix,
        "work_dir": work_dir,
    }
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                data = json.loads(line.decode().strip())
                kind = data.get("type")
                yield kind, data
                if kind == "status" and data.get("status") in ("completed", "failed"):
                    break
            except json.JSONDecodeError:
                continue
    finally:
        writer.close()
        await writer.wait_closed()
