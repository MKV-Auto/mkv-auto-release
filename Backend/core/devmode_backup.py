"""
Devmode backup and restore utilities.

Provides functions to backup and restore both file system state and database state
for devmode stage reversals (finalize, post-process, transfer).
"""
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy.inspection import inspect

from api import models as db_models
from core.utils import get_mkvauto_root, get_mkvauto_data
import os

logger = logging.getLogger(__name__)


def get_backup_root() -> Path:
    """
    Returns the root directory for backups: ${MKVAUTO_DATA}/backups
    Uses the same base as export root (data root) but in a separate backups folder.
    """
    # Use same logic as get_export_root to get data root
    env_data = os.getenv("MKVAUTO_DATA_DIR") or os.getenv("MKVAUTO_DATA") or os.getenv("MAKEMKV_DATA_DIR")
    if env_data:
        base_root = Path(env_data).expanduser()
    else:
        base_root = get_mkvauto_root()
    backup_root = base_root / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    return backup_root


def get_stage_backup_dir(job_id: str, stage: str) -> Path:
    """Returns the backup directory path for a specific stage."""
    return get_backup_root() / job_id / stage


def _model_to_dict(obj) -> Dict[str, Any]:
    """
    Convert SQLAlchemy model instance to dictionary.
    Excludes relationships (they should be loaded separately).
    """
    mapper = inspect(obj.__class__)
    result = {}
    for column in mapper.columns:
        value = getattr(obj, column.name)
        # Handle datetime/timestamp serialization
        if hasattr(value, 'isoformat'):
            value = value.isoformat()
        result[column.name] = value
    return result


def create_stage_backup(job_id: str, stage: str, db: Session) -> Path:
    """
    Create a backup of database state (Job + Disc + Release records) for a stage.
    Returns the path to the backup directory.
    """
    backup_dir = get_stage_backup_dir(job_id, stage)
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    # Load job and related entities
    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise ValueError(f"Job {job_id} not found")
    
    disc = getattr(job, "disc", None)
    release = getattr(disc, "release", None) if disc else None
    
    # Serialize to JSON
    backup_data = {
        "job_id": job_id,
        "stage": stage,
        "backed_up_at": datetime.utcnow().isoformat(),
        "job": _model_to_dict(job) if job else None,
        "disc": _model_to_dict(disc) if disc else None,
        "release": _model_to_dict(release) if release else None,
    }
    
    # Write database.json
    db_json_path = backup_dir / "database.json"
    with open(db_json_path, 'w') as f:
        json.dump(backup_data, f, indent=2, default=str)
    
    logger.info(f"Created database backup for job {job_id} stage {stage} at {backup_dir}")
    return backup_dir


def restore_stage_backup(job_id: str, stage: str, db: Session) -> Dict[str, Any]:
    """
    Restore database state from backup.
    Returns a summary of restored records.
    """
    backup_dir = get_stage_backup_dir(job_id, stage)
    db_json_path = backup_dir / "database.json"
    
    if not db_json_path.exists():
        raise FileNotFoundError(f"Backup database.json not found at {db_json_path}")
    
    # Load backup data
    with open(db_json_path, 'r') as f:
        backup_data = json.load(f)
    
    summary = {
        "job_restored": False,
        "disc_restored": False,
        "release_restored": False,
    }
    
    # Helper to deserialize datetime strings
    def _deserialize_value(value: Any, field_name: str) -> Any:
        """Convert ISO format datetime strings back to datetime objects if needed."""
        if isinstance(value, str) and ("_at" in field_name.lower() or field_name in ("created_at", "updated_at", "finalized_at", "release_date")):
            try:
                from datetime import datetime
                # Handle Z suffix (UTC indicator)
                if value.endswith('Z'):
                    value = value[:-1] + '+00:00'
                # fromisoformat handles ISO format strings
                return datetime.fromisoformat(value)
            except (ValueError, AttributeError):
                # If parsing fails, return the string value as-is
                pass
        return value
    
    # Restore Job
    if backup_data.get("job"):
        job_data = backup_data["job"]
        job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
        if job:
            # Update all fields from backup
            for key, value in job_data.items():
                if key != "id":  # Don't change the ID
                    setattr(job, key, _deserialize_value(value, key))
            summary["job_restored"] = True
    
    # Restore Disc
    if backup_data.get("disc"):
        disc_data = backup_data["disc"]
        disc_id = disc_data.get("id")
        if disc_id:
            disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
            if disc:
                # Update all fields from backup
                for key, value in disc_data.items():
                    if key != "id":
                        setattr(disc, key, _deserialize_value(value, key))
                summary["disc_restored"] = True
    
    # Restore Release
    if backup_data.get("release"):
        release_data = backup_data["release"]
        release_id = release_data.get("id")
        if release_id:
            release = db.query(db_models.Release).filter(db_models.Release.id == release_id).first()
            if release:
                # Update all fields from backup
                for key, value in release_data.items():
                    if key != "id":
                        setattr(release, key, _deserialize_value(value, key))
                summary["release_restored"] = True
    
    db.commit()
    logger.info(f"Restored database backup for job {job_id} stage {stage}: {summary}")
    return summary


def backup_files(src_dir: Path, backup_dir: Path) -> None:
    """
    Backup files/directories to backup location.
    Creates a 'files' subdirectory in backup_dir and creates placeholder files (touch) for .mkv files,
    while copying small metadata files (logs, json, txt) as they're useful and small.
    """
    if not src_dir.exists():
        logger.warning(f"Source directory {src_dir} does not exist, skipping file backup")
        return
    
    files_backup_dir = backup_dir / "files"
    # Remove existing backup if it exists
    if files_backup_dir.exists():
        shutil.rmtree(files_backup_dir, ignore_errors=True)
    
    files_backup_dir.mkdir(parents=True, exist_ok=True)
    
    # Walk through source directory and create placeholders
    if src_dir.is_dir():
        for root, dirs, files in os.walk(src_dir):
            # Calculate relative path from src_dir
            rel_path = Path(root).relative_to(src_dir)
            dest_dir = files_backup_dir / rel_path
            dest_dir.mkdir(parents=True, exist_ok=True)
            
            for file in files:
                src_file = Path(root) / file
                dest_file = dest_dir / file
                
                # In devmode, create empty placeholder for all files (touch)
                dest_file.touch()
                logger.debug(f"Created placeholder for {src_file} -> {dest_file}")
    else:
        # Single file case - create placeholder
        (files_backup_dir / src_dir.name).touch()
    
    logger.info(f"Backed up files from {src_dir} to {files_backup_dir} (placeholders for all files in devmode)")


def restore_files(backup_dir: Path, target_dir: Path) -> None:
    """
    Restore files from backup to target location.
    Clears target directory first if it exists.
    For .mkv files in the backup (which are placeholders), creates empty files (touch).
    For other files, copies them normally.
    """
    files_backup_dir = backup_dir / "files"
    
    if not files_backup_dir.exists():
        logger.warning(f"Backup files directory {files_backup_dir} does not exist, skipping file restore")
        return
    
    # Clear target directory if it exists
    if target_dir.exists():
        if target_dir.is_dir():
            shutil.rmtree(target_dir, ignore_errors=True)
        else:
            target_dir.unlink()
    
    # Restore from backup
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    
    if files_backup_dir.is_dir():
        # Walk through backup directory and restore files
        for root, dirs, files in os.walk(files_backup_dir):
            # Calculate relative path from backup files directory
            rel_path = Path(root).relative_to(files_backup_dir)
            dest_dir = target_dir / rel_path
            dest_dir.mkdir(parents=True, exist_ok=True)
            
            for file in files:
                backup_file = Path(root) / file
                dest_file = dest_dir / file
                
                # In devmode, create empty placeholder for all files (touch) - they're already placeholders in backup
                dest_file.touch()
                logger.debug(f"Created placeholder {dest_file} from backup")
    else:
        # Single file case - create placeholder
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / files_backup_dir.name).touch()
    
    logger.info(f"Restored files from {files_backup_dir} to {target_dir} (placeholders for all files in devmode)")


def cleanup_stage_backup(job_id: str, stage: str) -> None:
    """Remove backup for a specific stage."""
    backup_dir = get_stage_backup_dir(job_id, stage)
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
        logger.info(f"Cleaned up backup for job {job_id} stage {stage}")

