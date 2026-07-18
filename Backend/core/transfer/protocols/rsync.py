import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable, Tuple, Dict, Any

from core.utils import get_transfer_root, get_mkvauto_root

# Where we persist rsync config + key material
BASE_DIR = get_transfer_root()
CONFIG_PATH = BASE_DIR / "rsync_config.json"
# Store SSH key under MKVAUTO_ROOT/backend/keys for better organization
KEYS_DIR = get_mkvauto_root() / "backend" / "keys"
KEY_PATH = KEYS_DIR / "id_rsa"


@dataclass
class RsyncConfig:
    host: str
    user: str
    path: str
    port: int = 22
    bwlimit: Optional[int] = None


def ensure_dirs() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    KEYS_DIR.mkdir(parents=True, exist_ok=True)


def save_config(cfg: RsyncConfig) -> None:
    ensure_dirs()
    payload = {
        "host": cfg.host,
        "user": cfg.user,
        "path": cfg.path,
        "port": cfg.port,
        "bwlimit": cfg.bwlimit,
    }
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_config() -> tuple[Optional[RsyncConfig], bool]:
    ensure_dirs()
    has_key = KEY_PATH.exists()
    if not CONFIG_PATH.exists():
        return None, has_key
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = RsyncConfig(
        host=data.get("host", ""),
        user=data.get("user", ""),
        path=data.get("path", ""),
        port=int(data.get("port", 22) or 22),
        bwlimit=data.get("bwlimit"),
    )
    return cfg, has_key


def save_key(raw_bytes: bytes) -> None:
    ensure_dirs()
    # Write to a temp file first, then move into place to avoid partial writes
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.chmod(0o600)
    tmp_path.replace(KEY_PATH)


def delete_key() -> None:
    KEY_PATH.unlink(missing_ok=True)


def has_key() -> bool:
    return KEY_PATH.exists()


def validate_connection(cfg: RsyncConfig) -> Tuple[bool, str]:
    """
    Validate SSH connectivity to the target host/path using the stored key.
    """
    if not has_key():
        return False, "SSH key not uploaded"
    ssh_cmd = [
        "ssh",
        "-i",
        str(KEY_PATH),
        "-p",
        str(cfg.port or 22),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        f"{cfg.user}@{cfg.host}",
        f"test -d {shlex.quote(cfg.path)} || mkdir -p {shlex.quote(cfg.path)}",
    ]
    try:
        result = subprocess.run(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
        if result.returncode == 0:
            return True, "Connection succeeded"
        return False, result.stderr.strip() or "SSH validation failed"
    except Exception as exc:
        return False, str(exc)


def _rsync_delete_remote_file(
    config,
    remote_file: str,
    timeout: int = 60,
) -> Tuple[bool, str]:
    """Delete ``remote_file`` on the rsync destination via ``ssh ... rm``.

    ``config`` is either an :class:`RsyncConfig` or an
    ``api.models.TransferConfig`` — the helper reads ``host``, ``user``,
    ``port`` from ``config.config_data`` in the model case, or from
    attributes on the dataclass. Used by the capability probe (#635
    commit B) to detect ``can_delete``.
    """
    config_data = getattr(config, "config_data", None) or {}
    host = config_data.get("host") or getattr(config, "host", None)
    user = config_data.get("user") or getattr(config, "user", None)
    port = int(config_data.get("port", 22) or getattr(config, "port", 22) or 22)
    if not host or not user:
        return False, "rsync host/user not configured"
    if not KEY_PATH.exists():
        return False, "SSH key not uploaded"
    cmd = [
        "ssh",
        "-i", str(KEY_PATH),
        "-p", str(port),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        f"{user}@{host}",
        f"rm -f {shlex.quote(remote_file)}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "ssh rm timed out"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or f"ssh rm exited {result.returncode}").strip()
    return True, ""


def run_rsync(
    src_dir: Path,
    cfg: RsyncConfig,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> subprocess.CompletedProcess:
    """
    Execute rsync with progress reporting. Progress callbacks receive (percent, line).
    """
    if not KEY_PATH.exists():
        raise RuntimeError("SSH key not uploaded")
    if not src_dir.exists():
        raise RuntimeError(f"Source path not found: {src_dir}")

    dest = f"{cfg.user}@{cfg.host}:{cfg.path}"
    ssh_opts = [
        "-i",
        str(KEY_PATH),
        "-p",
        str(cfg.port or 22),
        "-o",
        "StrictHostKeyChecking=no",
    ]
    cmd = [
        "rsync",
        "-av",
        "--partial",
        "--info=progress2",
        "-e",
        " ".join(shlex.quote(p) for p in ["ssh", *ssh_opts]),
        f"{str(src_dir)}/",
        dest,
    ]
    if cfg.bwlimit:
        cmd.insert(2, f"--bwlimit={cfg.bwlimit}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    progress_re = re.compile(r"(\d{1,3})%")
    stdout_lines: list[str] = []
    if proc.stdout:
        for line in proc.stdout:
            line = line.rstrip()
            stdout_lines.append(line)
            m = progress_re.search(line)
            if m and on_progress:
                try:
                    pct = int(m.group(1))
                    on_progress(pct, line)
                except ValueError:
                    pass
            elif on_progress:
                on_progress(-1, line)
    if proc.stdout:
        proc.stdout.close()
    proc.wait()
    # Keep stderr empty string for compatibility; stdout is joined from streamed lines.
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout="\n".join(stdout_lines), stderr="")


def transfer_rsync(
    db,
    job_id: str,
    src_path: Path,
    config,
    progress_callback: Optional[Callable[[int], None]] = None,
    speed_callback: Optional[Callable[[float], None]] = None
) -> Dict[str, Any]:
    """
    Transfer files via rsync using TransferConfig.
    
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
    from core.transfer.utils.credentials import get_decrypted_credentials
    from core.transfer.validation import calculate_file_hash
    from core.transfer.monitoring import SpeedTracker
    import tempfile
    import os
    
    config_data = config.config_data or {}
    credentials = get_decrypted_credentials(db, config.id)
    
    # Create RsyncConfig from TransferConfig
    rsync_cfg = RsyncConfig(
        host=config_data.get("host", ""),
        user=config_data.get("user", ""),
        path=config_data.get("path", ""),
        port=config_data.get("port", 22),
        bwlimit=config_data.get("bwlimit")
    )
    
    if not rsync_cfg.host or not rsync_cfg.user:
        return {"success": False, "error": "Rsync host and user not configured"}
    
    # Get SSH key from credentials
    ssh_key_data = credentials.get("rsync_key", "")
    if not ssh_key_data:
        return {"success": False, "error": "SSH key not configured"}
    
    # Write SSH key to temporary file
    temp_key_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='_rsync_key') as tmp:
            tmp.write(ssh_key_data)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_key_path = Path(tmp.name)
            temp_key_path.chmod(0o600)
        
        # Temporarily replace KEY_PATH
        original_key_exists = KEY_PATH.exists()
        if original_key_exists:
            original_key_backup = KEY_PATH.read_bytes()
        
        # Write temporary key
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        KEY_PATH.write_bytes(ssh_key_data.encode() if isinstance(ssh_key_data, str) else ssh_key_data)
        KEY_PATH.chmod(0o600)
        
        # Calculate source hash
        source_hash = None
        try:
            if src_path.is_file():
                source_hash = calculate_file_hash(src_path)
        except Exception as e:
            log.warning(f"[{job_id}] Could not calculate source hash: {e}")
        
        speed_tracker = SpeedTracker()
        speed_tracker.start()
        
        # Transfer with progress
        def combined_progress_cb(pct: int, line: str):
            if progress_callback:
                progress_callback(pct)
            
            # Calculate speed (rough estimate)
            if src_path.is_file() and pct > 0:
                bytes_transferred = int(src_path.stat().st_size * pct / 100)
                speed = speed_tracker.update(bytes_transferred)
                if speed_callback:
                    speed_callback(speed)
        
        result = run_rsync(src_path, rsync_cfg, on_progress=combined_progress_cb)
        
        # Restore original key if it existed
        if original_key_exists:
            KEY_PATH.write_bytes(original_key_backup)
        elif KEY_PATH.exists():
            KEY_PATH.unlink()
        
        elapsed_time = speed_tracker.get_elapsed_time()
        avg_speed = speed_tracker.get_average_speed()
        
        if result.returncode == 0:
            dest_path_str = f"{rsync_cfg.user}@{rsync_cfg.host}:{rsync_cfg.path}/{src_path.name}"
            return {
                "success": True,
                "dest_path": dest_path_str,
                "source_hash": source_hash,
                "verified": False,  # Rsync hash verification would require reading back the file
                "bytes_transferred": src_path.stat().st_size if src_path.is_file() else 0,
                "duration": elapsed_time,
                "speed_mbps": avg_speed,
            }
        else:
            return {
                "success": False,
                "error": result.stdout or "Rsync transfer failed",
            }
    except Exception as e:
        log.error(f"[{job_id}] Rsync transfer failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }
    finally:
        # Clean up temporary key file
        if temp_key_path and temp_key_path.exists():
            try:
                temp_key_path.unlink()
            except Exception:
                pass
