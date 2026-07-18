"""
Stage backup and restore utilities for pipeline recovery.

Provides functions to backup and restore both file system state and database state
for stage reversals (finalize, post-process, transfer, etc.).

This replaces the devmode-only backup system with a production-ready version.
"""
import json
import logging
import shutil
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.inspection import inspect

from api import models as db_models
from core.utils import get_mkvauto_root, get_mkvauto_data, is_dev_mode
from core import settings as app_settings
from core.logging_utils import get_logger

logger = logging.getLogger(__name__)

CHECKPOINT_RETENTION = int(os.getenv("MKVAUTO_CHECKPOINT_RETENTION", "5"))  # Keep last N checkpoints per stage


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


def create_stage_backup(job_id: str, stage: str, db: Session, reason: Optional[str] = None) -> Optional[Path]:
    """
    Create a backup of database state (Job + Disc + Release records) for a stage.
    Returns the path to the backup directory, or None if checkpoints are disabled.
    
    Args:
        job_id: Job ID
        stage: Stage name (e.g., "finalize", "postprocess", "transfer")
        db: Database session
        reason: Optional reason for creating this checkpoint
    """
    if not is_dev_mode():
        logger.debug(f"Not in dev mode, skipping backup for job {job_id} stage {stage}")
        return None


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
        "disc_titles_restored": 0,
    }
    
    # Helper to deserialize datetime strings and handle JSON fields
    def _deserialize_value(value: Any, field_name: str) -> Any:
        """Convert ISO format datetime strings back to datetime objects if needed.
        Also handles JSON fields that may have been serialized as strings.
        """
        # Handle datetime/timestamp fields
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
        # Handle JSON fields that may have been serialized as strings (shouldn't happen with json.dump, but be safe)
        if isinstance(value, str) and field_name in ("detection_flags", "metadata_scan", "chapters", "streams", "label_payload", "label_draft", "finalize_result", "artifacts", "disc_info", "disc_payload"):
            try:
                # Try to parse as JSON if it looks like JSON
                if value.strip().startswith(("{", "[")):
                    return json.loads(value)
            except (ValueError, json.JSONDecodeError):
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
    disc_id = None
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
    
    # Restore DiscTitle records
    disc_titles_restored = 0
    if backup_data.get("disc_titles") and disc_id:
        for title_data in backup_data["disc_titles"]:
            title_id = title_data.get("id")
            if title_id:
                disc_title = db.query(db_models.DiscTitle).filter(
                    db_models.DiscTitle.id == title_id
                ).first()
                if disc_title:
                    # Update all fields from backup
                    for key, value in title_data.items():
                        if key != "id":  # Don't change the ID
                            setattr(disc_title, key, _deserialize_value(value, key))
                    disc_titles_restored += 1
                else:
                    # Title not found - log warning but don't fail (title may have been deleted)
                    logger.warning(f"DiscTitle {title_id} not found during restore for job {job_id} stage {stage} - skipping")
    summary["disc_titles_restored"] = disc_titles_restored
    
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
    logger.info(f"Restored database backup for job {job_id} stage {stage}: {summary} (disc_titles_restored: {disc_titles_restored})")
    return summary


def backup_files(src_dir: Path, backup_dir: Path) -> None:
    """
    Copy all files from job directory to backup location. Only runs when is_dev_mode()
    and get_quick_postprocess_tests_enabled(); otherwise returns immediately.

    Copy-only: source is never modified. The backup/files directory is never deleted;
    on re-backup we copy and overwrite existing files. Stale files in backup (present
    in backup but not in src) may remain. On failure we do not delete or modify backup;
    we log and re-raise.
    """
    backup_logger = get_logger("core.stage_backup", "backup_files")
    is_dev = is_dev_mode()
    quick_tests_enabled = app_settings.get_quick_postprocess_tests_enabled()
    
    backup_logger.info(f"backup_files: Starting backup from {src_dir} to {backup_dir}")
    
    if not is_dev or not quick_tests_enabled:
        backup_logger.info(f"backup_files: Skipped (is_dev_mode={is_dev}, quick_postprocess_tests_enabled={quick_tests_enabled})")
        return


def restore_files(backup_dir: Path, target_dir: Path) -> None:
    """
    Restore files from backup to target location. Only runs when is_dev_mode().
    Clears target directory first if it exists, then copies from backup to target.
    Never modifies or deletes the backup directory; only reads from it.
    """
    restore_logger = get_logger("core.stage_backup", "restore_files")
    is_dev = is_dev_mode()
    
    restore_logger.info(f"restore_files: Starting restore from {backup_dir} to {target_dir}")
    
    if not is_dev:
        restore_logger.info(f"restore_files: Skipped (is_dev_mode={is_dev})")
        return


def validate_backup(
    backup_dir: Path,
    expected_file_sizes: Dict[str, int],
    job_id: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """
    Validate that a stage backup is complete and correct before overwriting originals.

    Used after backup and before devmode mock creation. Checks that database.json
    exists and is well-formed, and that each expected file exists in backup/files
    with the expected size. Returns (ok, errors); mocks should only be created when ok.
    """
    errors: List[str] = []
    validate_logger = get_logger("core.stage_backup", "validate_backup")
    validate_logger.info("validate_backup: Starting backup validation for %s", backup_dir)

    # DB validation
    db_json_path = backup_dir / "database.json"
    if not db_json_path.exists():
        errors.append(f"database.json missing at {db_json_path}")
        validate_logger.warning("validate_backup: %s", errors[-1])
        return (False, errors)
    try:
        with open(db_json_path, "r") as f:
            data = json.load(f)
    except Exception as exc:
        errors.append(f"database.json invalid or unreadable: {exc}")
        validate_logger.warning("validate_backup: %s", errors[-1])
        return (False, errors)
    for key in ("job", "disc", "disc_titles"):
        if key not in data:
            errors.append(f"database.json missing required key: {key}")
        elif key == "disc_titles" and not isinstance(data["disc_titles"], list):
            errors.append("database.json disc_titles must be a list")
    if job_id is not None and data.get("job_id") != job_id:
        errors.append(f"database.json job_id mismatch: expected {job_id!r}, got {data.get('job_id')!r}")
    if errors:
        validate_logger.warning("validate_backup: DB validation failed: %s", errors[:5])
        return (False, errors)

    # File validation
    files_dir = backup_dir / "files"
    for rel, expected_size in expected_file_sizes.items():
        path = files_dir / rel
        if not path.exists():
            errors.append(f"Expected file missing in backup: {rel}")
            continue
        if not path.is_file():
            errors.append(f"Expected path is not a file in backup: {rel}")
            continue
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            errors.append(f"Size mismatch for {rel}: expected {expected_size}, got {actual_size}")
    if errors:
        validate_logger.warning("validate_backup: File validation failed: %s", errors[:5])
        return (False, errors)

    validate_logger.info("validate_backup: Passed (DB + %d files)", len(expected_file_sizes))
    return (True, [])


def cleanup_stage_backup(job_id: str, stage: str) -> None:
    """Remove backup for a specific stage."""
    backup_dir = get_stage_backup_dir(job_id, stage)
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
        logger.info(f"Cleaned up backup for job {job_id} stage {stage}")


def cleanup_old_checkpoints(job_id: str, stage: str, keep_count: int = CHECKPOINT_RETENTION) -> None:
    """
    Clean up old checkpoints, keeping only the most recent N.
    
    Note: Current implementation uses a single backup per stage.
    If we want to support multiple checkpoints per stage, we'd need to timestamp
    the backup directories (e.g., backup_dir / f"{timestamp}_{stage}").
    For now, this is a placeholder for future enhancement.
    """
    # TODO: Implement timestamped checkpoints if needed
    # For now, each stage has one checkpoint that gets overwritten
    pass


def list_checkpoints(job_id: str) -> List[Dict[str, Any]]:
    """
    List all available checkpoints for a job.
    Returns list of checkpoint info dicts.
    """
    job_backup_dir = get_backup_root() / job_id
    if not job_backup_dir.exists():
        return []
    
    checkpoints = []
    for stage_dir in job_backup_dir.iterdir():
        if not stage_dir.is_dir():
            continue
        
        db_json_path = stage_dir / "database.json"
        if not db_json_path.exists():
            continue
        
        try:
            with open(db_json_path, 'r') as f:
                data = json.load(f)
                checkpoints.append({
                    "stage": data.get("stage"),
                    "backed_up_at": data.get("backed_up_at"),
                    "reason": data.get("reason"),
                    "path": str(stage_dir),
                })
        except Exception as exc:
            logger.warning(f"Failed to read checkpoint info from {db_json_path}: {exc}")
    
    # Sort by backup time (newest first)
    checkpoints.sort(key=lambda x: x.get("backed_up_at", ""), reverse=True)
    return checkpoints

