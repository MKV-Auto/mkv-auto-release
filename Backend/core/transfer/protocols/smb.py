"""
SMB transfer implementation using smbprotocol library.
Falls back to smbclient command-line tool for anonymous/guest access.
"""
from pathlib import Path
from typing import Optional, Callable, Dict, Any, Tuple
from sqlalchemy.orm import Session
from api import models
from core.transfer.validation import calculate_file_hash
from core.transfer.monitoring import SpeedTracker
from core.transfer.utils.credentials import get_decrypted_credentials
import logging
import re
import subprocess
import shutil
import os
import math
log = logging.getLogger(__name__)


# #712: the smbclient put ran under a blunt hardcoded 1-hour cap, which killed
# large UHD transfers mid-flight ("Transfer timeout") — a ~60-90 GB main title
# over SMB can take well over an hour. Scale the per-file timeout by size,
# assuming a conservative minimum throughput, with a 1-hour floor and an
# absolute env override. This fails only genuinely-stuck transfers, not
# slow-but-progressing large files.
#   MKVAUTO_SMB_PUT_TIMEOUT          absolute per-put timeout (seconds) — wins if set
#   MKVAUTO_SMB_MIN_BYTES_PER_SEC    assumed floor throughput (default 2 MiB/s)
#   MKVAUTO_SMB_PUT_TIMEOUT_FLOOR    minimum timeout (default 3600s)
def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "") or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _smb_put_timeout(total_size_bytes: int | None) -> int:
    """Per-put subprocess timeout scaled by file size (see note above)."""
    override = os.getenv("MKVAUTO_SMB_PUT_TIMEOUT")
    if override:
        try:
            n = int(override)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    floor = _env_int("MKVAUTO_SMB_PUT_TIMEOUT_FLOOR", 3600)
    bps = _env_int("MKVAUTO_SMB_MIN_BYTES_PER_SEC", 2 * 1024 * 1024)
    return max(floor, int(math.ceil((total_size_bytes or 0) / bps)))


_BENIGN_MKDIR_MARKERS: tuple[str, ...] = (
    "NT_STATUS_OBJECT_NAME_COLLISION",
    "NT_STATUS_OBJECT_NAME_EXISTS",
    "already exists",
    "file exists",
)


_NT_STATUS_RE = re.compile(r"NT_STATUS_[A-Z0-9_]+")


def _smb_mkdir_error_benign(stdout: str, stderr: str) -> bool:
    """True if mkdir failed only because the directory already exists."""
    combined = f"{stderr or ''}\n{stdout or ''}"
    if not combined.strip():
        return False
    return any(m in combined for m in _BENIGN_MKDIR_MARKERS)


def _extract_nt_status_error(stdout: str, stderr: str) -> str | None:
    """Return the first non-benign NT_STATUS_* token in smbclient output, or None.

    smbclient often prints `NT_STATUS_*` errors to stdout while still exiting 0
    (e.g. mkdir failing with ACCESS_DENIED on a server that allows the auth but
    not the operation). Callers must inspect output regardless of returncode.
    """
    combined = f"{stderr or ''}\n{stdout or ''}"
    for match in _NT_STATUS_RE.finditer(combined):
        token = match.group(0)
        if token in _BENIGN_MKDIR_MARKERS:
            continue
        return token
    return None


def _smb_quote_path(path: str) -> str:
    """Quote a path for smbclient -c (double quotes; escape embedded quotes)."""
    p = path.replace("\\", "/")
    escaped = p.replace('"', r"\"")
    return f'"{escaped}"'


def _smb_run_mkdir(
    smb_url: str,
    auth_args: list,
    remote_dir_path: str,
) -> tuple[bool, str]:
    """
    Create one remote directory (full path from share root).
    Returns (success, stderr_or_combined_on_failure).

    Note: smbclient often exits 0 even when mkdir fails with NT_STATUS_ACCESS_DENIED
    (or similar) — the error appears only in stdout/stderr. So we always scan output
    for non-benign NT_STATUS_* tokens, independent of returncode. Without this guard,
    an inaccessible parent directory looks like a successful mkdir to the caller, the
    subsequent `put` then fails with the cascaded NT_STATUS_OBJECT_PATH_NOT_FOUND, and
    the user sees the wrong root cause in their notification.
    """
    quoted = _smb_quote_path(remote_dir_path)
    cmd = ["smbclient", smb_url, *auth_args, "-c", f"mkdir {quoted}"]
    result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if _smb_mkdir_error_benign(stdout, stderr):
        return True, ""
    silent_err = _extract_nt_status_error(stdout, stderr)
    if silent_err:
        # NT_STATUS in output — fail regardless of returncode.
        suffix = stderr.strip() or stdout.strip()
        return False, f"{silent_err} ({remote_dir_path}): {suffix}".strip(": ")
    if result.returncode != 0:
        combined = (stderr + stdout).strip()
        return False, combined or f"smbclient mkdir exited {result.returncode}"
    return True, ""


def validate_connection(db: Session, config: models.TransferConfig) -> Tuple[bool, str]:
    """
    Validate SMB connection.
    
    Supports both authenticated and anonymous/guest access.
    For anonymous access, uses smbclient command-line tool as fallback.
    
    Args:
        db: Database session
        config: TransferConfig instance
        
    Returns:
        Tuple of (success, message)
    """
    config_data = config.config_data or {}
    credentials = get_decrypted_credentials(db, config.id)
    
    host = config_data.get("host")
    share = config_data.get("share")
    username = credentials.get("smb_username", "") or ""
    password = credentials.get("smb_password", "") or ""
    domain = credentials.get("smb_domain", "") or ""
    port = config_data.get("port", 445)
    
    if not host or not share:
        return False, "SMB host and share not configured"
    
    # Check if this is anonymous/guest access (no username/password)
    is_anonymous = not username and not password
    
    if is_anonymous:
        # Use smbclient for anonymous access (smbprotocol doesn't support it well)
        if not shutil.which("smbclient"):
            return False, "smbclient command not found. Install samba-client package for anonymous SMB access."
        
        try:
            # Test anonymous connection using smbclient
            # -U% means anonymous/guest access
            result = subprocess.run(
                ["smbclient", f"//{host}/{share}", "-U%", "-N", "-c", "exit"],
                capture_output=True,
                timeout=10,
                text=True
            )
            
            if result.returncode == 0:
                return True, "Anonymous connection successful"
            else:
                return False, f"Anonymous connection failed: {result.stderr.strip() or result.stdout.strip()}"
        except subprocess.TimeoutExpired:
            return False, "Connection timeout"
        except Exception as e:
            return False, f"Anonymous connection failed: {str(e)}"
    else:
        # Use smbprotocol for authenticated access
        try:
            from smbprotocol.connection import Connection
            from smbprotocol.session import Session as SMBSession
            import uuid
            
            # Test connection
            connection = Connection(uuid.uuid4(), host, port)
            connection.connect()
            
            session = SMBSession(connection, username, password, domain)
            session.connect()
            
            # Try to list share
            from smbprotocol.tree import TreeConnect
            tree = TreeConnect(session, f"\\\\{host}\\{share}")
            tree.connect()
            tree.disconnect()
            
            session.disconnect()
            connection.disconnect()
            
            return True, "Connection successful"
        except ImportError:
            return False, "smbprotocol library not installed. Install with: pip install smbprotocol"
        except Exception as e:
            return False, f"SMB connection failed: {str(e)}"


def transfer_smb(
    db: Session,
    job_id: str,
    src_path: Path,
    config: models.TransferConfig,
    progress_callback: Optional[Callable[[int], None]] = None,
    speed_callback: Optional[Callable[[float], None]] = None
) -> Dict[str, Any]:
    """
    Transfer files via SMB.
    
    Args:
        db: Database session
        job_id: Job ID
        src_path: Source path
        config: TransferConfig instance
        progress_callback: Optional callback for progress
        speed_callback: Optional callback for speed
        
    Returns:
        Dictionary with transfer results
    """
    try:
        from smbprotocol.connection import Connection
        from smbprotocol.session import Session as SMBSession
        from smbprotocol.tree import TreeConnect
        from smbprotocol.open import Open, ImpersonationLevel, FilePipePrinterAccessMask
        from smbprotocol.file_info import FileInformationClass
        import uuid
    except ImportError:
        return {
            "success": False,
            "error": "smbprotocol library not installed. Install with: pip install smbprotocol"
        }
    
    config_data = config.config_data or {}
    credentials = get_decrypted_credentials(db, config.id)
    
    host = config_data.get("host")
    share = config_data.get("share")
    path = config_data.get("path", "")
    username = credentials.get("smb_username", "") or ""
    password = credentials.get("smb_password", "") or ""
    domain = credentials.get("smb_domain", "") or ""
    port = config_data.get("port", 445)
    
    if not host or not share:
        return {"success": False, "error": "SMB host and share not configured"}
    
    # Check if this is anonymous/guest access (no username/password)
    is_anonymous = not username and not password
    
    # Calculate source hash
    source_hash = None
    try:
        if src_path.is_file():
            source_hash = calculate_file_hash(src_path)
    except Exception as e:
        log.warning(f"[{job_id}] Could not calculate source hash: {e}")
    
    speed_tracker = SpeedTracker()
    speed_tracker.start()
    
    # Use smbclient for anonymous access (smbprotocol doesn't support it well)
    if is_anonymous:
        return _transfer_smb_anonymous(
            job_id, src_path, host, share, path, port,
            source_hash, speed_tracker, progress_callback, speed_callback,
            conflict_resolution=getattr(config, "conflict_resolution", "overwrite"),
        )
    
    # Use smbprotocol for authenticated access
    try:
        # Connect to SMB share
        connection = Connection(uuid.uuid4(), host, port)
        connection.connect()
        
        session = SMBSession(connection, username, password, domain)
        session.connect()
        
        tree = TreeConnect(session, f"\\\\{host}\\{share}")
        tree.connect()
        
        # Build destination path
        dest_path_str = f"{path}/{src_path.name}" if path else src_path.name
        dest_path_str = dest_path_str.lstrip("/")
        
        # Transfer file or directory
        if src_path.is_file():
            # Open destination file for writing
            file_handle = Open(tree, dest_path_str)
            file_handle.create(
                ImpersonationLevel.Impersonation,
                FilePipePrinterAccessMask.FILE_WRITE_DATA | FilePipePrinterAccessMask.FILE_WRITE_ATTRIBUTES,
                0,
                0,
                FileInformationClass.FILE_NON_DIRECTORY_FILE,
                0
            )
            
            # Read source and write to destination
            chunk_size = 1024 * 1024  # 1MB chunks
            total_size = src_path.stat().st_size
            bytes_written = 0
            
            with open(src_path, "rb") as src_file:
                while True:
                    chunk = src_file.read(chunk_size)
                    if not chunk:
                        break
                    
                    file_handle.write(chunk, offset=bytes_written)
                    bytes_written += len(chunk)
                    
                    # Update progress
                    if progress_callback:
                        progress = int(bytes_written * 100 / total_size)
                        progress_callback(progress)
                    
                    # Update speed
                    speed = speed_tracker.update(bytes_written)
                    if speed_callback:
                        speed_callback(speed)
            
            file_handle.close()
        else:
            # Directory transfer using smbclient (more reliable for recursive transfers)
            tree.disconnect()
            session.disconnect()
            connection.disconnect()
            return _transfer_smb_directory_smbclient(
                job_id, src_path, host, share, path, port, username, password, domain,
                source_hash, speed_tracker, progress_callback, speed_callback,
                conflict_resolution=getattr(config, "conflict_resolution", "overwrite"),
            )
        
        tree.disconnect()
        session.disconnect()
        connection.disconnect()
        
        elapsed_time = speed_tracker.get_elapsed_time()
        avg_speed = speed_tracker.get_average_speed()
        
        return {
            "success": True,
            "dest_path": f"smb://{host}/{share}/{dest_path_str}",
            "source_hash": source_hash,
            "verified": False,  # SMB hash verification would require reading back the file
            "bytes_transferred": bytes_written,
            "duration": elapsed_time,
            "speed_mbps": avg_speed,
        }
    except Exception as e:
        log.error(f"[{job_id}] SMB transfer failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }


def _transfer_smb_anonymous(
    job_id: str,
    src_path: Path,
    host: str,
    share: str,
    path: str,
    port: int,
    source_hash: Optional[str],
    speed_tracker: SpeedTracker,
    progress_callback: Optional[Callable[[int], None]],
    speed_callback: Optional[Callable[[float], None]],
    conflict_resolution: str = "overwrite",
) -> Dict[str, Any]:
    """
    Transfer files via SMB using smbclient command-line tool for anonymous access.
    
    Args:
        job_id: Job ID
        src_path: Source path
        host: SMB host
        share: SMB share name
        path: Optional subdirectory path on share
        port: SMB port (usually 445)
        source_hash: Pre-calculated source file hash
        speed_tracker: Speed tracking instance
        progress_callback: Optional progress callback
        speed_callback: Optional speed callback
        
    Returns:
        Dictionary with transfer results
    """
    if not shutil.which("smbclient"):
        return {
            "success": False,
            "error": "smbclient command not found. Install samba-client package for anonymous SMB access."
        }
    
    try:
        # Build destination path
        dest_path_str = f"{path}/{src_path.name}" if path else src_path.name
        dest_path_str = dest_path_str.lstrip("/").replace("\\", "/")
        
        # Build smbclient command
        # -U% means anonymous/guest access
        # -N means no password prompt
        # -c "put" command to upload file
        smb_url = f"//{host}/{share}"
        if port != 445:
            smb_url = f"//{host}:{port}/{share}"
        
        if src_path.is_file():
            # Transfer single file
            total_size = src_path.stat().st_size
            bytes_transferred = 0
            
            # Use smbclient put command
            # Note: smbclient doesn't provide great progress feedback, so we'll estimate
            cmd = [
                "smbclient",
                smb_url,
                "-U%",  # Anonymous access
                "-N",   # No password
                "-c",   # Command mode
                f"put {src_path} {dest_path_str}"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_smb_put_timeout(total_size)  # #712: scale by file size
            )
            
            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"smbclient failed: {result.stderr.strip() or result.stdout.strip()}"
                }
            
            bytes_transferred = total_size
            if progress_callback:
                progress_callback(100)
            
            elapsed_time = speed_tracker.get_elapsed_time()
            avg_speed = speed_tracker.get_average_speed()
            
            return {
                "success": True,
                "dest_path": f"smb://{host}/{share}/{dest_path_str}",
                "source_hash": source_hash,
                "verified": False,  # Hash verification would require reading back
                "bytes_transferred": bytes_transferred,
                "duration": elapsed_time,
                "speed_mbps": avg_speed,
            }
        else:
            # Directory transfer using smbclient recursive
            return _transfer_smb_directory_smbclient(
                job_id, src_path, host, share, path, port, "", "", "",
                source_hash, speed_tracker, progress_callback, speed_callback,
                conflict_resolution=conflict_resolution,
            )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Transfer timeout"
        }
    except Exception as e:
        log.error(f"[{job_id}] Anonymous SMB transfer failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }


def _smb_delete_remote_file(
    smb_url: str,
    auth_args: list,
    remote_file: str,
    timeout: int = 60,
) -> Tuple[bool, str]:
    """
    Delete a single file at ``remote_file`` on the SMB share via ``smbclient del``.

    Returns ``(True, "")`` on success, ``(False, error_message)`` on failure.
    Used by the reactive overwrite fallback (#635 commit A): when ``smbclient put``
    on an existing file returns ``NT_STATUS_ACCESS_DENIED`` — the common
    write-once share posture — try to delete the destination and retry the put.
    """
    quoted = _smb_quote_path(remote_file.replace("\\", "/"))
    cmd = ["smbclient", smb_url] + auth_args + ["-c", f"del {quoted}"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "smbclient del timed out"
    if result.returncode != 0:
        raw = (result.stderr or "").strip() or (result.stdout or "").strip()
        return False, raw or f"smbclient del exited {result.returncode}"
    # ``smbclient del`` returns 0 even on some silent errors (e.g. permission
    # denied writes to stdout as ``NT_STATUS_*`` without failing the process).
    silent = _extract_nt_status_error(result.stdout or "", result.stderr or "")
    if silent:
        return False, silent
    return True, ""


def _smb_rename_remote_file(
    smb_url: str,
    auth_args: list,
    src_name: str,
    dst_name: str,
    timeout: int = 60,
) -> Tuple[bool, str]:
    """Rename ``src_name`` to ``dst_name`` on the SMB share via
    ``smbclient rename``. Mirrors :func:`_smb_delete_remote_file`.

    Used by the capability probe (#635 commit B) to detect
    ``can_rename`` — some write-once share configurations refuse
    overwrite but permit rename, letting us implement the intent as
    rename-old-then-copy-new.
    """
    quoted_src = _smb_quote_path(src_name.replace("\\", "/"))
    quoted_dst = _smb_quote_path(dst_name.replace("\\", "/"))
    cmd = ["smbclient", smb_url] + auth_args + ["-c", f"rename {quoted_src} {quoted_dst}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "smbclient rename timed out"
    if result.returncode != 0:
        raw = (result.stderr or "").strip() or (result.stdout or "").strip()
        return False, raw or f"smbclient rename exited {result.returncode}"
    silent = _extract_nt_status_error(result.stdout or "", result.stderr or "")
    if silent:
        return False, silent
    return True, ""


def _transfer_smb_directory_smbclient(
    job_id: str,
    src_path: Path,
    host: str,
    share: str,
    path: str,
    port: int,
    username: str,
    password: str,
    domain: str,
    source_hash: Optional[str],
    speed_tracker: SpeedTracker,
    progress_callback: Optional[Callable[[int], None]],
    speed_callback: Optional[Callable[[float], None]],
    conflict_resolution: str = "overwrite",
) -> Dict[str, Any]:
    """
    Transfer directory via SMB using smbclient command-line tool.
    
    Args:
        job_id: Job ID
        src_path: Source directory path
        host: SMB host
        share: SMB share name
        path: Optional subdirectory path on share
        port: SMB port (usually 445)
        username: SMB username (empty for anonymous)
        password: SMB password (empty for anonymous)
        domain: SMB domain (empty for anonymous)
        source_hash: Pre-calculated source hash (not used for directories)
        speed_tracker: Speed tracking instance
        progress_callback: Optional progress callback
        speed_callback: Optional speed callback
        
    Returns:
        Dictionary with transfer results
    """
    if not shutil.which("smbclient"):
        return {
            "success": False,
            "error": "smbclient command not found. Install samba-client package for SMB directory transfer."
        }
    
    try:
        # Calculate total size for progress tracking
        total_bytes = 0
        file_list = []
        for dirpath, _, filenames in os.walk(src_path):
            for fn in filenames:
                file_path = Path(dirpath) / fn
                try:
                    size = file_path.stat().st_size
                    total_bytes += size
                    file_list.append((file_path, dirpath, fn))
                except FileNotFoundError:
                    continue
        
        # Build smbclient command
        smb_url = f"//{host}/{share}"
        if port != 445:
            smb_url = f"//{host}:{port}/{share}"
        
        # Build destination base path.
        # Under remote modes (rsync/smb/nfs), rename writes to
        # ``paths.transient`` as local staging (see
        # core/transfer/path_resolution.resolve_rename_dest_root). When
        # SMB transfer picks up that staging directory the src_path.name
        # is literally "transient" — strip it from the destination so
        # the rip's Movies/Series subdirs land at the configured remote
        # root, not under <remote>/transient/. For any other src_path
        # (e.g. a directory passed in from the use_final_map flow), use
        # its name as the destination base.
        if src_path.name == "transient":
            dest_base = path if path else ""
        else:
            dest_base = f"{path}/{src_path.name}" if path else src_path.name
        dest_base = dest_base.lstrip("/").replace("\\", "/")
        
        # Build authentication arguments
        if username:
            auth_args = [f"-U{username}"]
            if password:
                auth_args[0] += f"%{password}"
            if domain:
                auth_args.insert(0, f"-W{domain}")
        else:
            auth_args = ["-U%", "-N"]
        
        # Track remote directories we have successfully mkdir'd (or confirmed exist).
        remote_dirs_created: set[str] = set()

        def ensure_remote_dir(remote_dir: str) -> tuple[bool, str]:
            """Create remote_dir and all path prefixes; fail on non-benign mkdir errors."""
            if not remote_dir:
                return True, ""
            parts = remote_dir.replace("\\", "/").split("/")
            cumulative = ""
            for part in parts:
                if not part:
                    continue
                cumulative = f"{cumulative}/{part}" if cumulative else part
                if cumulative in remote_dirs_created:
                    continue
                ok, err = _smb_run_mkdir(smb_url, auth_args, cumulative)
                if not ok:
                    log.error(f"[{job_id}] SMB mkdir failed for {cumulative}: {err}")
                    return False, err or f"mkdir failed: {cumulative}"
                remote_dirs_created.add(cumulative)
            return True, ""

        if dest_base:
            ok_base, err_base = ensure_remote_dir(dest_base)
            if not ok_base:
                elapsed_time = speed_tracker.get_elapsed_time()
                avg_speed = speed_tracker.get_average_speed()
                return {
                    "success": False,
                    "error": f"Could not create destination base path {dest_base!r}: {err_base}",
                    "dest_path": f"smb://{host}/{share}/{dest_base}",
                    "source_hash": source_hash,
                    "verified": False,
                    "bytes_transferred": 0,
                    "duration": elapsed_time,
                    "speed_mbps": avg_speed,
                }
        
        # Transfer files recursively (put uses full remote path from share root — no fragile cd;put chain)
        copied_bytes = 0
        failed_files = []
        total_files = len(file_list)
        
        for local_file, dirpath, filename in file_list:
            # Calculate relative path from source
            rel_dir = Path(dirpath).relative_to(src_path)
            # Build remote directory path
            # If dest_base is empty (transient contents to root), use just rel_dir
            # Otherwise, combine dest_base and rel_dir
            if dest_base:
                remote_dir = f"{dest_base}/{rel_dir}" if str(rel_dir) != '.' else dest_base
            else:
                remote_dir = str(rel_dir) if str(rel_dir) != '.' else ""
            remote_dir = remote_dir.replace("\\", "/").rstrip("/")
            
            if remote_dir:
                ok_dir, err_dir = ensure_remote_dir(remote_dir)
                if not ok_dir:
                    failed_files.append((str(local_file), err_dir))
                    continue
            
            # Build remote file path (full path from share root for put/ls)
            remote_file = f"{remote_dir}/{filename}" if remote_dir else filename
            remote_file = remote_file.replace("\\", "/")
            
            local_dir = str(local_file.parent)
            quoted_filename = _smb_quote_path(filename)
            quoted_remote_file = _smb_quote_path(remote_file)
            
            put_command = f"put {quoted_filename} {quoted_remote_file}"
            
            put_cmd = [
                "smbclient",
                smb_url,
            ] + auth_args + [
                "-c",
                put_command,
            ]
            
            file_result = subprocess.run(
                put_cmd,
                cwd=local_dir,
                capture_output=True,
                text=True,
                timeout=_smb_put_timeout(local_file.stat().st_size),  # #712: scale by file size
            )

            put_stdout = file_result.stdout or ""
            put_stderr = file_result.stderr or ""
            put_silent_err = _extract_nt_status_error(put_stdout, put_stderr)
            if file_result.returncode != 0 or put_silent_err:
                raw = put_stderr.strip() or put_stdout.strip()
                if put_silent_err:
                    error_msg = f"{put_silent_err} writing {remote_file}: {raw}".rstrip(": ")
                else:
                    error_msg = raw or f"smbclient put exited {file_result.returncode}"

                # #635 commit A: reactive overwrite fallback.
                #
                # Many production SMB shares (Unraid/Synology write-once
                # configurations) refuse in-place overwrite with
                # ``NT_STATUS_ACCESS_DENIED`` even though ``delete + put`` is
                # allowed. When the user's conflict_resolution intent is
                # ``overwrite``, honour it by trying del then re-issuing the
                # put once. Only ACCESS_DENIED triggers the retry — other
                # NT_STATUS codes (DISK_FULL, INSUFFICIENT_RESOURCES, etc.)
                # bubble through as-is.
                #
                # The proactive capability probe + strategy selector lands in
                # commit B; this in-band retry is the safety net that fixes
                # the immediate Cell 1 failure without waiting for that
                # infrastructure.
                if (
                    conflict_resolution == "overwrite"
                    and put_silent_err == "NT_STATUS_ACCESS_DENIED"
                ):
                    log.info(
                        "[%s] Overwrite denied by share; attempting delete + retry put for %s",
                        job_id, remote_file,
                    )
                    del_ok, del_err = _smb_delete_remote_file(smb_url, auth_args, remote_file)
                    if del_ok:
                        file_result = subprocess.run(
                            put_cmd,
                            cwd=local_dir,
                            capture_output=True,
                            text=True,
                            timeout=_smb_put_timeout(local_file.stat().st_size),  # #712: scale by file size
                        )
                        put_stdout = file_result.stdout or ""
                        put_stderr = file_result.stderr or ""
                        put_silent_err = _extract_nt_status_error(put_stdout, put_stderr)
                        if file_result.returncode == 0 and not put_silent_err:
                            log.info(
                                "[%s] Reactive delete+put succeeded for %s",
                                job_id, remote_file,
                            )
                        else:
                            raw = put_stderr.strip() or put_stdout.strip()
                            if put_silent_err:
                                error_msg = (
                                    f"{put_silent_err} writing {remote_file} after delete+retry: {raw}"
                                ).rstrip(": ")
                            else:
                                error_msg = raw or f"smbclient put exited {file_result.returncode}"
                            log.error(
                                "[%s] Reactive delete+put still failed for %s: %s",
                                job_id, remote_file, error_msg,
                            )
                            failed_files.append((str(local_file), error_msg))
                            continue
                    else:
                        log.error(
                            "[%s] Overwrite fallback: could not delete existing %s: %s",
                            job_id, remote_file, del_err,
                        )
                        failed_files.append(
                            (str(local_file),
                             f"{error_msg} (delete-then-retry fallback failed: {del_err})")
                        )
                        continue
                else:
                    log.error(f"[{job_id}] Failed to transfer {local_file} to {remote_file}: {error_msg}")
                    failed_files.append((str(local_file), error_msg))
                    continue

            verify_command = f"ls {quoted_remote_file}"
            verify_cmd = [
                "smbclient",
                smb_url,
            ] + auth_args + [
                "-c",
                verify_command,
            ]

            verify_result = subprocess.run(verify_cmd, capture_output=True, timeout=10, text=True)

            verify_stdout = verify_result.stdout or ""
            verify_stderr = verify_result.stderr or ""
            verify_silent_err = _extract_nt_status_error(verify_stdout, verify_stderr)
            if verify_result.returncode != 0 or verify_silent_err:
                if verify_silent_err:
                    error_msg = (
                        f"{verify_silent_err} verifying {remote_file}: file transfer reported "
                        f"success but ls failed on remote"
                    )
                else:
                    error_msg = f"File transfer reported success but file not found on remote: {remote_file}"
                log.error(f"[{job_id}] {error_msg}")
                failed_files.append((str(local_file), error_msg))
                continue
            
            file_size = local_file.stat().st_size
            copied_bytes += file_size
            
            if progress_callback and total_bytes > 0:
                progress = int(min(100, (copied_bytes / total_bytes) * 100))
                progress_callback(progress)
            
            speed = speed_tracker.update(copied_bytes)
            if speed_callback:
                speed_callback(speed)
        
        elapsed_time = speed_tracker.get_elapsed_time()
        avg_speed = speed_tracker.get_average_speed()
        
        # If any files failed, report failure
        if failed_files:
            error_summary = f"Failed to transfer {len(failed_files)}/{total_files} files. First error: {failed_files[0][1]}"
            log.error(f"[{job_id}] SMB transfer partially failed: {error_summary}")
            return {
                "success": False,
                "error": error_summary,
                "dest_path": f"smb://{host}/{share}/{dest_base}",
                "source_hash": source_hash,
                "verified": False,
                "bytes_transferred": copied_bytes,
                "duration": elapsed_time,
                "speed_mbps": avg_speed,
                "failed_files": failed_files,
            }
        
        return {
            "success": True,
            "dest_path": f"smb://{host}/{share}/{dest_base}",
            "source_hash": source_hash,
            "verified": False,  # Directory verification would require recursive hash check
            "bytes_transferred": copied_bytes,
            "duration": elapsed_time,
            "speed_mbps": avg_speed,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Transfer timeout"
        }
    except Exception as e:
        log.error(f"[{job_id}] SMB directory transfer failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }

