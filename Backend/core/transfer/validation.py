"""
Pre-transfer validation and hash verification system.
Combines transfer_validation.py and transfer_verification.py.
"""
import os
import shutil
import hashlib
from pathlib import Path
from typing import Tuple, List, Dict, Any, Optional, Callable
from sqlalchemy.orm import Session
import logging

log = logging.getLogger(__name__)


# ===== Validation Functions (from transfer_validation.py) =====

def validate_transfer(db: Session, job_id: str, config, source_size: int) -> Tuple[bool, List[str]]:
    """
    Run all validation checks for a transfer.
    
    Args:
        db: Database session
        job_id: Job ID
        config: TransferConfig instance
        source_size: Size of source files in bytes
        
    Returns:
        Tuple of (passed, errors)
    """
    errors = []
    
    # Run mode-specific validations
    if config.mode == "local":
        passed, error = validate_local_transfer(config, source_size)
        if not passed:
            errors.append(error)
    elif config.mode == "rsync":
        passed, error = validate_rsync_transfer(db, config, source_size)
        if not passed:
            errors.append(error)
    elif config.mode == "smb":
        passed, error = validate_smb_transfer(db, config, source_size)
        if not passed:
            errors.append(error)
    elif config.mode == "nfs":
        passed, error = validate_nfs_transfer(db, config, source_size)
        if not passed:
            errors.append(error)
    else:
        errors.append(f"Unknown transfer mode: {config.mode}")
    
    return len(errors) == 0, errors


def validate_local_transfer(config, source_size: int) -> Tuple[bool, str]:
    """
    Validate local transfer preconditions.
    
    Args:
        config: TransferConfig instance
        source_size: Size of source files in bytes
        
    Returns:
        Tuple of (passed, error_message)
    """
    transfer_dir = config.transfer_dir
    if not transfer_dir:
        return False, "Transfer directory not configured"
    
    dest_path = Path(transfer_dir)
    
    # Check if path exists or can be created
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"Cannot create destination path: {str(e)}"
    
    # Check write permissions
    if not os.access(dest_path, os.W_OK):
        return False, f"No write permission for destination path: {dest_path}"
    
    # Check available space
    try:
        from core.utils import has_enough_space
        if not has_enough_space(str(dest_path), source_size):
            needed_gb = source_size / (1024 ** 3)
            return False, f"Insufficient disk space. Need at least {needed_gb:.2f} GB"
    except Exception as e:
        log.warning(f"Could not check disk space: {e}")
        # Don't fail validation if we can't check space
    
    return True, ""


def validate_rsync_transfer(db: Session, config, source_size: int) -> Tuple[bool, str]:
    """
    Validate rsync transfer preconditions.
    
    Args:
        db: Database session
        config: TransferConfig instance
        source_size: Size of source files in bytes
        
    Returns:
        Tuple of (passed, error_message)
    """
    # Check if config data is available
    config_data = config.config_data or {}
    if not config_data.get("host") or not config_data.get("user"):
        return False, "Rsync host and user not configured"
    
    # Validate connection
    from core.transfer.protocols.rsync import validate_connection, RsyncConfig
    rsync_cfg = RsyncConfig(
        host=config_data.get("host", ""),
        user=config_data.get("user", ""),
        path=config_data.get("path", ""),
        port=config_data.get("port", 22),
        bwlimit=config_data.get("bwlimit")
    )
    passed, error = validate_connection(rsync_cfg)
    if not passed:
        return False, error
    
    # Check available storage
    from core.storage_detection import get_rsync_storage_info
    storage_info, storage_error = get_rsync_storage_info(db, config)
    if storage_error:
        log.warning(f"Could not check rsync storage: {storage_error}")
        # Don't fail validation if we can't check storage, but log it
    elif storage_info:
        free_bytes = storage_info.get("free", 0)
        if free_bytes < source_size:
            needed_gb = source_size / (1024 ** 3)
            available_gb = free_bytes / (1024 ** 3)
            return False, f"Insufficient disk space. Need at least {needed_gb:.2f} GB, but only {available_gb:.2f} GB available"
    
    return True, ""


def validate_smb_transfer(db: Session, config, source_size: int) -> Tuple[bool, str]:
    """
    Validate SMB transfer preconditions.
    
    Args:
        db: Database session
        config: TransferConfig instance
        source_size: Size of source files in bytes
        
    Returns:
        Tuple of (passed, error_message)
    """
    # Check basic config
    config_data = config.config_data or {}
    if not config_data.get("host") or not config_data.get("share"):
        return False, "SMB host and share not configured"
    
    # Validate connection
    from core.transfer.protocols.smb import validate_connection
    passed, error = validate_connection(db, config)
    if not passed:
        return False, error
    
    # Check available storage
    from core.storage_detection import get_smb_storage_info
    storage_info, storage_error = get_smb_storage_info(db, config)
    if storage_error:
        log.warning(f"Could not check SMB storage: {storage_error}")
        # Don't fail validation if we can't check storage, but log it
    elif storage_info:
        free_bytes = storage_info.get("free", 0)
        if free_bytes < source_size:
            needed_gb = source_size / (1024 ** 3)
            available_gb = free_bytes / (1024 ** 3)
            return False, f"Insufficient disk space. Need at least {needed_gb:.2f} GB, but only {available_gb:.2f} GB available"
    
    return True, ""


def validate_nfs_transfer(db: Session, config, source_size: int) -> Tuple[bool, str]:
    """
    Validate NFS transfer preconditions.
    
    Args:
        db: Database session
        config: TransferConfig instance
        source_size: Size of source files in bytes
        
    Returns:
        Tuple of (passed, error_message)
    """
    # Check basic config
    config_data = config.config_data or {}
    if not config_data.get("server") or not config_data.get("export_path"):
        return False, "NFS server and export_path not configured"
    
    # Validate connection
    from core.transfer.protocols.nfs import validate_connection
    passed, error = validate_connection(config)
    if not passed:
        return False, error
    
    # Check available storage
    from core.storage_detection import get_nfs_storage_info
    storage_info, storage_error = get_nfs_storage_info(config, db)
    if storage_error:
        log.warning(f"Could not check NFS storage: {storage_error}")
        # Don't fail validation if we can't check storage, but log it
    elif storage_info:
        free_bytes = storage_info.get("free", 0)
        if free_bytes < source_size:
            needed_gb = source_size / (1024 ** 3)
            available_gb = free_bytes / (1024 ** 3)
            return False, f"Insufficient disk space. Need at least {needed_gb:.2f} GB, but only {available_gb:.2f} GB available"
    
    return True, ""


def validate_connectivity(db: Session, config) -> Tuple[bool, str]:
    """Test connection to destination."""
    if config.mode == "local":
        return True, ""
    elif config.mode == "rsync":
        from core.transfer.protocols.rsync import validate_connection as validate_rsync
        # Create temporary RsyncConfig from TransferConfig
        config_data = config.config_data or {}
        from core.transfer.protocols.rsync import RsyncConfig
        rsync_cfg = RsyncConfig(
            host=config_data.get("host", ""),
            user=config_data.get("user", ""),
            path=config_data.get("path", ""),
            port=config_data.get("port", 22),
            bwlimit=config_data.get("bwlimit")
        )
        return validate_rsync(rsync_cfg)
    elif config.mode == "smb":
        from core.transfer.protocols.smb import validate_connection as validate_smb
        return validate_smb(db, config)
    elif config.mode == "nfs":
        from core.transfer.protocols.nfs import validate_connection as validate_nfs
        return validate_nfs(config, db)
    else:
        return False, f"Unknown transfer mode: {config.mode}"


def validate_authentication(db: Session, config) -> Tuple[bool, str]:
    """Test authentication credentials."""
    if config.mode == "local":
        return True, ""
    elif config.mode == "rsync":
        # Rsync authentication is tested during connection validation
        return validate_connectivity(db, config)
    elif config.mode == "smb":
        # SMB authentication is tested during connection validation
        return validate_connectivity(db, config)
    elif config.mode == "nfs":
        # NFS authentication is tested during connection validation
        return validate_connectivity(db, config)
    else:
        return False, f"Unknown transfer mode: {config.mode}"


def validate_permissions(config, dest_path: str) -> Tuple[bool, str]:
    """Test write permissions."""
    if config.mode == "local":
        path = Path(dest_path)
        if not os.access(path, os.W_OK):
            return False, f"No write permission for {dest_path}"
        return True, ""
    else:
        # Remote permissions will be tested during connection validation
        return True, ""


def validate_space(config, dest_path: str, required_bytes: int) -> Tuple[bool, str]:
    """Check available space."""
    if config.mode == "local":
        return validate_local_transfer(config, required_bytes)
    else:
        # Remote space checks are now implemented in validate_transfer
        # This function is kept for backward compatibility
        from sqlalchemy.orm import Session
        from api.database import get_db
        # Note: This function signature doesn't include db, so we can't check remote storage here
        # The actual checks happen in validate_transfer which has access to db
        return True, ""


def validate_path(config, dest_path: str) -> Tuple[bool, str]:
    """Check/create destination path."""
    if config.mode == "local":
        path = Path(dest_path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            return True, ""
        except Exception as e:
            return False, f"Cannot create path: {str(e)}"
    else:
        # Remote path creation will be tested during connection validation
        return True, ""


# ===== Verification Functions (from transfer_verification.py) =====

def calculate_file_hash(
    file_path: Path,
    algorithm: str = "sha256",
    chunk_size: int = 1024 * 1024,
    progress_cb: Optional[Callable[[int, int, str], None]] = None
) -> str:
    """
    Calculate hash of a file with optional progress reporting.
    
    Args:
        file_path: Path to file
        algorithm: Hash algorithm (default: sha256)
        chunk_size: Chunk size for reading file (default: 1MB)
        progress_cb: Optional callback function(bytes_read: int, total_bytes: int, file_path: str)
        
    Returns:
        Hexadecimal hash string
    """
    hasher = hashlib.new(algorithm)
    total_bytes = os.path.getsize(file_path) if file_path.exists() else 0
    bytes_read = 0
    
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
                bytes_read += len(chunk)
                if progress_cb:
                    try:
                        progress_cb(bytes_read, total_bytes, str(file_path))
                    except Exception:
                        pass  # Don't fail hashing if callback errors
    except Exception as e:
        log.error(f"Error calculating hash for {file_path}: {e}")
        raise
    
    return hasher.hexdigest()


def verify_transferred_file(
    local_path: Path,
    expected_hash: str,
    algorithm: str = "sha256"
) -> Tuple[bool, Optional[str]]:
    """
    Verify that a transferred file matches the expected hash.
    
    Args:
        local_path: Path to the transferred file
        expected_hash: Expected hash value
        algorithm: Hash algorithm (default: sha256)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not local_path.exists():
        return False, f"File does not exist: {local_path}"
    
    try:
        actual_hash = calculate_file_hash(local_path, algorithm)
        if actual_hash == expected_hash:
            return True, None
        else:
            return False, f"Hash mismatch: expected {expected_hash}, got {actual_hash}"
    except Exception as e:
        return False, f"Error verifying hash: {str(e)}"


def store_verification_result(job, db_session, verified: bool, hash_value: Optional[str] = None, error: Optional[str] = None) -> None:
    """
    Store verification result in job record.
    
    Args:
        job: Job model instance
        db_session: Database session
        verified: Whether verification passed
        hash_value: Hash value used for verification
        error: Error message if verification failed
    """
    if verified:
        job.transfer_verification_status = "verified"
        if hash_value:
            job.transfer_verification_hash = hash_value
    else:
        job.transfer_verification_status = "failed"
        if hash_value:
            job.transfer_verification_hash = hash_value
    
    db_session.commit()
