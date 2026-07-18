"""
NFS transfer implementation using libnfs-python or mount-based approach.
"""
from pathlib import Path
from typing import Optional, Callable, Dict, Any, Tuple
from sqlalchemy.orm import Session
from api import models
from core.transfer.validation import calculate_file_hash
from core.transfer.monitoring import SpeedTracker
from core.transfer.utils.credentials import get_decrypted_credentials
import logging
import tempfile
import shutil

log = logging.getLogger(__name__)


def _nfs_delete_remote_file(
    config: models.TransferConfig,
    remote_file: str,
    timeout: int = 60,
) -> Tuple[bool, str]:
    """Delete ``remote_file`` on the NFS export.

    Uses ``libnfs`` when available (unlink on the connection); otherwise
    the file is expected to be a mounted path and this falls through to
    :meth:`Path.unlink`. Used by the capability probe (#635 commit B)
    to detect ``can_delete``.
    """
    config_data = config.config_data or {}
    server = config_data.get("server")
    export_path = config_data.get("export_path")
    try:
        import libnfs
    except ImportError:
        try:
            p = Path(remote_file)
            p.unlink(missing_ok=True)
            return True, ""
        except Exception as exc:
            return False, str(exc)
    if not server or not export_path:
        return False, "NFS server/export_path not configured"
    try:
        nfs = libnfs.NFS(f"nfs://{server}{export_path}")
        try:
            nfs.unlink(remote_file.lstrip("/"))
        finally:
            try:
                nfs.close()
            except Exception:
                pass
        return True, ""
    except Exception as exc:
        return False, str(exc)


def validate_connection(config: models.TransferConfig, db: Optional[Session] = None) -> Tuple[bool, str]:
    """
    Validate NFS connection.
    
    Note: NFS supports anonymous access by default (no credentials required).
    The server must allow anonymous access for this to work.
    
    Args:
        config: TransferConfig instance
        db: Optional database session for retrieving credentials
        
    Returns:
        Tuple of (success, message)
    """
    config_data = config.config_data or {}
    server = config_data.get("server")
    export_path = config_data.get("export_path")
    
    if not server or not export_path:
        return False, "NFS server and export_path not configured"
    
    # Get NFS options from credentials if available
    nfs_options = ""
    if db:
        try:
            credentials = get_decrypted_credentials(db, config.id)
            nfs_options = credentials.get("nfs_options", "")
        except Exception as e:
            log.warning(f"Failed to retrieve NFS options: {e}")
    
    # Try to use libnfs-python first
    try:
        import libnfs
        nfs = libnfs.NFS(f"nfs://{server}{export_path}")
        nfs.close()
        return True, "Connection successful"
    except ImportError:
        # Fall back to mount test using root helper
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                mount_point = Path(tmpdir) / "nfs_test"
                mount_point.mkdir()
                
                # Use root helper to mount (same as storage detection)
                from core.utils import _root_helper_mount_nfs, _root_helper_unmount
                
                actual_mount_point, mount_error = _root_helper_mount_nfs(
                    server, export_path, str(mount_point), nfs_options
                )
                
                if mount_error:
                    return False, f"Mount failed: {mount_error}"
                
                # Unmount using root helper
                check_point = Path(actual_mount_point) if actual_mount_point else mount_point
                _root_helper_unmount(str(check_point))
                
                return True, "Connection successful"
        except Exception as e:
            return False, f"NFS connection failed: {str(e)}"


def transfer_nfs(
    db: Session,
    job_id: str,
    src_path: Path,
    config: models.TransferConfig,
    progress_callback: Optional[Callable[[int], None]] = None,
    speed_callback: Optional[Callable[[float], None]] = None
) -> Dict[str, Any]:
    """
    Transfer files via NFS.
    
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
    config_data = config.config_data or {}
    server = config_data.get("server")
    export_path = config_data.get("export_path")
    path = config_data.get("path", "")
    
    if not server or not export_path:
        return {"success": False, "error": "NFS server and export_path not configured"}
    
    # Calculate source hash
    source_hash = None
    try:
        if src_path.is_file():
            source_hash = calculate_file_hash(src_path)
    except Exception as e:
        log.warning(f"[{job_id}] Could not calculate source hash: {e}")
    
    speed_tracker = SpeedTracker()
    speed_tracker.start()
    
    # Try libnfs-python first
    try:
        import libnfs
        nfs = libnfs.NFS(f"nfs://{server}{export_path}")
        
        # Build destination path
        dest_path_str = f"{path}/{src_path.name}" if path else src_path.name
        dest_path_str = dest_path_str.lstrip("/")
        
        # Transfer file
        if src_path.is_file():
            total_size = src_path.stat().st_size
            bytes_written = 0
            
            with open(src_path, "rb") as src_file:
                with nfs.open(dest_path_str, "w") as dest_file:
                    chunk_size = 1024 * 1024  # 1MB chunks
                    
                    while True:
                        chunk = src_file.read(chunk_size)
                        if not chunk:
                            break
                        
                        dest_file.write(chunk)
                        bytes_written += len(chunk)
                        
                        # Update progress
                        if progress_callback:
                            progress = int(bytes_written * 100 / total_size)
                            progress_callback(progress)
                        
                        # Update speed
                        speed = speed_tracker.update(bytes_written)
                        if speed_callback:
                            speed_callback(speed)
        else:
            # Directory transfer with per-file progress
            # Enumerate files first
            files = list(src_path.rglob("*.mkv"))
            total_files = len(files)
            
            if total_files == 0:
                # No MKV files, just copy tree
                shutil.copytree(src_path, Path(nfs.getcwd()) / dest_path_str, dirs_exist_ok=True)
                if progress_callback:
                    progress_callback(100)
            else:
                # Copy files individually with progress
                dest_base = Path(nfs.getcwd()) / dest_path_str
                for idx, file_path in enumerate(files):
                    rel_path = file_path.relative_to(src_path)
                    dest_file = dest_base / rel_path
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Copy file
                    total_size = file_path.stat().st_size
                    bytes_written = 0
                    
                    with open(file_path, "rb") as src_file:
                        with nfs.open(str(dest_file.relative_to(dest_base)), "w") as dest_file_handle:
                            chunk_size = 1024 * 1024  # 1MB chunks
                            
                            while True:
                                chunk = src_file.read(chunk_size)
                                if not chunk:
                                    break
                                
                                dest_file_handle.write(chunk)
                                bytes_written += len(chunk)
                                
                                # Update progress for this file
                                if progress_callback:
                                    # Overall progress: completed files + current file progress
                                    file_progress = (bytes_written * 100) / total_size
                                    overall = int((idx * 100 + file_progress) / total_files)
                                    progress_callback(overall)
                                
                                # Update speed
                                speed = speed_tracker.update(bytes_written)
                                if speed_callback:
                                    speed_callback(speed)
                
                # Copy non-MKV files/directories
                for item in src_path.rglob("*"):
                    if item.is_file() and not item.suffix == ".mkv":
                        rel_path = item.relative_to(src_path)
                        dest_item = dest_base / rel_path
                        dest_item.parent.mkdir(parents=True, exist_ok=True)
                        if not dest_item.exists():
                            with open(item, "rb") as src_file:
                                with nfs.open(str(dest_item.relative_to(dest_base)), "w") as dest_file_handle:
                                    shutil.copyfileobj(src_file, dest_file_handle)
                
                if progress_callback:
                    progress_callback(100)
        
        nfs.close()
        
        elapsed_time = speed_tracker.get_elapsed_time()
        avg_speed = speed_tracker.get_average_speed()
        
        return {
            "success": True,
            "dest_path": f"nfs://{server}{export_path}/{dest_path_str}",
            "source_hash": source_hash,
            "verified": False,  # NFS hash verification would require reading back the file
            "bytes_transferred": bytes_written if src_path.is_file() else 0,
            "duration": elapsed_time,
            "speed_mbps": avg_speed,
        }
    except ImportError:
        # Fall back to mount-based approach
        return _transfer_nfs_mount(db, job_id, src_path, config, progress_callback, speed_callback, source_hash)
    except Exception as e:
        log.error(f"[{job_id}] NFS transfer failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }


def _transfer_nfs_mount(
    db: Session,
    job_id: str,
    src_path: Path,
    config: models.TransferConfig,
    progress_callback: Optional[Callable[[int], None]],
    speed_callback: Optional[Callable[[float], None]],
    source_hash: Optional[str]
) -> Dict[str, Any]:
    """Transfer using system mount (fallback)."""
    config_data = config.config_data or {}
    server = config_data.get("server")
    export_path = config_data.get("export_path")
    path = config_data.get("path", "")
    
    # Get NFS options from credentials
    nfs_options = ""
    try:
        credentials = get_decrypted_credentials(db, config.id)
        nfs_options = credentials.get("nfs_options", "")
    except Exception as e:
        log.warning(f"Failed to retrieve NFS options: {e}")
    
    mount_point = None
    try:
        # Create temporary mount point
        mount_point = Path(tempfile.mkdtemp())
        
        # Use root helper to mount NFS share
        from core.utils import _root_helper_mount_nfs, _root_helper_unmount
        
        actual_mount_point, mount_error = _root_helper_mount_nfs(
            server, export_path, str(mount_point), nfs_options
        )
        
        if mount_error:
            return {"success": False, "error": f"Mount failed: {mount_error}"}
        
        # Use the mount point returned by root helper
        check_point = Path(actual_mount_point) if actual_mount_point else mount_point
        
        # Transfer files
        dest_path = check_point / path / src_path.name if path else check_point / src_path.name
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        if src_path.is_file():
            shutil.copy2(src_path, dest_path)
            if progress_callback:
                progress_callback(100)
        else:
            # Directory transfer with per-file progress
            # Enumerate files first
            files = list(src_path.rglob("*.mkv"))
            total_files = len(files)
            
            if total_files == 0:
                # No MKV files, just copy tree
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
                if progress_callback:
                    progress_callback(100)
            else:
                # Copy files individually with progress
                for idx, file_path in enumerate(files):
                    rel_path = file_path.relative_to(src_path)
                    dest_file = dest_path / rel_path
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Copy file
                    shutil.copy2(file_path, dest_file)
                    
                    # Update progress
                    if progress_callback:
                        file_pct = int((idx + 1) * 100 / total_files)
                        progress_callback(file_pct)
                
                # Copy non-MKV files/directories
                for item in src_path.rglob("*"):
                    if item.is_file() and not item.suffix == ".mkv":
                        rel_path = item.relative_to(src_path)
                        dest_item = dest_path / rel_path
                        dest_item.parent.mkdir(parents=True, exist_ok=True)
                        if not dest_item.exists():
                            shutil.copy2(item, dest_item)
                
                if progress_callback:
                    progress_callback(100)
        
        bytes_transferred = src_path.stat().st_size if src_path.is_file() else 0
        
        return {
            "success": True,
            "dest_path": str(dest_path),
            "source_hash": source_hash,
            "verified": False,
            "bytes_transferred": bytes_transferred,
            "duration": 0,  # Not tracked in mount approach
            "speed_mbps": 0,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        # Unmount using root helper
        if 'check_point' in locals() and check_point:
            try:
                from core.utils import _root_helper_unmount
                _root_helper_unmount(str(check_point))
                if mount_point and mount_point.exists():
                    mount_point.rmdir()
            except Exception as e:
                log.warning(f"Failed to unmount NFS share: {e}")

