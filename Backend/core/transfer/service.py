"""
Main transfer service abstraction layer for all transfer modes.
"""
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Callable
from sqlalchemy.orm import Session
from api import models
import logging
import time

log = logging.getLogger(__name__)


class ProgressThrottle:
    """Throttle progress updates to avoid excessive database writes while ensuring smooth frontend updates."""
    
    def __init__(self, callback: Callable[[int], None], min_change: int = 1, min_interval: float = 0.2):
        """
        Args:
            callback: Progress callback function
            min_change: Minimum percentage change to trigger update (default: 1%)
            min_interval: Minimum time interval between updates in seconds (default: 0.2s)
        """
        self.callback = callback
        self.min_change = min_change
        self.min_interval = min_interval
        self.last_progress = -1
        self.last_update_time = 0.0
    
    def update(self, progress: int):
        """Update progress if it changed enough or enough time passed."""
        now = time.time()
        progress_changed = abs(progress - self.last_progress) >= self.min_change
        time_passed = (now - self.last_update_time) >= self.min_interval
        
        if progress_changed or time_passed:
            self.callback(progress)
            self.last_progress = progress
            self.last_update_time = now


def enumerate_transfer_files(src_path: Path, job: Any) -> List[Path]:
    """
    Enumerate files to transfer.
    
    Args:
        src_path: Source path (file or directory)
        job: Job instance (for accessing disc_payload)
        
    Returns:
        List of file paths to transfer
    """
    files = []
    
    if src_path.is_file():
        files = [src_path]
    else:
        # Check for post_paths in job (post-processed files) or disc_payload
        post_paths = getattr(job, "post_paths", None) or (getattr(job, "disc_payload", None) or {}).get("post_paths") or {}
        if post_paths:
            # Use post_paths to determine files (has title_id keys -> relative_path)
            for rel_path in post_paths.values():
                file_path = src_path / rel_path
                if file_path.exists() and file_path.is_file():
                    files.append(file_path)
        else:
            # Enumerate all MKV files
            files = list(src_path.rglob("*.mkv"))
    
    return files


def verify_transferred_files_batch(
    files: List[Path],
    expected_hashes: Dict[str, str],
    progress_cb: Optional[Callable[[int, str], None]] = None
) -> Dict[str, bool]:
    """
    Verify hashes of multiple transferred files with progress reporting.
    
    Progress is reported in the 50-100% range (hash verification step).
    Each file's hash contributes to overall progress within that range.
    
    Args:
        files: List of file paths to verify
        expected_hashes: Dictionary mapping file keys to expected hash values
        progress_cb: Optional callback function(progress_pct: int, filename: str)
        
    Returns:
        Dictionary mapping file keys to verification results (True/False/None)
    """
    from core.transfer.validation import calculate_file_hash
    
    num_files = len(files)
    if num_files == 0:
        return {}
    
    hash_step_weight = 50 / num_files  # Each file contributes 50/num_files percent
    results = {}
    
    for idx, file_path in enumerate(files):
        # Create hash progress callback for this file
        def make_hash_progress_cb(file_idx: int):
            def hash_progress(bytes_read: int, total_bytes: int, file_path: str):
                if total_bytes > 0 and progress_cb:
                    # Calculate progress for this file's hash (0-100% of its step)
                    file_hash_pct = (bytes_read * 100) / total_bytes
                    # Calculate overall progress: 50% (transfer) + completed files + current file progress
                    completed_files = file_idx
                    current_file_progress = file_hash_pct * hash_step_weight / 100
                    overall = int(50 + completed_files * hash_step_weight + current_file_progress)
                    progress_cb(overall, str(file_path))
            return hash_progress
        
        # Get expected hash - try multiple key formats
        file_key = file_path.name
        expected_hash = (
            expected_hashes.get(file_key) or
            expected_hashes.get(str(file_path)) or
            expected_hashes.get(str(file_path.relative_to(file_path.parents[-1])))
        )
        
        if expected_hash:
            # Calculate hash with progress
            hash_progress_cb = make_hash_progress_cb(idx) if progress_cb else None
            try:
                actual_hash = calculate_file_hash(file_path, progress_cb=hash_progress_cb)
                results[file_key] = (actual_hash == expected_hash)
            except Exception as e:
                log.error(f"Error calculating hash for {file_path}: {e}")
                results[file_key] = False
            
            # Final update for this file
            if progress_cb:
                completed_files = idx + 1
                overall = int(50 + completed_files * hash_step_weight)
                progress_cb(overall, str(file_path))
        else:
            # No expected hash, skip verification
            results[file_key] = None
    
    return results


def get_active_config(db: Session) -> Optional[models.TransferConfig]:
    """
    Get the currently active transfer config.
    
    Args:
        db: Database session
        
    Returns:
        Active TransferConfig instance or None
    """
    return models.TransferConfig.get_active_config(db)


# Attribute names that are relationships, not columns; do not set on create.
_TRANSFER_CONFIG_SKIP_ATTRS = frozenset({"id", "credentials", "history", "health_checks"})


def create_config(
    db: Session,
    mode: str,
    name: Optional[str],
    config_data: Dict[str, Any],
    credentials: Optional[Dict[str, str]] = None,
    extra_attrs: Optional[Dict[str, Any]] = None,
) -> models.TransferConfig:
    """
    Create a new transfer config.

    Args:
        db: Database session
        mode: Transfer mode (local, rsync, smb, nfs)
        name: User-friendly name
        config_data: Mode-specific configuration
        credentials: Credentials dictionary (will be encrypted)
        extra_attrs: Optional dict of additional fields (e.g. transfer_dir, path_template)
            to set on the new config; keys must be valid model attributes.

    Returns:
        Created TransferConfig instance
    """
    existing_count = db.query(models.TransferConfig).count()
    is_first_or_only = existing_count == 0
    config = models.TransferConfig(
        mode=mode,
        name=name,
        config_data=config_data,
        is_active=is_first_or_only,  # First or only config is active by default (#292)
    )
    for key, value in (extra_attrs or {}).items():
        if hasattr(config, key) and key not in _TRANSFER_CONFIG_SKIP_ATTRS:
            setattr(config, key, value)
    db.add(config)
    db.commit()
    db.refresh(config)
    
    # Store credentials if provided
    if credentials:
        from core.transfer.utils.credentials import encrypt_and_store_credentials
        encrypt_and_store_credentials(db, config.id, credentials)
    
    return config


def update_config(
    db: Session,
    config_id: str,
    updates: Dict[str, Any]
) -> models.TransferConfig:
    """
    Update a transfer config.
    
    Args:
        db: Database session
        config_id: Config ID
        updates: Dictionary of fields to update
        
    Returns:
        Updated TransferConfig instance
    """
    config = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not config:
        raise ValueError(f"Transfer config not found: {config_id}")
    
    # Update fields
    for key, value in updates.items():
        if hasattr(config, key) and key != "id":
            setattr(config, key, value)
    
    db.commit()
    db.refresh(config)
    return config


def delete_config(db: Session, config_id: str) -> None:
    """
    Delete a transfer config (cascade deletes credentials and history).
    
    Args:
        db: Database session
        config_id: Config ID
    """
    config = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not config:
        raise ValueError(f"Transfer config not found: {config_id}")
    
    if config.is_active:
        raise ValueError("Cannot delete active transfer config. Set another config as active first.")
    
    db.delete(config)
    db.commit()


def activate_config(db: Session, config_id: str) -> models.TransferConfig:
    """
    Activate a transfer config (deactivates all others).
    
    Args:
        db: Database session
        config_id: Config ID to activate
        
    Returns:
        Activated TransferConfig instance
    """
    config = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not config:
        raise ValueError(f"Transfer config not found: {config_id}")
    
    config.activate(db)
    return config


def validate_connection(db: Session, config_id: str) -> Tuple[bool, str]:
    """
    Test connection for a transfer config.
    
    Args:
        db: Database session
        config_id: Config ID
        
    Returns:
        Tuple of (success, message)
    """
    config = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not config:
        return False, "Transfer config not found"
    
    from core.transfer.validation import validate_connectivity
    return validate_connectivity(db, config)


def check_storage(
    db: Session,
    config: models.TransferConfig
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Check storage information for a transfer config.
    
    Args:
        db: Database session
        config: TransferConfig instance
        
    Returns:
        Tuple of (storage_info_dict, error_message)
        storage_info_dict contains: path, total, used, free (all in bytes)
    """
    from core.storage_detection import get_storage_info
    return get_storage_info(db, config)


def validate_transfer_preconditions(
    db: Session,
    job_id: str,
    config: models.TransferConfig,
    source_size: int
) -> Tuple[bool, List[str]]:
    """
    Run pre-transfer validation checks.
    
    Args:
        db: Database session
        job_id: Job ID
        config: TransferConfig instance
        source_size: Size of source files in bytes
        
    Returns:
        Tuple of (passed, errors)
    """
    from core.transfer.validation import validate_transfer
    return validate_transfer(db, job_id, config, source_size)


def check_deduplication(
    db: Session,
    job_id: str,
    source_path: Path,
    dest_path: Path,
    source_hash: str,
    config: models.TransferConfig
) -> Tuple[bool, Optional[str]]:
    """
    Check if transfer should be skipped due to deduplication.
    
    Args:
        db: Database session
        job_id: Job ID
        source_path: Source file path
        dest_path: Destination file path
        source_hash: Hash of source file
        config: TransferConfig instance
        
    Returns:
        Tuple of (should_skip, existing_hash)
    """
    from core.transfer_deduplication import should_skip_transfer
    return should_skip_transfer(job_id, source_path, dest_path, source_hash, config)


def resolve_path_template(template: str, job_data: Dict[str, Any]) -> str:
    """
    Resolve a path template with job/release/disc metadata.
    
    Args:
        template: Path template string
        job_data: Dictionary with job metadata (movie_name, year, etc.)
        
    Returns:
        Resolved path string
    """
    from core.path_templates import resolve_template
    return resolve_template(template, job_data)


def resolve_conflict(dest_path: Path, resolution: str) -> Tuple[Path, bool]:
    """
    Handle file conflicts.
    
    Args:
        dest_path: Destination file path
        resolution: Resolution strategy (overwrite, skip, rename, fail)
        
    Returns:
        Tuple of (resolved_path, should_proceed)
    """
    from core.transfer_conflicts import resolve_conflict as _resolve_conflict
    return _resolve_conflict(dest_path, resolution)


def verify_transfer(
    db: Session,
    job_id: str,
    config: models.TransferConfig,
    dest_path: Path,
    expected_hash: str
) -> Tuple[bool, Optional[str]]:
    """
    Verify transferred file hash.
    
    Args:
        db: Database session
        job_id: Job ID
        config: TransferConfig instance
        dest_path: Destination file path
        expected_hash: Expected hash value
        
    Returns:
        Tuple of (verified, error_message)
    """
    from core.transfer.validation import verify_transferred_file, store_verification_result
    
    verified, error = verify_transferred_file(dest_path, expected_hash)
    
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if job:
        store_verification_result(job, db, verified, expected_hash, error)
    
    return verified, error


def cleanup_source(
    db: Session,
    job_id: str,
    config: models.TransferConfig,
    source_paths: List[Path]
) -> Tuple[bool, str]:
    """
    Clean up source files after successful transfer.
    
    Args:
        db: Database session
        job_id: Job ID
        config: TransferConfig instance
        source_paths: List of source file paths
        
    Returns:
        Tuple of (success, error_message)
    """
    from core.transfer.monitoring import cleanup_source_safe
    return cleanup_source_safe(job_id, config, source_paths)


class TransferPlanError(Exception):
    """Raised when a ``conflict_resolution`` intent cannot be satisfied
    by the destination's known capabilities. Callers should treat this
    as a precondition failure — do not attempt the transfer."""


def resolve_transfer_plan(
    config: models.TransferConfig,
    conflict_resolution: str,
    capabilities: Optional[Any],
) -> str:
    """Map ``(conflict_resolution × capabilities) → primitive plan``.

    Returns one of:
      - ``'direct_write'``: put/copy over any existing file
      - ``'delete_then_write'``: unlink existing, then write
      - ``'rename_source'``: write with a rename-suffix
      - ``'skip'``: honor skip intent (caller handles collision check)
      - ``'precheck_fail'``: fail intent — caller must probe destination
        for existence and raise if collision

    ``capabilities`` may be ``TransferCapabilities`` or ``None``. When
    ``None``, the selector logs a warning and picks the conservative
    branch — the reactive fallback in the SMB path (#635 commit A)
    catches the write-once case even without probe data.

    Raises :class:`TransferPlanError` when the intent is impossible with
    the reported capabilities (e.g. ``rename`` requested but the share
    has ``can_rename=false``).
    """
    intent = (conflict_resolution or "overwrite").lower()

    if capabilities is None:
        log.warning(
            "resolve_transfer_plan: no capabilities probed for config %s; "
            "falling back to reactive path for intent=%s",
            getattr(config, "id", "?"),
            intent,
        )

    if intent == "skip":
        return "skip"

    if intent == "fail":
        return "precheck_fail"

    if intent == "rename":
        if capabilities is None or capabilities.can_rename:
            return "rename_source"
        raise TransferPlanError(
            "conflict_resolution=rename but destination reports can_rename=false"
        )

    if intent == "overwrite":
        if capabilities is None:
            return "direct_write"
        if capabilities.can_overwrite_in_place:
            return "direct_write"
        if capabilities.can_delete:
            return "delete_then_write"
        if capabilities.can_rename:
            return "rename_source"
        raise TransferPlanError(
            "conflict_resolution=overwrite but destination reports no "
            "in-place overwrite, no delete, and no rename"
        )

    log.warning("resolve_transfer_plan: unknown intent %r; defaulting to direct_write", intent)
    return "direct_write"


def execute_transfer(
    db: Session,
    job_id: str,
    src_path: Path,
    config: models.TransferConfig,
    progress_callback: Optional[Callable[[int], None]] = None,
    speed_callback: Optional[Callable[[float], None]] = None
) -> Dict[str, Any]:
    """
    Execute transfer using the appropriate mode.
    
    Args:
        db: Database session
        job_id: Job ID
        src_path: Source path
        config: TransferConfig instance
        progress_callback: Optional callback for progress updates (0-100)
        speed_callback: Optional callback for speed updates (MB/s)
        
    Returns:
        Dictionary with transfer results. When success is True, dest_path must be
        present and non-empty (job router treats success without dest_path as failure).
    """
    if config.mode == "local":
        from core.transfer.protocols.local import transfer_local
        return transfer_local(db, job_id, src_path, config, progress_callback, speed_callback)
    elif config.mode == "rsync":
        from core.transfer.protocols.rsync import transfer_rsync
        return transfer_rsync(db, job_id, src_path, config, progress_callback, speed_callback)
    elif config.mode == "smb":
        from core.transfer.protocols.smb import transfer_smb
        return transfer_smb(db, job_id, src_path, config, progress_callback, speed_callback)
    elif config.mode == "nfs":
        from core.transfer.protocols.nfs import transfer_nfs
        return transfer_nfs(db, job_id, src_path, config, progress_callback, speed_callback)
    else:
        raise ValueError(f"Unknown transfer mode: {config.mode}")

