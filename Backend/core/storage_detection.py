"""
Storage detection for remote transfer destinations (SMB, NFS, rsync).
Uses hybrid approach: protocol-level queries first, fallback to mounting.
"""
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from sqlalchemy.orm import Session
from api import models
import logging
import shutil
import subprocess
import tempfile
import os
import re
import shlex

log = logging.getLogger(__name__)


def get_storage_info(
    db: Session,
    config: models.TransferConfig
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Get storage information for a transfer config.
    
    Args:
        db: Database session
        config: TransferConfig instance
        
    Returns:
        Tuple of (storage_info_dict, error_message)
        storage_info_dict contains: path, total, used, free (all in bytes)
    """
    if config.mode == "local":
        return get_local_storage_info(config)
    elif config.mode == "smb":
        return get_smb_storage_info(db, config)
    elif config.mode == "nfs":
        return get_nfs_storage_info(config, db)
    elif config.mode == "rsync":
        return get_rsync_storage_info(db, config)
    else:
        return None, f"Unknown transfer mode: {config.mode}"


def get_local_storage_info(config: models.TransferConfig) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Get storage info for local transfer destination.
    
    Args:
        config: TransferConfig instance
        
    Returns:
        Tuple of (storage_info_dict, error_message)
    """
    transfer_dir = config.transfer_dir
    if not transfer_dir:
        return None, "Transfer directory not configured"
    
    try:
        dest_path = Path(transfer_dir).expanduser().resolve()
        
        # Find existing parent if path doesn't exist
        target = dest_path
        while not target.exists() and target != target.parent:
            target = target.parent
        if not target.exists():
            target = Path("/")
        
        usage = shutil.disk_usage(target)
        return {
            "path": str(target),
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
        }, None
    except Exception as e:
        log.error(f"Error getting local storage info: {e}")
        return None, str(e)


def get_smb_storage_info(
    db: Session,
    config: models.TransferConfig
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Get storage info for SMB share using hybrid approach.
    
    Args:
        db: Database session
        config: TransferConfig instance
        
    Returns:
        Tuple of (storage_info_dict, error_message)
    """
    config_data = config.config_data or {}
    from core.transfer.utils.credentials import get_decrypted_credentials
    
    credentials = get_decrypted_credentials(db, config.id)
    host = config_data.get("host")
    share = config_data.get("share")
    username = credentials.get("smb_username", "") or ""
    password = credentials.get("smb_password", "") or ""
    domain = credentials.get("smb_domain", "") or ""
    port = config_data.get("port", 445)
    
    if not host or not share:
        return None, "SMB host and share not configured"
    
    # Try protocol-level query first (smbprotocol)
    # Note: smbprotocol filesystem info query API may vary, so we'll try it but fall back to mount
    try:
        from smbprotocol.connection import Connection
        from smbprotocol.session import Session as SMBSession
        from smbprotocol.tree import TreeConnect
        from smbprotocol.file_info import FileInformationClass
        import uuid
        
        is_anonymous = not username and not password
        
        if not is_anonymous:
            # Try smbprotocol query
            connection = Connection(uuid.uuid4(), host, port)
            connection.connect()
            
            session = SMBSession(connection, username, password, domain)
            session.connect()
            
            tree = TreeConnect(session, f"\\\\{host}\\{share}")
            tree.connect()
            
            try:
                # Try to query filesystem information
                # The exact API may vary, so we catch all exceptions and fall back
                fs_info = tree.query_info(FileInformationClass.FILE_FS_FULL_SIZE_INFORMATION)
                
                # Extract size information if available
                if hasattr(fs_info, 'total_allocation_units') and hasattr(fs_info, 'sectors_per_allocation_unit'):
                    total_bytes = fs_info.total_allocation_units * fs_info.sectors_per_allocation_unit * fs_info.bytes_per_sector
                    free_bytes = fs_info.actual_available_allocation_units * fs_info.sectors_per_allocation_unit * fs_info.bytes_per_sector
                    used_bytes = total_bytes - free_bytes
                    
                    tree.disconnect()
                    session.disconnect()
                    connection.disconnect()
                    
                    log.info(f"SMB storage info retrieved via protocol query: {free_bytes / (1024**3):.2f} GB free")
                    return {
                        "path": f"smb://{host}/{share}",
                        "total": total_bytes,
                        "used": used_bytes,
                        "free": free_bytes,
                    }, None
                else:
                    raise AttributeError("Filesystem info structure not as expected")
            except (AttributeError, Exception) as e:
                log.debug(f"SMB protocol query failed: {e}, falling back to mount")
                try:
                    tree.disconnect()
                    session.disconnect()
                    connection.disconnect()
                except Exception:
                    pass
    except ImportError:
        log.debug("smbprotocol not available, using mount fallback")
    except Exception as e:
        log.debug(f"SMB protocol query setup failed: {e}, falling back to mount")
    
    # Fallback: Mount and check
    return _get_smb_storage_via_mount(host, share, port, username, password, domain)


def _get_smb_storage_via_mount(
    host: str,
    share: str,
    port: int,
    username: str,
    password: str,
    domain: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Get SMB storage info by temporarily mounting the share using root helper.
    
    Args:
        host: SMB host
        share: Share name
        port: SMB port
        username: Username (empty for anonymous)
        password: Password (empty for anonymous)
        domain: Domain (optional)
        
    Returns:
        Tuple of (storage_info_dict, error_message)
    """
    mount_point = None
    try:
        mount_point = Path(tempfile.mkdtemp(prefix="smb_storage_check_"))
        
        # Use root helper to mount
        from core.utils import _root_helper_mount_smb, _root_helper_unmount
        
        actual_mount_point, mount_error = _root_helper_mount_smb(
            host, share, str(mount_point), port, username, password, domain
        )
        
        if mount_error:
            return None, f"Failed to mount SMB share via root helper: {mount_error}"
        
        # Use the mount point returned by root helper (or the one we created)
        check_point = Path(actual_mount_point) if actual_mount_point else mount_point
        
        try:
            # Get disk usage
            usage = shutil.disk_usage(check_point)
            
            return {
                "path": f"smb://{host}/{share}",
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
            }, None
        finally:
            # Unmount using root helper
            _root_helper_unmount(str(check_point))
    except Exception as e:
        log.error(f"Error getting SMB storage via mount: {e}")
        return None, str(e)
    finally:
        # Clean up mount point directory
        if mount_point and mount_point.exists():
            try:
                mount_point.rmdir()
            except Exception:
                pass


def get_nfs_storage_info(
    config: models.TransferConfig,
    db: Optional[Session] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Get storage info for NFS share using hybrid approach.
    
    Args:
        config: TransferConfig instance
        db: Optional database session for retrieving credentials
        
    Returns:
        Tuple of (storage_info_dict, error_message)
    """
    config_data = config.config_data or {}
    server = config_data.get("server")
    export_path = config_data.get("export_path")
    
    if not server or not export_path:
        return None, "NFS server and export_path not configured"
    
    # Get NFS options from credentials if available
    nfs_options = ""
    if db:
        try:
            from core.transfer.utils.credentials import get_decrypted_credentials
            credentials = get_decrypted_credentials(db, config.id)
            nfs_options = credentials.get("nfs_options", "")
        except Exception as e:
            log.warning(f"Failed to retrieve NFS options: {e}")
    
    # Try libnfs-python first
    try:
        import libnfs
        
        nfs = libnfs.NFS(f"nfs://{server}{export_path}")
        
        try:
            # Try to get filesystem info via statvfs
            # libnfs may not directly expose this, so we'll try mount fallback
            # For now, we'll go straight to mount fallback as libnfs statvfs support is unclear
            nfs.close()
        except Exception:
            nfs.close()
            raise
    except ImportError:
        log.debug("libnfs-python not available, using mount fallback")
    except Exception as e:
        log.warning(f"libnfs query failed: {e}, falling back to mount")
    
    # Fallback: Mount and check
    return _get_nfs_storage_via_mount(server, export_path, nfs_options)


def _get_nfs_storage_via_mount(
    server: str,
    export_path: str,
    options: str = ""
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Get NFS storage info by temporarily mounting the share using root helper.
    
    Args:
        server: NFS server
        export_path: Export path
        options: NFS mount options (e.g., "nfsvers=3,anonuid=65534,anongid=65534")
        
    Returns:
        Tuple of (storage_info_dict, error_message)
    """
    mount_point = None
    try:
        mount_point = Path(tempfile.mkdtemp(prefix="nfs_storage_check_"))
        
        # Use root helper to mount
        from core.utils import _root_helper_mount_nfs, _root_helper_unmount
        
        actual_mount_point, mount_error = _root_helper_mount_nfs(
            server, export_path, str(mount_point), options
        )
        
        if mount_error:
            return None, f"Failed to mount NFS share via root helper: {mount_error}"
        
        # Use the mount point returned by root helper (or the one we created)
        check_point = Path(actual_mount_point) if actual_mount_point else mount_point
        
        try:
            # Get disk usage
            usage = shutil.disk_usage(check_point)
            
            return {
                "path": f"nfs://{server}{export_path}",
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
            }, None
        finally:
            # Unmount using root helper
            _root_helper_unmount(str(check_point))
    except Exception as e:
        log.error(f"Error getting NFS storage via mount: {e}")
        return None, str(e)
    finally:
        # Clean up mount point directory
        if mount_point and mount_point.exists():
            try:
                mount_point.rmdir()
            except Exception:
                pass


def get_rsync_storage_info(
    db: Session,
    config: models.TransferConfig
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Get storage info for rsync destination via SSH df command.
    
    Args:
        db: Database session
        config: TransferConfig instance
        
    Returns:
        Tuple of (storage_info_dict, error_message)
    """
    config_data = config.config_data or {}
    from core.transfer.utils.credentials import get_decrypted_credentials
    from core.transfer.protocols.rsync import KEY_PATH
    
    credentials = get_decrypted_credentials(db, config.id)
    host = config_data.get("host", "")
    user = config_data.get("user", "")
    path = config_data.get("path", "")
    port = config_data.get("port", 22)
    
    if not host or not user or not path:
        return None, "Rsync host, user, and path not configured"
    
    # Get SSH key from credentials
    ssh_key_data = credentials.get("rsync_key", "")
    if not ssh_key_data:
        # Try to use existing key file if available
        if not KEY_PATH.exists():
            return None, "SSH key not configured"
        ssh_key_path = KEY_PATH
    else:
        # Write temporary key file
        temp_key_path = None
        try:
            temp_key_path = Path(tempfile.mkdtemp(prefix="rsync_key_")) / "id_rsa"
            temp_key_path.parent.mkdir(parents=True, exist_ok=True)
            temp_key_path.write_bytes(ssh_key_data.encode() if isinstance(ssh_key_data, str) else ssh_key_data)
            temp_key_path.chmod(0o600)
            ssh_key_path = temp_key_path
        except Exception as e:
            return None, f"Failed to create temporary SSH key: {e}"
    
    try:
        # Execute df command via SSH
        ssh_cmd = [
            "ssh",
            "-i",
            str(ssh_key_path),
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            f"{user}@{host}",
            f"df -B1 {shlex.quote(path)}"  # -B1 gives output in 1-byte blocks
        ]
        
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=20
        )
        
        if result.returncode != 0:
            return None, f"SSH df command failed: {result.stderr.strip()}"
        
        # Parse df output
        # Format: Filesystem     1B-blocks      Used Available Use% Mounted on
        # Example: /dev/sda1     10737418240  5368709120  5368709120  50% /data
        lines = result.stdout.strip().split('\n')
        if len(lines) < 2:
            return None, "Unexpected df output format"
        
        # Parse the data line (second line)
        parts = lines[1].split()
        if len(parts) < 4:
            return None, "Unexpected df output format"
        
        try:
            total_bytes = int(parts[1])
            used_bytes = int(parts[2])
            available_bytes = int(parts[3])
            
            return {
                "path": f"{user}@{host}:{path}",
                "total": total_bytes,
                "used": used_bytes,
                "free": available_bytes,
            }, None
        except (ValueError, IndexError) as e:
            return None, f"Failed to parse df output: {e}"
    except subprocess.TimeoutExpired:
        return None, "SSH command timed out"
    except Exception as e:
        log.error(f"Error getting rsync storage info: {e}")
        return None, str(e)
    finally:
        # Clean up temporary key if we created one
        if ssh_key_path != KEY_PATH and ssh_key_path.exists():
            try:
                ssh_key_path.unlink()
                ssh_key_path.parent.rmdir()
            except Exception:
                pass

