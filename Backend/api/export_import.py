"""
Export/Import utilities for rip history.

Serializes database records and job directories (excluding MKV files and previews)
for transfer between instances.
"""
import datetime
import json
import logging
import os
import shutil
import socket
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any
from zipfile import ZipFile, ZIP_DEFLATED

from sqlalchemy.orm import Session, joinedload
from sqlalchemy.inspection import inspect

from api import models as db_models
from core.job_paths import JobPaths
from core.title_type_normalize import normalize_title_type_for_storage
from core.utils import get_mkvauto_data, get_mkvauto_root, resolve_jobs_root

logger = logging.getLogger(__name__)

# Maximum ZIP size (500MB)
MAX_ZIP_SIZE = 500 * 1024 * 1024


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


def _revive_row(model_cls, row: Dict[str, Any]) -> Dict[str, Any]:
    """#611: re-hydrate ISO-format datetime strings produced by
    ``_model_to_dict`` back into ``datetime`` objects on import.

    psycopg2 (production) silently coerces ISO strings into TIMESTAMP
    columns, so the legacy code path "just worked" without an explicit
    revive step. SQLAlchemy's SQLite driver (CI / unit tests) does not —
    it raises ``TypeError: SQLite DateTime type only accepts Python
    datetime and date objects as input``. Doing the conversion at import
    time here keeps the import robust across both back-ends.
    """
    out = dict(row)
    mapper = inspect(model_cls)
    for column in mapper.columns:
        val = out.get(column.name)
        if not isinstance(val, str):
            continue
        py_type = getattr(column.type, "python_type", None)
        if py_type is datetime.datetime:
            try:
                out[column.name] = datetime.datetime.fromisoformat(val)
            except ValueError:
                pass  # leave as-is; the column may accept the string form
        elif py_type is datetime.date:
            try:
                out[column.name] = datetime.date.fromisoformat(val)
            except ValueError:
                pass
    return out


def serialize_database(db: Session) -> Dict[str, List[Dict[str, Any]]]:
    """
    Query all tables and serialize to JSON-compatible dictionaries.
    Returns a dictionary keyed by table name.
    """
    result: Dict[str, List[Dict[str, Any]]] = {}
    
    # Movies (no dependencies)
    movies = db.query(db_models.Movie).all()
    result["movies"] = [_model_to_dict(m) for m in movies]
    
    # Releases (depends on movies, but we'll use movie_id FK)
    releases = (
        db.query(db_models.Release)
        .options(joinedload(db_models.Release.movie))
        .all()
    )
    result["releases"] = [_model_to_dict(r) for r in releases]
    
    # Discs (depends on releases)
    discs = (
        db.query(db_models.Disc)
        .options(joinedload(db_models.Disc.release))
        .all()
    )
    result["discs"] = [_model_to_dict(d) for d in discs]
    
    # Jobs (depends on discs)
    jobs = (
        db.query(db_models.Job)
        .options(joinedload(db_models.Job.disc))
        .all()
    )
    result["jobs"] = [_model_to_dict(j) for j in jobs]
    
    # DiscTitles (depends on discs)
    disc_titles = db.query(db_models.DiscTitle).all()
    result["disc_titles"] = [_model_to_dict(dt) for dt in disc_titles]
    
    # TitleStream rows (depends on discs and disc_titles)
    title_streams = db.query(db_models.TitleStream).all()
    result["title_streams"] = [_model_to_dict(ts) for ts in title_streams]
    
    # Boxsets (no dependencies). The Boxset → Release relationship is
    # captured via Release.boxset_id (already in result["releases"]); no
    # separate join table to export.
    boxsets = db.query(db_models.Boxset).all()
    result["boxsets"] = [_model_to_dict(b) for b in boxsets]

    return result


def collect_job_directories(job: db_models.Job, jobs_root: Path, temp_export_dir: Path) -> Optional[Path]:
    """
    Collect metadata/ and finalize/ directories for a job, excluding .mkv files.
    Returns the path to the collected job directory in temp_export_dir, or None if nothing to collect.
    """
    try:
        job_paths = JobPaths.from_job(job)
        job_root = job_paths.root
        
        if not job_root.exists():
            return None
        
        # Create target directory structure
        target_job_dir = temp_export_dir / "jobs" / job.id
        target_job_dir.mkdir(parents=True, exist_ok=True)
        
        # Include metadata/ directory (exclude .mkv files)
        metadata_src = job_paths.metadata
        if metadata_src.exists():
            target_metadata = target_job_dir / "metadata"
            target_metadata.mkdir(parents=True, exist_ok=True)
            _copy_directory_excluding_mkv(metadata_src, target_metadata)
        
        # Include finalize/ directory (exclude .mkv files)
        finalize_src = job_paths.finalize
        if finalize_src.exists():
            target_finalize = target_job_dir / "finalize"
            target_finalize.mkdir(parents=True, exist_ok=True)
            _copy_directory_excluding_mkv(finalize_src, target_finalize)
        
        # Return target if we actually copied something
        if (target_job_dir / "metadata").exists() or (target_job_dir / "finalize").exists():
            return target_job_dir
        return None
        
    except Exception as exc:
        logger.warning(f"Failed to collect job directory for job {job.id}: {exc}")
        return None


def _copy_directory_excluding_mkv(src: Path, dst: Path) -> None:
    """Copy directory tree excluding .mkv files."""
    if not src.exists() or not src.is_dir():
        return
    
    for item in src.iterdir():
        if item.is_dir():
            target_dir = dst / item.name
            target_dir.mkdir(exist_ok=True)
            _copy_directory_excluding_mkv(item, target_dir)
        elif item.is_file() and not item.suffix.lower() == '.mkv':
            shutil.copy2(item, dst / item.name)


def create_export_zip(db: Session, output_path: Path) -> Path:
    """
    Create a ZIP archive containing database export and job directories.
    Returns the path to the created ZIP file.
    """
    temp_dir = get_mkvauto_root() / "tmp" / f"export_{uuid.uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Serialize database
        logger.info("Serializing database...")
        db_data = serialize_database(db)
        
        # Add metadata
        export_data = {
            "database": db_data,
            "version": "1.0",
            "exported_at": datetime.datetime.utcnow().isoformat(),
            "instance_id": socket.gethostname(),
        }
        
        # Write database.json
        db_json_path = temp_dir / "database.json"
        with open(db_json_path, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)
        
        # Collect job directories
        jobs_root = resolve_jobs_root(None)
        jobs = db.query(db_models.Job).all()
        logger.info(f"Collecting directories for {len(jobs)} jobs...")
        
        jobs_collected = 0
        for job in jobs:
            collected = collect_job_directories(job, jobs_root, temp_dir)
            if collected:
                jobs_collected += 1
        
        logger.info(f"Collected directories for {jobs_collected} jobs")
        
        # Create ZIP archive
        zip_path = output_path
        if zip_path.exists():
            zip_path.unlink()
        
        logger.info(f"Creating ZIP archive at {zip_path}...")
        with ZipFile(zip_path, 'w', ZIP_DEFLATED) as zipf:
            # Add database.json
            zipf.write(db_json_path, "database.json")
            
            # Add job directories
            jobs_dir = temp_dir / "jobs"
            if jobs_dir.exists():
                for job_dir in jobs_dir.iterdir():
                    if job_dir.is_dir():
                        for root, dirs, files in os.walk(str(job_dir)):
                            for file in files:
                                file_path = Path(root) / file
                                arcname = file_path.relative_to(temp_dir)
                                zipf.write(str(file_path), str(arcname))
        
        # Check ZIP size
        zip_size = zip_path.stat().st_size
        if zip_size > MAX_ZIP_SIZE:
            zip_path.unlink()
            raise ValueError(f"Export ZIP exceeds maximum size of {MAX_ZIP_SIZE / (1024*1024):.0f}MB")
        
        logger.info(f"Export ZIP created: {zip_path} ({zip_size / (1024*1024):.2f}MB)")
        return zip_path
        
    finally:
        # Cleanup temp directory
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def deserialize_database(data: Dict[str, List[Dict[str, Any]]], db: Session) -> Dict[str, int]:
    """
    Import database records with merge strategy (skip conflicts).
    Returns a summary of imported records.
    """
    summary: Dict[str, int] = {
        "movies_imported": 0,
        "releases_imported": 0,
        "discs_imported": 0,
        "jobs_imported": 0,
        "disc_titles_imported": 0,
        "title_streams_imported": 0,
        "boxsets_imported": 0,
        "movies_skipped": 0,
        "releases_skipped": 0,
        "discs_skipped": 0,
        "jobs_skipped": 0,
        "disc_titles_skipped": 0,
        "title_streams_skipped": 0,
        "boxsets_skipped": 0,
    }
    
    db_data = data.get("database", {})
    
    # Import in dependency order
    # 1. Movies
    for movie_data in db_data.get("movies", []):
        existing = db.query(db_models.Movie).filter(db_models.Movie.id == movie_data["id"]).first()
        if existing:
            summary["movies_skipped"] += 1
            continue
        
        row = _revive_row(db_models.Movie, {k: v for k, v in movie_data.items() if k != "id"})
        movie = db_models.Movie(**row)
        movie.id = movie_data["id"]  # Set ID explicitly
        db.add(movie)
        summary["movies_imported"] += 1
    
    db.commit()
    
    # 2. Releases (depends on movies)
    for release_data in db_data.get("releases", []):
        existing = db.query(db_models.Release).filter(db_models.Release.id == release_data["id"]).first()
        if existing:
            summary["releases_skipped"] += 1
            continue
        
        # Ensure movie exists (should already be imported)
        movie_id = release_data.get("movie_id")
        if movie_id and not db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first():
            logger.warning(f"Release {release_data['id']} references non-existent movie {movie_id}, skipping")
            summary["releases_skipped"] += 1
            continue
        
        row = _revive_row(db_models.Release, {k: v for k, v in release_data.items() if k != "id"})
        release = db_models.Release(**row)
        release.id = release_data["id"]
        db.add(release)
        summary["releases_imported"] += 1
    
    db.commit()
    
    # 3. Boxsets (no dependencies)
    for boxset_data in db_data.get("boxsets", []):
        existing = db.query(db_models.Boxset).filter(db_models.Boxset.id == boxset_data["id"]).first()
        if existing:
            summary["boxsets_skipped"] += 1
            continue
        
        row = _revive_row(db_models.Boxset, {k: v for k, v in boxset_data.items() if k != "id"})
        boxset = db_models.Boxset(**row)
        boxset.id = boxset_data["id"]
        db.add(boxset)
        summary["boxsets_imported"] += 1
    
    db.commit()
    
    # (Boxset → Release link is stored on Release.boxset_id which the
    # releases import block above already restored.)

    # 5. Discs (depends on releases)
    for disc_data in db_data.get("discs", []):
        # Check by content_hash (unique) or id
        existing = db.query(db_models.Disc).filter(
            (db_models.Disc.id == disc_data["id"]) | 
            (db_models.Disc.content_hash == disc_data.get("content_hash"))
        ).first()
        if existing:
            summary["discs_skipped"] += 1
            continue
        
        # Check release dependency
        release_id = disc_data.get("release_id")
        if release_id and not db.query(db_models.Release).filter(db_models.Release.id == release_id).first():
            logger.warning(f"Disc {disc_data['id']} references non-existent release {release_id}, skipping")
            summary["discs_skipped"] += 1
            continue
        
        row = _revive_row(db_models.Disc, {k: v for k, v in disc_data.items() if k != "id"})
        disc = db_models.Disc(**row)
        disc.id = disc_data["id"]
        db.add(disc)
        summary["discs_imported"] += 1
    
    db.commit()
    
    # 6. DiscTitles (depends on discs)
    for dt_data in db_data.get("disc_titles", []):
        existing = db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == dt_data["id"]).first()
        if existing:
            summary["disc_titles_skipped"] += 1
            continue
        
        # Check disc dependency
        disc_id = dt_data.get("disc_id")
        if disc_id and not db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first():
            logger.warning(f"DiscTitle {dt_data['id']} references non-existent disc {disc_id}, skipping")
            summary["disc_titles_skipped"] += 1
            continue
        
        row = _revive_row(db_models.DiscTitle, {k: v for k, v in dt_data.items() if k != "id"})
        if "type" in row:
            row["type"] = normalize_title_type_for_storage(row.get("type"))
        dt = db_models.DiscTitle(**row)
        dt.id = dt_data["id"]
        db.add(dt)
        summary["disc_titles_imported"] += 1
    
    db.commit()
    
    # 7. TitleStream (depends on discs and disc_titles)
    for ts_data in db_data.get("title_streams", []):
        existing = db.query(db_models.TitleStream).filter(db_models.TitleStream.id == ts_data["id"]).first()
        if existing:
            summary["title_streams_skipped"] += 1
            continue

        disc_id = ts_data.get("disc_id")
        title_id = ts_data.get("title_id")
        if disc_id and not db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first():
            logger.warning(f"TitleStream {ts_data['id']} references non-existent disc {disc_id}, skipping")
            summary["title_streams_skipped"] += 1
            continue
        if title_id and not db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == title_id).first():
            logger.warning(f"TitleStream {ts_data['id']} references non-existent disc_title {title_id}, skipping")
            summary["title_streams_skipped"] += 1
            continue

        row = _revive_row(db_models.TitleStream, {k: v for k, v in ts_data.items() if k != "id"})
        for obsolete in ("season", "episode", "type"):
            row.pop(obsolete, None)
        ts = db_models.TitleStream(**row)
        ts.id = ts_data["id"]
        db.add(ts)
        summary["title_streams_imported"] += 1
    
    db.commit()
    
    # 8. Jobs (depends on discs) - last because it has the most dependencies
    for job_data in db_data.get("jobs", []):
        existing = db.query(db_models.Job).filter(db_models.Job.id == job_data["id"]).first()
        if existing:
            summary["jobs_skipped"] += 1
            continue
        
        # Check disc dependency
        disc_id = job_data.get("disc_id")
        if disc_id and not db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first():
            logger.warning(f"Job {job_data['id']} references non-existent disc {disc_id}, skipping")
            summary["jobs_skipped"] += 1
            continue
        
        row = _revive_row(db_models.Job, {k: v for k, v in job_data.items() if k != "id"})
        job = db_models.Job(**row)
        job.id = job_data["id"]
        db.add(job)
        summary["jobs_imported"] += 1
    
    db.commit()
    
    return summary
