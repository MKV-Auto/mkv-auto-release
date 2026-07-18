"""
Dev-seed shared helpers: mock file creation (same logic as devmode postprocess prep).

Used by resume_postprocess (devmode prep) and by create_devseed.py to replace
MKV files with 1–10KB random content and compute hashes/sizes for patches.
"""
import hashlib
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def mock_prep_directory(
    source_dir: Path,
    rel_paths: List[Union[Path, str]],
    db: Optional[Session],
    job: Any,
) -> Tuple[Dict[str, str], Dict[str, int]]:
    """
    Replace each MKV at source_dir/rel with 1–10KB random content and compute SHA256.

    Returns (source_hashes, title_mkv_sizes) where:
      - source_hashes: mapping source_file -> hex sha256 (for job.disc_payload["source_hashes"])
      - title_mkv_sizes: mapping title_id (str) -> size in bytes (for DiscTitle.mkv_size)
    """
    source_hashes: Dict[str, str] = {}
    title_mkv_sizes: Dict[str, int] = {}

    path_to_title: Dict[str, str] = {}
    if job:
        ripped = getattr(job, "ripped_files", None) or {}
        if isinstance(ripped, dict):
            path_to_title = {v: k for k, v in ripped.items()}

    disc_id = None
    if job and getattr(job, "disc", None):
        disc_id = getattr(job.disc, "id", None)

    for rel in rel_paths:
        rel_path = Path(rel) if not isinstance(rel, Path) else rel
        mock_path = source_dir / rel_path
        mock_path.parent.mkdir(parents=True, exist_ok=True)

        random_size = random.randint(1024, 10 * 1024)
        with open(mock_path, "wb") as f:
            f.write(os.urandom(random_size))

        hasher = hashlib.sha256()
        with open(mock_path, "rb") as f:
            while chunk := f.read(8 * 1024 * 1024):
                hasher.update(chunk)
        new_hash = hasher.hexdigest()
        actual_size = mock_path.stat().st_size

        source_file = mock_path.name
        disc_title = None

        if db and disc_id:
            try:
                from api import models as db_models

                rel_str = str(rel_path).replace("\\", "/")
                title_id = path_to_title.get(rel_str) or path_to_title.get(mock_path.name)
                if not title_id:
                    for p, tid in path_to_title.items():
                        if not p:
                            continue
                        parts = p.replace("\\", "/").split("/")
                        last = parts[-1] if parts else ""
                        if last == mock_path.name or p.endswith(mock_path.name):
                            title_id = tid
                            break
                if title_id:
                    disc_title = db.query(db_models.DiscTitle).filter(
                        db_models.DiscTitle.disc_id == disc_id,
                        db_models.DiscTitle.id == title_id,
                    ).first()
                if not disc_title:
                    disc_title = db.query(db_models.DiscTitle).filter(
                        db_models.DiscTitle.disc_id == disc_id,
                        db_models.DiscTitle.source_file.like(f"%{mock_path.name}%"),
                    ).first()
                if disc_title and disc_title.source_file:
                    source_file = disc_title.source_file
            except Exception:  # noqa: BLE001
                pass

        source_hashes[source_file] = new_hash
        if disc_title:
            title_mkv_sizes[str(disc_title.id)] = actual_size

    return (source_hashes, title_mkv_sizes)
