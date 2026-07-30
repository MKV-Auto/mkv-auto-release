"""Background jobs for the bulk TheDiscDB export.

Building the archive is dominated by cover-art fetches — roughly one round trip
per release — so a library of any size pushes a synchronous request past proxy
timeouts. The work runs on a thread instead, and the caller polls.

Deliberately in-process rather than Celery: the result is a file the API process
must hand back over HTTP, so putting the work on a worker would mean shipping the
archive between processes for no benefit. The trade-off is that a restart loses
an in-flight export, which is fine — nothing is consumed until the download, and
re-running is cheap.

Shape mirrors :mod:`core.makemkv_update_jobs` (registry, retention sweep, status
polling) minus its WebSocket and root-helper machinery.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4

log = logging.getLogger(__name__)

JOB_RETENTION_HOURS = 6


class ExportJob:
    """One bulk export. ``status`` is pending → running → completed | failed."""

    def __init__(self, disc_ids: "list[str] | None" = None) -> None:
        self.id = str(uuid4())
        # None = the whole library; a list scopes to those discs (library page
        # export of one release or boxset).
        self.disc_ids = disc_ids
        self.status = "pending"
        self.done = 0
        self.total = 0
        self.current = ""
        self.error: Optional[str] = None
        self.path: Optional[Path] = None
        self.filename: Optional[str] = None
        self.summary: Dict = {}
        self.cancel_requested = False
        # Held so tests (and shutdown) can wait for the worker to finish. A
        # daemon thread that outlives its test keeps a DB session open against
        # whatever `api.database.SessionLocal` resolves to *at that moment* —
        # and pytest's monkeypatch has by then put the real one back.
        self.thread: Optional[threading.Thread] = None
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at

    def to_dict(self) -> Dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "current": self.current,
            "error": self.error,
            "included": self.summary.get("included", 0),
            # Entries that overwrite files upstream already has, with their
            # change summaries — the UI surfaces these when the export lands.
            "updates": self.summary.get("updates", []),
            "skipped": self.summary.get("skipped", 0),
            "cancelled": self.summary.get("cancelled", False),
            "download_ready": (
                self.status == "completed" and bool(self.path) and self.path.exists()
            ),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            # So the UI can say how long a finished archive stays collectable
            # rather than leaving the user to guess.
            "expires_at": (
                self.created_at + timedelta(hours=JOB_RETENTION_HOURS)
            ).isoformat(),
        }


_jobs: Dict[str, ExportJob] = {}
_lock = threading.Lock()


def _sweep() -> None:
    """Drop old jobs and delete their archives. Called on each new start."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=JOB_RETENTION_HOURS)
    for job_id, job in list(_jobs.items()):
        if job.created_at >= cutoff or job.status in ("pending", "running"):
            continue
        _discard(job)
        _jobs.pop(job_id, None)


def _discard(job: ExportJob) -> None:
    if not job.path:
        return
    try:
        job.path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not remove export archive %s: %s", job.path, exc)


def get_job(job_id: str) -> Optional[ExportJob]:
    return _jobs.get(job_id)


def get_active_job() -> Optional[ExportJob]:
    """The in-flight export, if any. There is at most one by construction."""
    for job in _jobs.values():
        if job.status in ("pending", "running"):
            return job
    return None


def get_latest_downloadable_job() -> Optional[ExportJob]:
    """The most recent finished export whose archive is still on disk.

    A long export usually finishes while nobody is looking at the page, and
    without this the archive would sit in the retention window unreachable —
    the user's only option being to build the whole thing again.

    The file is checked rather than assumed: the tmp volume can be cleared
    underneath us, and offering a download that 410s is worse than offering none.
    """
    candidates = [
        job for job in _jobs.values()
        if job.status == "completed" and job.path and job.path.exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda j: j.updated_at)


def get_resumable_job() -> Optional[ExportJob]:
    """What a freshly-loaded page should attach to: a run in progress, else a
    finished archive still waiting to be collected."""
    return get_active_job() or get_latest_downloadable_job()


def cancel_job(job_id: str) -> bool:
    """Ask a running job to stop. It stops between discs, not mid-disc."""
    job = _jobs.get(job_id)
    if not job or job.status not in ("pending", "running"):
        return False
    job.cancel_requested = True
    job.updated_at = datetime.now(timezone.utc)
    return True


def start_export_job(disc_ids: "list[str] | None" = None) -> ExportJob:
    """Start a bulk export, or return the one already running.

    Serialised deliberately: two concurrent exports would duplicate every
    cover-art fetch and race each other stamping the same discs as exported.
    A scoped request while another export runs joins the running one — the
    caller can see its scope in the returned job and decide to wait.
    """
    with _lock:
        active = get_active_job()
        if active:
            return active
        _sweep()
        job = ExportJob(disc_ids=disc_ids)
        _jobs[job.id] = job
        job.thread = threading.Thread(target=_run, args=(job,), daemon=True,
                                      name=f"discdb-export-{job.id[:8]}")
        job.thread.start()
        return job


def _archive_dir() -> Path:
    root = os.getenv("MKVAUTO_TMP_DIR") or tempfile.gettempdir()
    path = Path(root) / "discdb-exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(job: ExportJob) -> None:
    """Worker body. Owns its own DB session — the request's is long closed."""
    from api.database import SessionLocal
    from core.discdb_export import build_discdb_bulk_zip

    job.status = "running"
    job.updated_at = datetime.now(timezone.utc)
    db = SessionLocal()
    dest = _archive_dir() / f"{job.id}.zip"
    try:
        def on_progress(done: int, total: int, label: str) -> None:
            job.done, job.total, job.current = done, total, label
            job.updated_at = datetime.now(timezone.utc)

        filename, _, summary = build_discdb_bulk_zip(
            db, dest=dest, progress=on_progress,
            should_cancel=lambda: job.cancel_requested,
            disc_ids=job.disc_ids,
        )
        job.summary = summary
        job.filename = filename

        if not summary["included"]:
            # An archive containing only a README is not worth downloading, and
            # a "completed" job with nothing in it reads as success.
            dest.unlink(missing_ok=True)
            job.status = "failed"
            details = summary.get("skipped_detail") or []
            if details and all("already matches TheDiscDB" in d for d in details):
                job.error = (
                    "Nothing to submit — the selected discs already match "
                    "TheDiscDB's current entries; there are no corrections "
                    "upstream doesn't have."
                )
            else:
                job.error = (
                    "No discs are ready to export — a disc needs a finished job, a linked "
                    "release, and must not already be in TheDiscDB."
                )
            return

        _mark_exported(db, summary["disc_ids"])
        job.path = dest
        job.status = "completed"
        log.info(
            "Bulk contribution export %s: %d included, %d skipped",
            job.id, summary["included"], summary["skipped"],
        )
    except Exception as exc:
        log.warning("Bulk contribution export %s failed: %s", job.id, exc)
        dest.unlink(missing_ok=True)
        job.status = "failed"
        job.error = str(exc)
    finally:
        job.done = job.total
        job.current = ""
        job.updated_at = datetime.now(timezone.utc)
        db.close()


def _mark_exported(db, disc_ids) -> None:
    """Same bookkeeping the single-disc export does — equally handed over.

    Only once the archive is actually built: stamping discs for an export that
    then failed would hide them from `status=not_submitted`.
    """
    from api import models as db_models

    now = datetime.now(timezone.utc)
    for disc_id in disc_ids:
        disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
        if not disc:
            continue
        if (disc.discdb_contribution_status or "not_submitted") in ("not_submitted", "draft"):
            disc.discdb_contribution_status = "exported"
        disc.discdb_exported_at = now
    db.commit()


def await_all_jobs(timeout: float = 30.0) -> bool:
    """Wait for every worker thread to finish. Returns False if any outlived it.

    Exists for tests: a worker that outlives its test resolves
    ``api.database.SessionLocal`` after monkeypatch has restored the real one,
    so it can open a session against the developer's or CI's actual database
    and hold it open. Cancelling first keeps the wait short.
    """
    jobs = list(_jobs.values())
    for job in jobs:
        job.cancel_requested = True
    deadline_ok = True
    for job in jobs:
        if job.thread is not None and job.thread.is_alive():
            job.thread.join(timeout)
            if job.thread.is_alive():
                log.warning("Export worker %s did not stop within %ss", job.id, timeout)
                deadline_ok = False
    return deadline_ok
