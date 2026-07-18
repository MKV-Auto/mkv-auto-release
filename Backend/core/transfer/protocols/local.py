"""
Local file transfer implementation.
"""
from pathlib import Path
from typing import Optional, Callable, Dict, Any
from sqlalchemy.orm import Session
from api import models
from core.transfer.validation import calculate_file_hash
from core.transfer.monitoring import SpeedTracker
from core.utils import move_with_progress
import logging
import shutil

log = logging.getLogger(__name__)


def transfer_local(
    db: Session,
    job_id: str,
    src_path: Path,
    config: models.TransferConfig,
    progress_callback: Optional[Callable[[int], None]] = None,
    speed_callback: Optional[Callable[[float], None]] = None
) -> Dict[str, Any]:
    """
    Transfer files locally.
    
    Args:
        db: Database session
        job_id: Job ID
        src_path: Source path (file or directory)
        config: TransferConfig instance
        progress_callback: Optional callback for progress (0-100)
        speed_callback: Optional callback for speed (MB/s)
        
    Returns:
        Dictionary with transfer results
    """
    # Resolve destination path and ensure it exists (create if missing)
    transfer_dir = Path(config.transfer_dir) if config.transfer_dir else Path.cwd()
    transfer_dir = transfer_dir.resolve()
    transfer_dir.mkdir(parents=True, exist_ok=True)
    dest_path = transfer_dir / src_path.name

    # Ensure destination parent for this transfer exists
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Calculate source hash before transfer
    source_hash = None
    try:
        if src_path.is_file():
            source_hash = calculate_file_hash(src_path)
    except Exception as e:
        log.warning(f"[{job_id}] Could not calculate source hash: {e}")
    
    # Track speed
    speed_tracker = SpeedTracker()
    speed_tracker.start()
    
    # Transfer with progress
    def combined_progress_cb(pct: int):
        if progress_callback:
            progress_callback(pct)
        
        # Calculate and report speed
        if src_path.is_file():
            bytes_transferred = int(src_path.stat().st_size * pct / 100)
            speed = speed_tracker.update(bytes_transferred)
            if speed_callback:
                speed_callback(speed)
    
    try:
        if src_path.is_file():
            # Transfer single file
            # Note: hash verification will be done in batch after transfer
            move_with_progress(
                str(src_path),
                str(dest_path),
                hash_verify=False,  # Batch hash verification after transfer
                progress_cb=combined_progress_cb
            )
        else:
            # Transfer directory with per-file progress
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
                    
                    # Update speed
                    if speed_callback:
                        bytes_transferred = sum(f.stat().st_size for f in files[:idx+1])
                        speed = speed_tracker.update(bytes_transferred)
                        speed_callback(speed)
                
                # Copy non-MKV files/directories
                for item in src_path.rglob("*"):
                    if item.is_file() and not item.suffix == ".mkv":
                        rel_path = item.relative_to(src_path)
                        dest_item = dest_path / rel_path
                        dest_item.parent.mkdir(parents=True, exist_ok=True)
                        if not dest_item.exists():
                            shutil.copy2(item, dest_item)
        
        # Hash verification is now done in batch after transfer completes
        # (handled in transfer_job endpoint)
        verified = False
        
        elapsed_time = speed_tracker.get_elapsed_time()
        avg_speed = speed_tracker.get_average_speed()
        
        return {
            "success": True,
            "dest_path": str(dest_path),
            "source_hash": source_hash,
            "verified": verified,
            "bytes_transferred": src_path.stat().st_size if src_path.is_file() else 0,
            "duration": elapsed_time,
            "speed_mbps": avg_speed,
        }
    except Exception as e:
        log.error(f"[{job_id}] Local transfer failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }








