"""
Transfer deduplication system to skip files that already exist with matching hash.
"""
from pathlib import Path
from typing import Tuple, Optional
import logging

log = logging.getLogger(__name__)


def check_file_exists(dest_path: Path, expected_hash: str, config) -> Tuple[bool, Optional[str]]:
    """
    Check if file exists at destination and hash matches.
    
    Args:
        dest_path: Destination file path
        expected_hash: Expected hash of source file
        config: TransferConfig instance
        
    Returns:
        Tuple of (exists_and_matches, actual_hash)
    """
    if not dest_path.exists():
        return False, None
    
    if not dest_path.is_file():
        return False, None
    
    # Calculate hash of existing file
    try:
        from core.transfer.validation import calculate_file_hash
        actual_hash = calculate_file_hash(dest_path)
        matches = actual_hash == expected_hash
        return matches, actual_hash
    except Exception as e:
        log.warning(f"Error checking file hash for {dest_path}: {e}")
        return False, None


def should_skip_transfer(
    job_id: str,
    source_path: Path,
    dest_path: Path,
    source_hash: str,
    config
) -> Tuple[bool, Optional[str]]:
    """
    Determine if a transfer should be skipped by hash match at destination.

    Gated on ``config.conflict_resolution == "skip"`` — the four documented
    strategies are:

    - ``overwrite`` / ``fail`` / ``rename`` — a hash check is pointless; the
      strategy is decided by path collision (or by unconditional write).
    - ``skip`` — hash the destination; if it matches source, skip the copy
      (that's what the user picked skip for: don't re-transfer content we
      already have).

    Replaces the previous ``enable_deduplication`` gate, which was a
    separate toggle duplicating the same intent.

    Args:
        job_id: Job ID
        source_path: Source file path
        dest_path: Destination file path
        source_hash: Hash of source file
        config: TransferConfig instance

    Returns:
        Tuple of (should_skip, existing_hash)
    """
    if getattr(config, "conflict_resolution", None) != "skip":
        return False, None

    exists, existing_hash = check_file_exists(dest_path, source_hash, config)

    if exists:
        log.info(f"[{job_id}] File already exists with matching hash, skipping transfer: {dest_path}")
        return True, existing_hash

    return False, None


def get_destination_file_hash(dest_path: Path, config) -> Optional[str]:
    """
    Get hash of existing file at destination.
    
    Args:
        dest_path: Destination file path
        config: TransferConfig instance
        
    Returns:
        Hash string if file exists, None otherwise
    """
    if not dest_path.exists() or not dest_path.is_file():
        return None
    
    try:
        from core.transfer.validation import calculate_file_hash
        return calculate_file_hash(dest_path)
    except Exception as e:
        log.warning(f"Error calculating destination file hash: {e}")
        return None











