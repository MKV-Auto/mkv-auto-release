#!/usr/bin/env python3
"""
Root-only helper that performs MakeMKV updates and streams logs over a Unix socket.
Run this as root (e.g., via systemd) and the main app can request an update without
running itself as root.
"""
import asyncio
import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

from core.logging_utils import get_logger
from core.makemkv_updater import update_makemkv, MakeMKVUpdateError

logger = get_logger("root_update_helper")

SOCKET_PATH = os.getenv("MAKEMKV_ROOT_HELPER_SOCK", "/run/makemkv_updater.sock")
FALLBACK_SOCKET = "/tmp/makemkv_updater.sock"


def _mount_device(device: str) -> dict:
    if not device:
        return {"status": "error", "error": "No device specified"}
    mount_dir = Path(tempfile.mkdtemp(prefix="makemkv-mount-"))
    cmd = ["mount", "-o", "ro", "-t", "udf", device, str(mount_dir)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=int(os.getenv("MAKEMKV_MOUNT_TIMEOUT", "30")))
        return {"status": "ok", "mount_point": str(mount_dir)}
    except subprocess.TimeoutExpired:
        # try to detach any partial mount and clean the directory
        try:
            subprocess.run(["umount", "-l", str(mount_dir)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        try:
            mount_dir.rmdir()
        except Exception:
            pass
        return {"status": "error", "error": f"Mount timed out after {os.getenv('MAKEMKV_MOUNT_TIMEOUT', '30')}s"}
    except subprocess.CalledProcessError as exc:
        try:
            mount_dir.rmdir()
        except Exception:
            pass
        return {"status": "error", "error": f"Mount failed: {exc}"}


def _mount_smb(host: str, share: str, mount_point: str, port: int, username: str, password: str, domain: str) -> dict:
    """Mount SMB/CIFS share."""
    mount_options = []
    if username:
        mount_options.append(f"username={username}")
    if password:
        mount_options.append(f"password={password}")
    if domain:
        mount_options.append(f"domain={domain}")
    if not username and not password:
        mount_options.append("guest")
    
    # Use mount.cifs directly instead of mount -t cifs
    # This is the proper way and works even if mount.cifs is in a non-standard location
    # Try to find mount.cifs in common locations
    mount_cifs_path = None
    search_paths = ["/sbin/mount.cifs", "/usr/sbin/mount.cifs"]
    for path in search_paths:
        if os.path.exists(path):
            mount_cifs_path = path
            break
    
    # Try shutil.which to find it in PATH
    if not mount_cifs_path:
        import shutil
        which_path = shutil.which("mount.cifs")
        if which_path:
            mount_cifs_path = which_path
    
    # If still not found, return error before trying to mount
    if not mount_cifs_path:
        return {"status": "error", "error": "mount.cifs not found. Please install cifs-utils package (e.g., 'apt-get install cifs-utils' or 'yum install cifs-utils')"}
    
    mount_cmd = [mount_cifs_path]
    if mount_options:
        mount_cmd.extend(["-o", ",".join(mount_options)])
    
    smb_path = f"//{host}/{share}"
    if port != 445:
        smb_path = f"//{host}:{port}/{share}"
    
    mount_cmd.extend([smb_path, mount_point])
    
    try:
        result = subprocess.run(
            mount_cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return {"status": "ok", "mount_point": mount_point}
        else:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown mount error"
            # Check if it's a missing mount helper error
            if "mount.cifs" in error_msg.lower() or "helper program" in error_msg.lower() or "bad option" in error_msg.lower() or result.returncode == 127:
                return {"status": "error", "error": f"Mount failed: {error_msg}. Please install cifs-utils package (e.g., 'apt-get install cifs-utils' or 'yum install cifs-utils')"}
            return {"status": "error", "error": f"Mount failed: {error_msg}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Mount timed out"}
    except Exception as exc:
        return {"status": "error", "error": f"Mount failed: {exc}"}


def _mount_nfs(server: str, export_path: str, mount_point: str, options: str = "") -> dict:
    """Mount NFS share."""
    # Build mount options
    # If no options provided, use minimal defaults (nfsvers=3)
    # Note: anonuid/anongid may not be valid for all NFS versions, so we use minimal options
    # Users can specify their own options via credentials if needed
    if not options:
        mount_opts = "nfsvers=3"
    else:
        mount_opts = options
    
    # Always use mount -n -t nfs to skip fstab
    # The -n flag tells mount not to write to or read from /etc/fstab
    # This will still call mount.nfs internally, but without trying to apply fstab options
    mount_cmd = ["mount", "-n", "-t", "nfs", "-o", mount_opts, f"{server}:{export_path}", mount_point]
    
    try:
        result = subprocess.run(
            mount_cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return {"status": "ok", "mount_point": mount_point}
        else:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown mount error"
            # Check if it's a missing mount helper error (not a permission/access error)
            if ("mount.nfs" in error_msg.lower() and ("not found" in error_msg.lower() or "no such file" in error_msg.lower())) or \
               "helper program" in error_msg.lower() or \
               ("bad option" in error_msg.lower() and "mount.nfs" in error_msg.lower()) or \
               result.returncode == 127:
                return {"status": "error", "error": f"Mount failed: {error_msg}. Please install nfs-common package (e.g., 'apt-get install nfs-common' or 'yum install nfs-utils')"}
            # For other errors (like access denied), return the actual error without package suggestion
            return {"status": "error", "error": f"Mount failed: {error_msg}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Mount timed out"}
    except Exception as exc:
        return {"status": "error", "error": f"Mount failed: {exc}"}

def _reset_device(device: str) -> dict:
    """
    Best-effort reset of a stuck block device by deleting it from sysfs and rescanning its host.
    """
    if not device:
        return {"status": "error", "error": "No device specified"}
    name = Path(device).name
    dev_sys = Path(f"/sys/block/{name}/device")
    if not dev_sys.exists():
        return {"status": "error", "error": f"No sysfs entry for {device}"}

    try:
        (dev_sys / "delete").write_text("1")
    except Exception as exc:
        return {"status": "error", "error": f"Delete failed: {exc}"}

    # find hostX in the resolved path to trigger a rescan
    host = None
    for parent in dev_sys.resolve().parents:
        if parent.name.startswith("host"):
            host = parent.name
            break
    if host:
        try:
            Path(f"/sys/class/scsi_host/{host}/scan").write_text("- - -")
        except Exception:
            pass

    return {"status": "ok", "message": f"{device} deleted; rescanned {host or 'unknown host'}"}


def _unmount_device(mount_point: str) -> dict:
    if not mount_point:
        return {"status": "error", "error": "No mount_point specified"}
    try:
        subprocess.run(["umount", mount_point], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # best-effort cleanup
        try:
            Path(mount_point).rmdir()
        except Exception:
            pass
        return {"status": "ok"}
    except subprocess.CalledProcessError as exc:
        return {"status": "error", "error": f"Unmount failed: {exc}"}


async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        raw = await reader.readline()
        payload = json.loads(raw.decode().strip())
        cmd = payload.get("cmd", "update")

        if cmd == "mount":
            device = payload.get("device")
            status = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _mount_device(device)
            )
            writer.write((json.dumps(status) + "\n").encode())
            await writer.drain()
            return

        if cmd == "mount_smb":
            host = payload.get("host")
            share = payload.get("share")
            mount_point = payload.get("mount_point")
            port = payload.get("port", 445)
            username = payload.get("username", "")
            password = payload.get("password", "")
            domain = payload.get("domain", "")
            status = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _mount_smb(host, share, mount_point, port, username, password, domain)
            )
            writer.write((json.dumps(status) + "\n").encode())
            await writer.drain()
            return

        if cmd == "mount_nfs":
            server = payload.get("server")
            export_path = payload.get("export_path")
            mount_point = payload.get("mount_point")
            options = payload.get("options", "")
            status = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _mount_nfs(server, export_path, mount_point, options)
            )
            writer.write((json.dumps(status) + "\n").encode())
            await writer.drain()
            return

        if cmd == "unmount":
            mount_point = payload.get("mount_point")
            status = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _unmount_device(mount_point)
            )
            writer.write((json.dumps(status) + "\n").encode())
            await writer.drain()
            return

        if cmd == "reset_device":
            device = payload.get("device")
            status = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _reset_device(device)
            )
            writer.write((json.dumps(status) + "\n").encode())
            await writer.drain()
            return

        # default: update
        version = payload.get("version")
        build_ffmpeg = bool(payload.get("build_ffmpeg", True))
        ffmpeg_advanced_features = bool(payload.get("ffmpeg_advanced_features", True))
        install_prefix = payload.get("install_prefix")
        work_dir = payload.get("work_dir")

        loop = asyncio.get_running_loop()
        log_queue = asyncio.Queue()

        async def log_writer_task():
            """Async task that consumes logs from queue and writes to socket.
            This prevents deadlock by decoupling the blocking thread from async socket writing.
            """
            try:
                while True:
                    line = await log_queue.get()
                    if line is None:  # Sentinel value to stop
                        log_queue.task_done()
                        break
                    msg = json.dumps({"type": "log", "line": line}) + "\n"
                    writer.write(msg.encode())
                    await writer.drain()
                    log_queue.task_done()
            except Exception as e:
                # Log the error for debugging
                import traceback
                error_msg = f"Log writer task failed: {e}\n{traceback.format_exc()}"
                try:
                    # Try to write error to socket before failing
                    err_msg = json.dumps({"type": "log", "line": error_msg}) + "\n"
                    writer.write(err_msg.encode())
                    await writer.drain()
                except Exception:
                    pass
                # Drain remaining queue to unblock any waiting threads
                while not log_queue.empty():
                    try:
                        log_queue.get_nowait()
                        log_queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                # Re-raise to propagate the error
                raise

        # Start the log writer task
        writer_task = asyncio.create_task(log_writer_task())

        def emit(line: str):
            """Called from blocking thread - put log in queue for async writing."""
            loop.call_soon_threadsafe(lambda l=line: log_queue.put_nowait(l))

        async def heartbeat_while_running(interval: float = 5.0):
            """Emit a heartbeat every interval seconds so the event loop runs and flushes queued log lines from the executor."""
            while True:
                await asyncio.sleep(interval)
                loop.call_soon_threadsafe(lambda: log_queue.put_nowait("Still downloading…"))

        heartbeat_task = asyncio.create_task(heartbeat_while_running())

        try:
            # If already running as root (uid 0), don't use sudo
            # This is common in Docker containers where sudo isn't installed
            is_root = os.getuid() == 0
            
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: update_makemkv(
                    version=version,
                    build_ffmpeg=build_ffmpeg,
                    ffmpeg_advanced_features=ffmpeg_advanced_features,
                    install_prefix=install_prefix,
                    work_dir=work_dir,
                    use_sudo_install=not is_root,  # Only use sudo if not already root
                    log_cb=emit,
                )
            )
            status = {"type": "status", "status": "completed", "version": result.get("version")}
        except Exception as exc:
            status = {"type": "status", "status": "failed", "error": str(exc)}
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            # Send sentinel to stop log writer and wait for it to finish
            loop.call_soon_threadsafe(lambda: log_queue.put_nowait(None))
            await writer_task
        
        # Write final status
        writer.write((json.dumps(status) + "\n").encode())
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    # ensure socket path
    sock_path = Path(SOCKET_PATH)
    owner_uid = int(os.getenv("SUDO_UID", os.getuid()))
    owner_gid = int(os.getenv("SUDO_GID", os.getgid()))
    try:
        if sock_path.exists():
            sock_path.unlink()
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        server = await asyncio.start_unix_server(handle_conn, path=SOCKET_PATH, limit=1024*1024)
        os.chmod(SOCKET_PATH, 0o660)
        os.chown(SOCKET_PATH, owner_uid, owner_gid)
    except PermissionError:
        # fallback to /tmp if /run not writable
        fallback = Path(FALLBACK_SOCKET)
        if fallback.exists():
            fallback.unlink()
        fallback.parent.mkdir(parents=True, exist_ok=True)
        server = await asyncio.start_unix_server(handle_conn, path=FALLBACK_SOCKET, limit=1024*1024)
        os.chmod(FALLBACK_SOCKET, 0o660)
        os.chown(FALLBACK_SOCKET, owner_uid, owner_gid)
        logger.info("Using fallback socket at %s", FALLBACK_SOCKET)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
