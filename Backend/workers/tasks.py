# workers/tasks.py
# pylint: disable=missing-function-docstring
import os, traceback, re, tempfile, shutil, json, subprocess, logging, time
import requests
from typing import Callable, Any, Optional
from pathlib import Path
from contextlib import contextmanager
from copy import deepcopy
from core.utils import (
    get_lock_path,
    get_mkvauto_data,
    get_mkvauto_root,
    hash_file,
    is_dev_mode,
    is_disc_read_error,
    move_with_progress,
    resolve_jobs_root,
    retrieve_discdb_data,
)
from core.job_paths import JobPaths
from core.makemkv_output import makemkv_mkv_rel_path_sort_key, sort_makemkv_mkv_filenames

from celery import Celery, Task
from celery.signals import (
    task_prerun,
    task_postrun,
    task_failure,
    task_success,
    task_retry,
    task_received,
    task_revoked,
    worker_ready,
)
from celery.schedules import crontab
from filelock import FileLock, Timeout

from api import crud, database              # <- single source of truth
from sqlalchemy.orm import Session
from core.disc import Disc
from core.disc_manager import get_disc_info
from core import settings
from core.job_state import apply_job_state, StateViolation, StageState
from core.stage_backup import create_stage_backup, backup_files, validate_backup
from core.devseed_utils import mock_prep_directory
from core.disc_locks import (
    acquire_operation_lock,
    get_disc_lock_debug_snapshot,
    release_operation_lock,
    OPERATION_RIP,
)
import concurrent.futures
import threading
from datetime import datetime, timedelta, timezone

from core.ffmpeg_detection import (
    FFMPEG_DETECTION_CONFIDENCE_THRESHOLD,
    detect_padding_junk,
    is_detection_disabled,
)
from core.ffprobe_metadata import is_metadata_scan_disabled, scan_file_metadata

# ────────────────────────────────────────────────────────────────
# Use REDIS_URL so Docker (redis://redis:6379/0) and local (localhost) share the same broker
_redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BROKER = _redis_url
BACKEND = _redis_url
LOCK_PATH = get_lock_path()         # Win example: r'C:\tmp\disc_ripper.lock'
SCAN_LOCK_PATH = LOCK_PATH + ".scan"
LOCK_TIMEOUT = 1.0                          # seconds
PREVIEW_LOCK_PATH = LOCK_PATH + ".preview"
PREVIEW_SEMAPHORE_DIR = Path(get_lock_path()).parent / "ffmpeg_preview_slots"
celery_app = Celery('tasks', broker=BROKER, backend=BACKEND)

# Configure Celery logging
from core.logging_utils import _get_log_level_from_env

# Get log level from environment and map to Celery log level string
log_level = _get_log_level_from_env()
celery_log_level_map = {
    logging.ERROR: 'ERROR',
    logging.WARNING: 'WARNING',
    logging.INFO: 'INFO',
    logging.DEBUG: 'DEBUG',
}
celery_log_level = celery_log_level_map.get(log_level, 'INFO')

_task_eager = os.getenv("CELERY_TASK_ALWAYS_EAGER", "").lower() in ("1", "true", "yes")
celery_app.conf.update(
    worker_log_format='[%(asctime)s: %(levelname)s/%(processName)s] %(message)s',
    worker_task_log_format='[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s',
    worker_hijack_root_logger=False,  # Don't hijack root logger
    worker_log_level=celery_log_level,  # Respect MKVAUTO_DEBUG_LEVEL
    task_always_eager=_task_eager,  # E2E sets CELERY_TASK_ALWAYS_EAGER=true so rip_disc runs in-process
    task_eager_propagates=True,  # Propagate exceptions in eager mode
    # Persist STARTED in the result backend when the worker begins executing a task (not only at SUCCESS).
    task_track_started=True,
    # Separate queues: rip / postprocess / transfer / preview / default (maintenance).
    # Route by both short name and module path (Celery may use either when sending).
    task_routes={
        "rip_disc": {"queue": "rip"},
        "recover_running_rip": {"queue": "rip"},
        "workers.tasks.rip_disc": {"queue": "rip"},
        "workers.tasks.recover_running_rip": {"queue": "rip"},
        "rip_verification": {"queue": "rip"},
        "workers.tasks.rip_verification": {"queue": "rip"},
        # #365 step 6 (Phase 2 § 6.7): resume_postprocess Celery task
        # removed. Callers use start_transfer; the prep body remains
        # via _run_prep_phase.
        "start_transfer": {"queue": "postprocess"},
        "workers.tasks.start_transfer": {"queue": "postprocess"},
        "move_file": {"queue": "transfer"},
        "workers.tasks.move_file": {"queue": "transfer"},
        "transfer_remote": {"queue": "transfer"},
        "workers.tasks.transfer_remote": {"queue": "transfer"},
        "generate_previews": {"queue": "preview"},
        "generate_preview_track": {"queue": "preview"},
        "preview_and_detect": {"queue": "preview"},
        "preview_raw_titles": {"queue": "preview"},
        "detect_raw_titles": {"queue": "preview"},
        "workers.tasks.generate_previews": {"queue": "preview"},
        "workers.tasks.generate_preview_track": {"queue": "preview"},
        "workers.tasks.preview_and_detect": {"queue": "preview"},
        "workers.tasks.preview_raw_titles": {"queue": "preview"},
        "workers.tasks.detect_raw_titles": {"queue": "preview"},
        "cleanup_zombies": {"queue": "celery"},
        "cleanup_job_mkv": {"queue": "celery"},
        "reconcile_job_mkv_cleanup": {"queue": "celery"},
        "load_disc_info": {"queue": "celery"},
        "probe_transfer_capabilities": {"queue": "celery"},
        "workers.tasks.cleanup_zombies": {"queue": "celery"},
        "workers.tasks.cleanup_job_mkv": {"queue": "celery"},
        "workers.tasks.reconcile_job_mkv_cleanup": {"queue": "celery"},
        "workers.tasks.load_disc_info": {"queue": "celery"},
        "workers.tasks.probe_transfer_capabilities": {"queue": "celery"},
    },
)
# Queue name constants for worker commands (see Docker/supervisord.conf)
CELERY_QUEUE_RIP = "rip"
CELERY_QUEUE_POSTPROCESS = "postprocess"
CELERY_QUEUE_TRANSFER = "transfer"
CELERY_QUEUE_PREVIEW = "preview"
CELERY_QUEUE_DEFAULT = "celery"

# Rip progress: copy phase uses 0..RIP_PROGRESS_COPY_END, verification (gather_final_outputs) uses RIP_PROGRESS_COPY_END..100
RIP_PROGRESS_COPY_END = 85

# Configure logging
from core.logging_utils import get_logger, _get_log_level_from_env
from core.log_file_config import LOG_ROTATE_BACKUP_COUNT, LOG_ROTATE_MAX_BYTES
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
log = get_logger(__name__)

# Ensure Celery worker logs go to a file under the data dir (same place as api.log in Docker/local).
# Supervisor may also capture stdout; this guarantees worker output in e.g. docker-data/logs/celery_worker.log.
def _configure_worker_file_logging() -> None:
    try:
        logs_dir = get_mkvauto_root() / "logs"
        if not logs_dir.exists():
            return
        workers_logger = logging.getLogger("workers")
        if any(isinstance(h, _RotatingFileHandler) and getattr(h, "baseFilename", "").endswith("celery_worker.log") for h in workers_logger.handlers):
            return
        level = _get_log_level_from_env()
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        handler = _RotatingFileHandler(
            logs_dir / "celery_worker.log", maxBytes=LOG_ROTATE_MAX_BYTES, backupCount=LOG_ROTATE_BACKUP_COUNT
        )
        handler.setLevel(level)
        handler.setFormatter(formatter)
        workers_logger.addHandler(handler)
        workers_logger.setLevel(level)
    except Exception:  # noqa: S110
        pass
_configure_worker_file_logging()

# Celery signal handlers for task lifecycle events
@task_prerun.connect
def task_prerun_handler(sender=None, task_id=None, task=None, args=None, kwargs=None, **kwds):
    """Log when a task starts executing."""
    task_name = task.name if task else 'unknown'
    log.info(
        f"TASK START: {task_name}",
        extra={
            'task_id': task_id,
            'task_name': task_name,
            'task_args': args,  # Renamed from 'args' to avoid conflict with LogRecord reserved field
            'task_kwargs': kwargs,  # Renamed for consistency
            'sender': str(sender) if sender else None,
        }
    )

@task_postrun.connect
def task_postrun_handler(sender=None, task_id=None, task=None, args=None, kwargs=None, retval=None, state=None, **kwds):
    """Log when a task completes (success or failure)."""
    task_name = task.name if task else 'unknown'
    log.info(
        f"TASK POSTRUN: {task_name}",
        extra={
            'task_id': task_id,
            'task_name': task_name,
            'state': state,
            'retval': str(retval)[:200] if retval else None,  # Truncate long return values
        }
    )

@task_success.connect
def task_success_handler(sender=None, result=None, **kwds):
    """Log when a task completes successfully."""
    task_name = sender.name if sender else 'unknown'
    log.info(
        f"TASK SUCCESS: {task_name}",
        extra={
            'task_name': task_name,
            'result': str(result)[:200] if result else None,
        }
    )

@task_failure.connect
def task_failure_handler(sender=None, task_id=None, exception=None, traceback=None, einfo=None, **kwds):
    """Log detailed failure information."""
    # Note: 'traceback' parameter is a traceback object from Celery, not the module
    # The 'traceback' module is imported at top of file, but the parameter shadows it
    # Use einfo (which contains formatted traceback) or import the module with a different name
    import traceback as tb_module  # Import module with different name to avoid shadowing
    task_name = sender.name if sender else 'unknown'
    # Use einfo if available (contains formatted traceback), otherwise format from exception
    traceback_str = str(einfo) if einfo else (tb_module.format_exc() if exception else None)
    log.error(
        f"TASK FAILURE: {task_name}",
        extra={
            'task_id': task_id,
            'task_name': task_name,
            'exception_type': type(exception).__name__ if exception else None,
            'exception_message': str(exception) if exception else None,
            'traceback': traceback_str,
            'einfo': str(einfo) if einfo else None,
        },
        exc_info=exception
    )

@task_retry.connect
def task_retry_handler(sender=None, task_id=None, reason=None, einfo=None, **kwds):
    """Log when a task is retried."""
    task_name = sender.name if sender else 'unknown'
    log.warning(
        f"TASK RETRY: {task_name}",
        extra={
            'task_id': task_id,
            'task_name': task_name,
            'reason': str(reason) if reason else None,
            'einfo': str(einfo) if einfo else None,
        }
    )

@task_received.connect
def task_received_handler(sender=None, request=None, **kwds):
    """Log when a task is received by a worker (including redeliveries)."""
    if request:
        task_name = request.task if request else 'unknown'
        task_id = request.id if request else None
        retries = getattr(request, 'retries', 0)
        
        # For rip_disc tasks, check if this might be a redelivery by checking job state
        is_potential_redelivery = False
        if task_name == 'rip_disc' and task_id:
            try:
                # Extract job_id from args if available
                args = getattr(request, 'args', [])
                if args and len(args) > 0:
                    job_id = args[0]
                    # Check if job has an active rip_pid
                    with db_session() as db:
                        job = crud.get_job(db, job_id)
                        if job:
                            rip_pid = getattr(job, "rip_pid", None)
                            if rip_pid:
                                from core.drive_gatekeeper import is_pid_alive
                                if is_pid_alive(rip_pid):
                                    is_potential_redelivery = True
                                    log.warning(
                                        f"POTENTIAL REDELIVERY DETECTED: TASK RECEIVED: {task_name}",
                                        extra={
                                            'task_id': task_id,
                                            'task_name': task_name,
                                            'job_id': job_id,
                                            'rip_pid': rip_pid,
                                            'retries': retries,
                                            'is_eager': getattr(request, 'is_eager', False),
                                            'hostname': getattr(request, 'hostname', None),
                                            'warning': 'Task received but job.rip_pid is still alive - likely a Celery redelivery',
                                        }
                                    )
            except Exception as check_exc:
                # If check fails, just log normally
                pass
        
        if not is_potential_redelivery:
            log.info(
                f"TASK RECEIVED: {task_name}",
                extra={
                    'task_id': task_id,
                    'task_name': task_name,
                    'is_eager': getattr(request, 'is_eager', False),
                    'retries': retries,
                    'hostname': getattr(request, 'hostname', None),
                }
            )
        if task_name == 'rip_disc' and task_id:
            args = getattr(request, 'args', [])
            log.debug(
                "rip_disc task received task_id=%s args=%s hostname=%s",
                task_id, list(args)[:4] if args else [], getattr(request, 'hostname', None),
            )

@task_revoked.connect
def task_revoked_handler(sender=None, request=None, terminated=None, signum=None, expired=None, **kwds):
    """Log when a task is revoked, and for rip tasks propagate the kill to
    the spawned ``makemkvcon`` subprocess so it does not become orphaned.

    Closes #544: the 2026-06 diagnostic observed a revoked rip_disc leave
    its ``makemkvcon mkv dev:/dev/sr1`` subprocess alive for 30-60s after
    the Celery task itself was killed. The PID is persisted on Job.rip_pid
    (since #541) so we can look it up here and SIGTERM it within the
    revoke handler — eliminating the orphan window.
    """

    if not request:
        return

    task_name = request.task if request else 'unknown'
    task_id = request.id if request else None
    log.warning(
        f"TASK REVOKED: {task_name}",
        extra={
            'task_id': task_id,
            'task_name': task_name,
            'terminated': terminated,
            'signum': signum,
            'expired': expired,
            'hostname': getattr(request, 'hostname', None),
        },
    )

    # Only the rip_disc task spawns a long-running makemkvcon subprocess
    # that needs explicit termination on revoke.
    if not (task_name and task_name.endswith("rip_disc")):
        return

    job_id = None
    if isinstance(task_id, str) and task_id.startswith("rip_disc:"):
        job_id = task_id.split(":", 1)[1]
    else:
        # Fallback: rip_disc's first positional argument is job_id.
        try:
            args = getattr(request, 'args', None) or ()
            if args:
                job_id = str(args[0])
        except Exception:
            job_id = None

    if not job_id:
        log.warning("TASK REVOKED rip_disc but no job_id resolvable from task_id=%s", task_id)
        return

    rip_pid = None
    try:
        with db_session() as db:
            job = crud.get_job(db, job_id)
            if job is not None:
                rip_pid = getattr(job, "rip_pid", None)
    except Exception as db_exc:
        log.warning("revoke: failed to look up rip_pid for job %s: %s", job_id, db_exc)
        return

    if not rip_pid:
        log.info("revoke: job %s has no rip_pid (not yet spawned, already exited, or pre-#541 job)", job_id)
        return

    # SIGTERM → wait up to 10s → SIGKILL. is_pid_alive guards against
    # PID-reuse races where the rip_pid slot now belongs to an unrelated process.
    import signal as _signal
    import os as _os
    import time as _time
    from core.drive_gatekeeper import is_pid_alive

    if not is_pid_alive(rip_pid):
        log.info("revoke: makemkvcon pid=%s for job %s already gone", rip_pid, job_id)
        return

    try:
        _os.kill(rip_pid, _signal.SIGTERM)
        log.info("revoke: sent SIGTERM to makemkvcon pid=%s for job %s", rip_pid, job_id)
    except ProcessLookupError:
        return
    except PermissionError as perm_exc:
        log.warning("revoke: SIGTERM denied for pid=%s: %s", rip_pid, perm_exc)
        return

    # Brief wait, then SIGKILL if still alive.
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        if not is_pid_alive(rip_pid):
            log.info("revoke: makemkvcon pid=%s exited after SIGTERM", rip_pid)
            return
        _time.sleep(0.5)

    try:
        _os.kill(rip_pid, _signal.SIGKILL)
        log.warning("revoke: SIGKILL on stubborn makemkvcon pid=%s for job %s", rip_pid, job_id)
    except ProcessLookupError:
        pass
    except Exception as kill_exc:
        log.warning("revoke: SIGKILL failed for pid=%s: %s", rip_pid, kill_exc)


def check_worker_health() -> dict:
    """
    Check the health of Celery workers and return diagnostic information.
    
    Returns:
        Dictionary with health status, active workers, and any issues detected
    """
    health_info = {
        'status': 'unknown',
        'active_workers': [],
        'issues': [],
        'worker_count': 0,
    }
    
    try:
        inspect = celery_app.control.inspect()
        
        # Get active workers
        active = inspect.active()
        if active:
            health_info['active_workers'] = list(active.keys())
            health_info['worker_count'] = len(active)
            health_info['status'] = 'healthy'
        else:
            health_info['status'] = 'no_workers'
            health_info['issues'].append("No active workers found")
        
        # Get registered workers
        registered = inspect.registered()
        if registered:
            registered_workers = set()
            for worker, tasks in registered.items():
                registered_workers.add(worker)
            
            # Check if any registered workers are not active
            if active:
                active_workers = set(active.keys())
                missing_workers = registered_workers - active_workers
                if missing_workers:
                    health_info['issues'].append(
                        f"Registered but inactive workers: {', '.join(missing_workers)}"
                    )
                    health_info['status'] = 'degraded'
        
        # Get stats to check for worker crashes
        stats = inspect.stats()
        if stats:
            for worker_name, worker_stats in stats.items():
                # Check for high task counts that might indicate stuck workers
                if 'total' in worker_stats:
                    total_tasks = worker_stats.get('total', {})
                    if isinstance(total_tasks, dict):
                        # Log worker stats for diagnostics
                        log.debug(f"Worker {worker_name} stats: {total_tasks}")
        
    except Exception as exc:
        health_info['status'] = 'error'
        health_info['issues'].append(f"Failed to check worker health: {exc}")
        log.error(f"Error checking worker health: {exc}", exc_info=True)
    
    return health_info


def log_worker_health_periodic():
    """Periodically log worker health status (can be called from a background task)."""
    health = check_worker_health()
    if health['status'] != 'healthy':
        log.warning(
            f"Worker health check: status={health['status']}, "
            f"workers={health['worker_count']}, issues={health['issues']}"
        )
    else:
        log.debug(
            f"Worker health check: {health['worker_count']} active worker(s): "
            f"{', '.join(health['active_workers'])}"
        )

MIN_OUTPUT_FREE_BYTES = int(float(os.getenv("MIN_OUTPUT_FREE_BYTES", str(10 * 1024 * 1024 * 1024))))  # 10 GiB default
PREVIEW_BYTES_PER_TRACK = 5 * 1024 * 1024  # ~5MB per track preview estimate
# default data root; monkeypatched in tests
DATA_ROOT = get_mkvauto_data()
# ────────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────────
@contextmanager
def db_session():
    """Yield a short‑lived SQLAlchemy Session and guarantee close()."""
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()
# ────────────────────────────────────────────────────────────────
def _stale_lock_path(path: str | Path) -> Path:
    p = Path(path)
    try:
        if p.exists() and not p.is_symlink():
            # If file is zero bytes or pid inside is not running, treat as stale.
            if p.stat().st_size == 0:
                return p
            try:
                pid_txt = p.read_text().strip()
                if pid_txt.isdigit():
                    os.kill(int(pid_txt), 0)
            except ProcessLookupError:
                return p
            except Exception:
                pass
    except Exception:
        pass
    return Path(path)


def _cleanup_stale_lock(path: str | Path) -> None:
    stale = _stale_lock_path(path)
    try:
        if stale.exists():
            stale.unlink()
    except Exception:
        pass


def _disc_title_for_ripped_key(db, key, disc_id=None):
    """
    Resolve a key from ripped_files (either title_id UUID or filename) to a DiscTitle.
    gather_final_outputs may use filename as key when it can't match to title_id; we still set mkv_size for those.
    """
    from api import models as db_models
    key_str = str(key)
    # UUID-like: 36 chars, 4 hyphens
    if len(key_str) == 36 and key_str.count("-") == 4:
        tr = db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == key_str).first()
        if tr:
            return tr
    if disc_id and ".mkv" in key_str.lower():
        tr = (
            db.query(db_models.DiscTitle)
            .filter(
                db_models.DiscTitle.disc_id == disc_id,
                db_models.DiscTitle.source_file == key_str,
            )
            .order_by(
                db_models.DiscTitle.index.asc().nulls_last(),
                db_models.DiscTitle.order_index.asc().nulls_last(),
                db_models.DiscTitle.id.asc(),
            )
            .first()
        )
        if tr:
            return tr
    return None


def _mkv_file_sizes(workdir: Path) -> dict[str, int]:
    """Return mapping of relative path -> size in bytes for all ``*.mkv`` under workdir (recursive)."""
    from workers.rip_raw_ready import mkv_sizes_by_relpath

    return mkv_sizes_by_relpath(workdir)


def _normalize_ripped_files_to_title_ids(
    db, paths_dict: dict, disc_id=None, filename_to_title_id: dict | None = None
) -> dict:
    """
    Ensure ripped_files is always title_id (UUID) -> relative_path for postprocess/transfer.
    gather_final_outputs can return filename keys (output MKV names); consumers expect UUID keys.
    When filename_to_title_id is provided (inverted title_filename_map from disc_payload), use it
    to resolve output-filename keys to title_id when DiscTitle.source_file lookup does not match.

    Final fallback: parse the MakeMKV ``_tNN`` index from the filename and match
    against ``DiscTitle.index`` on the same disc. Mirrors the
    ``core.makemkv_output.map_mkv_filenames_to_title_ids`` resolver already used
    in rip_verification's downstream title_filename_map population. Without this,
    early gathering on a fresh disc (no payload title_filename_map yet, no
    source_file match for output-named files) returns empty even when the index
    is unambiguous — and the MISS path then fails with "Incomplete rip: 0/N".
    """
    if not paths_dict:
        return {}
    out = {}
    filename_map = filename_to_title_id or {}

    # Build index → title_id map lazily for the index-based fallback below.
    _index_to_id: dict[int, str] | None = None

    def _resolve_index_to_id() -> dict[int, str]:
        nonlocal _index_to_id
        if _index_to_id is not None:
            return _index_to_id
        result: dict[int, str] = {}
        if disc_id:
            try:
                from api import models as db_models
                rows = db.query(db_models.DiscTitle).filter(
                    db_models.DiscTitle.disc_id == disc_id
                ).all()
                for r in rows:
                    if isinstance(getattr(r, "index", None), int) and getattr(r, "id", None):
                        result[int(r.index)] = str(r.id)
            except Exception:
                pass
        _index_to_id = result
        return result

    for key, rel_path in paths_dict.items():
        key_str = str(key)
        if len(key_str) == 36 and key_str.count("-") == 4:
            out[key_str] = rel_path
            continue
        title_id = None
        if disc_id:
            tr = _disc_title_for_ripped_key(db, key, disc_id=disc_id)
            if tr:
                title_id = str(tr.id)
        if not title_id and filename_map:
            title_id = filename_map.get(key_str) or filename_map.get(Path(key_str).name)
        if not title_id and disc_id:
            from core.makemkv_output import makemkv_output_title_index
            idx = makemkv_output_title_index(key_str) or makemkv_output_title_index(str(rel_path))
            if idx is not None:
                title_id = _resolve_index_to_id().get(idx)
        if title_id:
            out[title_id] = rel_path
    return out


def _sync_disc_title_mkv_sizes_from_ripped(
    db,
    rip_root: Path,
    ripped: dict[str, str] | None,
    disc_id: str | None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """
    Set disc_titles.mkv_size from on-disk file size for each ripped path.
    Uses primary-key lookup for UUID keys so sizes always match ripped_files.
    """
    if not ripped or not rip_root:
        return
    from api import models as db_models

    for tid, rp in ripped.items():
        if not tid or not rp:
            continue
        tid_str = str(tid)
        try:
            full = (rip_root / rp).resolve()
            if not full.exists():
                continue
            msz = full.stat().st_size
            if len(tid_str) == 36 and tid_str.count("-") == 4:
                tr = db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == tid_str).first()
            else:
                tr = _disc_title_for_ripped_key(db, tid_str, disc_id=disc_id)
            if tr:
                tr.mkv_size = msz
                db.flush()
        except Exception as ex:
            if on_error:
                try:
                    on_error(f"mkv_size update for {tid_str}: {ex}")
                except Exception:
                    pass


def _count_active_ffmpeg_slots() -> int:
    """Count active ffmpeg slots (files with valid PIDs)."""
    if not PREVIEW_SEMAPHORE_DIR.exists():
        return 0
    
    count = 0
    for slot_file in PREVIEW_SEMAPHORE_DIR.iterdir():
        if not slot_file.is_file():
            continue
        try:
            pid_txt = slot_file.read_text().strip()
            if pid_txt.isdigit():
                # Check if process is still running
                os.kill(int(pid_txt), 0)  # Raises ProcessLookupError if not running
                count += 1
        except (ProcessLookupError, ValueError, OSError):
            # PID not running or invalid - slot is stale, will be cleaned up
            pass
        except Exception:
            # Other errors, count it as active to be safe
            count += 1
    return count


def _cleanup_stale_ffmpeg_slots() -> None:
    """Remove slot files where the PID is no longer running."""
    if not PREVIEW_SEMAPHORE_DIR.exists():
        return
    
    for slot_file in PREVIEW_SEMAPHORE_DIR.iterdir():
        if not slot_file.is_file():
            continue
        try:
            pid_txt = slot_file.read_text().strip()
            if pid_txt.isdigit():
                os.kill(int(pid_txt), 0)  # Raises ProcessLookupError if not running
                # Process is running, slot is valid - keep it
            else:
                # Invalid PID format, remove it
                slot_file.unlink()
        except ProcessLookupError:
            # PID not running, remove stale slot
            slot_file.unlink()
        except Exception:
            # Other errors (permission, etc.), leave it alone
            pass


@contextmanager
def ffmpeg_semaphore(max_parallel: int):
    """
    Context manager that enforces max_parallel concurrent ffmpeg processes globally.
    Uses file-based semaphore where each slot is a file containing the PID.
    """
    # Ensure semaphore directory exists
    PREVIEW_SEMAPHORE_DIR.mkdir(parents=True, exist_ok=True)
    
    # Clean up stale slots (files where PID is no longer running)
    _cleanup_stale_ffmpeg_slots()
    
    slot_file = None
    max_wait_time = 300  # Max 5 minutes waiting
    wait_interval = 1.0  # Check every second
    waited = 0.0
    
    try:
        # Try to acquire a slot
        while True:
            # Count active slots (files with valid PIDs)
            active_slots = _count_active_ffmpeg_slots()
            
            if active_slots < max_parallel:
                # We can proceed - create our slot file
                slot_id = f"{os.getpid()}_{int(time.time() * 1000000)}"
                slot_file = PREVIEW_SEMAPHORE_DIR / slot_id
                slot_file.write_text(str(os.getpid()))
                break
            
            # Too many active slots, wait
            if waited >= max_wait_time:
                raise TimeoutError(f"Could not acquire ffmpeg slot after {max_wait_time}s (max_parallel={max_parallel})")
            
            time.sleep(wait_interval)
            waited += wait_interval
            # Clean up stale slots periodically while waiting
            if int(waited) % 10 == 0:  # Every 10 seconds
                _cleanup_stale_ffmpeg_slots()
        
        # We've acquired the slot, run the code
        yield
        
    finally:
        # Always release our slot
        if slot_file and slot_file.exists():
            try:
                slot_file.unlink()
            except Exception:
                pass


@worker_ready.connect(weak=False)
def _cleanup_ffmpeg_preview_slots_on_worker_ready(sender=None, **kwargs) -> None:
    """Remove stale PID slot files when a worker process starts (e.g. after Docker restart).

    Slots are normally cleaned when ``ffmpeg_semaphore`` runs; if nothing acquires the
    semaphore, dead PID files could otherwise linger on a shared volume.
    """
    try:
        _cleanup_stale_ffmpeg_slots()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "ffmpeg preview slot cleanup on worker_ready failed: %s", exc
        )


def one_at_a_time(fn):
    """Decorator: retry the task later if another instance holds the lock; cleans stale locks."""
    def wrapper(self: Task, *args, **kwargs):
        job_id: str | None = args[0] if (args and isinstance(args[0], str)) else None
        if fn.__name__ == "rip_disc" and job_id:
            log.debug("rip_disc one_at_a_time entered job_id=%s lock_path=%s", job_id, LOCK_PATH)
        try:
            _cleanup_stale_lock(LOCK_PATH)
            with FileLock(LOCK_PATH, timeout=LOCK_TIMEOUT):
                # Ensure no drive scan is running; if scan lock is held, retry shortly.
                # Hold the scan lock for the ENTIRE duration of the rip operation to prevent
                # concurrent scans from accessing the disc while ripping.
                try:
                    _cleanup_stale_lock(SCAN_LOCK_PATH)
                    scan_lock = FileLock(SCAN_LOCK_PATH, timeout=LOCK_TIMEOUT)
                    disc_num_for_log = args[1] if len(args) > 1 and isinstance(args[1], str) else None
                    func_logger = get_logger("workers.tasks", "one_at_a_time")
                    func_logger.debug("Rip task acquiring scan lock job_id=%s disc_num=%s scan_lock_path=%s", 
                                    job_id, disc_num_for_log, str(SCAN_LOCK_PATH))
                    scan_lock.acquire()
                    func_logger.debug("Rip task acquired scan lock job_id=%s disc_num=%s", job_id, disc_num_for_log)
                    try:
                        return fn(self, *args, **kwargs)
                    finally:
                        # Before releasing the lock, check if makemkvcon is still running for this disc
                        # This prevents releasing the lock prematurely if the process is still active
                        disc_num = None
                        if len(args) > 1 and isinstance(args[1], str):
                            # For rip_disc, disc_num is the second argument (after job_id)
                            disc_num = args[1]
                        
                        if disc_num:
                            from core.utils import _is_makemkvcon_running_for_disc
                            if _is_makemkvcon_running_for_disc(disc_num):
                                logging.warning(
                                    "WARNING: Releasing lock for job %s (disc %s) but makemkvcon is still running. "
                                    "Recovery should detect this and skip requeuing.",
                                    job_id, disc_num
                                )
                        
                        func_logger.debug("Rip task releasing scan lock job_id=%s disc_num=%s", job_id, disc_num)
                        scan_lock.release()
                except Timeout:
                    raise self.retry(countdown=5, exc=RuntimeError('Drive scan in progress'))
        except Timeout:
            # Lock is held; mark the job failed so the UI can recover.
            if job_id:
                try:
                    with db_session() as db:
                        job = crud.get_job(db, job_id)
                        if job:
                            crud.append_log(db, job, "ERROR: Job failed to start (lock held)")
                            fields = {
                                "job_status": "failed",
                                "error_reason": "Lock held; another job is running",
                            }
                            if getattr(job, "rip_state", None) not in ("completed", "skipped"):
                                fields["rip_state"] = "failed"
                            # #365 step 5 — post_state column dropped.
                            # job_status="failed" + rip_state in (completed,
                            # skipped) is what Job.derived_post_state needs
                            # to return "failed" (decision-table step 2).
                            apply_job_state(db, job, updates=fields, reason="lock held")
                except Exception:
                    pass
            raise RuntimeError('Job already running')
    return wrapper
# ────────────────────────────────────────────────────────────────
def _update_title_file_paths(db: Session, disc_id: str, path_map: dict, stage: str, base_dir: str | None = None) -> None:
    """Set file_path and file_path_stage on DiscTitle rows for a batch of titles.

    Args:
        db: SQLAlchemy session.
        disc_id: Disc ID owning the titles.
        path_map: {title_id: relative_or_absolute_path}.
        stage: One of "rip", "postprocess", "transfer".
        base_dir: If provided, relative paths are resolved against this directory.
    """
    from api.models import DiscTitle
    if not path_map:
        return
    try:
        titles = db.query(DiscTitle).filter(
            DiscTitle.disc_id == disc_id,
            DiscTitle.id.in_(list(path_map.keys())),
        ).all()
        title_by_id = {t.id: t for t in titles}
        for title_id, rel_path in path_map.items():
            title = title_by_id.get(str(title_id))
            if not title:
                continue
            abs_path = os.path.join(base_dir, rel_path) if base_dir and not os.path.isabs(rel_path) else rel_path
            title.file_path = abs_path
            title.file_path_stage = stage
        db.flush()
    except Exception as exc:
        logging.warning("_update_title_file_paths: failed for disc_id=%s stage=%s: %s", disc_id, stage, exc)


# #365 — the path-resolution helpers moved to core/transfer/path_resolution.py
# so core/stage_validation.py can call them without a layering inversion
# (workers cannot be imported from core without circulars). The names
# below are re-exports kept for the original callers (this module's own
# rename call site, api.routers.jobs, and the existing test files).
from core.transfer.path_resolution import (
    resolve_rename_dest_root as _resolve_rename_dest_root,
    resolve_transfer_src_root as _resolve_transfer_src_root,
)


class DebouncedRippedFilesCommit:
    """
    Helper class to debounce ripped_files database commits.
    Commits when:
    - N titles complete (default: 3)
    - Time threshold reached (default: 2 seconds)
    - Force commit requested (final title, error, etc.)
    """
    def __init__(self, job, db, commit_threshold: int = 3, time_threshold: float = 2.0):
        self.job = job
        self.db = db
        self.commit_threshold = commit_threshold
        self.time_threshold = time_threshold
        self.pending_updates: dict[str, str] = {}
        self.last_commit_time: float = time.time()
        self.commit_count: int = 0
    
    def add(self, title_id: str, rel_path: str) -> bool:
        """
        Add a title_id -> rel_path mapping to pending updates.
        Returns True if a commit was triggered, False otherwise.
        """
        self.pending_updates[title_id] = rel_path
        self.commit_count += 1
        current_time = time.time()
        time_since_commit = current_time - self.last_commit_time
        
        # Commit if threshold reached (count or time)
        should_commit = (
            self.commit_count >= self.commit_threshold or
            time_since_commit >= self.time_threshold
        )
        
        if should_commit:
            self.commit()
            return True
        return False
    
    def commit(self) -> None:
        """Force commit all pending updates to database."""
        if not self.pending_updates:
            return
        
        try:
            # Get existing ripped_files and merge with pending
            existing_ripped_files = getattr(self.job, "ripped_files", None) or {}
            if not isinstance(existing_ripped_files, dict):
                existing_ripped_files = {}
            
            # Merge pending updates into existing
            merged_ripped_files = {**existing_ripped_files, **self.pending_updates}
            
            # Update job via apply_job_state (which handles state validation)
            from core.job_state import apply_job_state
            apply_job_state(self.db, self.job, updates={"ripped_files": merged_ripped_files}, reason="incremental ripped_files update")
            
            # Clear pending updates and reset counters
            self.pending_updates.clear()
            self.last_commit_time = time.time()
            self.commit_count = 0
        except Exception as exc:
            logging.warning(f"Failed to commit ripped_files updates: {exc}")
            # Don't raise - we'll retry on next commit or final commit
    
    def flush(self) -> None:
        """Force commit any remaining pending updates (for final commit, errors, etc.)."""
        if self.pending_updates:
            self.commit()


class JobTask(Task):
    """Shared DB‑update helpers for rip & move tasks."""

    def set_status(self, job, db, **fields):
        try:
            job_id = getattr(job, "id", None)
            job_status = getattr(job, "job_status", None)
            rip_state = getattr(job, "rip_state", None)
            log.info(
                "set_status job=%s job_status=%s rip_state=%s fields=%s",
                job_id,
                job_status,
                rip_state,
                {k: fields.get(k) for k in sorted(fields.keys()) if k in STATE_FIELDS or k in ("error_reason", "rip_progress", "rip_phase", "post_progress", "transfer_progress")},
            )
        except Exception:
            pass
        incoming_payload = fields.get("disc_payload")
        incoming_previews = incoming_payload.get("previews") if isinstance(incoming_payload, dict) else None
        existing_payload = getattr(job, "disc_payload", None) or {}
        existing_previews = existing_payload.get("previews") if isinstance(existing_payload, dict) else None
        if isinstance(incoming_payload, dict) and incoming_payload is existing_payload:
            try:
                from copy import deepcopy
                incoming_payload = deepcopy(incoming_payload)
                fields["disc_payload"] = incoming_payload
            except Exception:
                pass
        progress_fields = {
            "rip_progress",
            "rip_phase",
            "current_title_id",
            "current_title_number",
            "current_title_progress",
            "per_title_progress",
            "titles_completed",
            "total_titles",
        }
        if isinstance(incoming_payload, dict) and "disc_payload" in fields and any(field in fields for field in progress_fields):
            try:
                db.refresh(job)
                fresh_payload = getattr(job, "disc_payload", None) or {}
                fresh_previews = fresh_payload.get("previews") if isinstance(fresh_payload, dict) else None
                incoming_previews = incoming_payload.get("previews") if isinstance(incoming_payload, dict) else None
                if isinstance(fresh_previews, dict):
                    incoming_status = incoming_previews.get("status") if isinstance(incoming_previews, dict) else None
                    fresh_status = fresh_previews.get("status")
                    if incoming_status in (None, "queued") and fresh_status in ("running", "completed"):
                        incoming_payload["previews"] = fresh_previews
                        fields["disc_payload"] = incoming_payload
            except Exception:
                pass
        if isinstance(incoming_previews, dict) and incoming_previews.get("tracks"):
            incoming_status = incoming_previews.get("status")
            existing_status = existing_previews.get("status") if isinstance(existing_previews, dict) else None
            payload_same_object = incoming_payload is existing_payload
            if incoming_status == "queued" and existing_status in ("running", "completed"):
                if any(field in fields for field in progress_fields) and isinstance(existing_previews, dict):
                    incoming_payload["previews"] = existing_previews
                    fields["disc_payload"] = incoming_payload
        # Check if job is already in a terminal state - prevent invalid transitions
        current_status = getattr(job, "job_status", None)
        new_status = fields.get("job_status")
        
        # Recovery mechanism: If job is marked as "failed" but Celery task is still running,
        # allow transition back to "running" or other non-terminal states
        if current_status == "failed" and new_status and new_status != "failed":
            celery_task_id = getattr(job, "celery_task_id", None)
            if celery_task_id:
                try:
                    from celery.result import AsyncResult
                    task_result = AsyncResult(celery_task_id, app=celery_app)
                    task_state = task_result.state
                    # If task is still in a running state, allow recovery
                    if task_state in ("PENDING", "STARTED", "PROGRESS"):
                        logging.info(
                            f"Job {getattr(job, 'id', 'unknown')} is marked as failed but Celery task {celery_task_id} "
                            f"is still running (state: {task_state}). Allowing recovery transition to {new_status}."
                        )
                        try:
                            self.add_log(job, db, f"Recovering job: task is still running (state: {task_state}), transitioning from 'failed' to '{new_status}'")
                        except Exception:
                            pass
                        # Allow the transition by not removing job_status from fields
                        # Continue with normal processing below
                    else:
                        # Task is not running - block the transition
                        logging.warning(
                            f"Job {getattr(job, 'id', 'unknown')} is already {current_status}, cannot transition to {new_status}. "
                            f"Task {celery_task_id} is in state '{task_state}'."
                        )
                        try:
                            self.add_log(job, db, f"Skipped status update: job is {current_status}, cannot transition to {new_status} (task state: {task_state})")
                        except Exception:
                            pass
                        # Remove job_status from fields to prevent the invalid transition
                        fields = {k: v for k, v in fields.items() if k != "job_status"}
                        # If no other fields to update, return early
                        if not fields:
                            return
                except Exception as exc:
                    # If we can't check task state, log warning but block transition to be safe
                    logging.warning(
                        f"Job {getattr(job, 'id', 'unknown')}: Could not check Celery task state for recovery: {exc}. "
                        f"Blocking transition from {current_status} to {new_status}."
                    )
                    # Remove job_status from fields to prevent the invalid transition
                    fields = {k: v for k, v in fields.items() if k != "job_status"}
                    # If no other fields to update, return early
                    if not fields:
                        return
            else:
                # No celery_task_id - block the transition
                logging.warning(f"Job {getattr(job, 'id', 'unknown')} is already {current_status}, cannot transition to {new_status}. No celery_task_id to check.")
                try:
                    self.add_log(job, db, f"Skipped status update: job is {current_status}, cannot transition to {new_status}")
                except Exception:
                    pass
                # Remove job_status from fields to prevent the invalid transition
                fields = {k: v for k, v in fields.items() if k != "job_status"}
                # If no other fields to update, return early
                if not fields:
                    return
        elif current_status == "completed":
            # Completed jobs cannot transition to other states (no recovery for completed)
            if new_status and new_status != current_status:
                logging.warning(f"Job {getattr(job, 'id', 'unknown')} is already {current_status}, cannot transition to {new_status}. Skipping status update.")
                try:
                    self.add_log(job, db, f"Skipped status update: job is {current_status}, cannot transition to {new_status}")
                except Exception:
                    pass
                # Remove job_status from fields to prevent the invalid transition
                fields = {k: v for k, v in fields.items() if k != "job_status"}
                # If no other fields to update, return early
                if not fields:
                    return
        
        # If rip completed successfully, don't mark job as failed for post-processing errors
        # Only mark the specific stage as failed, keep job_status as 'running' or 'pending' for recovery
        rip_state = getattr(job, "rip_state", None)
        rip_completed = rip_state in ("completed", "skipped")
        
        # DISABLED: Automatic recovery - jobs should fail and wait for user to manually retry
        # If rip completed but post-processing failed, only fail the stage, not the job
        if fields.get("job_status") == "failed" and "error_reason" in fields:
            error_reason = fields.get("error_reason")
            if rip_completed and fields.get("post_state") == "failed":
                # Don't mark job as failed if rip completed - only fail the post-processing stage
                fields = {k: v for k, v in fields.items() if k != "job_status"}
                # Keep job_status as 'running' or current status to allow manual retry
                current_status = getattr(job, "job_status", "running")
                if current_status not in ("running", "pending"):
                    fields["job_status"] = "running"  # Set to running to allow manual retry
                self.add_log(job, db, f"Post-processing failed after successful rip - stage marked as failed, job remains in running state for manual retry")
            # NOTE: No automatic recovery - user must manually retry via frontend
            # This ensures failed jobs are clearly visible and files are preserved

        # Rip-complete failure guard: do not overwrite a successful rip with failure
        # (duplicate callback or late failure report after the rip already produced output).
        #
        # Three orthogonal success signals — any one is sufficient to refuse a
        # late ``rip_failed`` write:
        #   1. ``rip_state in ("completed", "skipped")`` — terminal state already set.
        #   2. ``rip_progress >= 100`` — copy/verify reached completion even if the
        #      terminal state transition hasn't been written yet (e.g. callback race).
        #   3. ``ripped_files`` is a non-empty dict — output files exist on disk;
        #      a failure write here would orphan them.
        if fields.get("job_status") == "failed" and fields.get("rip_state") == "failed":
            current_rip_state = getattr(job, "rip_state", None)
            try:
                current_rip_progress = int(getattr(job, "rip_progress", 0) or 0)
            except (TypeError, ValueError):
                current_rip_progress = 0
            current_ripped_files = getattr(job, "ripped_files", None)
            has_ripped_output = (
                isinstance(current_ripped_files, dict) and bool(current_ripped_files)
            )
            if (
                current_rip_state in ("completed", "skipped")
                or current_rip_progress >= 100
                or has_ripped_output
            ):
                logging.info(
                    "Ignoring failure update for job %s: rip already completed "
                    "(rip_state=%s, rip_progress=%s, ripped_files_count=%s)",
                    getattr(job, "id", "unknown"),
                    current_rip_state,
                    current_rip_progress,
                    len(current_ripped_files) if isinstance(current_ripped_files, dict) else 0,
                )
                for key in ("job_status", "rip_state", "error_reason"):
                    fields.pop(key, None)
                if not fields:
                    return

        try:
            apply_job_state(db, job, updates=fields, reason=None)
            # Emit debounced progress updates if progress fields changed
            progress_fields = ["rip_progress", "rip_phase", "post_progress", "transfer_progress", "per_title_progress", "current_title_progress"]
            if any(field in fields for field in progress_fields):
                try:
                    from core.progress_emitter import emit_job_progress_debounced
                    progress_data = {
                        "disc_id": str(job.disc_id) if getattr(job, "disc_id", None) else None,
                        "rip_progress": getattr(job, "rip_progress", 0),
                        "rip_phase": getattr(job, "rip_phase", None),
                        # #604 / #605: ship stage states alongside progress so
                        # the frontend never has to rely on a separate context
                        # event to learn that a stage state has advanced.
                        "rip_state": getattr(job, "rip_state", None),
                        "post_state": getattr(job, "derived_post_state", None),
                        "transfer_state": getattr(job, "transfer_state", None),
                        "post_progress": getattr(job, "post_progress", 0),
                        "transfer_progress": getattr(job, "transfer_progress", None),
                        "per_title_progress": getattr(job, "per_title_progress", None),
                        "current_title_progress": getattr(job, "current_title_progress", None),
                        "current_title_id": getattr(job, "current_title_id", None),
                        "current_title_number": getattr(job, "current_title_number", None),
                    }
                    emit_job_progress_debounced(str(job.id), progress_data)
                except Exception as exc:
                    logging.warning(f"Failed to emit progress update for job {getattr(job, 'id', 'unknown')}: {exc}")
        except StateViolation as exc:
            # DISABLED: Automatic recovery on state violation - jobs should fail and wait for user retry
            # If state violation occurs, just log it and let the job fail
            # User must manually retry via frontend, which will create a new job
            
            try:
                crud.append_log(db, job, f"ERROR: state violation: {exc}")
            except Exception:
                pass
            # Do not mutate the job on guard failure; surface the error to the caller.
            raise

    def add_log(self, job, db, line):
        crud.append_log(db, job, line)
    
    def log_task_error(self, job, db, exception: Exception, context: dict = None):
        """Log detailed error information for task failures."""
        import traceback
        from core.utils import _is_makemkvcon_running_for_disc, _find_makemkvcon_process_for_disc
        
        error_details = {
            'exception_type': type(exception).__name__,
            'exception_message': str(exception),
            'traceback': traceback.format_exc(),
            'job_id': getattr(job, 'id', None) if job else None,
            'job_status': getattr(job, 'job_status', None) if job else None,
            'rip_state': getattr(job, 'rip_state', None) if job else None,
            'celery_task_id': getattr(job, 'celery_task_id', None) if job else None,
        }
        
        # Check if makemkvcon is still running (for rip tasks)
        disc_num = getattr(job, 'disc_num', None) if job else None
        if disc_num:
            try:
                is_running = _is_makemkvcon_running_for_disc(str(disc_num))
                mp = getattr(job, "mount_point", None)
                pid, cmdline = (
                    _find_makemkvcon_process_for_disc(str(disc_num), mount_point=mp)
                    if is_running
                    else (None, None)
                )
                error_details['makemkvcon_running'] = is_running
                error_details['makemkvcon_pid'] = pid
                error_details['makemkvcon_cmdline'] = cmdline[:500] if cmdline else None
            except Exception as proc_exc:
                error_details['makemkvcon_check_error'] = str(proc_exc)
        
        # Add any additional context
        if context:
            error_details.update(context)
        
        # Log to both Python logger and job log
        log.error(
            f"Task error for job {getattr(job, 'id', 'unknown')}: {type(exception).__name__}: {exception}",
            extra=error_details,
            exc_info=exception
        )
        
        # Also add to job log
        try:
            error_summary = f"ERROR: {type(exception).__name__}: {exception}"
            self.add_log(job, db, error_summary)
            # Add traceback to job log (truncated if too long)
            tb = traceback.format_exc()
            if len(tb) > 2000:
                tb = tb[:2000] + "\n... (truncated)"
            self.add_log(job, db, f"Traceback:\n{tb}")
        except Exception as log_exc:
            log.warning(f"Failed to add error to job log: {log_exc}")
    
    def cleanup_dirs(self, job, paths: list[str]) -> None:
        """Best-effort removal of temp/result directories after failure."""
        import json, time, traceback
        from core.utils import _is_makemkvcon_running_for_disc
        # CRITICAL: Don't delete files if makemkvcon is still running for this disc
        disc_num = getattr(job, "disc_num", None) if job else None
        if disc_num and _is_makemkvcon_running_for_disc(str(disc_num)):
            logging.warning(f"Skipping cleanup for job {job.id if job else 'unknown'}: makemkvcon is still running for disc {disc_num}")
            return
        for p in paths:
            if not p:
                continue
            try:
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass

    def gather_final_outputs(self, root: Path, final_paths: dict | None = None,
                             progress_cb: Callable[[int, str], None] | None = None,
                             disc_id: str | None = None, db: Session | None = None,
                             cached_hashes: dict | None = None,
                             skip_hashes: bool = False) -> tuple[dict, dict]:
        """
        Build/validate the file paths map and optionally return a hash map for each MKV.
        Returns dict with title_id keys -> relative_path values.
        If skip_hashes=True, returns (paths, {}); no hash calculation.
        
        Args:
            final_paths: Optional dict with title_id -> rel_path (if already known)
            progress_cb: Optional callback (progress_pct: int, filename: str)
            cached_hashes: Optional dict of title_id -> hash (ignored if skip_hashes=True)
            skip_hashes: If True, only discover paths and return (paths, {}).
        
        Returns:
            Tuple of (paths_dict, hashes_dict). hashes_dict is {} when skip_hashes=True.
        """
        if not root.exists():
            raise FileNotFoundError(f"Output folder missing: {root}")

        paths = final_paths or {}
        if not paths:
            # Build mapping from disc_titles if available
            if disc_id and db:
                try:
                    from api import models as db_models
                    disc_titles = db.query(db_models.DiscTitle).filter(
                        db_models.DiscTitle.disc_id == disc_id
                    ).all()
                    
                    # Build source_file -> title_id and source_file -> comment (output_file) mappings
                    source_to_title_id = {}
                    source_to_output = {}
                    for title in disc_titles:
                        if title.source_file:
                            if title.id:
                                source_to_title_id[title.source_file] = str(title.id)
                            if title.comment:
                                source_to_output[title.source_file] = title.comment
                    
                    # Find actual files on disk and map title_id -> relative path
                    # First try to match by comment (original MakeMKV output filename)
                    if source_to_output:
                        matched_files = set()
                        for dirpath, _, filenames in os.walk(root):
                            for fn in filenames:
                                if fn.lower().endswith(".mkv"):
                                    rel = os.path.relpath(os.path.join(dirpath, fn), root)
                                    # Find matching source_file by output filename (comment)
                                    matched = False
                                    for source_file, output_file in source_to_output.items():
                                        if fn == output_file or fn.endswith(output_file):
                                            # Map to title_id instead of source_file
                                            title_id = source_to_title_id.get(source_file)
                                            if title_id:
                                                paths[title_id] = rel
                                                matched_files.add(fn)
                                                matched = True
                                            break
                                        # Also try reverse match (output_file might be a substring of fn after renaming)
                                        if output_file and fn.endswith(output_file.replace(".mkv", "")):
                                            title_id = source_to_title_id.get(source_file)
                                            if title_id:
                                                paths[title_id] = rel
                                                matched_files.add(fn)
                                                matched = True
                                                break
                        
                        # If we didn't match all files, try to match remaining files by iterating through all titles
                        # This handles cases where files were renamed and don't match comment
                        if len(matched_files) < len([f for f in os.walk(root) for _ in [None] for fn in _[2] if fn.lower().endswith(".mkv")]):
                            # Get all unmatched files
                            for dirpath, _, filenames in os.walk(root):
                                for fn in filenames:
                                    if fn.lower().endswith(".mkv") and fn not in matched_files:
                                        rel = os.path.relpath(os.path.join(dirpath, fn), root)
                                        # Try to match by iterating through all disc_titles and checking if this file
                                        # might correspond to a title_id (we'll use filename as key if no match)
                                        # This is a fallback - the file will be mapped later using title_filename_map
                                        paths[fn] = rel
                except Exception as exc:
                    # Fallback to filename-based mapping if disc_titles lookup fails
                    pass
            
            # Fallback: if no paths found, use filename-based mapping
            # Note: This fallback doesn't have title_id, so keys will be filenames
            # This should only happen if disc_id/db are not provided
            if not paths:
                for dirpath, _, filenames in os.walk(root):
                    for fn in filenames:
                        if fn.lower().endswith(".mkv"):
                            rel = os.path.relpath(os.path.join(dirpath, fn), root)
                            paths[fn] = rel

        if not paths:
            raise ValueError("No MKV files found in output")

        # Count total steps for progress calculation
        total_steps = len(paths)
        step_weight = 100 / total_steps if total_steps > 0 else 0

        hashes: dict[str, str] = {}
        items = list(paths.items())
        for idx, (key, rel) in enumerate(items):
            src = (root / rel).resolve()
            if not src.exists():
                raise FileNotFoundError(f"Missing output file: {src}")
            if skip_hashes:
                if progress_cb and total_steps > 0:
                    try:
                        progress_cb(int((idx + 1) * step_weight), rel)
                    except Exception:
                        pass
                continue
            # Create hash progress callback for this file
            def make_hash_progress_cb(file_idx: int, file_rel: str):
                def hash_progress(bytes_read: int, total_bytes: int, file_path: str):
                    if total_bytes > 0 and progress_cb:
                        step_progress_pct = (bytes_read * 100) / total_bytes
                        completed_steps = file_idx
                        overall_progress = int(completed_steps * step_weight + step_progress_pct * step_weight / 100)
                        try:
                            progress_cb(overall_progress, file_rel)
                        except Exception:
                            pass
                return hash_progress
            if cached_hashes and key in cached_hashes:
                hashes[key] = cached_hashes[key]
                import logging
                hash_logger = logging.getLogger("workers.tasks")
                try:
                    file_size = src.stat().st_size
                    hash_logger.debug(f"gather_final_outputs: Using cached hash for {rel} ({file_size} bytes): {cached_hashes[key][:16]}...")
                except Exception:
                    hash_logger.debug(f"gather_final_outputs: Using cached hash for {rel}: {cached_hashes[key][:16]}...")
                if progress_cb:
                    import time
                    start_time = time.time()
                    simulated_duration = 0.5
                    last_progress = int(idx * step_weight)
                    target_progress = int((idx + 1) * step_weight)
                    while time.time() - start_time < simulated_duration:
                        elapsed = time.time() - start_time
                        progress_pct = min(100, int((elapsed / simulated_duration) * 100))
                        current_progress = int(idx * step_weight + (progress_pct * step_weight / 100))
                        if current_progress > last_progress:
                            try:
                                progress_cb(current_progress, rel)
                            except Exception:
                                pass
                            last_progress = current_progress
                        time.sleep(0.05)
                    try:
                        progress_cb(target_progress, rel)
                    except Exception:
                        pass
            else:
                import logging
                hash_logger = logging.getLogger("workers.tasks")
                try:
                    file_size = src.stat().st_size
                    hash_logger.debug(f"gather_final_outputs: Calculating hash for {rel} ({file_size} bytes)")
                except Exception:
                    hash_logger.debug(f"gather_final_outputs: Calculating hash for {rel}")
                hash_progress_cb = make_hash_progress_cb(idx, rel) if progress_cb else None
                hashes[key] = hash_file(str(src), hash_type="sha256", progress_cb=hash_progress_cb)
                hash_logger.debug(f"gather_final_outputs: Hash calculated for {rel}: {hashes[key][:16]}...")
                if progress_cb:
                    try:
                        progress_cb(int((idx + 1) * step_weight), rel)
                    except Exception:
                        pass

        return paths, hashes


def gather_final_outputs(root: Path, final_paths: dict | None = None,
                         progress_cb: Callable[[int, str], None] | None = None,
                         disc_id: str | None = None, db: Session | None = None,
                         cached_hashes: dict | None = None,
                         skip_hashes: bool = False) -> tuple[dict, dict]:
    """
    Module-level helper to discover outputs and optionally compute hashes.
    Returns (paths, hashes). When skip_hashes=True, returns (paths, {}).
    """
    if not root.exists():
        raise FileNotFoundError(f"Output folder missing: {root}")

    paths = final_paths or {}
    if not paths:
        # Build mapping from disc_titles if available
        if disc_id and db:
            try:
                from api import models as db_models
                disc_titles = db.query(db_models.DiscTitle).filter(
                    db_models.DiscTitle.disc_id == disc_id
                ).all()
                
                # Build source_file -> title_id and source_file -> comment (output_file) mappings
                source_to_title_id = {}
                source_to_output = {}
                for title in disc_titles:
                    if title.source_file:
                        if title.id:
                            source_to_title_id[title.source_file] = str(title.id)
                        if title.comment:
                            source_to_output[title.source_file] = title.comment
                
                # Find actual files on disk and map title_id -> relative path
                if source_to_output:
                    for dirpath, _, filenames in os.walk(root):
                        for fn in filenames:
                            if fn.lower().endswith(".mkv"):
                                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                                # Find matching source_file by output filename (comment)
                                for source_file, output_file in source_to_output.items():
                                    if fn == output_file or fn.endswith(output_file):
                                        # Map to title_id instead of source_file
                                        title_id = source_to_title_id.get(source_file)
                                        if title_id:
                                            paths[title_id] = rel
                                        break
            except Exception as exc:
                # Fallback to filename-based mapping if disc_titles lookup fails
                pass
        
        # Fallback: if no paths found, use filename-based mapping
        # Note: This fallback doesn't have title_id, so keys will be filenames
        # This should only happen if disc_id/db are not provided
        if not paths:
            for dirpath, _, filenames in os.walk(root):
                for fn in filenames:
                    if fn.lower().endswith(".mkv"):
                        rel = os.path.relpath(os.path.join(dirpath, fn), root)
                        paths[fn] = rel

    if not paths:
        raise ValueError("No MKV files found in output")

    # Count total steps for progress calculation
    total_steps = len(paths)
    step_weight = 100 / total_steps if total_steps > 0 else 0

    hashes: dict[str, str] = {}
    items = list(paths.items())
    for idx, (key, rel) in enumerate(items):
        src = (root / rel).resolve()
        if not src.exists():
            raise FileNotFoundError(f"Missing output file: {src}")
        if skip_hashes:
            if progress_cb and total_steps > 0:
                try:
                    progress_cb(int((idx + 1) * step_weight), rel)
                except Exception:
                    pass
            continue
        def make_hash_progress_cb(file_idx: int, file_rel: str):
            def hash_progress(bytes_read: int, total_bytes: int, file_path: str):
                if total_bytes > 0 and progress_cb:
                    step_progress_pct = (bytes_read * 100) / total_bytes
                    completed_steps = file_idx
                    overall_progress = int(completed_steps * step_weight + step_progress_pct * step_weight / 100)
                    try:
                        progress_cb(overall_progress, file_rel)
                    except Exception:
                        pass
            return hash_progress
        if cached_hashes and key in cached_hashes:
            hashes[key] = cached_hashes[key]
            if progress_cb:
                import time
                start_time = time.time()
                simulated_duration = 0.5
                last_progress = int(idx * step_weight)
                target_progress = int((idx + 1) * step_weight)
                while time.time() - start_time < simulated_duration:
                    elapsed = time.time() - start_time
                    progress_pct = min(100, int((elapsed / simulated_duration) * 100))
                    current_progress = int(idx * step_weight + (progress_pct * step_weight / 100))
                    if current_progress > last_progress:
                        try:
                            progress_cb(current_progress, rel)
                        except Exception:
                            pass
                        last_progress = current_progress
                    time.sleep(0.05)
                try:
                    progress_cb(target_progress, rel)
                except Exception:
                    pass
        else:
            hash_progress_cb = make_hash_progress_cb(idx, rel) if progress_cb else None
            hashes[key] = hash_file(str(src), hash_type="sha256", progress_cb=hash_progress_cb)
            if progress_cb:
                try:
                    progress_cb(int((idx + 1) * step_weight), rel)
                except Exception:
                    pass

    return paths, hashes


def _safe_track_folder(name: str) -> str:
    base = Path(name).stem
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in base)
    return safe or "track"


def _ensure_previews_map(
    disc_payload: dict,
    final_paths: dict | None = None,
    preview_maps: dict | None = None,
) -> dict:
    """
    Initialize previews payload and track entries for provided paths if not present.
    """
    payload = disc_payload if isinstance(disc_payload, dict) else {}
    previews = payload.get("previews") or {
        "status": "queued",
        "tracks": {},
        "queue_position": None,
        "updated_at": datetime.utcnow().isoformat(),
    }
    tracks_map = previews.get("tracks") or {}
    for track_key in (final_paths or {}):
        rel_path = None
        try:
            rel_path = (final_paths or {}).get(track_key)
        except Exception:
            rel_path = None
        entry = tracks_map.get(track_key) or {}
        resolved_title_id = None
        if preview_maps:
            try:
                resolved_title_id = _resolve_preview_title_id(track_key, rel_path, preview_maps)
            except Exception:
                resolved_title_id = None
        entry.setdefault("status", "queued")
        entry.setdefault("manifest", f"previews/{_safe_track_folder(track_key)}/preview.m3u8")
        entry.setdefault("error", None)
        if resolved_title_id:
            entry.setdefault("title_id", resolved_title_id)
        title_for_source = None
        if preview_maps:
            title_for_source = preview_maps.get("id_to_title", {}).get(str(resolved_title_id or track_key))
        if title_for_source:
            source_file = getattr(title_for_source, "source_file", None)
            if source_file:
                entry.setdefault("source_file", source_file)
                entry.setdefault("track_id", source_file)
        elif not preview_maps:
            entry.setdefault("title_id", str(track_key))
        if rel_path:
            entry.setdefault("source", rel_path)
        tracks_map[track_key] = entry
    previews["tracks"] = tracks_map
    previews.setdefault("status", "queued")
    previews["updated_at"] = datetime.utcnow().isoformat()
    payload["previews"] = previews
    return payload


def _backfill_preview_title_ids(disc_payload: dict) -> dict:
    """Ensure preview tracks include title_id using title_filename_map when available."""
    payload = disc_payload if isinstance(disc_payload, dict) else {}
    previews = payload.get("previews") if isinstance(payload, dict) else None
    if not isinstance(previews, dict):
        return payload
    tracks = previews.get("tracks")
    if not isinstance(tracks, dict):
        return payload
    title_filename_map = payload.get("title_filename_map") or {}
    if not isinstance(title_filename_map, dict):
        return payload
    filename_to_id = {str(filename): str(title_id) for title_id, filename in title_filename_map.items() if filename}
    if not filename_to_id:
        return payload
    updated = 0
    for key, entry in tracks.items():
        if not isinstance(entry, dict):
            continue
        existing = entry.get("title_id")
        if existing and str(existing) != str(key):
            continue
        rel = entry.get("source")
        if not rel:
            continue
        rel_str = str(rel)
        rel_base = Path(rel_str).name
        mapped = filename_to_id.get(rel_str) or filename_to_id.get(rel_base)
        if mapped and mapped != existing:
            entry["title_id"] = mapped
            updated += 1
    return payload


def _build_title_output_map(
    title_keys: list[str],
    final_paths: dict[str, str],
    *,
    disc_titles: list[Any] | None = None,
) -> dict[str, str]:
    """
    Build ``{title_id: rel_path}`` for the ripped MKVs.

    When ``disc_titles`` is supplied we parse the MakeMKV ``_tNN`` index from
    each MKV filename and match it against ``DiscTitle.index`` — this is the
    correct mapping for both full and selective rips (Path A). When the caller
    can't pass disc_titles we fall back to the legacy positional zip of
    ``title_keys`` against MKVs sorted by ``_tNN``; that pairing happens to be
    right for full rips and wrong for selective rips, but we keep it so callers
    without DB access still get *something* sensible.
    """
    if not final_paths:
        return {}
    if disc_titles:
        from core.makemkv_output import map_mkv_filenames_to_title_ids
        mapping = map_mkv_filenames_to_title_ids(
            (rel for _k, rel in final_paths.items()),
            disc_titles,
        )
        if mapping:
            return mapping
    if not title_keys:
        return {}
    ordered_titles = list(title_keys)
    mkv_items = sorted(final_paths.items(), key=lambda kv: makemkv_mkv_rel_path_sort_key(kv[1]))
    mapping = {}
    for idx, t_key in enumerate(ordered_titles):
        if idx >= len(mkv_items):
            break
        # value is the relative path within the job root (or rip_workdir)
        mapping[t_key] = mkv_items[idx][1]
    return mapping


def _build_title_id_maps(job, disc_payload: dict) -> dict[str, Any]:
    """
    Build lookup maps for resolving title_id and preview sources.
    """
    titles = []
    try:
        if job and getattr(job, "disc", None) and getattr(job.disc, "titles", None):
            titles = list(job.disc.titles)
    except Exception:
        titles = []

    id_to_title = {str(t.id): t for t in titles if getattr(t, "id", None)}
    # Duplicate source_file: prefer lowest (index, order_index, id), not arbitrary dict order.
    def _row_sort_key(t: Any) -> tuple[int, int, str]:
        iv = getattr(t, "index", None)
        ov = getattr(t, "order_index", None)
        return (
            iv if isinstance(iv, int) else 999_999,
            ov if isinstance(ov, int) else 999_999,
            str(getattr(t, "id", "")),
        )

    source_to_id: dict[str, str] = {}
    for t in sorted(
        (x for x in titles if getattr(x, "source_file", None)),
        key=_row_sort_key,
    ):
        sf = str(t.source_file)
        if sf not in source_to_id:
            source_to_id[sf] = str(t.id)
    # MakeMKV index is not unique; only map indices that appear on exactly one title.
    index_counts: dict[str, int] = {}
    for t in titles:
        iv = getattr(t, "index", None)
        if iv is not None:
            k = str(iv)
            index_counts[k] = index_counts.get(k, 0) + 1
    index_to_id: dict[str, str] = {}
    for t in titles:
        iv = getattr(t, "index", None)
        if iv is None:
            continue
        k = str(iv)
        if index_counts.get(k, 0) == 1:
            index_to_id[k] = str(t.id)
    source_ids_by_file: dict[str, list[str]] = {}
    for t in titles:
        sf = getattr(t, "source_file", None)
        if not sf:
            continue
        sf = str(sf)
        source_ids_by_file.setdefault(sf, []).append(str(t.id))
    ambiguous_source_files = {sf for sf, ids in source_ids_by_file.items() if len(ids) > 1}
    if ambiguous_source_files:
        log.warning(
            "disc %s: duplicate disc_titles.source_file (%d files); migrate/dedupe recommended; "
            "resolution uses lowest index/order_index",
            getattr(getattr(job, "disc", None), "id", "?"),
            len(ambiguous_source_files),
        )

    title_output_map = disc_payload.get("title_output_map") or {}
    output_to_title_key: dict[str, str] = {}
    if isinstance(title_output_map, dict):
        output_to_title_key = {str(v): str(k) for k, v in title_output_map.items()}

    title_filename_map = disc_payload.get("title_filename_map") or {}
    filename_to_id: dict[str, str] = {}
    if isinstance(title_filename_map, dict):
        for title_id, filename in title_filename_map.items():
            if filename and title_id:
                filename_to_id[str(filename)] = str(title_id)

    return {
        "id_to_title": id_to_title,
        "source_to_id": source_to_id,
        "index_to_id": index_to_id,
        "ambiguous_source_files": ambiguous_source_files,
        "output_to_title_key": output_to_title_key,
        "filename_to_id": filename_to_id,
        "title_output_map": title_output_map if isinstance(title_output_map, dict) else {},
        "title_filename_map": title_filename_map if isinstance(title_filename_map, dict) else {},
    }


def _resolve_preview_title_id(track_key: str | None, rel_path: str | None, maps: dict) -> str | None:
    if not maps:
        return None
    amb = maps.get("ambiguous_source_files") or set()
    if track_key:
        key = str(track_key)
        if key in maps["id_to_title"]:
            return key
        # Stable identity: source_file, then unambiguous MakeMKV index only.
        if key in maps["source_to_id"] and key not in amb:
            return maps["source_to_id"][key]
        if key in maps.get("index_to_id", {}):
            return maps["index_to_id"][key]
        if key in maps["source_to_id"]:
            return maps["source_to_id"][key]
        filename_id = maps["filename_to_id"].get(key)
        if filename_id:
            return filename_id
        title_key = maps["output_to_title_key"].get(key)
        if title_key:
            if title_key in maps["id_to_title"]:
                return title_key
            if title_key in maps["source_to_id"] and title_key not in amb:
                return maps["source_to_id"][title_key]
            if title_key in maps.get("index_to_id", {}):
                return maps["index_to_id"][title_key]
            if title_key in maps["source_to_id"]:
                return maps["source_to_id"][title_key]
    if rel_path:
        rel = str(rel_path)
        title_key = maps["output_to_title_key"].get(rel)
        if title_key:
            if title_key in maps["id_to_title"]:
                return title_key
            if title_key in maps["source_to_id"] and title_key not in amb:
                return maps["source_to_id"][title_key]
            if title_key in maps.get("index_to_id", {}):
                return maps["index_to_id"][title_key]
            if title_key in maps["source_to_id"]:
                return maps["source_to_id"][title_key]
        rel_base = Path(rel).name
        filename_id = maps["filename_to_id"].get(rel_base)
        if filename_id:
            return filename_id
    return None


def _resolve_preview_rel_path(track_key: str, final_paths: dict[str, str], maps: dict) -> str | None:
    if track_key in final_paths:
        return final_paths.get(track_key)
    title_output_map = maps.get("title_output_map") or {}
    mapped = title_output_map.get(track_key) if isinstance(title_output_map, dict) else None
    if mapped:
        return mapped
    # If the track_key is a title_id, use disc title metadata to map to rel_path.
    title = maps.get("id_to_title", {}).get(str(track_key))
    if title:
        source = getattr(title, "source_file", None)
        if source:
            if source in title_output_map:
                return title_output_map[source]
            if source in final_paths:
                return final_paths.get(source)
        filename = maps.get("title_filename_map", {}).get(str(track_key))
        if filename and filename in final_paths:
            return final_paths.get(filename)
    return None


def _apply_release_title(disc: Disc, job) -> None:
    """
    Ensure disc.movie_name is populated so rename_outputs doesn't fall back to
    Unknown Movie/Show. Prefer release name, then slug, then any payload title.
    """
    title = None
    try:
        rel = getattr(getattr(job, "disc", None), "release", None)
        if rel:
            title = getattr(rel, "name", None) or getattr(rel, "slug", None)
    except Exception:
        title = None
    if not title:
        payload = job.disc_payload or {}
        title = payload.get("release_name") or payload.get("disc_name") or payload.get("disc_title")
    if title:
        disc.movie_name = title


class RipCallbackTransportError(RuntimeError):
    """Worker could not get HTTP 2xx from API rip callback after retries."""


def _parse_makemkv_titles_saved_failed(log_content: str) -> tuple[int | None, int]:
    """
    Parse MakeMKV progress log for "N titles saved" and optional "M failed".
    MSG:5036,0,0,"Copy complete. N titles saved." or similar.
    Returns (titles_saved, titles_failed); (None, 0) if no match.
    """
    titles_saved: int | None = None
    titles_failed = 0
    # Copy complete. N titles saved.
    m1 = re.search(r'Copy complete\.\s*(\d+)\s*titles?\s*saved', log_content, re.IGNORECASE)
    if m1:
        titles_saved = int(m1.group(1))
    # Optional: N titles saved, M failed
    m2 = re.search(r'(\d+)\s*titles?\s*saved[^.]*,\s*(\d+)\s*failed', log_content, re.IGNORECASE)
    if m2:
        titles_saved = int(m2.group(1))
        titles_failed = int(m2.group(2))
    return (titles_saved, titles_failed)


def _extract_makemkv_read_error_hint(log_content: str) -> str | None:
    """Last human-readable fragment from a MSG:2003 line (read errors), if any."""
    hint: str | None = None
    for line in log_content.splitlines():
        stripped = line.strip()
        if "MSG:2003" not in stripped:
            continue
        idx = stripped.find("MSG:2003")
        robot = stripped[idx:]
        parts = robot.split('"')
        if len(parts) >= 2:
            candidate = parts[1]
            if "occurred while reading" in candidate or "Error" in candidate:
                hint = candidate
    if hint and len(hint) > 280:
        hint = hint[:277] + "..."
    return hint


def _post_rip_complete_callback(
    job_id: str,
    success: bool,
    error_reason: str | None = None,
    error_type: str | None = None,
    source_hashes: dict | None = None,
    debug: dict[str, Any] | None = None,
    *,
    heal_stuck_copy: bool = False,
) -> None:
    """Apply rip-complete state directly via DB (#365 cleanup).

    Previously POSTed to ``/jobs/{job_id}/rip-complete``. Now imports
    the API handler function and invokes it directly via a worker-owned
    DB session, eliminating the localhost HTTP roundtrip (#378-class
    fragility removal).

    The API handler's side effects (acknowledging the copy boundary,
    setting ``rip_phase=verification``, enqueueing ``rip_verification``)
    all still fire — only the transport hop is removed. Imports are
    local to the function body to avoid the circular
    ``workers.tasks`` ↔ ``api.routers.jobs`` import.

    ``source_hashes`` is ignored by the API on this route (verification
    sends the hashes); the parameter is kept on the worker signature
    for backward compatibility with existing callers but not forwarded.

    **Failure-raise semantics preserved.** Caller treats a raise as a
    hard rip-task failure → retry/escalation. Previously the raise came
    from HTTP transport; now from exceptions inside the handler. Both
    wrapped as ``RipCallbackTransportError`` so worker retry behaviour
    is unchanged.

    The legacy ``POST /jobs/{job_id}/rip-complete`` endpoint stays
    registered for one release as a safety net.
    """
    from api.routers.jobs import rip_complete_callback, RipCompleteRequest
    log.info("rip-complete callback (in-process): job_id=%s success=%s", job_id, success)
    body_kwargs: dict[str, Any] = {"success": success}
    if not success:
        body_kwargs["error_reason"] = error_reason or "Rip failed"
        if error_type:
            body_kwargs["error_type"] = error_type
        if debug:
            body_kwargs["debug"] = debug
        if heal_stuck_copy:
            body_kwargs["heal_stuck_copy"] = True
    try:
        body = RipCompleteRequest(**body_kwargs)
    except Exception as exc:
        raise RipCallbackTransportError(
            f"rip-complete body construction failed for job {job_id}: {exc}"
        ) from exc

    db = database.SessionLocal()
    try:
        rip_complete_callback(
            job_id=job_id,
            body=body,
            db=db,
            client_host="127.0.0.1",
        )
    except Exception as exc:
        log.warning(
            "rip-complete in-process apply failed for job %s: %s",
            job_id, exc, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
        raise RipCallbackTransportError(
            f"rip-complete failed for job {job_id}: {exc}"
        ) from exc
    finally:
        db.close()


def _post_rip_verification_complete_callback(
    job_id: str,
    success: bool,
    *,
    ripped_files: dict | None = None,
    source_hashes: dict | None = None,
    error_reason: str | None = None,
    error_type: str | None = None,
    preview_detect_keys: list[str] | None = None,
    preview_detect_overrides: dict[str, str] | None = None,
) -> None:
    """Apply rip-verification-complete state directly via DB (#365 cleanup).

    Previously POSTed to ``/jobs/{job_id}/rip-verification-complete``.
    Now imports the API handler function and invokes it directly via a
    worker-owned DB session, eliminating the localhost HTTP roundtrip
    (the same fragility class that caused #378).

    The API handler has substantial side-effect logic — branch
    determination, missing-titles validation, Path A intercept, the
    ``start_transfer`` enqueue for the hit branch, ``preview_raw_titles``
    enqueue for the miss branch, ``StageState.rip_complete``,
    ``_maybe_advance_canonical_complete``. All of that runs unchanged
    here; only the transport hop is removed. Imports are local to the
    function body to avoid the circular ``workers.tasks`` ↔
    ``api.routers.jobs`` import.

    **Failure-raise semantics preserved.** The caller treats a raise as
    a hard rip-task failure that triggers retry/escalation. Previously
    that raise came from HTTP transport failures; now it comes from
    exceptions inside the handler (state violations, DB errors, etc.).
    Both are wrapped as ``RipCallbackTransportError`` so the worker's
    retry behaviour is unchanged.

    The legacy ``POST /jobs/{job_id}/rip-verification-complete``
    endpoint stays registered for one release as a safety net.
    """
    from api.routers.jobs import (
        rip_verification_complete_callback,
        RipVerificationCompleteRequest,
    )
    log.info("rip-verification-complete callback (in-process): job_id=%s success=%s", job_id, success)
    body_kwargs: dict[str, Any] = {"success": success}
    if success:
        body_kwargs["ripped_files"] = ripped_files or {}
        if source_hashes:
            body_kwargs["source_hashes"] = source_hashes
        if preview_detect_keys:
            body_kwargs["preview_detect_keys"] = preview_detect_keys
        if preview_detect_overrides:
            body_kwargs["preview_detect_overrides"] = preview_detect_overrides
    else:
        body_kwargs["error_reason"] = error_reason or "Rip verification failed"
        if error_type:
            body_kwargs["error_type"] = error_type
    try:
        body = RipVerificationCompleteRequest(**body_kwargs)
    except Exception as exc:
        raise RipCallbackTransportError(
            f"rip-verification-complete body construction failed for job {job_id}: {exc}"
        ) from exc

    db = database.SessionLocal()
    try:
        # Pass "127.0.0.1" for client_host to satisfy the localhost guard
        # in the handler. The check still exists on the HTTP endpoint
        # path for any in-flight legacy tasks; the in-process path is
        # trivially safe since we never expose this to the network.
        rip_verification_complete_callback(
            job_id=job_id,
            body=body,
            db=db,
            client_host="127.0.0.1",
        )
    except Exception as exc:
        # Preserve the legacy contract: a failure here must propagate as
        # RipCallbackTransportError so the worker fails the rip task.
        log.warning(
            "rip-verification-complete in-process apply failed for job %s: %s",
            job_id, exc, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
        raise RipCallbackTransportError(
            f"rip-verification-complete failed for job {job_id}: {exc}"
        ) from exc
    finally:
        db.close()


# Client-side throttling for rip-progress: only POST when 2s passed or phase changed or progress +5%
_rip_progress_last_send: dict[str, float] = {}
_rip_progress_last_pct: dict[str, int] = {}
RIP_PROGRESS_THROTTLE_SECONDS = 2
RIP_PROGRESS_THROTTLE_PCT = 5


def _post_rip_progress(
    job_id: str,
    *,
    rip_phase: Optional[str] = None,
    clear_rip_phase: bool = False,
    rip_progress: int | None = None,
    titles_completed: int | None = None,
    total_titles: int | None = None,
    current_title_id: str | None = None,
    current_title_number: int | None = None,
    current_title_progress: int | None = None,
    per_title_progress: dict | None = None,
    is_final: bool = False,
) -> None:
    """Apply rip-progress state directly via DB (#365 cleanup).

    Previously POSTed to ``/jobs/{job_id}/rip-progress`` on every
    progress tick. The worker and API live in the same process tree;
    the HTTP roundtrip bought us nothing (#378-class fragility).

    Throttling: the worker's existing client-side throttle
    (``_rip_progress_last_send`` + ``_rip_progress_last_pct``) is the
    sole gate now — the API used to have a duplicate server-side
    rate-limit (``_rip_progress_last_accept``) that this conversion
    bypasses. Behaviour-equivalent: the worker's throttle is the
    stricter of the two so production traffic is identical.

    is_final / clear_rip_phase / rip_phase changes bypass throttling
    so the API state catches up reliably at stage boundaries.

    Imports are local to the function body to avoid the circular
    ``workers.tasks`` ↔ ``api.routers.jobs`` import.

    Mirrors the transfer-progress conversion from PR #430.
    """
    body: dict[str, Any] = {}
    if clear_rip_phase:
        body["rip_phase"] = None
    elif rip_phase is not None:
        body["rip_phase"] = rip_phase
    if rip_progress is not None:
        body["rip_progress"] = rip_progress
    if titles_completed is not None:
        body["titles_completed"] = titles_completed
    if total_titles is not None:
        body["total_titles"] = total_titles
    if current_title_id is not None:
        body["current_title_id"] = current_title_id
    if current_title_number is not None:
        body["current_title_number"] = current_title_number
    if current_title_progress is not None:
        body["current_title_progress"] = current_title_progress
    if per_title_progress is not None:
        body["per_title_progress"] = per_title_progress
    if not body:
        return
    # Throttle: always send phase changes / final; for progress-only,
    # send if 2s elapsed or progress jumped by ≥5%.
    now = time.time()
    last_ts = _rip_progress_last_send.get(job_id, 0)
    last_pct = _rip_progress_last_pct.get(job_id, -1)
    pct = body.get("rip_progress")
    phase_changed = "rip_phase" in body
    if not phase_changed and not is_final and not clear_rip_phase:
        if pct is None:
            return
        if now - last_ts < RIP_PROGRESS_THROTTLE_SECONDS and (pct - last_pct) < RIP_PROGRESS_THROTTLE_PCT:
            return
    _rip_progress_last_send[job_id] = now
    if pct is not None:
        _rip_progress_last_pct[job_id] = pct

    from core.job_state import apply_job_state
    db = database.SessionLocal()
    try:
        job = crud.get_job(db, job_id)
        if not job:
            log.warning("rip-progress: job %s not found", job_id)
            return
        rip_state = getattr(job, "rip_state", None)
        if rip_state not in ("running", "pending"):
            # Stale callback after stage advanced — drop. Matches API
            # endpoint's 409 guard.
            return
        apply_job_state(db, job, updates=body, reason="rip_progress in-process", skip_context_changed=True)
        try:
            from core.progress_emitter import emit_job_progress_debounced
            progress_data = {
                "rip_progress": getattr(job, "rip_progress", 0),
                "rip_phase": getattr(job, "rip_phase", None),
                # #604 / #605: ship stage states alongside progress. This
                # in-process path calls apply_job_state with
                # skip_context_changed=True (line above), so the
                # authoritative context_changed event never fires. Without
                # rip_state / post_state / transfer_state here, the
                # frontend's local jobStatus stage fields go stale and the
                # spinner / button-label gates stay on the wrong value
                # until the user hard-refreshes.
                "rip_state": getattr(job, "rip_state", None),
                "post_state": getattr(job, "derived_post_state", None),
                "transfer_state": getattr(job, "transfer_state", None),
                "post_progress": getattr(job, "post_progress", 0),
                "transfer_progress": getattr(job, "transfer_progress", None),
                "per_title_progress": getattr(job, "per_title_progress", None),
                "current_title_progress": getattr(job, "current_title_progress", None),
                "current_title_id": getattr(job, "current_title_id", None),
                "current_title_number": getattr(job, "current_title_number", None),
            }
            emit_job_progress_debounced(job_id, progress_data)
        except Exception as exc:
            log.warning("rip-progress: failed to emit WS progress for job %s: %s", job_id, exc)
    except Exception as exc:
        log.warning("rip-progress in-process apply failed for job %s: %s", job_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _post_postprocess_complete_callback(
    job_id: str,
    success: bool,
    post_paths: dict | None = None,
    post_progress: int = 100,
    disc_payload_updates: dict | None = None,
    error_reason: str | None = None,
) -> None:
    """Apply postprocess-complete state directly via DB (#365 cleanup).

    Previously this function POSTed to the API's
    ``/jobs/{job_id}/postprocess-complete`` endpoint with retries. The
    worker and API run in the same process tree (single container in
    production, or both bound to localhost in dev) — the HTTP roundtrip
    bought us nothing beyond an extra failure mode (localhost guard
    rejecting non-127.0.0.1 callbacks, see #378). Now we open our own
    DB session and call ``StageState`` directly. Downstream effects
    (WebSocket emit via ``_emit_job_state_websocket_updates``,
    milestone notifications) all still fire because they're triggered
    inside ``apply_job_state``, not by the HTTP layer.

    The legacy ``POST /jobs/{job_id}/postprocess-complete`` endpoint
    stays registered on the API for one release as a safety net for
    any in-flight Celery tasks queued under the old worker image
    across a deploy window. A follow-up cleanup PR removes it.

    Idempotency: if the job's ``post_state`` is already
    ``"completed"`` or ``"failed"``, the call is a no-op.
    """
    from core.job_state import StageState
    db = database.SessionLocal()
    try:
        job = crud.get_job(db, job_id)
        if not job:
            logging.warning("postprocess-complete: job %s not found", job_id)
            return
        # Idempotency: refuse to re-terminate a job that's already there.
        # #365 — derived, not column.
        post_state = job.derived_post_state
        if post_state in ("completed", "failed"):
            return
        if success:
            StageState.postprocess_complete(
                db, job,
                post_paths=post_paths or {},
                post_progress=post_progress,
                disc_payload_updates=disc_payload_updates,
                reason="postprocess_complete in-process",
            )
        else:
            StageState.postprocess_failed(
                db, job,
                error_reason=error_reason or "Postprocess failed",
                reason="postprocess_complete in-process (failure)",
            )
    except Exception as exc:
        logging.warning("postprocess-complete in-process apply failed for job %s: %s", job_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


# ────────────────────────────────────────────────────────────────
@celery_app.task(bind=True, base=JobTask, name='rip_disc', acks_late=False)
@one_at_a_time
def rip_disc(
    self,
    job_id: str,
    disc_num: str,
    mount_point: str,
    mode: str = 'copy',
    out_dir: str | None = None,
    rip_request_id: str | None = None
):
    """
    Celery task to rip a disc.  Uses FileLock to ensure only one rip at a time
    and parses MakeMKV's "NN%|…" progress lines to update job.rip_progress.
    """
    # Log task start with comprehensive context
    import traceback
    call_stack = ''.join(traceback.format_stack()[-3:-1])  # Last 2 frames (excluding this one)
    log.info(
        "rip_disc TASK STARTED celery_task_id=%s job_id=%s disc_num=%s mount_point=%s mode=%s out_dir=%s rid=%s call_stack=%s",
        self.request.id, job_id, disc_num, mount_point, mode, out_dir, rip_request_id,
        call_stack.replace('\n', ' | ')
    )
    func_logger = get_logger("workers.tasks", "rip_disc")
    func_logger.info(
        "rip_disc task started celery_task_id=%s job_id=%s disc_num=%s mount_point=%s mode=%s rid=%s",
        self.request.id, job_id, disc_num, mount_point, mode, rip_request_id
    )
    log.info("rip_disc task started job_id=%s celery_task_id=%s disc_num=%s mount_point=%s", job_id, self.request.id, disc_num, mount_point)
    # pre-compile regexes for progress: PRGV from makemkv and tqdm-style “4%|”
    prgv_re = re.compile(r'^PRGV:(\d+),(\d+),(\d+)')
    tqdm_re = re.compile(r'^\s*(\d{1,3})%\|')
    title_re = re.compile(r'^Title\s+#(\d+)', re.IGNORECASE)
    added_re = re.compile(r'^MSG:3307,\d+,\d+,"File (\d{5}\.(?:mpls|m2ts)) was added as title #(\d+)"')
    skipped_re = re.compile(r'^MSG:(3309|3025),\d+,\d+,"(?:Title|File) #?(\d{5}\.(?:mpls|m2ts)) .*skipped"')
    tcount_re = re.compile(r'^TCOUNT:(\d+)')
    saving_titles_re = re.compile(r'Saving\s+(\d+)\s+titles', re.IGNORECASE)
    prgt_re = re.compile(r'^PRGT:\d+,\d+,"(.+)"')
    prgc_re = re.compile(r'^PRGC:(\d+),(\d+),"(.+)"')

    # Track map of title_id -> filename so previews can be keyed by title_id.
    title_filename_map: dict[str, str] = {}

    # #562 PR 5: dropped the ``ensure_makemkv_index_for_mount`` re-resolve.
    # ``_makemkv_source_spec()`` already targets ``dev:{mount_point}``, so
    # the MakeMKV index is no longer load-bearing here — it was kept only
    # for cache-consistency logging. The re-resolve called ``disc:9999``
    # (when needed), which is exactly the contention this cluster removes.

    rip_op_lock = None  # Set when rip lock is acquired; released in finally
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            return

        # Ensure disc relationship is loaded for accessing disc.disc_info
        if job.disc_id:
            from api import models
            job.disc = db.query(models.Disc).filter(models.Disc.id == job.disc_id).first()

        # Lock acquisition removed - concurrency is now handled by DriveGatekeeper using Celery+PID checks
        # The gatekeeper should have already prevented duplicate rips before dispatching this task

        try:
            # Check worker health at task start (helps diagnose worker crashes)
            try:
                health = check_worker_health()
                if health['status'] != 'healthy':
                    func_logger.warning(
                        f"Worker health check at task start: status={health['status']}, "
                        f"issues={health['issues']}. This may indicate previous worker crashes."
                    )
            except Exception as health_exc:
                func_logger.debug(f"Could not check worker health at task start: {health_exc}")
            
            # Duplicate prevention is the API's responsibility: can_start_rip/start_rip decide whether
            # to queue a task. The worker only enforces idempotency (skip if job already failed/completed/skipped)
            # and handles recovery (orphaned makemkvcon process when job is not in running state).

            current_job_status = getattr(job, "job_status", None)
            current_rip_state = getattr(job, "rip_state", None)

            # Recovery: job not in running state but makemkvcon is running (e.g. worker died after starting rip)
            if current_rip_state not in ("running", "completed", "skipped") and current_job_status != "failed":
                from core.utils import _find_makemkvcon_process_for_disc
                pid, cmdline = _find_makemkvcon_process_for_disc(mount_point)
                if pid:
                    logging.info(
                        "Found running makemkvcon for job %s (PID %s) but rip_state is %s - using recover_running_rip",
                        job_id, pid, current_rip_state
                    )
                    func_logger.debug("Dispatching recover_running_rip job_id=%s disc_num=%s pid=%s", job_id, disc_num, pid)
                    try:
                        StageState.rip_started(db, job, reason="recovery")
                    except StateViolation as sv:
                        func_logger.warning("rip_started for recovery skipped (state violation): %s", sv)
                    from workers.tasks import recover_running_rip
                    recover_running_rip.delay(job_id, pid, cmdline)
                    return

            # Check if job is already in a terminal state (failed/completed) - if so, skip
            # This prevents state violation errors when duplicate tasks are dispatched
            if current_job_status == "failed" or current_rip_state == "failed":
                func_logger.warning(
                    "Skipping rip_disc task for job %s: job is already failed (job_status=%s, rip_state=%s). "
                    "This is likely a duplicate task that should have been prevented by the gatekeeper.",
                    job_id, current_job_status, current_rip_state
                )
                self.add_log(job, db, f"Skipping duplicate rip task: job is already failed (job_status={current_job_status}, rip_state={current_rip_state})")
                log.debug("rip_disc skipped job_id=%s reason=job_already_failed", job_id)
                return
            
            if current_job_status == "completed":
                func_logger.warning(
                    "Skipping rip_disc task for job %s: job is already completed (job_status=%s, rip_state=%s).",
                    job_id, current_job_status, current_rip_state
                )
                self.add_log(job, db, f"Skipping rip task: job is already completed")
                log.debug("rip_disc skipped job_id=%s reason=job_already_completed", job_id)
                return

            # Idempotency: if rip already completed/skipped (e.g. redelivery), exit successfully without failure callback
            if current_rip_state in ("completed", "skipped"):
                func_logger.info(
                    "Skipping rip_disc task for job %s: rip already completed/skipped (rip_state=%s).",
                    job_id, current_rip_state
                )
                log.debug("rip_disc skipped job_id=%s reason=rip_already_completed_or_skipped", job_id)
                return

            # One operation per drive: acquire rip lock before any destructive work.
            # Lock by mount_point (stable, #542) — disc_num is the volatile
            # MakeMKV index which renumbers across hot-plug.
            rip_op_lock = acquire_operation_lock(mount_point, OPERATION_RIP)
            if rip_op_lock is None:
                snap = get_disc_lock_debug_snapshot(mount_point)
                active_ops = snap.get("active_operations") or []
                extra = ""
                if active_ops:
                    extra = f" Active operations: {', '.join(active_ops)}."
                if snap.get("duplicate_rip_suspected"):
                    extra += (
                        " Another rip holds the disc lock or rip is active — often a second rip_disc "
                        "while the first is still preparing (makemkvcon may not appear in ps yet)."
                    )
                elif snap.get("other_op_blocking_rip"):
                    extra += " A hash/info (or similar) operation is active on this disc."
                msg = f"Drive busy: another operation in progress for disc {disc_num}.{extra}"
                rip_pid = getattr(job, "rip_pid", None)
                from core.drive_gatekeeper import is_pid_alive
                if (
                    current_rip_state == "running"
                    and rip_pid is not None
                    and is_pid_alive(rip_pid)
                ):
                    # Redelivery safety net: another instance is already ripping; don't call failure callback.
                    func_logger.info(
                        "Cannot acquire rip lock for job %s: job has active rip_pid %s (redelivery); skipping failure callback",
                        job_id, rip_pid,
                    )
                    return
                func_logger.warning(
                    "Cannot acquire rip lock job_id=%s disc_num=%s celery_task_id=%s rid=%s msg=%s snapshot=%s",
                    job_id,
                    disc_num,
                    self.request.id,
                    rip_request_id,
                    msg,
                    json.dumps(snap, default=str),
                )
                self.add_log(job, db, msg)
                _post_rip_complete_callback(
                    str(job.id),
                    success=False,
                    error_reason=msg,
                    error_type="drive_busy",
                    debug=snap,
                )
                return

            # Progress/observability only; API already set rip_state=running via rip_started when enqueueing.
            _post_rip_progress(str(job.id), rip_progress=0, rip_phase='copy')

            try:
                # Log immediately so the UI shows activity even if setup fails later.
                self.add_log(job, db, "Worker accepted job; preparing output paths")

                # Do not wipe job dir: only ensure_layout() (create dirs if missing) for idempotency.
                paths = JobPaths.from_job(job, out_dir)
                try:
                    paths.ensure_layout()
                except PermissionError as exc:
                    msg = f"Cannot create output root {paths.root}: {exc}"
                    self.add_log(job, db, msg)
                    self.set_status(job, db, job_status='failed', error_reason=msg)
                    return
                except Exception as exc:
                    msg = f"Failed to prepare output root {paths.root}: {exc}"
                    self.add_log(job, db, msg)
                    self.set_status(job, db, job_status='failed', error_reason=msg)
                    return
                storage_root = paths.jobs_root
                job_root = paths.root
                rip_workdir = paths.raw
                post_log_path = paths.metadata / "post-processing.log"
                # ensure enough free space before starting
                try:
                    usage = shutil.disk_usage(storage_root)
                    if usage.free < MIN_OUTPUT_FREE_BYTES:
                        msg = f"Insufficient free space in output dir {job_root} (free {usage.free} bytes, need at least {MIN_OUTPUT_FREE_BYTES})"
                        self.add_log(job, db, msg)
                        _post_rip_complete_callback(str(job.id), success=False, error_reason=msg)
                        return
                except Exception as exc:
                    self.add_log(job, db, f"WARNING: could not check free space on {job_root}: {exc}")

                def log_file(line: str):
                    try:
                        with open(post_log_path, "a", encoding="utf-8") as fh:
                            fh.write(line.rstrip() + "\n")
                    except Exception:
                        pass

                def write_summary(folder: Path, status: str, error: str | None = None, file_paths: dict | None = None, final_hashes: dict | None = None):
                    try:
                        summary = {
                            "job_id": str(job.id),
                            "disc_num": disc_num,
                            "mount_point": mount_point,
                            "mode": mode,
                            "status": status,
                            "progress": job.rip_progress,
                            "titles_completed": titles_completed,
                            "total_titles": total_titles,
                            "current_title_id": current_title_id,
                            "current_title_number": current_title_number,
                            "per_title_progress": per_title_progress,
                            "created_at": job.created_at.isoformat() if job.created_at else None,
                            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
                            "file_paths": file_paths,  # Generic - can be ripped_files or post_paths
                            "final_hashes": final_hashes,
                            "error": error,
                        }
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / "job_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
                    except Exception:
                        pass

                # estimate titles to rip (title_id-only)
                titles_map = None
                # Try disc.disc_info first (new location), fallback to job.disc_payload for backward compatibility
                if hasattr(job, "disc") and job.disc and job.disc.disc_info:
                    titles_map = job.disc.disc_info.get("titles") or job.disc.disc_info.get("titles_map")
                if not titles_map and job.disc_payload:
                    titles_map = job.disc_payload.get("titles") or job.disc_payload.get("tracks")
                title_id_maps = _build_title_id_maps(job, job.disc_payload or {})
                id_to_title = title_id_maps.get("id_to_title", {})
                source_to_id = title_id_maps.get("source_to_id", {})
                index_to_id = title_id_maps.get("index_to_id", {})
                ambiguous_source_files = title_id_maps.get("ambiguous_source_files") or set()

                title_keys: list[str] = []
                if id_to_title:
                    def _title_sort_key(t: Any) -> tuple[int, int]:
                        index_val = getattr(t, "index", None)
                        order_val = getattr(t, "order_index", None)
                        index_key = index_val if isinstance(index_val, int) else 9999
                        order_key = order_val if isinstance(order_val, int) else 9999
                        return (index_key, order_key)

                    titles_sorted = sorted(id_to_title.values(), key=_title_sort_key)
                    title_keys = [str(t.id) for t in titles_sorted if getattr(t, "id", None)]
                total_titles = max(1, len(title_keys) if title_keys else 1)
                titles_completed = 0
                prev_done = 0
                prev_total = 0
                current_title_id: str | None = title_keys[0] if title_keys else None
                current_title_number: int | None = 1 if title_keys else None
                per_title_progress: dict[str, int] = {k: 0 for k in title_keys}
                completed_titles = set((job.disc_payload or {}).get("completed_titles") or [])
                preview_tracks_enqueued = set()
                try:
                    existing_previews = (job.disc_payload or {}).get("previews") or {}
                    tracks_existing = existing_previews.get("tracks") if isinstance(existing_previews, dict) else {}
                    if isinstance(tracks_existing, dict):
                        preview_tracks_enqueued = set(tracks_existing.keys())
                except Exception:
                    preview_tracks_enqueued = set()
                discovered_titles = list((job.disc_payload or {}).get("discovered_titles") or [])
                skipped_titles = set((job.disc_payload or {}).get("skipped_titles") or [])
                title_index_offset: int | None = None  # Makemkv sometimes numbers titles from 0
                last_overall_pct: int = 0
                copy_phase_started = False
                last_max_val: int | None = None
                ripped_paths: dict[str, str] = {}
                # Initialize debounced commit helper for ripped_files
                ripped_files_committer = DebouncedRippedFilesCommit(job, db, commit_threshold=3, time_threshold=2.0)
                # persist initial counts
                self.set_status(job, db, total_titles=total_titles, titles_completed=0, current_title_id=current_title_id)
            except Exception as exc:
                from core.utils import _is_makemkvcon_running_for_disc
                tb = traceback.format_exc(limit=8)
                self.add_log(job, db, f"ERROR during rip setup: {tb}")
                # CRITICAL: Don't mark as failed if already failed
                current_status = getattr(job, "job_status", None)
                if current_status != "failed":
                    self.set_status(job, db, job_status='failed', error_reason=str(exc))
                else:
                    logging.warning(f"Job {job.id} already marked as failed during setup, skipping status update")
                # DISABLED: Do not clean up files on failure - preserve them for user inspection
                # User must manually retry via frontend, which will create a new job
                logging.info(f"Job {job.id} failed during setup - files preserved. User must manually retry to create a new job.")
                return

            title_output_map: dict[str, str] = {}

            def _compute_title_output_map() -> dict[str, str]:
                nonlocal title_output_map, ripped_paths, ripped_files_committer
                try:
                    mkv_files = sort_makemkv_mkv_filenames([p.name for p in rip_workdir.glob("*.mkv")])
                    if not mkv_files:
                        return title_output_map
                    # Match each MKV to its disc_title by parsing the `_tNN` index
                    # from the filename and looking up `disc_titles.index`. For
                    # selective rips (Path A) the file list is sparse — only the
                    # ripped indices appear — so a positional zip of titles-vs-
                    # files silently mis-aligns rows. The parse-by-index path is
                    # correct for both full and selective rips.
                    from core.makemkv_output import map_mkv_filenames_to_title_ids
                    disc_titles_for_map = list(getattr(getattr(job, "disc", None), "titles", None) or [])
                    mapped: dict[str, str] = {}
                    if disc_titles_for_map:
                        mapped = map_mkv_filenames_to_title_ids(mkv_files, disc_titles_for_map)
                    if not mapped:
                        # Fallback: positional zip if we have no titles loaded or
                        # the parse failed for every file. Keeps the historical
                        # behaviour as a last resort so a missing disc relationship
                        # doesn't drop ripped_files entirely.
                        ordered_titles = list(title_keys)
                        if not ordered_titles:
                            return title_output_map
                        for idx, t_key in enumerate(ordered_titles):
                            if idx < len(mkv_files):
                                mapped[t_key] = mkv_files[idx]
                    title_output_map = mapped
                    for k, v in mapped.items():
                        ripped_paths.setdefault(k, v)
                        # Update ripped_files incrementally if file exists
                        if rip_workdir and (rip_workdir / v).exists():
                            ripped_files_committer.add(k, v)
                except Exception:
                    pass
                return title_output_map

            def log_hook(line: str):
                # append every line to the job's logs
                self.add_log(job, db, line)
                nonlocal total_titles, title_keys, per_title_progress, completed_titles, current_title_id, current_title_number, titles_completed, prev_done, prev_total, title_index_offset, copy_phase_started, last_max_val, preview_tracks_enqueued, title_output_map, ripped_paths

                def normalize_title_idx(raw: int) -> int:
                    """Makemkv may number titles starting at 0; normalize to a zero-based index."""
                    nonlocal title_index_offset
                    if title_index_offset is None:
                        title_index_offset = 0 if raw == 0 else 1
                    return max(0, raw - title_index_offset)

                def update_progress(track_pct: int, overall_pct_from_prgv: int | None = None) -> None:
                    """Update per-title and overall progress from the current track percent."""
                    nonlocal total_titles, titles_completed, current_title_id, current_title_number, per_title_progress, completed_titles, last_overall_pct, ripped_files_committer, ripped_paths
                    track_pct = max(0, min(100, track_pct))

                    if not current_title_id and title_keys:
                        try:
                            fallback_idx = min(titles_completed, len(title_keys) - 1)
                            current_title_id = title_keys[fallback_idx]
                            current_title_number = fallback_idx + 1
                        except Exception:
                            current_title_id = None
                            current_title_number = None

                    if current_title_id and current_title_id not in per_title_progress:
                        per_title_progress[current_title_id] = 0

                    total_slots = max(total_titles, len(title_keys), len(per_title_progress) or 0, 1)
                    if title_keys:
                        for k in title_keys:
                            per_title_progress.setdefault(k, 0)

                    total_titles = max(total_titles, total_slots)

                    if current_title_id:
                        prev_pct = per_title_progress.get(current_title_id, 0)
                        per_title_progress[current_title_id] = max(prev_pct, track_pct)

                    # recompute completed titles from the map to avoid double-counting
                    completed_titles.update(k for k, v in per_title_progress.items() if v >= 100)
                    completed_count = len(completed_titles)
                    titles_completed = min(total_titles, max(titles_completed, completed_count))
                    
                    # Track ripped_files incrementally for newly completed titles
                    newly_completed_for_files = [k for k in completed_titles if k not in getattr(ripped_files_committer, '_tracked_titles', set())]
                    if newly_completed_for_files and rip_workdir:
                        # Ensure we have title_output_map computed
                        if not title_output_map:
                            _compute_title_output_map()
                        for title_id in newly_completed_for_files:
                            # Try to get rel_path from ripped_paths, title_output_map, or compute from file
                            rel_path = ripped_paths.get(title_id) or title_output_map.get(title_id)
                            if not rel_path:
                                # Try to find the file by scanning
                                try:
                                    mkv_files = sort_makemkv_mkv_filenames([p.name for p in rip_workdir.glob("*.mkv")])
                                    if title_keys and title_id in title_keys:
                                        idx = title_keys.index(title_id)
                                        if idx < len(mkv_files):
                                            rel_path = mkv_files[idx]
                                except Exception:
                                    pass
                            
                            # If we have a rel_path and the file exists, add to ripped_files
                            if rel_path and (rip_workdir / rel_path).exists():
                                ripped_files_committer.add(title_id, rel_path)
                                ripped_paths[title_id] = rel_path
                                # Track that we've processed this title
                                if not hasattr(ripped_files_committer, '_tracked_titles'):
                                    ripped_files_committer._tracked_titles = set()
                                ripped_files_committer._tracked_titles.add(title_id)

                    in_progress_fraction = 0.0
                    if current_title_id and current_title_id not in completed_titles:
                        in_progress_fraction = track_pct / 100.0

                    if not current_title_id:
                        in_progress_fraction = track_pct / 100.0

                    if overall_pct_from_prgv is not None:
                        overall_pct = max(0, min(100, overall_pct_from_prgv))
                    else:
                        overall_pct = int(((titles_completed + in_progress_fraction) / max(total_titles, 1)) * 100)
                        overall_pct = max(0, min(100, overall_pct))

                    # Enforce monotonic overall progress to avoid regressions from noisy PRGV.
                    overall_pct = max(last_overall_pct, overall_pct)
                    last_overall_pct = overall_pct
                    # API rip_progress = PRGV overall progress (0-100). Verification phase still uses RIP_PROGRESS_COPY_END..100 elsewhere.
                    payload = {**(job.disc_payload or {}), "completed_titles": list(completed_titles)}

                    _post_rip_progress(
                        str(job.id),
                        rip_progress=overall_pct,
                        current_title_progress=track_pct,
                        titles_completed=titles_completed,
                        total_titles=total_titles,
                        current_title_id=current_title_id,
                        current_title_number=current_title_number,
                        per_title_progress=per_title_progress or None,
                    )
                    self.set_status(job, db, disc_payload=payload)
                    # queue previews for newly completed titles
                    try:
                        newly_completed = [k for k in completed_titles if k not in preview_tracks_enqueued]
                        # If we have final_paths from gather_final_outputs, prefer those filenames.
                        final_paths = (job.disc_payload or {}).get("final_paths") or {}
                        title_outputs = (job.disc_payload or {}).get("title_output_map") or {}
                        preview_maps = _build_title_id_maps(job, job.disc_payload or {})
                        if not final_paths:
                            _compute_title_output_map()
                        for tk in newly_completed:
                            if tk in preview_tracks_enqueued:
                                continue
                            rel = title_outputs.get(tk)
                            if not rel and title_outputs:
                                mapped = title_outputs.get(tk)
                                if mapped and mapped in final_paths:
                                    rel = final_paths.get(mapped)
                            if not rel:
                                rel = final_paths.get(tk) or title_output_map.get(tk) or ripped_paths.get(tk)
                            if rel and rip_workdir and (rip_workdir / rel).exists():
                                payload_preview = deepcopy(job.disc_payload) if isinstance(job.disc_payload, dict) else {}
                                resolved_title_id = _resolve_preview_title_id(tk, rel, preview_maps)
                                canonical_key = resolved_title_id or tk
                                payload_preview = _ensure_previews_map(payload_preview, {canonical_key: rel}, preview_maps)
                                try:
                                    previews_state = payload_preview.get("previews") if isinstance(payload_preview, dict) else {}
                                    tracks_state = previews_state.get("tracks") if isinstance(previews_state, dict) else {}
                                    if resolved_title_id and isinstance(tracks_state, dict) and canonical_key in tracks_state:
                                        entry = tracks_state.get(canonical_key)
                                        if isinstance(entry, dict):
                                            entry["title_id"] = str(resolved_title_id)
                                except Exception:
                                    pass
                                self.set_status(job, db, disc_payload=payload_preview)
                                if getattr(disc, "label_required", False):
                                    # chain_detect=False: don't run detection mid-rip —
                                    # analysis under rip I/O contention false-positives
                                    # and auto-ignores real titles (#518). The
                                    # post-verification preview pass runs the one
                                    # clean detect for the whole job.
                                    preview_raw_titles.delay(
                                        str(job.id), [canonical_key],
                                        rel_path_overrides={canonical_key: rel} if rel else None,
                                        chain_detect=False,
                                    )
                                preview_tracks_enqueued.add(tk)
                                preview_tracks_enqueued.add(canonical_key)
                    except Exception as exc:
                        self.add_log(job, db, f"Per-title preview queue failed: {exc}")

                # try to parse a “NN%” or PRGV in the line
                pct: int | None = None

                m_prgc = prgc_re.match(line)
                if m_prgc:
                    stage_text = m_prgc.group(3) or ""
                    stage_lower = stage_text.lower()
                    try:
                        raw_track = int(m_prgc.group(2))
                    except ValueError:
                        raw_track = None

                    # MakeMKV emits PRGC 5057/5017 per title; treat “analyzing…” as the
                    # boundary between tracks so we can align progress with the right slot.
                    if "analyzing seamless segments" in stage_lower:
                        # Transition to the next title; treat the prior one as done even if MakeMKV
                        # never emitted PRGV lines (tiny tracks sometimes skip progress output).
                        # treat the prior title as done when a new analysis begins
                        if current_title_id:
                            if copy_phase_started or last_max_val or prev_done > 0:
                                per_title_progress[current_title_id] = max(per_title_progress.get(current_title_id, 0), 100)
                                completed_titles.add(current_title_id)
                                titles_completed = max(titles_completed, len(completed_titles))
                        # Do not clear copy_phase_started here: PRGV between titles was wrongly ignored,
                        # leaving rip_progress at 0% while makemkv_progress.log showed activity.
                        prev_done = 0
                        prev_total = 0
                        last_max_val = None

                        if raw_track is not None:
                            title_num = normalize_title_idx(raw_track)
                            if 0 <= title_num < len(title_keys):
                                total_titles = max(total_titles, len(title_keys), title_num + 1)
                                current_title_id = title_keys[title_num]
                                current_title_number = title_num + 1
                                per_title_progress.setdefault(current_title_id, 0)
                                prev_done = 0
                                prev_total = 0
                                payload = {**(job.disc_payload or {}), "completed_titles": list(completed_titles)}
                                self.set_status(
                                    job,
                                    db,
                                    total_titles=total_titles,
                                    titles_completed=titles_completed,
                                    current_title_id=current_title_id,
                                    current_title_number=current_title_number,
                                    per_title_progress=per_title_progress or None,
                                    disc_payload=payload,
                                )

                    if "saving to mkv file" in stage_lower:
                        copy_phase_started = True
                        if raw_track is not None:
                            title_num = normalize_title_idx(raw_track)
                            if 0 <= title_num < len(title_keys):
                                current_title_id = title_keys[title_num]
                                current_title_number = title_num + 1
                                per_title_progress.setdefault(current_title_id, 0)
                    return

                m_saving = saving_titles_re.search(line)
                if m_saving:
                    try:
                        count = int(m_saving.group(1))
                        if count > 0:
                            total_titles = max(total_titles, len(title_keys), count)
                    except ValueError:
                        pass
                m_tcount = tcount_re.match(line)
                if m_tcount:
                    try:
                        count = int(m_tcount.group(1))
                        if count > 0:
                            total_titles = max(total_titles, len(title_keys), count)
                            # surface the count immediately so the UI can render track rows
                            self.set_status(
                                job,
                                db,
                                total_titles=total_titles,
                                per_title_progress=per_title_progress or None,
                                titles_completed=titles_completed,
                                current_title_id=current_title_id,
                                current_title_number=current_title_number,
                                disc_payload=job.disc_payload,
                            )
                    except ValueError:
                        pass

                m_title = title_re.match(line)
                if m_title:
                    try:
                        raw_num = int(m_title.group(1))
                        title_num = normalize_title_idx(raw_num)
                        if 0 <= title_num < len(title_keys):
                            current_title_id = title_keys[title_num]
                            current_title_number = title_num + 1
                    except ValueError:
                        pass
                m_prgv = prgv_re.match(line)
                if m_prgv:
                    try:
                        # Only start tracking progress once the copy phase begins (PRGC “Saving to MKV file” seen).
                        if not copy_phase_started:
                            return
                        # PRGV:current,total,max — current=current title progress, total=total disc progress (0–max), max=constant denominator
                        cur_val, total_val, max_val = map(int, m_prgv.groups())
                        if max_val > 0:
                            last_max_val = max_val
                            track_pct = int(cur_val * 100 / max_val)
                            # Integer ceil so small total_val does not stick at 0% for ages (65536 scale).
                            overall_pct = max(
                                0,
                                min(100, (total_val * 100 + max_val - 1) // max_val),
                            )
                            # detect title transition when the per-title counter resets but total keeps increasing
                            if cur_val < prev_done and total_val >= prev_total and total_titles > 1:
                                titles_completed = min(total_titles - 1, titles_completed + 1)
                                if current_title_id:
                                    per_title_progress[current_title_id] = max(per_title_progress.get(current_title_id, 0), 100)
                                    completed_titles.add(current_title_id)
                                if title_keys:
                                    try:
                                        next_idx = min(titles_completed, len(title_keys) - 1)
                                        current_title_id = title_keys[next_idx]
                                        current_title_number = next_idx + 1
                                    except Exception:
                                        current_title_id = None
                            prev_done, prev_total = cur_val, total_val
                            update_progress(track_pct, overall_pct_from_prgv=overall_pct)
                            return
                    except ValueError:
                        pct = None
                if pct is None:
                    m_tqdm = tqdm_re.match(line)
                    if m_tqdm:
                        try:
                            pct = int(m_tqdm.group(1))
                        except ValueError:
                            pct = None
                    if pct is not None:
                        try:
                            update_progress(pct)
                        except Exception:
                            pass

                # Track when copy actually begins so we ignore PRGV noise from earlier info/analysis stages.
                m_prgt = prgt_re.match(line)
                if m_prgt:
                    # Keep PRGT purely informational; PRGV should only count after PRGC “Saving to MKV file”.
                    stage_text = m_prgt.group(1).lower()
                    # Fallback: PRGT "Saving all titles to MKV files" appears before PRGC; use it to start reporting PRGV progress.
                    if "saving all titles" in stage_text or "saving to mkv" in stage_text:
                        copy_phase_started = True
                m_add = added_re.match(line)
                if m_add:
                    fn, tid = m_add.groups()
                    # map the discovered filename to a title slot if we have one
                    title_id = None
                    try:
                        idx = normalize_title_idx(int(tid))
                        # Filename is stable; index can collide across titles after rescans.
                        title_id = source_to_id.get(fn)
                        if not title_id and idx >= 0:
                            title_id = index_to_id.get(str(idx)) or index_to_id.get(str(idx + 1))
                        if title_id:
                            title_id = str(title_id)
                            if title_id not in per_title_progress:
                                per_title_progress[title_id] = 0
                            if copy_phase_started:
                                current_title_id = title_id
                                current_title_number = idx + 1
                            title_filename_map[title_id] = fn
                    except Exception:
                        pass

                    if title_id and title_id not in discovered_titles:
                        discovered_titles.append(title_id)
                    payload = {
                        **(job.disc_payload or {}),
                        "discovered_titles": discovered_titles,
                        "skipped_titles": list(skipped_titles),
                        "title_filename_map": title_filename_map,
                    }
                    payload = _backfill_preview_title_ids(payload)
                    self.set_status(
                        job,
                        db,
                        disc_payload=payload,
                        per_title_progress=per_title_progress or None,
                        total_titles=total_titles,
                        current_title_id=current_title_id,
                        current_title_number=current_title_number,
                    )
                    return
                m_skip = skipped_re.match(line)
                if m_skip:
                    fn = m_skip.group(2)
                    title_id = None
                    if fn not in ambiguous_source_files:
                        title_id = source_to_id.get(fn)
                    if title_id:
                        skipped_titles.add(str(title_id))
                    payload = {**(job.disc_payload or {}), "discovered_titles": discovered_titles, "skipped_titles": list(skipped_titles)}
                    self.set_status(job, db, disc_payload=payload)
                    return

            try:
                # 1) gather disc metadata (use Disc Manager, fallback to job payload)
                disc = Disc(disc_num, mount_point)
                disc.log_fn = log_file
            
                # #562 PR 5: rip task is cache-pure. The API-level gate at
                # ``api.routers.jobs.start_rip`` returns 409
                # ``disc_scan_in_progress`` on cache miss and enqueues
                # ``discinfo_scan`` — we should NEVER reach here without
                # either ``job.disc_payload.disc_hash`` or a populated cache
                # entry. If we do, the disc-cache was wiped between gate and
                # rip dispatch (e.g. by an udev event) — fail loudly rather
                # than fall back to ``disc.load_db_info(allow_reentrant=True)``,
                # which would open the disc inline and race a sibling drive's
                # in-flight rip (MSG:5010 root cause).
                disc_info = None
                if job.disc_payload and job.disc_payload.get("disc_hash"):
                    disc_info = job.disc_payload
                else:
                    func_logger.debug(
                        "About to call get_disc_info in rip_disc job_id=%s disc_num=%s mount_point=%s",
                        job_id, disc_num, mount_point,
                    )
                    try:
                        disc_info = get_disc_info(str(disc_num), mount_point, refresh=False)
                    except Exception as exc:
                        msg = (
                            f"Rip task reached cache-miss path despite #562 PR 5 gate "
                            f"(disc_num={disc_num} mount_point={mount_point}): {exc}. "
                            f"The disc-info cache was likely wiped between rip-start and "
                            f"task dispatch (udev event during dispatch?). Failing this "
                            f"rip cleanly — retry from the UI."
                        )
                        self.add_log(job, db, f"ERROR: {msg}")
                        raise RuntimeError(msg) from exc
            
                # Populate disc object with info if we got it from Disc Manager
                if disc_info:
                    disc.disc_hash = disc_info.get("disc_hash")
                    disc.movie_name = disc_info.get("movie_name")
                    disc.release_image = disc_info.get("release_image")
                    disc.disc_slug = disc_info.get("disc_slug")
                    disc.resolution = disc_info.get("resolution")
                    disc.disc_format = disc_info.get("disc_format")
                    disc.title_type = disc_info.get("title_type")
                    disc.disc_group = disc_info.get("disc_group")
                    disc.group_type = disc_info.get("group_type")
                    disc.release_year = disc_info.get("release_year")
                    disc.release_date = disc_info.get("release_date")
                    disc.original_year = disc_info.get("original_year")
                    disc.original_release_date = disc_info.get("original_release_date")
                    disc.tmdb_id = disc_info.get("tmdb_id")
                    disc.tmdb_type = disc_info.get("tmdb_type")
                    disc.production_year = disc_info.get("production_year")
                    disc.label_required = disc_info.get("label_required", False)
                    disc.titles = disc_info.get("titles", {})
                    disc.db_mapping = disc_info.get("tracks", {})
                    disc.info_log = disc_info.get("info_log") or disc_info.get("raw_info_log")
                    disc.raw_db_query = disc_info.get("raw_db_query")

                # Raw TheDiscDB response for raw/disc_db_query.json (labeling audit). Payload / DB
                # cache may omit it; fetch once before rip when we have a hash.
                if disc.raw_db_query is None and disc.disc_hash:
                    try:
                        disc.raw_db_query = retrieve_discdb_data(disc.disc_hash)
                    except Exception as exc:
                        func_logger.warning(
                            "Could not fetch raw DiscDB payload for disc_db_query.json job_id=%s: %s",
                            job_id,
                            exc,
                        )
                        disc.raw_db_query = {"error": str(exc)}

                # 2) perform the rip; MakeMKV prints "NN%|…" to stdout
                self.add_log(job, db, f"Starting rip into temp dir {rip_workdir}")
                log_file(f"Starting rip into temp dir {rip_workdir}")
                _apply_release_title(disc, job)
                # Selective-rip path (Phase 2 Path A): when job.rip_set is set,
                # disc.rip() loops per-title instead of running `mkv DEV all OUT`.
                # Default path (rip_set None or empty) preserves today's all-mode.
                rip_set = getattr(job, "rip_set", None)
                if rip_set is not None and not isinstance(rip_set, list):
                    rip_set = None
                func_logger.debug("About to call disc.rip() job_id=%s disc_num=%s mount_point=%s mode=%s rip_workdir=%s rip_set=%s",
                                job_id, disc_num, mount_point, mode, str(rip_workdir), rip_set)
                # #541: persist rip_pid the moment makemkvcon is spawned so
                # restart-during-rip recovery can validate the live job.
                # disc.rip() runs synchronously for hours; without this callback
                # rip_pid would not be visible until AFTER the rip completed.
                def _persist_rip_pid_early(pid: int) -> None:
                    try:
                        with db_session() as inner_db:
                            inner_job = crud.get_job(inner_db, job_id)
                            if inner_job is not None:
                                inner_job.rip_pid = pid
                                inner_db.commit()
                                func_logger.info(
                                    "Persisted rip_pid=%s for job %s at spawn time",
                                    pid, job_id,
                                )
                    except Exception as cb_exc:
                        func_logger.warning(
                            "Failed to persist rip_pid=%s for job %s: %s",
                            pid, job_id, cb_exc,
                        )

                try:
                    makemkv_pid = disc.rip(
                        rip_workdir, mode,
                        log_hook=log_hook,
                        rip_set=rip_set,
                        pid_callback=_persist_rip_pid_early,
                    )
                    # Store PID on job for tracking (post-rip; harmless if the
                    # early-callback above already persisted it).
                    if makemkv_pid:
                        self.set_status(job, db, rip_pid=makemkv_pid)
                        func_logger.info("Stored makemkv PID %s for job %s", makemkv_pid, job_id)
                        try:
                            self.update_state(
                                state="PROGRESS",
                                meta={"rip_pid": makemkv_pid, "job_id": job_id},
                            )
                        except Exception as state_exc:
                            func_logger.debug(
                                "rip_disc update_state(PROGRESS) skipped job_id=%s: %s",
                                job_id,
                                state_exc,
                            )
                    func_logger.debug("disc.rip() completed job_id=%s disc_num=%s", job_id, disc_num)
                except OSError as exc:
                    # Check if this is a "No space left on device" error
                    if exc.errno == 28:  # Errno 28 = No space left on device
                        error_msg = (
                            f"Rip failed: No space left on device. "
                            f"Please free up disk space and try again. "
                            f"Error: {exc}"
                        )
                        func_logger.error("Disk space error during rip for job %s: %s", job_id, exc, exc_info=True)
                        self.add_log(job, db, error_msg)
                        _post_rip_complete_callback(str(job.id), success=False, error_reason=error_msg)
                        try:
                            from core.notifications import emit_notification_sync
                            from core.pipeline_notification_labels import job_audience_label

                            label = job_audience_label(job, getattr(job, "disc", None))
                            emit_notification_sync(
                                f"{label}: {error_msg}",
                                "error",
                                "error_disk_space",
                                job_id=str(job.id),
                            )
                        except Exception as notify_exc:
                            func_logger.warning("Failed to emit disk space notification: %s", notify_exc)
                        
                        return
                    else:
                        # Other OSError - re-raise to be handled by outer exception handler
                        raise

                # Post-rip check: failed titles or 0 saved -> report failure via callback (#313)
                progress_log_path = rip_workdir / "makemkv_progress.log"
                if progress_log_path.exists():
                    try:
                        log_content = progress_log_path.read_text(encoding="utf-8", errors="ignore")
                        titles_saved, titles_failed = _parse_makemkv_titles_saved_failed(log_content)
                        if titles_failed > 0:
                            saved_part = (
                                f"{titles_saved} title(s) saved, " if titles_saved is not None else ""
                            )
                            hint = _extract_makemkv_read_error_hint(log_content)
                            detail = (
                                f"MakeMKV copy finished with {saved_part}{titles_failed} title(s) failed. "
                                "Some streams could not be read from the disc."
                            )
                            if hint:
                                detail += f" Last read error: {hint}"
                            detail += (
                                " This is often a dirty or damaged disc, a loose USB/SATA cable or "
                                "under-powered drive, or an unstable kernel/driver for the optical drive."
                            )
                            self.add_log(job, db, f"Rip failed: {detail}")
                            _post_rip_complete_callback(
                                str(job.id),
                                success=False,
                                error_reason=detail,
                                error_type="disc_read",
                            )
                            return
                        if titles_saved is not None and titles_saved == 0:
                            self.add_log(job, db, "Rip failed: 0 titles saved by MakeMKV")
                            _post_rip_complete_callback(
                                str(job.id),
                                success=False,
                                error_reason="MakeMKV saved 0 titles",
                            )
                            return
                    except Exception as parse_exc:
                        func_logger.debug("Could not parse makemkv progress log for 0-titles check: %s", parse_exc)

                # 3) If DiscDB is missing and labels are required, pause here until labels are completed.
                needs_label = bool((job.disc_payload or {}).get("label_required")) and not bool((job.disc_payload or {}).get("label_ready"))
                if needs_label:
                    payload = {**(job.disc_payload or {}), "label_required": True, "label_ready": False}
                    current_status = getattr(job, "job_status", None)
                    if current_status in ("failed", "completed"):
                        self.add_log(job, db, f"Skipping label phase transition - job is already {current_status}")
                        log_file(f"Skipping label phase transition - job is already {current_status}")
                    else:
                        self.add_log(job, db, "Copy finished; verification will hash outputs (API-enqueued)")
                        log_file("Copy finished; verification will hash outputs")
                    if current_status not in ("failed", "completed"):
                        try:
                            ripped_files_committer.flush()
                        except Exception:
                            pass
                        self.set_status(job, db, disc_payload=payload)
                    _post_rip_complete_callback(str(job.id), success=True)
                    return

                # 4) Hit path: copy only — API enqueues rip_verification, then resume_postprocess after verify-complete.
                try:
                    ripped_files_committer.flush()
                except Exception:
                    pass
                self.add_log(job, db, "Copy finished; verification will hash outputs (API-enqueued)")
                _post_rip_complete_callback(str(job.id), success=True)
                return
            except RipCallbackTransportError:
                raise
            except Exception as exc:
                # Flush any pending ripped_files updates before handling error
                try:
                    if 'ripped_files_committer' in locals():
                        ripped_files_committer.flush()
                except Exception:
                    pass
                
                # Use detailed error logging
                self.log_task_error(
                    job, db, exc,
                    context={
                        'disc_num': disc_num,
                        'mount_point': mount_point,
                        'mode': mode,
                        'rip_workdir': str(rip_workdir) if 'rip_workdir' in locals() else None,
                    }
                )
                try:
                    write_summary(paths.metadata, "failed", error=str(exc))
                except Exception:
                    pass
                # Prefer callback so API applies terminal state; fall back to set_status if callback fails (e.g. network).
                current_status = getattr(job, "job_status", None)
                if current_status != "failed":
                    try:
                        err_type = "disc_read" if is_disc_read_error(str(exc)) else None
                        _post_rip_complete_callback(
                            str(job.id), success=False, error_reason=str(exc), error_type=err_type
                        )
                    except RipCallbackTransportError:
                        raise
                    except Exception as callback_exc:
                        logging.warning("rip-complete callback failed, falling back to worker set_status: %s", callback_exc)
                        failed_updates = {
                            "job_status": "failed",
                            "error_reason": str(exc),
                        }
                        if getattr(job, "rip_state", None) not in ("completed", "skipped"):
                            failed_updates["rip_state"] = "failed"
                        # #365 step 5 — post_state column dropped; derivation
                        # returns "failed" from job_status="failed" + rip
                        # finished (decision-table step 2).
                        self.set_status(job, db, **failed_updates)

                    # Reset workflow_step on job when job fails
                    try:
                        sp = getattr(job, "stage_profile", None)
                        if isinstance(sp, str) and sp.strip():
                            profile = sp.strip().lower()
                        else:
                            dr = getattr(job, "discdb_result", None)
                            profile = dr.strip().lower() if isinstance(dr, str) and dr.strip() else "miss"
                        job.workflow_step = "summary" if profile == "hit" else "film"
                        db.commit()
                        logging.info(f"Reset workflow_step to '{job.workflow_step}' on job {job.id} after failure")
                    except Exception as step_exc:
                        logging.warning(f"Failed to reset workflow_step on job {job.id}: {step_exc}")
                else:
                    # Job already failed - just log the error without trying to update status
                    logging.warning(f"Job {job.id} already marked as failed, skipping status update. Error: {exc}")
                # DISABLED: Do not clean up files on failure - preserve them for user inspection
                # Files are preserved so user can inspect what was created before retry
                # User must manually retry via frontend, which will create a new job
                logging.info(f"Job {job.id} failed - files preserved at {rip_workdir} for inspection. User must manually retry to create a new job.")
                raise
        finally:
            # Release rip operation lock so other operations can use the drive (release is no-op if lock is None)
            try:
                release_operation_lock(rip_op_lock)
            except Exception as release_exc:
                func_logger.warning("Failed to release rip operation lock for job %s: %s", job_id, release_exc)
            # Always clear PID on exit (whether success or failure)
            # This ensures cleanup happens if the task crashes or is killed
            # Wrap in try-except to prevent cleanup errors from marking successful tasks as failed
            try:
                # Clear PID on exit (whether success or failure)
                if job:
                    try:
                        self.set_status(job, db, rip_pid=None)
                        func_logger.debug("Cleared rip_pid for job %s", job_id)
                    except Exception as pid_clear_exc:
                        func_logger.warning("Failed to clear rip_pid for job %s: %s", job_id, pid_clear_exc)
            except Exception as cleanup_exc:
                # Log cleanup errors but don't re-raise - task may have completed successfully
                # Use centralized logging utility for consistent formatting
                log.error(
                    f"Error during cleanup in rip_disc task (job {job_id}, disc {disc_num})",
                    extra={
                        'job_id': job_id,
                        'disc_num': disc_num,
                        'cleanup_error_type': type(cleanup_exc).__name__,
                        'cleanup_error_message': str(cleanup_exc),
                    },
                    exc_info=cleanup_exc
                )
                # Also log to standard logging for visibility
                logging.warning(
                    f"Cleanup error in rip_disc task (job {job_id}, disc {disc_num}): {cleanup_exc}",
                    exc_info=cleanup_exc
                )

# ────────────────────────────────────────────────────────────────
@celery_app.task(bind=True, base=JobTask, name="rip_verification", acks_late=False)
def rip_verification(self, job_id: str):
    """Hash/map MKV outputs and POST rip-verification-complete; enqueued by API after copy ack."""
    log.info("rip_verification task started job_id=%s celery_task_id=%s", job_id, self.request.id)
    from workers.rip_verification_impl import run_rip_verification_for_job

    run_rip_verification_for_job(self, job_id)


def rip_verification_task_id(job_id: str) -> str:
    """Stable Celery task id: one logical rip_verification execution slot per job."""
    return f"rip_verification:{job_id}"


def enqueue_rip_verification_for_job(job_id: str, *, reason: str = "api") -> None:
    """
    Enqueue rip_verification with stable task_id ``rip_verification:{job_id}`` (observability / dedupe).
    Skips if the same task id is already STARTED (task_track_started). Falls back to delay() if
    apply_async rejects (broker-specific duplicate-id rules).
    """
    jid = str(job_id)
    tid = rip_verification_task_id(jid)
    try:
        from celery.result import AsyncResult

        existing = AsyncResult(tid, app=celery_app)
        if existing.state == "STARTED":
            log.info(
                "enqueue_rip_verification_for_job skip job_id=%s task_id=%s already STARTED reason=%s",
                jid,
                tid,
                reason,
            )
            return
    except Exception as exc:
        log.debug(
            "enqueue_rip_verification_for_job could not inspect existing result job_id=%s: %s",
            jid,
            exc,
        )

    try:
        rip_verification.apply_async(args=[jid], task_id=tid)
        log.info(
            "enqueue_rip_verification_for_job job_id=%s task_id=%s reason=%s",
            jid,
            tid,
            reason,
        )
    except Exception as exc:
        log.warning(
            "enqueue_rip_verification_for_job apply_async failed job_id=%s task_id=%s reason=%s: %s; using delay()",
            jid,
            tid,
            reason,
            exc,
        )
        rip_verification.delay(jid)


# ────────────────────────────────────────────────────────────────
@celery_app.task(bind=True, base=JobTask, name='recover_running_rip', acks_late=True)
def recover_running_rip(
    self,
    job_id: str,
    pid: int,
    cmdline: str,
):
    """
    Recovery task: Monitor a running makemkvcon process and resume progress tracking.
    This is used when a job failed but makemkvcon is still running - we can recover
    the job by monitoring the existing process.
    """
    log.info(
        f"recover_running_rip task started",
        extra={
            'task_id': self.request.id,
            'job_id': job_id,
            'pid': pid,
            'cmdline': cmdline[:200] if cmdline else None,
        }
    )
    from core.utils import _extract_output_dir_from_cmdline, monitor_running_makemkvcon
    from core.job_paths import JobPaths
    from core.job_state import apply_job_state
    from pathlib import Path
    
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            logging.warning(f"Job {job_id} not found for recovery")
            return
        
        logging.info(f"Recovering job {job_id} by monitoring running makemkvcon process (PID {pid})")
        self.add_log(job, db, f"Recovering: Monitoring existing makemkvcon process (PID {pid})")
        
        # Check worker health before recovery (helps diagnose if worker crashed)
        try:
            health = check_worker_health()
            if health['status'] != 'healthy':
                logging.warning(
                    f"Worker health check during recovery: status={health['status']}, "
                    f"issues={health['issues']}. This may indicate a worker crash."
                )
                self.add_log(
                    job, db,
                    f"Warning: Worker health check detected issues: {', '.join(health['issues'])}. "
                    f"This may indicate the original worker process crashed."
                )
        except Exception as health_exc:
            logging.debug(f"Could not check worker health during recovery: {health_exc}")
        
        # Determine log file path
        from core.job_paths import JobPaths
        job_paths = JobPaths.for_id(job_id)
        log_path = job_paths.raw / "makemkv_progress.log"
        if not log_path.exists():
            log_path = job_paths.metadata / "makemkv_progress.log"
        if not log_path or not log_path.exists():
            output_dir = _extract_output_dir_from_cmdline(cmdline)
            if output_dir:
                log_path = output_dir / "makemkv_progress.log"
        
        # Set up progress tracking (reuse logic from rip_disc)
        # We'll use a simplified version that just updates progress
        prgv_re = re.compile(r'^PRGV:(\d+),(\d+),(\d+)')
        tqdm_re = re.compile(r'^\s*(\d{1,3})%\|')
        
        last_overall_pct = getattr(job, "rip_progress", 0) or 0
        
        def log_hook(line: str):
            """Process each line from makemkvcon output"""
            self.add_log(job, db, line)
            
            # Parse progress
            nonlocal last_overall_pct
            pct = None
            
            # Try PRGV format first
            m_prgv = prgv_re.match(line)
            if m_prgv:
                _, done, total = map(int, m_prgv.groups())
                if total > 0:
                    pct = int((done / total) * 100)
            
            # Try tqdm format
            if pct is None:
                m_tqdm = tqdm_re.match(line)
                if m_tqdm:
                    pct = int(m_tqdm.group(1))
            
            if pct is not None:
                # Enforce monotonic progress; only progress/observability (API set rip_state=running via rip_started when enqueueing).
                pct = max(last_overall_pct, pct)
                last_overall_pct = pct
                _post_rip_progress(str(job.id), rip_progress=pct)
                self.set_status(job, db, job_status='running')
        
        # Monitor the running process
        exit_code = monitor_running_makemkvcon(pid, log_path, line_cb=log_hook)
        
        # Process completed: report via rip-complete API (no direct DB state)
        if exit_code == 0:
            self.add_log(job, db, "Recovery complete: makemkvcon process finished successfully")
            try:
                _post_rip_complete_callback(str(job.id), success=True)
            except Exception as rec_exc:
                logging.warning("Recovery rip-complete callback failed for job %s: %s", job_id, rec_exc)
                self.add_log(job, db, f"Recovery callback failed: {rec_exc}")
                err_type = "disc_read" if is_disc_read_error(str(rec_exc)) else None
                try:
                    _post_rip_complete_callback(
                        str(job.id),
                        success=False,
                        error_reason=str(rec_exc),
                        error_type=err_type,
                    )
                except RipCallbackTransportError:
                    raise
        else:
            self.add_log(job, db, f"Recovery failed: makemkvcon process exited with code {exit_code}")
            _post_rip_complete_callback(
                str(job.id),
                success=False,
                error_reason=f"makemkvcon exited with code {exit_code}",
            )

# ────────────────────────────────────────────────────────────────
# Preview error classification
# ────────────────────────────────────────────────────────────────

# FFmpeg stderr patterns that indicate the source file has no usable video content.
# When these match, the preview failure is non-retryable (retrying won't help).
_FFMPEG_NO_CONTENT_PATTERNS = [
    "output file is empty",
    "nothing was encoded",
    "matches no streams",
    "does not contain any stream",
    "invalid data found when processing input",
    "no such file or directory",
    "could not find codec",
    "decoder .* not found",
    "no video stream",
]


def _classify_preview_error(stderr: str, manifest_path: str | None = None) -> tuple[str, bool]:
    """Classify an ffmpeg preview generation error.

    Returns (user_message, is_retryable).
    - Non-retryable: the source file has no video content or is fundamentally broken.
    - Retryable: transient failure (resource limits, timeouts, etc.) — worth trying again.
    """
    stderr_lower = (stderr or "").lower()

    for pattern in _FFMPEG_NO_CONTENT_PATTERNS:
        if pattern in stderr_lower:
            # Determine a short user-friendly message
            if "no such file" in stderr_lower:
                return "Source file not found", True  # Transient: file may exist after rip completes
            if "matches no streams" in stderr_lower or "no video stream" in stderr_lower or "does not contain any stream" in stderr_lower:
                return "No video stream found", False
            if "invalid data" in stderr_lower:
                return "Invalid source file", False
            if "output file is empty" in stderr_lower or "nothing was encoded" in stderr_lower:
                return "No video content to preview", False
            return "No video content to preview", False

    # Check if manifest was written but is empty (0 bytes / no segments)
    if manifest_path:
        try:
            from pathlib import Path
            mp = Path(manifest_path)
            if mp.exists() and mp.stat().st_size == 0:
                return "No video content to preview", False
        except Exception:
            pass

    # Default: treat as transient / retryable
    return "Preview generation failed", True


def _generate_thumbnail(input_file: str | object, out_dir: object, max_parallel: int) -> str | None:
    """Extract a single JPEG thumbnail frame from a video file.

    Returns the thumbnail filename on success, None on failure.
    Failures are non-fatal — preview generation succeeds even if thumbnail fails.
    """
    thumbnail = out_dir / "thumbnail.jpg"  # type: ignore[operator]
    thumb_cmd = [
        "ffmpeg", "-y",
        "-ss", "2",          # 2 seconds in for a representative frame
        "-i", str(input_file),
        "-frames:v", "1",
        "-q:v", "5",         # JPEG quality (2=best, 31=worst)
        "-vf", "scale=320:-2",  # 320px wide, maintain aspect ratio
        str(thumbnail),
    ]
    try:
        with ffmpeg_semaphore(max_parallel):
            result = subprocess.run(thumb_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and thumbnail.exists() and thumbnail.stat().st_size > 0:  # type: ignore[union-attr]
            return "thumbnail.jpg"
    except Exception:
        pass
    return None


def _overall_generate_previews_status(tracks_state: dict) -> str:
    vals = [v.get("status") for v in tracks_state.values() if isinstance(v, dict)]
    if not vals:
        return "queued"
    if all(s == "completed" for s in vals):
        return "completed"
    if any(s in ("queued", "running") for s in vals):
        return "running"
    if any(s == "failed" for s in vals):
        return "failed"
    return "running"


def _sync_preview_alias_entries(tracks_state: dict, track_source_map: dict, source_to_key: dict) -> None:
    for alias_key, alias_info in list(tracks_state.items()):
        if alias_key in track_source_map:
            continue
        if not isinstance(alias_info, dict):
            continue
        src = alias_info.get("source")
        if not src:
            continue
        canonical_key = source_to_key.get(src)
        if canonical_key and canonical_key in tracks_state:
            canonical_entry = tracks_state[canonical_key]
            tracks_state[alias_key] = {
                **canonical_entry,
                "manifest": canonical_entry.get("manifest"),
                "source": src,
            }


# ────────────────────────────────────────────────────────────────
@celery_app.task(bind=True, base=JobTask, name='generate_previews', acks_late=True)
@one_at_a_time
def generate_previews(self, job_id: str, track_keys: list[str] | None = None):
    """
    Generate HLS previews for each MKV output of a completed job.
    Uses preview_config for duration/max_parallel and writes to {job_root}/previews/{track}/preview.m3u8.
    Does not remove the entire previews tree; only per-track output dirs for tracks being encoded.
    """
    log.info(
        f"generate_previews task started",
        extra={
            'task_id': self.request.id,
            'job_id': job_id,
            'track_keys': track_keys,
        }
    )
    preview_root: Path | None = None
    input_root: Path | None = None
    tracks_state: dict = {}
    track_source_map: dict[str, str] = {}
    source_to_key: dict[str, str] = {}
    duration = 120
    max_parallel = 1

    def persist_preview_progress(status: str) -> None:
        with db_session() as db:
            job = crud.get_job(db, job_id)
            if not job:
                return
            db.refresh(job)
            disc_payload = dict(job.disc_payload or {})
            prev_prev = disc_payload.get("previews") if isinstance(disc_payload.get("previews"), dict) else {}
            new_block: dict = {
                "status": status,
                "tracks": deepcopy(tracks_state),
                "updated_at": datetime.utcnow().isoformat(),
            }
            for k in ("auto_recovery_attempts", "auto_recovery_last_error"):
                if k in prev_prev:
                    new_block[k] = prev_prev[k]
            disc_payload["previews"] = new_block
            self.set_status(job, db, disc_payload=disc_payload)

    # First session: load job, validate, write initial "running" state. Do not hold session across ffmpeg.
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            return
        paths = JobPaths.from_job(job, out_dir=str(DATA_ROOT))
        paths.ensure_layout()
        input_root = None
        if paths.raw.exists():
            input_root = paths.raw
        if not input_root:
            self.add_log(job, db, "Preview generation skipped: raw directory not found")
            return
        disc_payload = job.disc_payload or {}
        post_paths = getattr(job, "post_paths", None) or disc_payload.get("post_paths") or {}
        ripped_files = getattr(job, "ripped_files", None) or disc_payload.get("ripped_files") or {}
        all_paths = post_paths if post_paths else ripped_files
        if not all_paths:
            self.add_log(job, db, "Preview generation skipped: no file paths found")
            return
        if track_keys:
            want = {str(k) for k in track_keys}
            work_paths = {k: v for k, v in all_paths.items() if str(k) in want}
            if not work_paths:
                self.add_log(job, db, f"Preview generation skipped: no matching tracks for {track_keys}")
                return
        else:
            work_paths = dict(all_paths)

        cfg = settings.get_preview_dict()
        duration = max(1, int(cfg.get("duration_seconds", 120)))
        max_parallel = max(1, int(cfg.get("max_parallel", os.cpu_count() or 1)))

        preview_root = paths.previews
        preview_root.mkdir(parents=True, exist_ok=True)

        existing_previews = disc_payload.get("previews") or {}
        existing_tracks = existing_previews.get("tracks") if isinstance(existing_previews, dict) else {}
        if not isinstance(existing_tracks, dict):
            existing_tracks = {}

        tracks_state = {}
        for k, v in existing_tracks.items():
            tracks_state[k] = deepcopy(v) if isinstance(v, dict) else v

        track_source_map = {}
        for title_id, rel_path in work_paths.items():
            tid = str(title_id)
            safe_folder = _safe_track_folder(tid)
            manifest_path = preview_root / safe_folder / "preview.m3u8"
            manifest_rel = f"previews/{safe_folder}/preview.m3u8"
            prev = tracks_state.get(tid) if isinstance(tracks_state.get(tid), dict) else {}
            if manifest_path.exists():
                src = rel_path or prev.get("source")
                tracks_state[tid] = {
                    "status": "completed",
                    "manifest": manifest_rel,
                    "error": None,
                    "title_id": prev.get("title_id") or tid,
                    "source": src,
                }
            else:
                src = rel_path or prev.get("source")
                tracks_state[tid] = {
                    "status": "queued",
                    "manifest": manifest_rel,
                    "error": None,
                    "title_id": prev.get("title_id") or tid,
                    "source": src,
                }
                if src:
                    track_source_map[tid] = src

        title_output_map = disc_payload.get("title_output_map") or {}
        for key, info in list(tracks_state.items()):
            if not isinstance(info, dict):
                continue
            if info.get("status") != "queued":
                continue
            if info.get("source"):
                if key in work_paths or str(key) in {str(wk) for wk in work_paths}:
                    track_source_map.setdefault(str(key), info["source"])
                continue
            rel = None
            if key in work_paths:
                rel = work_paths.get(key)
            if not rel and isinstance(title_output_map, dict):
                rel = title_output_map.get(key)
            if not rel:
                rel = all_paths.get(key)
            if rel:
                info["source"] = rel
                if str(key) in {str(wk) for wk in work_paths.keys()}:
                    track_source_map[str(key)] = rel

        for tid in list(work_paths.keys()):
            tid_s = str(tid)
            ent = tracks_state.get(tid_s) if tid_s in tracks_state else tracks_state.get(tid)
            if not isinstance(ent, dict):
                continue
            if ent.get("status") != "queued":
                continue
            if not ent.get("source"):
                ent["status"] = "failed"
                ent["error"] = "No preview source path"
                if tid_s in track_source_map:
                    del track_source_map[tid_s]
                if str(tid) in track_source_map and str(tid) != tid_s:
                    del track_source_map[str(tid)]

        track_source_map = {k: v for k, v in track_source_map.items() if v}

        source_to_key = {v: k for k, v in track_source_map.items()}
        _sync_preview_alias_entries(tracks_state, track_source_map, source_to_key)

        required_bytes = PREVIEW_BYTES_PER_TRACK * len(track_source_map) + (5 * 1024 * 1024)
        try:
            space_root = paths.root if paths.root.exists() else input_root
            usage = shutil.disk_usage(space_root)
            if usage.free < required_bytes and track_source_map:
                msg = f"Insufficient space for previews (free {usage.free} bytes, need {required_bytes})"
                logging.error(f"Preview generation failed for job {job.id}: {msg}")
                self.add_log(job, db, msg)
                for k in track_source_map:
                    te = tracks_state.get(k)
                    if isinstance(te, dict):
                        te["status"] = "failed"
                        te["error"] = msg
                disc_payload = dict(job.disc_payload or {})
                prev_prev = disc_payload.get("previews") if isinstance(disc_payload.get("previews"), dict) else {}
                new_block = {
                    "status": _overall_generate_previews_status(tracks_state),
                    "tracks": deepcopy(tracks_state),
                    "updated_at": datetime.utcnow().isoformat(),
                }
                for key in ("auto_recovery_attempts", "auto_recovery_last_error"):
                    if key in prev_prev:
                        new_block[key] = prev_prev[key]
                disc_payload["previews"] = new_block
                self.set_status(job, db, disc_payload=disc_payload)
                return
        except Exception as exc:
            self.add_log(job, db, f"Preview space check warning: {exc}")

        if not track_source_map:
            overall = _overall_generate_previews_status(tracks_state)
            disc_payload = dict(job.disc_payload or {})
            prev_prev = disc_payload.get("previews") if isinstance(disc_payload.get("previews"), dict) else {}
            new_block = {
                "status": overall,
                "tracks": deepcopy(tracks_state),
                "updated_at": datetime.utcnow().isoformat(),
            }
            for key in ("auto_recovery_attempts", "auto_recovery_last_error"):
                if key in prev_prev:
                    new_block[key] = prev_prev[key]
            disc_payload["previews"] = new_block
            self.set_status(job, db, disc_payload=disc_payload)
            self.add_log(job, db, f"Preview generation {overall} (no tracks to encode)")
            return

        disc_payload_update = dict(job.disc_payload or {})
        prev_prev_run = disc_payload_update.get("previews") if isinstance(disc_payload_update.get("previews"), dict) else {}
        new_block_run = {
            "status": "running",
            "tracks": deepcopy(tracks_state),
            "updated_at": datetime.utcnow().isoformat(),
        }
        for key in ("auto_recovery_attempts", "auto_recovery_last_error"):
            if key in prev_prev_run:
                new_block_run[key] = prev_prev_run[key]
        disc_payload_update["previews"] = new_block_run
        self.set_status(job, db, disc_payload=disc_payload_update)
        self.add_log(job, db, "Preview generation running")

    assert preview_root is not None and input_root is not None

    def build_preview(track_key: str, rel_path: str):
        out_dir = preview_root / _safe_track_folder(track_key)
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = out_dir / "preview.m3u8"
        segment_pattern = out_dir / "segment_%03d.ts"
        input_file = (input_root / rel_path).resolve()
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            "0",
            "-t",
            str(duration),
            "-i",
            str(input_file),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "128k",
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(segment_pattern),
            str(manifest),
        ]
        try:
            with ffmpeg_semaphore(max_parallel):
                result = subprocess.run(cmd, capture_output=True, text=True)
        except TimeoutError as te:
            logging.error(f"Could not acquire ffmpeg slot for track {track_key} (job {job_id}): {te}")
            return track_key, "failed", "Failed to acquire ffmpeg slot", True, None

        if result.returncode != 0:
            full_error = result.stderr.strip() or result.stdout.strip() or "Unknown ffmpeg error"
            logging.error(f"Preview generation failed for track {track_key} (job {job_id}): {full_error}")
            error_msg, is_retryable = _classify_preview_error(full_error, str(manifest))
            return track_key, "failed", error_msg, is_retryable, None

        # Generate thumbnail (non-fatal if it fails)
        thumb_name = _generate_thumbnail(input_file, out_dir, max_parallel)
        return track_key, "completed", None, True, thumb_name

    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
        for track_key, rel_path in track_source_map.items():
            futures.append(pool.submit(build_preview, track_key, rel_path))
    for fut in concurrent.futures.as_completed(futures):
        tk, status, err, retryable, thumb = fut.result()
        tk = str(tk)
        if tk not in tracks_state or not isinstance(tracks_state.get(tk), dict):
            tracks_state[tk] = {"manifest": f"previews/{_safe_track_folder(tk)}/preview.m3u8"}
        tracks_state[tk]["status"] = status
        tracks_state[tk]["error"] = err
        tracks_state[tk]["retryable"] = retryable if status == "failed" else None
        if thumb:
            safe = _safe_track_folder(tk)
            tracks_state[tk]["thumbnail"] = f"previews/{safe}/{thumb}"
        _sync_preview_alias_entries(tracks_state, track_source_map, source_to_key)
        persist_preview_progress("running")

    _sync_preview_alias_entries(tracks_state, track_source_map, source_to_key)
    overall_status = _overall_generate_previews_status(tracks_state)

    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            return
        db.refresh(job)
        disc_payload = dict(job.disc_payload or {})
        prev_prev = disc_payload.get("previews") if isinstance(disc_payload.get("previews"), dict) else {}
        new_block = {
            "status": overall_status,
            "tracks": deepcopy(tracks_state),
            "updated_at": datetime.utcnow().isoformat(),
        }
        for key in ("auto_recovery_attempts", "auto_recovery_last_error"):
            if key in prev_prev:
                new_block[key] = prev_prev[key]
        disc_payload["previews"] = new_block
        self.set_status(job, db, disc_payload=disc_payload)
        self.add_log(job, db, f"Preview generation {overall_status}")


@celery_app.task(bind=True, base=JobTask, name='generate_preview_track', acks_late=True)
def generate_preview_track(self, job_id: str, track_key: str, rel_path: str, title_id: str | None = None):
    """
    Generate a single track preview during an active rip. Uses a dedicated lock so we
    do not block the rip task; only one preview runs at a time.
    """
    log.info(
        f"generate_preview_track task started",
        extra={
            'task_id': self.request.id,
            'job_id': job_id,
            'track_key': track_key,
            'rel_path': rel_path,
        }
    )
    def _update_track_status(job, db, track_key: str, status: str, error: str | None, rel_path: str, title_id: str | None, retryable: bool | None = None, thumbnail: str | None = None) -> None:
        """Helper to update track status in disc_payload."""
        try:
            db.refresh(job)
            disc_payload = job.disc_payload or {}
            previews = disc_payload.get("previews") or {}
            tracks_state = previews.get("tracks") if isinstance(previews, dict) else {}
            if not isinstance(tracks_state, dict):
                tracks_state = {}
            preview_maps = _build_title_id_maps(job, disc_payload)
            resolved_title_id = None
            try:
                resolved_title_id = _resolve_preview_title_id(track_key, rel_path, preview_maps)
            except Exception:
                resolved_title_id = None
            existing_entry = tracks_state.get(track_key) if isinstance(tracks_state.get(track_key), dict) else {}
            
            # Verify manifest exists if status is "completed"
            manifest_rel = None
            if status == "completed":
                manifest_rel = f"previews/{_safe_track_folder(track_key)}/preview.m3u8"
                try:
                    paths = JobPaths.from_job(job, out_dir=str(DATA_ROOT))
                    manifest_path = paths.previews / _safe_track_folder(track_key) / "preview.m3u8"
                    if not manifest_path.exists():
                        logging.warning(f"Manifest file missing for completed preview: {manifest_path}")
                        status = "failed"
                        error = "Failed to generate preview"
                        manifest_rel = None
                except Exception as path_exc:
                    logging.warning(f"Failed to verify manifest path for track {track_key}: {path_exc}")
                    # Don't fail the status update if we can't verify the path
                    # The manifest might still exist, just couldn't verify it
            
            track_entry = {
                "status": status,
                "manifest": manifest_rel,
                "error": error,
                "title_id": title_id or existing_entry.get("title_id") or resolved_title_id,
                "source": rel_path,
            }
            # Include retryable flag for failed tracks
            if retryable is not None:
                track_entry["retryable"] = retryable
            elif status == "failed":
                track_entry["retryable"] = True  # default retryable for failed tracks
            # Include thumbnail if provided or preserve existing
            if thumbnail:
                track_entry["thumbnail"] = thumbnail
            elif existing_entry.get("thumbnail") and status == "completed":
                track_entry["thumbnail"] = existing_entry["thumbnail"]
            tracks_state[track_key] = track_entry
            # propagate status to any aliases that share the same source path
            for alias_key, alias_info in list(tracks_state.items()):
                if alias_key == track_key:
                    continue
                if not isinstance(alias_info, dict):
                    continue
                if alias_info.get("source") == rel_path:
                    tracks_state[alias_key] = {
                        **tracks_state[track_key],
                        "manifest": tracks_state[track_key].get("manifest"),
                        "title_id": title_id or alias_info.get("title_id") or tracks_state[track_key].get("title_id"),
                        "source": rel_path,
                    }
            overall_status = "completed" if all(v.get("status") == "completed" for v in tracks_state.values()) else "running"
            disc_payload["previews"] = {
                "status": overall_status,
                "tracks": tracks_state,
                "updated_at": datetime.utcnow().isoformat(),
            }
            disc_payload = _backfill_preview_title_ids(disc_payload)
            # Try to update status, with retry on failure
            try:
                self.set_status(job, db, disc_payload=disc_payload)
            except Exception as set_exc:
                # Log the error with full traceback
                logging.error(f"Failed to update preview status for job {job_id}, track {track_key}: {set_exc}", exc_info=True)
                # Try one more time with a fresh job object
                try:
                    db.rollback()
                    db.refresh(job)
                    disc_payload = job.disc_payload or {}
                    disc_payload["previews"] = {
                        "status": overall_status,
                        "tracks": tracks_state,
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                    self.set_status(job, db, disc_payload=disc_payload)
                    logging.info(f"Successfully updated preview status on retry for job {job_id}, track {track_key}")
                except Exception as retry_exc:
                    logging.error(f"Retry also failed for job {job_id}, track {track_key}: {retry_exc}", exc_info=True)
                    # Re-raise so outer handler can mark as failed
                    raise
        except Exception as update_exc:
            logging.error(f"Failed to update preview status for job {job_id}, track {track_key}: {update_exc}", exc_info=True)
            try:
                self.add_log(job, db, f"Warning: Preview status update failed: {update_exc}")
            except Exception:
                pass
            # Re-raise so the outer exception handler can mark the track as failed
            raise

    try:
        _cleanup_stale_lock(PREVIEW_LOCK_PATH)
        with FileLock(PREVIEW_LOCK_PATH, timeout=2.0):
            # First session: validate and build ffmpeg cmd. Do not hold session across subprocess.
            with db_session() as db:
                job = crud.get_job(db, job_id)
                if not job:
                    return
                disc_payload = job.disc_payload or {}
                preview_maps = _build_title_id_maps(job, disc_payload)
                canonical_key = _resolve_preview_title_id(track_key, rel_path, preview_maps) or track_key
                if canonical_key != track_key:
                    track_key = canonical_key
                if not rel_path:
                    # Get post_paths (preferred) or ripped_files (fallback) - both use title_id keys
                    post_paths = getattr(job, "post_paths", None) or disc_payload.get("post_paths") or {}
                    ripped_files = getattr(job, "ripped_files", None) or disc_payload.get("ripped_files") or {}
                    file_paths = post_paths if post_paths else ripped_files
                    rel_path = _resolve_preview_rel_path(track_key, file_paths, preview_maps)
                paths = JobPaths.from_job(job, out_dir=str(DATA_ROOT))
                paths.ensure_layout()
                # Previews must be generated from raw directory (raw ripped files)
                input_root = None
                if paths.raw.exists():
                    input_root = paths.raw
                if not input_root:
                    _update_track_status(job, db, track_key, "failed", "Raw directory not found", rel_path, title_id)
                    self.add_log(job, db, "Preview track skipped: raw directory not found")
                    return
                if not rel_path:
                    _update_track_status(job, db, track_key, "failed", "No preview source found", rel_path, title_id)
                    self.add_log(job, db, "Preview track skipped: missing source path")
                    return
                input_file = (input_root / rel_path).resolve()
                if not input_file.exists():
                    # Try resolving from post_paths/ripped_files if the provided path is stale.
                    post_paths = getattr(job, "post_paths", None) or disc_payload.get("post_paths") or {}
                    ripped_files = getattr(job, "ripped_files", None) or disc_payload.get("ripped_files") or {}
                    file_paths = post_paths if post_paths else ripped_files
                    fallback_rel = _resolve_preview_rel_path(track_key, file_paths, preview_maps)
                    if fallback_rel:
                        rel_path = fallback_rel
                        input_file = (input_root / rel_path).resolve()
                    if not input_file.exists():
                        _update_track_status(job, db, track_key, "failed", f"Input file not found: {input_file}", rel_path, title_id)
                        self.add_log(job, db, f"Preview track skipped: missing file {input_file}")
                        return

                cfg = settings.get_preview_dict()
                duration = max(1, int(cfg.get("duration_seconds", 120)))
                max_parallel = max(1, int(cfg.get("max_parallel", os.cpu_count() or 1)))

                out_dir = paths.previews / _safe_track_folder(track_key)
                shutil.rmtree(out_dir, ignore_errors=True)
                out_dir.mkdir(parents=True, exist_ok=True)
                manifest = out_dir / "preview.m3u8"
                segment_pattern = out_dir / "segment_%03d.ts"

                cmd = [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    "0",
                    "-t",
                    str(duration),
                    "-i",
                    str(input_file),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-tune",
                    "zerolatency",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-b:a",
                    "128k",
                    "-f",
                    "hls",
                    "-hls_time",
                    "4",
                    "-hls_list_size",
                    "0",
                    "-hls_segment_filename",
                    str(segment_pattern),
                    str(manifest),
                ]

            # Run ffmpeg outside any DB session to avoid holding connection "idle in transaction".
            try:
                with ffmpeg_semaphore(max_parallel):
                    result = subprocess.run(cmd, capture_output=True, text=True)
            except TimeoutError as te:
                logging.error(f"Could not acquire ffmpeg slot for track {track_key} (job {job_id}): {te}")
                with db_session() as db:
                    job = crud.get_job(db, job_id)
                    if job:
                        _update_track_status(job, db, track_key, "failed", "Failed to acquire ffmpeg slot", rel_path, title_id)
                return

            status = "completed" if result.returncode == 0 else "failed"
            error = None
            retryable = None
            thumb_rel = None
            if status == "failed":
                full_error = result.stderr.strip() or result.stdout.strip() or "Unknown ffmpeg error"
                logging.error(f"Preview generation failed for track {track_key} (job {job_id}): {full_error}")
                error, retryable = _classify_preview_error(full_error, str(manifest))
            else:
                # Generate thumbnail (non-fatal if it fails)
                thumb_name = _generate_thumbnail(input_file, out_dir, max_parallel)
                if thumb_name:
                    safe = _safe_track_folder(track_key)
                    thumb_rel = f"previews/{safe}/{thumb_name}"

            # Second session: update status and log only.
            with db_session() as db:
                job = crud.get_job(db, job_id)
                if not job:
                    return
                try:
                    _update_track_status(job, db, track_key, status, error, rel_path, title_id, retryable=retryable, thumbnail=thumb_rel)
                except Exception as update_exc:
                    logging.error(f"Status update failed for track {track_key} (job {job_id}): {update_exc}", exc_info=True)
                    try:
                        _update_track_status(job, db, track_key, "failed", "Preview generation failed", rel_path, title_id, retryable=True)
                    except Exception:
                        logging.error(f"Failed to mark track {track_key} as failed after status update error")
    except Timeout:
        try:
            with db_session() as db:
                job = crud.get_job(db, job_id)
                if job:
                    disc_payload = job.disc_payload or {}
                    previews = disc_payload.get("previews") or {}
                    tracks_state = previews.get("tracks") if isinstance(previews, dict) else {}
                    if not isinstance(tracks_state, dict):
                        tracks_state = {}
                    # Log full error details for debugging
                    logging.error(f"Preview generation failed for track {track_key} (job {job_id}): Preview lock held")
                    tracks_state[track_key] = {
                        "status": "failed",
                        "error": "Failed to generate preview",
                        "title_id": title_id or _resolve_preview_title_id(track_key, rel_path, _build_title_id_maps(job, disc_payload)),
                        "source": rel_path,
                        "manifest": None,
                    }
                    previews["tracks"] = tracks_state
                    previews["status"] = "failed"
                    previews["error"] = "Failed to generate preview"
                    disc_payload["previews"] = previews
                    crud.append_log(db, job, "ERROR: Preview generation failed (lock held)")
                    try:
                        self.set_status(job, db, disc_payload=disc_payload)
                    except Exception:
                        pass
        except Exception:
            pass
        raise RuntimeError("Preview lock held")
    except Exception as exc:
        # Catch all other exceptions and update status to failed
        logging.error(f"Unexpected error in generate_preview_track for job {job_id}, track {track_key}: {exc}", exc_info=True)
        try:
            with db_session() as db:
                job = crud.get_job(db, job_id)
                if job:
                    disc_payload = job.disc_payload or {}
                    previews = disc_payload.get("previews") or {}
                    tracks_state = previews.get("tracks") if isinstance(previews, dict) else {}
                    if not isinstance(tracks_state, dict):
                        tracks_state = {}
                    # Log full error details for debugging
                    logging.error(f"Preview generation exception for track {track_key} (job {job_id}): {exc}", exc_info=True)
                    tracks_state[track_key] = {
                        "status": "failed",
                        "error": "Failed to generate preview",
                        "title_id": title_id or _resolve_preview_title_id(track_key, rel_path, _build_title_id_maps(job, disc_payload)),
                        "source": rel_path,
                        "manifest": None,
                    }
                    disc_payload["previews"] = {
                        "status": previews.get("status", "running"),
                        "tracks": tracks_state,
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                    try:
                        self.set_status(job, db, disc_payload=disc_payload)
                        self.add_log(job, db, f"Preview generation failed for track {track_key}: {exc}")
                    except Exception as log_exc:
                        logging.error(f"Failed to log preview generation error: {log_exc}")
        except Exception as log_exc:
            logging.error(f"Failed to log preview generation error: {log_exc}")
        raise  # Re-raise to mark task as failed in Celery


from workers.preview_detect_phases import run_detect_raw_titles_phase, run_preview_raw_titles_phase


@celery_app.task(bind=True, base=JobTask, name="preview_raw_titles", acks_late=True)
def preview_raw_titles(
    self,
    job_id: str,
    title_keys: list[str] | None = None,
    rel_path_overrides: dict[str, str] | None = None,
    chain_detect: bool = True,
):
    """
    HLS preview + mkv_size for raw MKVs (DiscDB miss path). Enqueues detect_raw_titles after for
    titles that had a file + DiscTitle row, when metadata scan and/or padding detection is enabled.

    chain_detect: the during-rip incremental preview dispatch passes False —
    detection under rip I/O contention produced false-positive junk verdicts
    that auto-ignored real episodes (#518; Fallout S2 2026-06-10). Detection
    runs once per job via the post-rip-verification preview pass
    (rip_verification_complete_callback, miss branch), which keeps the
    default True.
    """
    log.info(
        "preview_raw_titles started",
        extra={"task_id": self.request.id, "job_id": job_id, "title_keys": title_keys},
    )
    eligible = run_preview_raw_titles_phase(self, job_id, title_keys, rel_path_overrides)
    metadata_on = not is_metadata_scan_disabled()
    detection_on = not is_detection_disabled()
    if chain_detect and eligible and (metadata_on or detection_on):
        detect_raw_titles.delay(job_id, sorted(set(eligible)), rel_path_overrides)
    log.info("preview_raw_titles finished", extra={"job_id": job_id})


@celery_app.task(bind=True, base=JobTask, name="detect_raw_titles", acks_late=True)
def detect_raw_titles(
    self,
    job_id: str,
    title_keys: list[str] | None = None,
    rel_path_overrides: dict[str, str] | None = None,
):
    """ffprobe metadata_scan + padding/junk detection for raw MKVs (runs after preview_raw_titles or via API)."""
    log.info(
        "detect_raw_titles started",
        extra={"task_id": self.request.id, "job_id": job_id, "title_keys": title_keys},
    )
    run_detect_raw_titles_phase(self, job_id, title_keys, rel_path_overrides)
    log.info("detect_raw_titles finished", extra={"job_id": job_id})


@celery_app.task(bind=True, base=JobTask, name='preview_and_detect', acks_late=True)
def preview_and_detect(self, job_id: str, title_keys: list[str] | None = None, rel_path_overrides: dict[str, str] | None = None):
    """
    Backward-compatible alias: enqueue preview_raw_titles (which chains to detect_raw_titles).
    Prefer preview_raw_titles / detect_raw_titles directly for new code.
    """
    preview_raw_titles.delay(job_id, title_keys, rel_path_overrides)


# ────────────────────────────────────────────────────────────────
@celery_app.task(bind=True, base=JobTask, name='move_file', acks_late=True)
@one_at_a_time
def move_file(self, job_id: str, src_path: str, dest_path: str,
              hash_verify: bool = True):
    """
    Move a single file with progress tracking and hash verification.
    This is a utility task for simple file moves, not the main transfer job.
    The main job transfer runs in-API (jobs.py) and uses StageState.transfer_complete/failed.
    This task sets transfer_state in the worker; if it is ever used for job transfer flows,
    it should be aligned with the callback pattern (caller calls transfer_started when
    enqueueing, task reports completion/failure via API).
    """
    log.info(
        f"move_file task started",
        extra={
            'task_id': self.request.id,
            'job_id': job_id,
            'src_path': src_path,
            'dest_path': dest_path,
            'hash_verify': hash_verify,
        }
    )
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            return

        self.set_status(job, db, job_status='running', transfer_state='running', transfer_progress=0)

        def progress_cb(pct: int):
            """Update transfer progress."""
            self.set_status(job, db, transfer_progress=pct, transfer_state='running')

        try:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            
            # Use move_with_progress utility (same as transfer service uses)
            move_with_progress(src_path, dest_path,
                               hash_verify=hash_verify,
                               progress_cb=progress_cb)
            
            # Calculate hash for verification if requested
            transfer_hash = None
            if hash_verify:
                try:
                    from core.transfer.validation import calculate_file_hash
                    transfer_hash = calculate_file_hash(Path(dest_path))
                except Exception:
                    pass

            self.set_status(
                job,
                db,
                job_status='completed',
                transfer_state='completed',
                transfer_progress=100,
                result_location=dest_path,
                transfer_verification_hash=transfer_hash,
                transfer_verification_status='verified' if hash_verify and transfer_hash else None,
            )
        except Exception as exc:
            tb = traceback.format_exc(limit=8)
            self.add_log(job, db, f'ERROR: {tb}')
            self.set_status(job, db, job_status='failed', transfer_state='failed', transfer_error=str(exc))
            raise exc

@celery_app.task(name='discinfo_scan', bind=True)
@one_at_a_time
def discinfo_scan(self, disc_num: str, mount: str):
    """Scoped per-drive disc-info scan, dispatched on rip-start cache miss.

    Introduced by #562 PR 5. Runs the same code path as the udev-insert
    refresh (``info dev:{mount}`` + hash + cache write) but on the
    ``celery`` queue so it doesn't block on the ``rip`` queue's
    serialized work. Failures propagate so the Celery result backend
    reflects the error — the UI retries on user action.
    """

    log.info(
        "discinfo_scan started",
        extra={
            "task_id": self.request.id,
            "disc_num": disc_num,
            "mount": mount,
        },
    )

    from core.disc_manager import refresh_disc_info

    try:
        result = refresh_disc_info(str(disc_num), mount)
        log.info(
            "discinfo_scan completed disc_num=%s mount=%s has_hash=%s",
            disc_num,
            mount,
            bool(result.get("disc_hash") if isinstance(result, dict) else False),
        )
        return result
    except Exception as exc:
        log.warning(
            "discinfo_scan failed disc_num=%s mount=%s: %s",
            disc_num,
            mount,
            exc,
        )
        raise


@celery_app.task(name='load_disc_info', bind=True)
@one_at_a_time
def load_disc_info(self, disc_num: str, mount: str):
    log.info(
        f"load_disc_info task started",
        extra={
            'task_id': self.request.id,
            'disc_num': disc_num,
            'mount': mount,
        }
    )
    from core.disc_cache import set_payload
    from core.disc import Disc

    disc = Disc(disc_num, mount)
    # Get disc info from Disc Manager
    try:
        disc_info = get_disc_info(str(disc_num), mount)
        # Populate disc object
        disc.disc_hash = disc_info.get("disc_hash")
        disc.movie_name = disc_info.get("movie_name")
        disc.release_image = disc_info.get("release_image")
        disc.disc_slug = disc_info.get("disc_slug")
        disc.resolution = disc_info.get("resolution")
        disc.disc_format = disc_info.get("disc_format")
        disc.title_type = disc_info.get("title_type")
        disc.disc_group = disc_info.get("disc_group")
        disc.group_type = disc_info.get("group_type")
        disc.release_year = disc_info.get("release_year")
        disc.release_date = disc_info.get("release_date")
        disc.original_year = disc_info.get("original_year")
        disc.original_release_date = disc_info.get("original_release_date")
        disc.tmdb_id = disc_info.get("tmdb_id")
        disc.tmdb_type = disc_info.get("tmdb_type")
        disc.production_year = disc_info.get("production_year")
        disc.label_required = disc_info.get("label_required", False)
        disc.titles = disc_info.get("titles", {})
        disc.db_mapping = disc_info.get("tracks", {})
        disc.info_log = disc_info.get("info_log") or disc_info.get("raw_info_log")
    except Exception as exc:
        # Fallback to old method for backward compatibility
        disc.load_db_info(allow_reentrant=True)

    tracks = {
        src: {
            "season":  info.get("season"),
            "episode": info.get("episode"),
            "format":  info.get("format"),
            "episode_name": info.get("episode_name") or info.get("title"),
            "title": info.get("title") or info.get("episode_name"),
        }
        for src, info in disc.db_mapping.items()
    }

    set_payload(disc_num, {
        "disc_num": disc_num,
        "mount_point": mount,
        "movie_name":  disc.movie_name or "",
        "release_image": disc.release_image,
        "tracks":      tracks,
    })


@celery_app.task(bind=True, base=JobTask, name='start_transfer', acks_late=True)
def start_transfer(self, job_id: str):
    """Unified transfer worker — entry point for the collapsed post-rip pipeline (#365).

    Phase 2 commit 1 scaffolding: delegates to ``resume_postprocess`` for the
    prep work (rename + hash + output validation). Subsequent commits will:

    - **Commit 2:** retarget ``rip-verification-complete`` to enqueue this
      task instead of ``resume_postprocess`` for the hit branch (auto-
      progression path).
    - **Commit 3:** shrink ``resume_postprocess`` to a forwarding shim that
      enqueues ``start_transfer`` for any in-flight jobs that were queued
      under the old name.
    - **Later commits:** fold the transfer + verification work into this
      task body so the full sequence runs under one Celery hop.

    For now this task only sets ``job.transfer_phase = "preparing"`` so the
    frontend can render the new sub-phase indicator, then runs the existing
    prep logic. The downstream ``postprocess-complete`` callback still fires
    at the end (unchanged) — the rest of the pipeline continues exactly as
    before.

    See ``docs/ADR-001-postprocess-collapse.md`` and
    ``docs/plans/postprocess-collapse-325-365.md``.
    """
    log.info("start_transfer: task received job_id=%s task_id=%s", job_id, getattr(self.request, "id", None))
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            self.add_log(None, db, f"start_transfer: Job {job_id} not found")
            return
        # Expose the collapsed sub-phase to the UI without touching post_state
        # — the existing flow still drives post_state on its own.
        job.transfer_phase = "preparing"
        db.commit()
    # Delegate the actual prep work to resume_postprocess. Calling the
    # Celery-wrapped function directly with our own ``self`` re-uses the
    # JobTask base infrastructure (add_log, retry, etc.) without an extra
    # Celery hop. Subsequent commits will inline this body.
    return _run_prep_phase(self, job_id)


def _maybe_auto_dispatch_remote_transfer(job_id: str, task_self) -> None:
    """Enqueue ``transfer_remote`` if the active TransferConfig is remote.

    Phase 2 § 6.1 (#365): closes the gap between post-process prep
    completing and the actual file transfer starting. Without this, a
    user clicking the "Start Transfer" button is the only thing that
    advances rsync / SMB / NFS jobs past ``transfer_state=ready`` —
    breaking the auto-progression promise of the collapsed pipeline.

    Local mode is handled by :func:`_maybe_auto_dispatch_local_transfer`
    (a sibling helper called right after this one). Both helpers no-op
    cleanly for the other's modes.

    Failures here are non-fatal — the prep work succeeded and the user
    can still trigger the transfer manually via the existing button.

    Args:
        job_id: Job identifier.
        task_self: The Celery task instance (passed for ``add_log`` access).
    """
    db = database.SessionLocal()
    try:
        job = crud.get_job(db, job_id)
        if not job:
            return
        from core.transfer import service as transfer_service
        config = transfer_service.get_active_config(db)
        if not config or getattr(config, "mode", None) not in ("rsync", "smb", "nfs"):
            return
        job_paths = JobPaths.for_id(job_id)
        src_root = _resolve_transfer_src_root(job, job_paths, db).resolve()
        if not src_root.exists():
            log.warning(
                "auto-dispatch transfer_remote: src_root %s missing for job %s; "
                "leaving for manual trigger",
                src_root, job_id,
            )
            return
        # Claim the job BEFORE enqueueing. Leaving transfer_state='ready'
        # here is what allowed POST /jobs/{id}/transfer to enqueue a second
        # transfer_remote for the same job and race us over the same
        # destination file (see claim_transfer_for_dispatch).
        from core.job_state import claim_transfer_for_dispatch

        if not claim_transfer_for_dispatch(db, job_id):
            log.info(
                "Job %s: transfer already claimed; not auto-dispatching a duplicate",
                job_id,
            )
            return
        task_result = transfer_remote.delay(job_id, str(src_root), str(config.id))
        task_id = getattr(task_result, "id", "unknown") if task_result else "unknown"
        log.info(
            "Job %s: auto-dispatched transfer_remote (mode=%s, task_id=%s)",
            job_id, config.mode, task_id,
        )
        try:
            task_self.add_log(job, db, f"auto-dispatched transfer_remote (mode={config.mode}, task_id={task_id})")
        except Exception:
            pass
    except Exception as exc:
        log.warning(
            "Job %s: auto-dispatch transfer_remote failed (%s); manual trigger still available",
            job_id, exc,
        )
    finally:
        db.close()


def _maybe_auto_dispatch_local_transfer(job_id: str, task_self) -> None:
    """Run the local-mode transfer inline if the active TransferConfig is local.

    Phase 2 § 6.1 finisher (#365): mirrors
    :func:`_maybe_auto_dispatch_remote_transfer` for local mode, closing
    the last gap in the collapsed pipeline. Before this, local-mode
    operators clicked "Start Transfer" twice — once to kick off prep
    (this worker, via ``_run_prep_phase``) and once again to start the
    actual file copy (the ``POST /jobs/{id}/transfer`` endpoint).
    Remote modes already collapsed to one click via the remote helper;
    this helper achieves the same parity for local.

    Calls into the :func:`api.routers.jobs._execute_local_transfer_use_final_map`
    helper extracted in #477 — same body the HTTP endpoint runs, so the
    behaviour is identical to a manual click. The endpoint stays as a
    recovery path: if auto-dispatch fails or the job has no per-file
    mapping (``post_paths``/``ripped_files``/``output_files``), the
    operator can still trigger the transfer manually via the existing
    button.

    The ``target_dir`` override surface on the endpoint isn't reachable
    from the frontend today (API-only, no UI affordance), so collapsing
    to one click doesn't take anything away from the operator.

    Failures here are non-fatal:
    * ``HTTPException`` from the helper (insufficient space, destination
      verification failure, etc.) → log + call ``_fail_transfer`` so the
      job lands in ``transfer_state="failed"`` and the operator can retry.
    * Anything else → log warning; manual trigger via the endpoint
      remains available.

    Args:
        job_id: Job identifier.
        task_self: The Celery task instance (passed for ``add_log`` access).
    """
    db = database.SessionLocal()
    try:
        job = crud.get_job(db, job_id)
        if not job:
            return
        from core.transfer import service as transfer_service
        config = transfer_service.get_active_config(db)
        if not config or getattr(config, "mode", None) != "local":
            return

        # use_final_map is the Phase 2 path: prep produced job.post_paths
        # (the per-title rel-path map). Other local-mode scenarios
        # (library_dirs merge, regular directory transfer) stay
        # endpoint-driven for now — see plan § "Out of scope".
        post_paths = getattr(job, "post_paths", None) or {}
        ripped_files = getattr(job, "ripped_files", None) or {}
        disc_payload = getattr(job, "disc_payload", None) or {}
        output_files = disc_payload.get("output_files") if isinstance(disc_payload, dict) else None
        if not (post_paths or ripped_files or output_files):
            log.info(
                "Job %s: auto-dispatch transfer_local skipped — no per-file mapping "
                "(post_paths/ripped_files/output_files all empty); leaving for manual trigger",
                job_id,
            )
            return

        job_paths = JobPaths.for_id(job_id)
        src_root = _resolve_transfer_src_root(job, job_paths, db).resolve()
        if not src_root.exists():
            log.warning(
                "auto-dispatch transfer_local: src_root %s missing for job %s; "
                "leaving for manual trigger",
                src_root, job_id,
            )
            return

        from api.routers.jobs import (
            _build_job_metadata,
            _execute_local_transfer_use_final_map,
            _fail_transfer,
            _try_src_equals_dest_shortcut,
        )
        from core.job_state import apply_job_state

        job_metadata = _build_job_metadata(job)

        # Imported here (not at module top) to avoid the worker startup
        # paying for fastapi when nothing has called this helper yet.
        from fastapi import HTTPException

        # src==dest case: under MKVAUTO_RENAME_DIRECT_TO_DEST + local mode,
        # rename already wrote to config.transfer_dir, so transfer has
        # nothing to copy. Mirrors the endpoint's first action; without
        # this the use_final_map helper would SameFileError on
        # ``shutil.copy2(src, src)``.
        try:
            if _try_src_equals_dest_shortcut(job, db, src_root, config, job_metadata):
                log.info(
                    "Job %s: auto-dispatched local transfer completed via src==dest shortcut",
                    job_id,
                )
                try:
                    task_self.add_log(
                        job, db,
                        "auto-dispatched local transfer: src==dest shortcut applied",
                    )
                except Exception:
                    pass
                return
        except HTTPException as http_exc:
            # Shortcut raises 500 when src==dest but preconditions are
            # violated (pre-#451 legacy job, file count mismatch). The
            # shortcut already called _fail_transfer for us — just log
            # and bail.
            log.warning(
                "Job %s: auto-dispatched local transfer aborted by src==dest "
                "preconditions (status=%s): %s",
                job_id, http_exc.status_code,
                getattr(http_exc, "detail", "unknown"),
            )
            return

        # Progress callbacks: thin DB-write equivalents of the throttled
        # callbacks the endpoint builds inline. The endpoint's
        # ProgressThrottle is a 0.2s rate-limit; we get away without it
        # here because the local copy is fast enough that the callback
        # fires once per file (vs streaming bytes on remote).
        def _progress_cb(pct: int) -> None:
            try:
                apply_job_state(
                    db, job,
                    updates={"transfer_progress": pct, "transfer_state": "running"},
                    reason="auto-dispatched local transfer progress",
                )
            except Exception:
                try:
                    job.transfer_progress = pct
                    job.transfer_state = "running"
                    db.commit()
                except Exception:
                    pass
            # Mirror _post_transfer_progress: emit debounced WS progress so the
            # frontend's transfer bar updates live instead of only on refresh.
            try:
                from core.progress_emitter import emit_job_progress_debounced
                progress_data = {
                    "rip_progress": getattr(job, "rip_progress", 0),
                    "rip_phase": getattr(job, "rip_phase", None),
                    # #604 / #605: ship stage states alongside progress so
                    # any transition during transfer (rip already terminal,
                    # post_state flips through derivation, transfer_state
                    # advances to running/completed) is carried by every WS
                    # frame the frontend sees.
                    "rip_state": getattr(job, "rip_state", None),
                    "post_state": getattr(job, "derived_post_state", None),
                    "transfer_state": getattr(job, "transfer_state", None),
                    "post_progress": getattr(job, "post_progress", 0),
                    "transfer_progress": pct,
                    "per_title_progress": getattr(job, "per_title_progress", None),
                    "current_title_progress": getattr(job, "current_title_progress", None),
                    "current_title_id": getattr(job, "current_title_id", None),
                    "current_title_number": getattr(job, "current_title_number", None),
                }
                emit_job_progress_debounced(job_id, progress_data)
            except Exception as exc:
                log.warning(
                    "local-transfer: failed to emit WS progress for job %s: %s",
                    job_id, exc,
                )

        def _hash_progress_cb(pct: int, _filename: str) -> None:
            _progress_cb(pct)

        log.info(
            "Job %s: auto-dispatching local transfer (config=%s)",
            job_id, config.id,
        )
        try:
            task_self.add_log(
                job, db,
                f"auto-dispatched local transfer (mode=local, config_id={config.id})",
            )
        except Exception:
            pass

        try:
            _execute_local_transfer_use_final_map(
                db, job, src_root, config, output_files, job_metadata,
                transfer_progress_callback=_progress_cb,
                hash_progress_callback=_hash_progress_cb,
            )
        except HTTPException as http_exc:
            # Detail can be a dict or a string depending on the raise site.
            detail = getattr(http_exc, "detail", None) or "Local transfer failed"
            error_msg = str(detail) if not isinstance(detail, str) else detail
            log.warning(
                "Job %s: auto-dispatched local transfer failed (status=%s): %s",
                job_id, http_exc.status_code, error_msg,
            )
            try:
                # Re-fetch the job — the helper may have already called
                # _fail_transfer for partial failures, but a setup-time
                # HTTPException (e.g. insufficient space) won't have.
                db.refresh(job)
                _fail_transfer(job, db, error_msg, [])
            except Exception as fail_exc:
                log.warning(
                    "Job %s: _fail_transfer after auto-dispatch HTTPException raised: %s",
                    job_id, fail_exc,
                )
    except Exception as exc:
        log.warning(
            "Job %s: auto-dispatch transfer_local failed (%s); manual trigger still available",
            job_id, exc,
        )
    finally:
        db.close()


def _run_prep_phase(self, job_id: str):
    """Shared body for the post-rip prep work (rename + hash + validate).

    Called by the ``start_transfer`` Celery task. (#365 step 6 / Phase 2
    § 6.7 removed the legacy ``resume_postprocess`` shim once the
    ``post_state`` column drop landed and the rollout window had closed.)
    The route sets ``job.transfer_phase="preparing"``
    so the UI sub-phase indicator works regardless of which entry point
    fires.

    Not decorated as a Celery task — runs inline within ``start_transfer``'s
    worker context, reusing the caller's ``self.add_log`` /
    ``self.request`` infrastructure. The log strings inside this body still
    say "resume_postprocess" because they describe the work and remain
    grep-compatible with existing runbooks/log archives.

    See ``docs/ADR-001-postprocess-collapse.md``.
    """
    log.info("resume_postprocess: task received job_id=%s task_id=%s", job_id, getattr(self.request, "id", None))
    log.info(
        f"resume_postprocess task started",
        extra={
            'task_id': self.request.id,
            'job_id': job_id,
        }
    )
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            self.add_log(None, db, f"resume_postprocess: Job {job_id} not found")
            return

        # Expose the collapsed sub-phase to the UI for any caller that
        # bypassed start_transfer (recovery paths, in-flight Redis tasks
        # queued under the old name pre-deploy). Idempotent if already set.
        if getattr(job, "transfer_phase", None) != "preparing":
            job.transfer_phase = "preparing"
            db.flush()
        
        # Early validation: verify job has correct state before proceeding
        rip_state = getattr(job, "rip_state", None) or ""
        rip_state_lower = rip_state.lower() if rip_state else ""
        if rip_state_lower not in ("completed", "skipped"):
            error_msg = f"resume_postprocess: Cannot resume post-process - rip_state is {rip_state} (expected completed/skipped)"
            self.add_log(job, db, error_msg)
            _post_postprocess_complete_callback(job_id, success=False, error_reason=error_msg)
            return
        
        # Log job state for debugging
        profile = getattr(job, "stage_profile", None) or "unknown"
        discdb_result = getattr(job, "discdb_result", None) or "unknown"
        self.add_log(job, db, f"resume_postprocess: Starting post-process for job {job_id} (profile={profile}, discdb_result={discdb_result}, rip_state={rip_state})")

        paths = JobPaths.from_job(job, out_dir=str(DATA_ROOT))
        paths.ensure_layout()
        base_root = paths.root
        trans_root = paths.transient
        disc_id = getattr(job.disc, "id", None) if getattr(job, "disc", None) else None
        ripped = getattr(job, "ripped_files", None) or {}
        ripped_keys = list(ripped.keys()) if isinstance(ripped, dict) else []
        log.info(
            "resume_postprocess: job_id=%s disc_id=%s ripped_files_keys_count=%s raw_path=%s",
            job_id, disc_id, len(ripped_keys), str(paths.raw)
        )

        # Set up post-process logger using the unified logging system
        post_logger = get_logger("workers.tasks", "resume_postprocess")
        
        # Helper function to log using the unified logger
        # This maintains backward compatibility with the old post_log() function signature
        def post_log(message: str, level: str = "info"):
            """Log message using the unified logger system."""
            # Remove [postprocess] prefix if present (we'll use logger facility instead)
            clean_message = message.replace("[postprocess] ", "")
            log_method = getattr(post_logger, level.lower(), post_logger.info)
            log_method(clean_message)

        # Check if files are already in transient (post-processing was partially completed).
        # Decide whether files were moved in a prior prep run (so rename
        # should be skipped). The check needs a per-rip signal under
        # #365 step 3b's MKVAUTO_RENAME_DIRECT_TO_DEST flag — when
        # trans_root is a shared library, walking it would always find
        # MKVs from prior rips and incorrectly flag a fresh job as
        # already-moved.
        #
        # Per-rip precedence (step 4d):
        # 1. Persisted job.post_paths (UUID-keyed) — the canonical signal
        #    that a prior prep completed successfully for THIS job.
        # 2. disc_payload.post_paths (UUID-keyed) — earlier persisted
        #    state from the same source.
        # 3. trans_root.rglob fallback — preserved for the no-persisted-
        #    state case (which is also where step 4b's walk fallback
        #    fires; both are unsafe under the flag but the surface is
        #    narrow). paths.raw is always per-job so walking it stays
        #    safe.
        files_already_moved = False
        transient_mkv_count = 0
        raw_mkv_count = 0
        if paths.raw.exists():
            try:
                raw_mkv_count = len(list(paths.raw.rglob("*.mkv")))
            except Exception as raw_exc:
                post_log(f"file_discovery: Error counting MKVs in raw {paths.raw}: {raw_exc}", "warning")

        def _keys_are_uuids(d):
            return d and all(len(str(k)) == 36 and "-" in str(k) for k in (d or {}))

        # Try per-rip signals first (safe under the shared-trans_root flag).
        persisted_post_paths = getattr(job, "post_paths", None) or {}
        restored_post_paths = (getattr(job, "disc_payload", None) or {}).get("post_paths") or {}
        has_per_rip_moved_signal = _keys_are_uuids(persisted_post_paths) or _keys_are_uuids(restored_post_paths)
        per_rip_moved_count = len(persisted_post_paths) if _keys_are_uuids(persisted_post_paths) else len(restored_post_paths) if _keys_are_uuids(restored_post_paths) else 0

        if has_per_rip_moved_signal and raw_mkv_count == 0:
            files_already_moved = True
            transient_mkv_count = per_rip_moved_count
            self.add_log(
                job,
                db,
                f"resume_postprocess: Found {per_rip_moved_count} per-rip post_paths entries and no MKVs in raw — "
                "treating as already moved, skipping rename step",
            )
        elif trans_root.exists():
            # Legacy / no-persisted-state path: walk trans_root. Under the
            # flag this can over-count, but the only consequence is
            # potentially skipping rename when raw is empty — which is the
            # correct outcome (nothing to rename).
            transient_mkv_count = len(list(trans_root.rglob("*.mkv")))
            if transient_mkv_count > 0 and raw_mkv_count == 0:
                files_already_moved = True
                self.add_log(
                    job,
                    db,
                    f"resume_postprocess: Found {transient_mkv_count} MKV file(s) at destination and none in raw — "
                    "treating as already moved, skipping rename step",
                )
            elif transient_mkv_count > 0 and raw_mkv_count > 0:
                self.add_log(
                    job,
                    db,
                    f"resume_postprocess: Found {transient_mkv_count} MKV(s) at destination and {raw_mkv_count} in raw — "
                    "will run rename (not skipping)",
                )
        
        source_dir = None
        if not files_already_moved:
            # Files not yet moved - look for them in raw directory
            # Log each directory check with detailed information
            directories_checked = []
            
            # Check raw directory
            raw_path = paths.raw
            exists = raw_path.exists()
            accessible = False
            mkv_count = 0
            if exists:
                try:
                    accessible = os.access(raw_path, os.R_OK)
                    if accessible:
                        mkv_count = sum(1 for _ in raw_path.rglob("*.mkv"))
                except Exception as exc:
                    post_log(f"file_discovery: Error checking raw {raw_path}: {exc}", "error")
            self.add_log(job, db, f"resume_postprocess: Checking raw: {raw_path} (exists={exists}, accessible={accessible}, mkv_count={mkv_count})")
            post_log(f"file_discovery: Checked raw: {raw_path} (exists={exists}, accessible={accessible}, mkv_count={mkv_count})", "debug")
            directories_checked.append(("raw", str(raw_path), exists, accessible, mkv_count))
            if exists and accessible and mkv_count > 0:
                source_dir = raw_path
                self.add_log(job, db, f"resume_postprocess: Selected raw as source: {raw_path} (found {mkv_count} MKV files)")
                post_log(f"file_discovery: Selected raw: {raw_path} (found {mkv_count} MKV files)", "info")
            
            # Log file listing if source_dir found
            if source_dir:
                try:
                    mkv_files = list(source_dir.rglob("*.mkv"))
                    self.add_log(job, db, f"resume_postprocess: Found {len(mkv_files)} MKV files in source directory {source_dir}")
                    post_log(f"file_discovery: Found {len(mkv_files)} MKV files in source directory {source_dir}", "info")
                    for mkv_file in mkv_files:
                        try:
                            size = mkv_file.stat().st_size
                            post_log(f"file_discovery:   - {mkv_file.name} ({size} bytes)", "debug")
                        except Exception:
                            post_log(f"file_discovery:   - {mkv_file.name}", "debug")
                except Exception as exc:
                    self.add_log(job, db, f"resume_postprocess: Warning: Error listing files in {source_dir}: {exc}")
                    post_log(f"file_discovery: Warning: Error listing files: {exc}", "warning")
            else:
                error_msg = f"No raw directory found to resume and no files in transient. Checked directories: {', '.join([f'{d[0]} ({d[2]} exists, {d[4]} MKV files)' for d in directories_checked])}"
                self.add_log(job, db, f"resume_postprocess: {error_msg}")
                post_log(f"file_discovery: {error_msg}", "error")
                _post_postprocess_complete_callback(job_id, success=False, error_reason=error_msg)
                return
        disc = Disc(job.disc_num, job.mount_point)
        # Load disc map - use source_dir if files not moved, otherwise use raw (where disc map should still exist)
        disc_map_source = None
        if not files_already_moved and source_dir:
            disc_map_source = source_dir
        elif files_already_moved:
            # If files already moved, try to load disc map from raw (where it should still exist)
            disc_map_source = paths.raw if paths.raw.exists() else source_dir
        else:
            disc_map_source = paths.raw if paths.raw.exists() else source_dir
        try:
            if disc_map_source:
                self.add_log(job, db, f"resume_postprocess: Loading disc map from {disc_map_source}")
                disc.load_disc_map(str(disc_map_source))
            # If raw/ had no titles_map (e.g. DiscDB miss), try metadata/ where rip may have written it
            if not disc.titles and paths.metadata.exists():
                metadata_as_source = paths.metadata
                self.add_log(job, db, f"resume_postprocess: No titles from disc map source, trying {metadata_as_source}")
                try:
                    disc.load_disc_map(str(metadata_as_source))
                except Exception:
                    pass
            # If still no titles but we have ripped_files (e.g. DiscDB miss, no titles_map on disk), build minimal map so _rename_series can run
            _ripped = getattr(job, "ripped_files", None) or (job.disc_payload or {}).get("ripped_files") or {}
            if not disc.titles and _ripped:
                import re
                built_titles = {}
                built_db_mapping = {}
                for _tid, src in _ripped.items():
                    if not src or not isinstance(src, str):
                        continue
                    base = os.path.basename(src.strip())
                    m = re.match(r".*_t(\d+)\.mkv$", base, re.IGNORECASE)
                    if m:
                        tid = int(m.group(1))
                        entry = {"file": base}
                        built_titles[tid] = entry
                        built_db_mapping[str(tid)] = entry
                if built_titles:
                    disc.titles = built_titles
                    disc.db_mapping = built_db_mapping
                    self.add_log(job, db, f"resume_postprocess: Built disc map from payload for {len(built_titles)} titles (no titles_map on disk)")
        except Exception as exc:
            error_msg = f"Failed to load disc map for resume: {exc}"
            self.add_log(job, db, f"resume_postprocess: {error_msg}")
            _post_postprocess_complete_callback(job_id, success=False, error_reason=str(exc))
            return
        # Set disc.log_fn to use the logger (maintains backward compatibility)
        disc.log_fn = lambda msg: post_log(msg, "info")

        # If files are already in transient, skip file count check in source_dir
        if not files_already_moved:
            # Verify we have a full set of ripped titles before attempting post-process.
            expected_count = 0
            if disc.titles:
                expected_count = len(disc.titles)
            elif hasattr(job, "disc") and job.disc and job.disc.disc_info:
                titles_map = job.disc.disc_info.get("titles") or job.disc.disc_info.get("titles_map")
                if isinstance(titles_map, dict):
                    expected_count = len(titles_map)
            elif job.disc_payload:
                titles_map = job.disc_payload.get("titles") or job.disc_payload.get("tracks")
                if isinstance(titles_map, dict):
                    expected_count = len(titles_map)
            # For discdb hits, expect only the selected titles (title_filename_map), not the full disc map
            payload = job.disc_payload or {}
            if payload.get("discdb_hit") and isinstance(payload.get("title_filename_map"), dict) and payload["title_filename_map"]:
                expected_count = len(payload["title_filename_map"])
            # Subtract titles marked 'ignore' — they have no destination and don't need a source file
            # in raw (their content may be a duplicate of another title that postprocess never moves
            # for ignored rows). Without this, a disc with auto-ignored duplicates would always trip
            # the count check on retry after sync filled some primaries with 'ignore'.
            if disc.titles:
                ignored_count = sum(
                    1 for t in disc.titles
                    if (getattr(t, "type", None) or "").strip().lower() == "ignore"
                )
                if ignored_count:
                    expected_count = max(0, expected_count - ignored_count)
            actual_count = 0
            if source_dir and source_dir.exists():
                for _, _, filenames in os.walk(source_dir):
                    actual_count += sum(1 for fn in filenames if fn.lower().endswith(".mkv"))
            # Resume case: a partially-completed postprocess may have moved
            # some MKVs from raw to the destination already.
            # _rename_movie / _rename_series detect already-renamed files
            # via source_hashes and skip them, so include the destination
            # count to avoid falsely aborting the retry.
            #
            # Phase 2 (#365 step 4e — last of 5 trans_root walks in this
            # function): prefer the per-rip post_paths count over a walk
            # of trans_root, which under MKVAUTO_RENAME_DIRECT_TO_DEST may
            # be a shared library. Walking the library would over-count
            # massively here and mask real resume failures (actual_count
            # passes the threshold based on unrelated library files).
            persisted_post_paths_for_count = getattr(job, "post_paths", None) or {}
            restored_post_paths_for_count = (
                getattr(job, "disc_payload", None) or {}
            ).get("post_paths") or {}

            def _keys_are_uuids_local(d):
                return d and all(len(str(k)) == 36 and "-" in str(k) for k in (d or {}))

            if _keys_are_uuids_local(persisted_post_paths_for_count):
                actual_count += len(persisted_post_paths_for_count)
            elif _keys_are_uuids_local(restored_post_paths_for_count):
                actual_count += len(restored_post_paths_for_count)
            elif trans_root.exists():
                # Fallback walk (only when no per-rip moved signal exists).
                # Under the flag this can over-count but only blocks the
                # abort path — the failure mode is "less likely to abort
                # incorrectly," not "loses files."
                for _, _, filenames in os.walk(str(trans_root)):
                    actual_count += sum(1 for fn in filenames if fn.lower().endswith(".mkv"))
            if expected_count and actual_count < expected_count:
                msg = f"Resume aborted: only {actual_count}/{expected_count} titles found; rerun rip."
                self.add_log(job, db, msg)
                _post_postprocess_complete_callback(job_id, success=False, error_reason=msg)
                # if source_dir:
                #     self.cleanup_dirs(job, [str(source_dir)])
                return
            if actual_count == 0:
                msg = "Resume aborted: no MKV files found to post-process; rerun rip."
                self.add_log(job, db, msg)
                _post_postprocess_complete_callback(job_id, success=False, error_reason=msg)
                # if source_dir:
                #     self.cleanup_dirs(job, [str(source_dir)])
                return
        try:
            # Dev mode: one process before postprocess — move MKV to backup, create mocks in raw,
            # compute/store mock mkv_size, update source_hashes. Runs first, only when raw has real (large) MKVs.
            from core.utils import is_dev_mode
            is_dev = is_dev_mode()
            quick_tests_enabled = settings.get_quick_postprocess_tests_enabled()
            post_log(f"devmode_check: is_dev_mode={is_dev}, quick_postprocess_tests_enabled={quick_tests_enabled}, source_dir={source_dir}, source_exists={source_dir.exists() if source_dir else False}", "info")
            self.add_log(job, db, f"resume_postprocess: Dev mode check - is_dev_mode={is_dev}, quick_postprocess_tests_enabled={quick_tests_enabled}")
            
            if is_dev and source_dir and source_dir.exists():
                pass
            else:
                if not is_dev:
                    post_log(f"devmode_prep: Skipped - dev mode not enabled", "info")
                    self.add_log(job, db, f"resume_postprocess: Dev mode prep skipped - dev mode not enabled")
                elif not source_dir or not source_dir.exists():
                    post_log(f"devmode_prep: Skipped - source_dir not available (source_dir={source_dir}, exists={source_dir.exists() if source_dir else False})", "info")
                    self.add_log(job, db, f"resume_postprocess: Dev mode prep skipped - source directory not available")

            # After devmode prep: raw MKV quiescence, then refresh disc_titles.mkv_size from disk (skip if already in transient).
            if (
                not files_already_moved
                and source_dir
                and source_dir.exists()
                and any(source_dir.rglob("*.mkv"))
            ):
                from workers.rip_raw_ready import mkv_sizes_by_relpath, wait_ripped_mkvs_quiescent

                ripped_for_sync = getattr(job, "ripped_files", None) or (job.disc_payload or {}).get("ripped_files") or {}
                if not isinstance(ripped_for_sync, dict):
                    ripped_for_sync = {}
                rel_paths_set = set()
                for v in ripped_for_sync.values():
                    if v and isinstance(v, str):
                        rel_paths_set.add(str(v).replace("\\", "/"))
                if not rel_paths_set:
                    rel_paths_set.update(mkv_sizes_by_relpath(source_dir).keys())
                rel_paths_quiesce = sorted(rel_paths_set)
                # wait_ripped_mkvs_quiescent never completes while any file is 0 bytes (all_ok stays false).
                # Skip the wait in that case and let pre-flight report invalid sources.
                zero_byte_rels = []
                for _rel in rel_paths_quiesce:
                    _p = (source_dir / _rel).resolve()
                    if _p.is_file():
                        try:
                            if _p.stat().st_size == 0:
                                zero_byte_rels.append(_rel)
                        except OSError:
                            pass
                if rel_paths_quiesce and not zero_byte_rels:
                    try:
                        post_log(
                            f"postprocess_raw_quiescence: waiting on {len(rel_paths_quiesce)} path(s) under {source_dir}",
                            "info",
                        )
                        self.add_log(
                            job,
                            db,
                            f"resume_postprocess: Waiting for raw MKV size quiescence ({len(rel_paths_quiesce)} file(s)) before pre-flight",
                        )
                        wait_ripped_mkvs_quiescent(
                            source_dir,
                            rel_paths_quiesce,
                            log_fn=lambda msg: post_log(f"postprocess_raw_quiescence: {msg}", "info"),
                        )
                    except RuntimeError as qu_exc:
                        error_msg = f"Raw MKV quiescence wait failed before post-process: {qu_exc}"
                        post_log(error_msg, "error")
                        self.add_log(job, db, f"resume_postprocess: {error_msg}")
                        log.error("resume_postprocess: job_id=%s %s", job_id, error_msg)
                        _post_postprocess_complete_callback(job_id, success=False, error_reason=error_msg)
                        return
                elif zero_byte_rels:
                    post_log(
                        f"postprocess_raw_quiescence: skipped (empty source file(s): {zero_byte_rels})",
                        "warning",
                    )
                    self.add_log(
                        job,
                        db,
                        f"resume_postprocess: Skipping quiescence wait — zero-byte raw MKV(s): {', '.join(zero_byte_rels)}",
                    )

                if ripped_for_sync:

                    def _on_mkv_sync_err(m: str) -> None:
                        post_log(f"postprocess_mkv_size_sync: {m}", "warning")
                        self.add_log(job, db, f"resume_postprocess: {m}")

                    _sync_disc_title_mkv_sizes_from_ripped(
                        db,
                        source_dir,
                        ripped_for_sync,
                        disc_id,
                        on_error=_on_mkv_sync_err,
                    )
                    # Flush only: a commit here can release the SQLite txn while this session is still in use
                    # and collide with the postprocess_complete callback session (database is locked).
                    db.flush()
                    post_log("postprocess_mkv_size_sync: refreshed disc_titles.mkv_size from quiescent raw files", "info")
                    self.add_log(job, db, "resume_postprocess: Refreshed disc_titles.mkv_size from disk after quiescence")
                else:
                    post_log(
                        "postprocess_mkv_size_sync: skipped (no ripped_files in job or payload; quiescence ran on all raw MKVs)",
                        "info",
                    )
                    self.add_log(
                        job,
                        db,
                        "resume_postprocess: No ripped_files mapping to sync mkv_size (quiescence completed on raw tree)",
                    )

            # Pre-flight validation: source files exist, non-empty; mkv_size gates relaxed in stage_validation (see post-quiescence sync above).
            # Runs AFTER devmode prep and raw quiescence + mkv_size refresh so validation sees stable files and updated rows.
            try:
                from core.stage_validation import validate_transfer_preconditions
                post_log("preflight_validation: Starting pre-flight validation checks", "info")
                self.add_log(job, db, "resume_postprocess: Running pre-flight validation checks")
                validation_result = validate_transfer_preconditions(job, db, paths)
                if not validation_result.valid:
                    error_details = "; ".join(validation_result.errors)
                    if validation_result.warnings:
                        warnings_str = "; ".join(validation_result.warnings)
                        error_details += f" (warnings: {warnings_str})"
                    error_msg = f"Pre-flight validation failed: {error_details}"
                    post_log(f"preflight_validation: {error_msg}", "error")
                    self.add_log(job, db, f"resume_postprocess: {error_msg}")
                    log.error("resume_postprocess: job_id=%s FAILED at pre-flight validation: %s", job_id, error_details, extra={"job_id": job_id, "error_type": "preflight_validation", "error_details": error_details})
                    _post_postprocess_complete_callback(job_id, success=False, error_reason=error_details)
                    return
                if validation_result.warnings:
                    warnings_str = "; ".join(validation_result.warnings)
                    post_log(f"preflight_validation: Passed with warnings: {warnings_str}", "info")
                    self.add_log(job, db, f"resume_postprocess: Pre-flight validation passed with warnings: {warnings_str}")
                else:
                    post_log("preflight_validation: Passed", "info")
                    self.add_log(job, db, "resume_postprocess: Pre-flight validation passed")
            except Exception as val_exc:
                error_msg = f"Pre-flight validation error: {val_exc}"
                post_log(f"preflight_validation: {error_msg}", "error")
                self.add_log(job, db, f"resume_postprocess: {error_msg}")
                log.error("resume_postprocess: job_id=%s FAILED at pre-flight validation exception: %s", job_id, str(val_exc), exc_info=True, extra={"job_id": job_id, "error_type": "preflight_validation_exception", "exception_type": type(val_exc).__name__})
                _post_postprocess_complete_callback(job_id, success=False, error_reason=str(val_exc))
                return

            self.add_log(job, db, "resume_postprocess: Resuming post-processing: renaming outputs")
            # Phase 2 collapse (#365): rename destination is resolved through
            # a helper so future PRs can switch local-mode jobs to write
            # directly to the transfer destination (eliminating transient/
            # entirely for that case) without churning the call site again.
            # Helper currently returns paths.transient unconditionally —
            # the env-var + active-TransferConfig logic that will flip it
            # to a destination path lands in the next migration step.
            trans_root = _resolve_rename_dest_root(job, paths, db)
            trans_root.mkdir(parents=True, exist_ok=True)
            self.add_log(job, db, f"resume_postprocess: Rename destination: {trans_root}")
            
            # Get release and movie info for new path structure
            release = getattr(job.disc, "release", None) if job.disc else None
            movie = getattr(release, "movie", None) if release else None
            release_type = None
            movie_name = None
            production_year = None
            release_name = None
            if release:
                release_type = getattr(release, "type", None) or "movie"
                release_name = getattr(release, "name", None)
            if movie:
                movie_name = getattr(movie, "name", None)
                production_year = getattr(movie, "production_year", None)
                # Use movie.tmdb_type (tv vs movie) for folder and rename branch, not release.type (movie/boxset)
                tmdb_type = (getattr(movie, "tmdb_type", None) or "").strip().lower()
                if tmdb_type in ("tv", "series"):
                    release_type = "tv"
                    disc.title_type = "Series"
                elif tmdb_type == "movie":
                    release_type = "movie"
                    disc.title_type = "Movie"
                # else: keep release_type from release; disc.title_type stays as set earlier or None
            
            # Get ripped_files from job (rip stage) or post_paths (post-process stage) - both have title_id keys
            disc_payload_check = job.disc_payload or {}
            ripped_files_from_payload = getattr(job, "ripped_files", None) or disc_payload_check.get("ripped_files") or {}
            post_paths_from_payload = getattr(job, "post_paths", None) or disc_payload_check.get("post_paths") or {}
            # Prefer ripped_files (files in raw/) for resume_postprocess
            file_paths_from_payload = ripped_files_from_payload if ripped_files_from_payload else post_paths_from_payload
            
            # Count rename steps
            rename_steps = 0
            if not files_already_moved:
                if file_paths_from_payload:
                    rename_steps = len(file_paths_from_payload)
                elif source_dir:
                    rename_steps = len(list(source_dir.rglob("*.mkv"))) if source_dir.exists() else 0
            
            # Rename phase only (no hashing); progress 0-100% over rename steps
            total_steps_estimate = rename_steps
            step_weight = 100 / total_steps_estimate if total_steps_estimate > 0 else 0
            
            # Initialize renamed_paths - will be populated by rename_outputs if files are moved
            renamed_paths = {}
            # When new path is used but no files moved, use count of non-ignore titles for verification message
            expected_count_if_no_renames = None
            
            # Skip rename if files are already in transient
            if not files_already_moved:
                # Progress callback for renaming (uses old signature for backward compat with rename_outputs)
                def update_rename_progress(done: int, total: int, filename: str):
                    # Each rename step contributes step_weight
                    if total > 0:
                        completed_rename_steps = done
                        post_progress = int(completed_rename_steps * step_weight)
                        try:
                            self.set_status(job, db, post_progress=post_progress)
                        except Exception:
                            pass
                
                # Use new path structure if we have all required info
                if job.id and release_type and movie_name:
                    # Build title_id -> title, type, source_file, edition, resolution from disc_titles
                    title_id_to_title = {}
                    title_id_to_type = {}
                    title_id_to_source_file = {}
                    title_id_to_edition = {}
                    title_id_to_resolution = {}
                    title_id_to_season = {}
                    title_id_to_episode = {}
                    title_id_to_part = {}
                    title_id_to_episode_end = {}
                    def _normalize_resolution_from_title(title_obj) -> str | None:
                        try:
                            metadata_scan = getattr(title_obj, "metadata_scan", None)
                            streams = getattr(title_obj, "streams", None)
                            if isinstance(metadata_scan, str):
                                try:
                                    metadata_scan = json.loads(metadata_scan)
                                except Exception:
                                    metadata_scan = None
                            if isinstance(streams, str):
                                try:
                                    streams = json.loads(streams)
                                except Exception:
                                    streams = None

                            height = None
                            if isinstance(metadata_scan, dict):
                                video_hints = metadata_scan.get("video_hints") or {}
                                height = video_hints.get("height") or video_hints.get("Height")
                            if not height and isinstance(streams, list):
                                for stream in streams:
                                    if not isinstance(stream, dict):
                                        continue
                                    stream_type = str(stream.get("type") or stream.get("Type") or "").lower()
                                    if stream_type and stream_type != "video":
                                        continue
                                    res_str = stream.get("resolution") or stream.get("Resolution")
                                    if isinstance(res_str, str) and "x" in res_str:
                                        try:
                                            height = int(res_str.split("x")[-1])
                                        except Exception:
                                            height = None
                                    if height:
                                        break

                            if not height:
                                return None
                            if height >= 2000:
                                return "2160p"
                            if height >= 1000:
                                return "1080p"
                            if height >= 700:
                                return "720p"
                            if height >= 480:
                                return "480p"
                            return None
                        except Exception:
                            return None
                    try:
                        disc_id = getattr(job.disc, "id", None) if job.disc else None
                        if disc_id:
                            from api import models as db_models
                            disc_titles = db.query(db_models.DiscTitle).filter(
                                db_models.DiscTitle.disc_id == disc_id
                            ).all()
                            for title in disc_titles:
                                if title.id:
                                    title_id_str = str(title.id)
                                    if title.title:
                                        title_id_to_title[title_id_str] = title.title
                                    if title.type:
                                        title_id_to_type[title_id_str] = title.type
                                    if title.source_file:
                                        title_id_to_source_file[title_id_str] = title.source_file
                                    if getattr(title, "edition", None):
                                        title_id_to_edition[title_id_str] = title.edition
                                    if getattr(title, "season", None) is not None:
                                        title_id_to_season[title_id_str] = title.season
                                    if getattr(title, "episode", None) is not None:
                                        title_id_to_episode[title_id_str] = title.episode
                                    # Multi-part layout (#796)
                                    if getattr(title, "part", None) is not None:
                                        title_id_to_part[title_id_str] = title.part
                                    if getattr(title, "episode_end", None) is not None:
                                        title_id_to_episode_end[title_id_str] = title.episode_end
                                    res = _normalize_resolution_from_title(title)
                                    if res:
                                        title_id_to_resolution[title_id_str] = res
                    except Exception as exc:
                        # If we can't build the mapping, log but continue (fallback to old behavior)
                        self.add_log(job, db, f"Warning: Could not build title_id mappings: {exc}")
                    # Prefer ripped filename (MKV on disk) over DB source_file (segment) for rename:
                    # DB has source_file=segment (e.g. 00011.m2ts); we need MKV filename so _rename_series
                    # can match fn from the filesystem and resolve title_id. DiscDB miss + series otherwise
                    # skips all files because source_file_to_title_id is keyed by segment and fn is MKV name.
                    for tid, src in (ripped_files_from_payload or {}).items():
                        if src and isinstance(src, str):
                            title_id_to_source_file[str(tid)] = src.strip()
                    # Count of non-ignore titles for post-move expected count when renamed_paths is empty
                    expected_count_if_no_renames = sum(
                        1 for tid in (file_paths_from_payload or {})
                        if (title_id_to_type or {}).get(str(tid), "").strip().lower() != "ignore"
                    )
                    # Get source_hashes from disc_payload for hash verification of already-processed files
                    disc_payload_rename = job.disc_payload or {}
                    source_hashes_rename = disc_payload_rename.get("source_hashes", {})
                    # DiscDB miss: disc.title_type is never set from disc_info; ensure series uses _rename_series
                    if release_type and str(release_type).strip().lower() in ("tv", "series"):
                        disc.title_type = "Series"
                    try:
                        # Phase 2 collapse (#365): pass dest_root explicitly
                        # so the destination decision is owned by the caller
                        # (the prep code) rather than implicit in
                        # rename_outputs' default. trans_root currently
                        # points at jobs/<id>/transient/ — the historical
                        # location — so this is a no-op behaviour change.
                        # Future PRs in the transient/ drop will set
                        # dest_root to the actual transfer destination (or
                        # a transfer-protocol-appropriate pre-staging
                        # area) so transient/ stops being created.
                        # Extras that share a name with a lower-numbered sibling
                        # disc of this release get " (Disc N)" (#831).
                        _reserved_extra_names: set = set()
                        try:
                            from core.extra_name_collisions import reserved_extra_names_for_disc
                            if job.disc:
                                _reserved_extra_names = reserved_extra_names_for_disc(
                                    job.disc, settings.get_media_server()
                                )
                        except Exception as _exc:
                            log.warning("resume_postprocess: reserved extra names lookup failed: %s", _exc)
                        renamed_paths = disc.rename_outputs(
                            str(source_dir),
                            job_id=job.id,
                            release_type=release_type,
                            movie_name=movie_name,
                            production_year=production_year,
                            release_name=release_name,
                            final_paths=file_paths_from_payload,  # Has title_id keys
                            source_file_to_title=None,  # Deprecated
                            source_file_to_type=None,  # Deprecated
                            title_id_to_title=title_id_to_title,
                            title_id_to_type=title_id_to_type,
                            title_id_to_source_file=title_id_to_source_file,
                            title_id_to_edition=title_id_to_edition,
                            title_id_to_resolution=title_id_to_resolution,
                            title_id_to_season=title_id_to_season,
                            title_id_to_episode=title_id_to_episode,
                            title_id_to_part=title_id_to_part,
                            title_id_to_episode_end=title_id_to_episode_end,
                            progress_cb=update_rename_progress,
                            source_hashes=source_hashes_rename,
                            media_server=settings.get_media_server(),
                            dest_root=trans_root,
                            disc_number=getattr(job.disc, "disc_number", None) if job.disc else None,
                            reserved_extra_names=_reserved_extra_names,
                        )
                        log.info("resume_postprocess: job_id=%s rename_outputs returned %s paths", job_id, len(renamed_paths) if renamed_paths else 0)
                        if not renamed_paths:
                            log.warning("resume_postprocess: job_id=%s rename_outputs returned empty mapping - no files were moved", job_id)
                    except Exception as rename_exc:
                        error_msg = f"Post-process error during rename_outputs: {type(rename_exc).__name__}: {str(rename_exc)}"
                        self.add_log(job, db, error_msg)
                        post_log(f"rename_outputs: ERROR - {error_msg}", "error")
                        log.error("resume_postprocess: job_id=%s FAILED at rename_outputs: %s", job_id, error_msg, exc_info=True, extra={"job_id": job_id, "error_type": "rename_outputs_exception", "exception_type": type(rename_exc).__name__})
                        _post_postprocess_complete_callback(job_id, success=False, error_reason=error_msg)
                        return
                else:
                    # Fallback to legacy behavior
                    _apply_release_title(disc, job)
                    prev_lib_root = os.getenv("MAKEMKV_LIBRARY_ROOT")
                    os.environ["MAKEMKV_LIBRARY_ROOT"] = str(trans_root)
                    try:
                        # Get source_hashes from disc_payload for hash verification of already-processed files
                        disc_payload_rename = job.disc_payload or {}
                        source_hashes_rename = disc_payload_rename.get("source_hashes", {})
                        renamed_paths = disc.rename_outputs(str(source_dir), progress_cb=update_rename_progress, source_hashes=source_hashes_rename)
                        # renamed_paths will be empty dict for legacy path (no job_id)
                    finally:
                        if prev_lib_root is None:
                            os.environ.pop("MAKEMKV_LIBRARY_ROOT", None)
                        else:
                            os.environ["MAKEMKV_LIBRARY_ROOT"] = prev_lib_root
                
                # Post-move verification: verify files actually exist in the rename destination
                # after rename_outputs completes.
                #
                # Phase 2 collapse (#365 step 4a): walk only this rip's
                # files via renamed_paths instead of rglob'ing trans_root.
                # When trans_root is paths.transient (the historical
                # default), both approaches give the same answer because
                # transient/ contains only this rip's output. When the
                # MKVAUTO_RENAME_DIRECT_TO_DEST flag is on and trans_root
                # points at a shared library, the per-rip walk is the
                # only correct one — rglob would count every MKV in the
                # library and the verification would fail wildly.
                #
                # Fallback: when renamed_paths is empty (no rename ran —
                # legacy MAKEMKV_LIBRARY_ROOT path) we still rglob, since
                # there's no other source of truth.
                self.add_log(job, db, "resume_postprocess: Verifying renamed files exist at destination")
                post_log("post_move_verification: Starting verification after rename_outputs", "info")
                try:
                    if renamed_paths:
                        # Per-rip walk: derive absolute paths from the
                        # rename output and check each one. Files renamed
                        # to identical name (idempotent re-run) and
                        # files moved fresh both show up as existing.
                        transient_mkv_files = [
                            trans_root / rel for rel in renamed_paths.values()
                            if (trans_root / rel).exists()
                        ]
                    else:
                        # Legacy fallback: walk the whole tree. Safe when
                        # trans_root is per-job transient/; unsafe when
                        # it's a shared library (the flag's caveat).
                        transient_mkv_files = list(trans_root.rglob("*.mkv"))
                    transient_mkv_count = len(transient_mkv_files)
                    self.add_log(job, db, f"resume_postprocess: Found {transient_mkv_count} MKV files at destination after move")
                    post_log(f"post_move_verification: Found {transient_mkv_count} MKV files at destination", "info")
                    # When we have renamed_paths from rename_outputs, expect only the files we actually moved (selected titles).
                    # When renamed_paths is empty but we have expected_count_if_no_renames (new path), use non-ignore count so message says 0/3 not 0/10.
                    use_renamed_for_expected = bool(renamed_paths)
                    if use_renamed_for_expected:
                        expected_count = len(renamed_paths)
                        expected_files = set(str(p).replace("\\", "/") for p in renamed_paths.values())
                    elif expected_count_if_no_renames is not None:
                        expected_count = expected_count_if_no_renames
                        expected_files = set()  # No path list when nothing was moved; count is correct for error message
                    elif file_paths_from_payload:
                        expected_count = len(file_paths_from_payload)
                        expected_files = set(str(p).replace("\\", "/") for p in file_paths_from_payload.values())
                    else:
                        expected_count = 0
                        expected_files = set()
                    found_files = {str(f.relative_to(trans_root)).replace("\\", "/") for f in transient_mkv_files}
                    if expected_count > 0:
                        if transient_mkv_count < expected_count:
                            missing_count = expected_count - transient_mkv_count
                            missing_files = expected_files - found_files
                            error_msg = f"Post-move verification failed: Only {transient_mkv_count}/{expected_count} files found in transient directory ({missing_count} missing)"
                            self.add_log(job, db, f"resume_postprocess: {error_msg}")
                            post_log(f"post_move_verification: ERROR - {error_msg}", "error")
                            for missing_file in list(missing_files)[:20]:
                                self.add_log(job, db, f"resume_postprocess: Missing file: {missing_file}")
                                post_log(f"post_move_verification: Missing file: {missing_file}", "error")
                            if len(missing_files) > 20:
                                self.add_log(job, db, f"resume_postprocess: ... and {len(missing_files) - 20} more missing")
                            log.error("resume_postprocess: job_id=%s FAILED at post-move verification (missing files): %s (expected=%s, found=%s, missing=%s)", job_id, error_msg, expected_count, transient_mkv_count, len(missing_files), extra={"job_id": job_id, "error_type": "post_move_verification_missing", "expected_count": expected_count, "found_count": transient_mkv_count, "missing_files": list(missing_files)})
                            _post_postprocess_complete_callback(job_id, success=False, error_reason=error_msg)
                            return
                        else:
                            self.add_log(job, db, f"resume_postprocess: Post-move verification passed: {transient_mkv_count} files found (expected {expected_count})")
                            post_log(f"post_move_verification: Passed - {transient_mkv_count} files found (expected {expected_count})", "info")
                            for mkv_file in transient_mkv_files:
                                try:
                                    size = mkv_file.stat().st_size
                                    rel_path = mkv_file.relative_to(trans_root)
                                    post_log(f"post_move_verification:   - {rel_path} ({size} bytes)", "debug")
                                except Exception:
                                    rel_path = mkv_file.relative_to(trans_root)
                                    post_log(f"post_move_verification:   - {rel_path}", "debug")
                    elif transient_mkv_count == 0:
                        error_msg = "Post-move verification failed: No MKV files found in transient directory after move"
                        self.add_log(job, db, f"resume_postprocess: {error_msg}")
                        post_log(f"post_move_verification: ERROR - {error_msg}", "error")
                        log.error("resume_postprocess: job_id=%s FAILED at post-move verification (zero files): %s (transient_dir=%s)", job_id, error_msg, str(trans_root), extra={"job_id": job_id, "error_type": "post_move_verification_zero", "transient_dir": str(trans_root)})
                        _post_postprocess_complete_callback(job_id, success=False, error_reason=error_msg)
                        return
                    else:
                        self.add_log(job, db, f"resume_postprocess: Post-move verification passed: {transient_mkv_count} files found in transient")
                        post_log(f"post_move_verification: Passed - {transient_mkv_count} files found", "info")
                        # Log each file found
                        for mkv_file in transient_mkv_files:
                            try:
                                size = mkv_file.stat().st_size
                                rel_path = mkv_file.relative_to(trans_root)
                                post_log(f"post_move_verification:   - {rel_path} ({size} bytes)", "debug")
                            except Exception:
                                rel_path = mkv_file.relative_to(trans_root)
                                post_log(f"post_move_verification:   - {rel_path}", "debug")
                except Exception as exc:
                    error_msg = f"Post-move verification error: {exc}"
                    self.add_log(job, db, f"resume_postprocess: {error_msg}")
                    post_log(f"post_move_verification: ERROR - {error_msg}", "error")
                    log.error("resume_postprocess: job_id=%s FAILED at post-move verification exception: %s", job_id, error_msg, exc_info=True, extra={"job_id": job_id, "error_type": "post_move_verification_exception", "exception_type": type(exc).__name__})
                    _post_postprocess_complete_callback(job_id, success=False, error_reason=error_msg)
                    return
            else:
                self.add_log(job, db, f"resume_postprocess: Skipping rename_outputs - files already at destination")
                post_log("post_move_verification: Skipped - files already at destination", "info")
                # files_already_moved branch (typical for reverted/resumed jobs).
                # Prefer per-rip authoritative sources over a walk of trans_root,
                # because under #365 step 3b's MKVAUTO_RENAME_DIRECT_TO_DEST flag
                # trans_root may resolve to a shared library and the walk would
                # discover unrelated MKVs from prior rips.
                def _keys_are_uuids(d):
                    return d and all(len(str(k)) == 36 and "-" in str(k) for k in (d or {}))

                # 1. Restored disc_payload post_paths (UUID-keyed → trustworthy
                #    per-rip mapping written by an earlier prep that already ran).
                restored = (job.disc_payload or {}).get("post_paths") or {}
                # 2. job.post_paths column (canonical persisted post_paths).
                persisted = getattr(job, "post_paths", None) or {}

                if _keys_are_uuids(restored):
                    renamed_paths = restored
                    log.info(
                        "resume_postprocess: job_id=%s Using restored post_paths from disc_payload (files_already_moved=True, count=%s)",
                        job_id, len(renamed_paths),
                    )
                elif _keys_are_uuids(persisted):
                    renamed_paths = persisted
                    log.info(
                        "resume_postprocess: job_id=%s Using job.post_paths (files_already_moved=True, count=%s)",
                        job_id, len(renamed_paths),
                    )
                else:
                    # Phase 2 (#365 step 4b): fall back to gather_final_outputs
                    # ONLY when we have neither a restored nor a persisted
                    # per-rip mapping. The gather_final_outputs walk uses
                    # disc_titles.comment as the per-title filename anchor —
                    # safe even on a shared trans_root because it filters by
                    # disc-specific output names — but pass job.post_paths as
                    # final_paths if non-empty (skips the walk entirely).
                    try:
                        disc_id = getattr(job.disc, "id", None) if job.disc else None
                        seed_paths = persisted or restored or None
                        renamed_paths, _ = self.gather_final_outputs(
                            trans_root, seed_paths, progress_cb=None, disc_id=disc_id, db=db, skip_hashes=True
                        )
                        log.info(
                            "resume_postprocess: job_id=%s Discovered %s paths from destination (files_already_moved=True, seed=%s)",
                            job_id, len(renamed_paths) if renamed_paths else 0,
                            "post_paths" if seed_paths else "disc_titles_walk",
                        )
                    except Exception as gather_exc:
                        log.warning(
                            "resume_postprocess: job_id=%s Failed to discover files at destination (files_already_moved=True): %s",
                            job_id, gather_exc,
                        )
                        renamed_paths = {}
            
            final_root = trans_root
            # Phase 2 (#365 step 4c): pick the read root.
            # - files_already_moved → files are already at trans_root
            # - rename produced renamed_paths → use trans_root
            # - otherwise → fall back to source_dir (rip output) if it exists
            #
            # The pre-step-4c version walked trans_root with rglob to decide
            # whether to use it. That walk is unsafe under the
            # MKVAUTO_RENAME_DIRECT_TO_DEST flag because trans_root may be a
            # shared library: rglob would always see MKVs (from prior rips)
            # and we'd never fall back to source_dir even when rename
            # actually produced nothing for THIS rip. The renamed_paths
            # bool check is the per-rip authoritative signal.
            if files_already_moved or renamed_paths:
                active_root = trans_root
            elif source_dir and source_dir.exists():
                active_root = source_dir
            else:
                active_root = trans_root

            validation_error = None
            post_paths = {}
            final_hashes = {}
            
            try:
                initial_progress = 100  # Rename phase is 0-100%; we're done
                self.set_status(job, db, job_status='validating', rip_progress=100, post_progress=initial_progress)
                disc_id = getattr(job.disc, "id", None) if job.disc else None

                # Use mapping captured during rename_outputs (or discovered if files_already_moved)
                if not renamed_paths:
                    # This should not happen in normal flow - log warning
                    log.warning("resume_postprocess: job_id=%s No renamed_paths available - rename_outputs may have failed or no files found", job_id)
                    post_paths = {}
                    final_hashes = {}
                else:
                    # Use mapping from rename_outputs (or gather_final_outputs if files_already_moved)
                    post_paths = renamed_paths
                    final_hashes = {}  # Will be calculated later if needed
                
                self.set_status(job, db, post_progress=100)
                
                # Set post_paths and title_filename_map on job BEFORE validation so generate_expected_transfer_prep_output can use it
                # This ensures expected_files uses title_id keys instead of filename keys
                if post_paths:
                    try:
                        # Build title_filename_map from post_paths (maps title_id -> renamed filename)
                        title_filename_map = {}
                        for title_id, rel_path in post_paths.items():
                            if title_id and rel_path:
                                # Extract filename from relative path
                                filename = os.path.basename(rel_path)
                                title_filename_map[str(title_id)] = filename
                        
                        # Update disc_payload with post_paths and title_filename_map so validation can access them
                        disc_payload_temp = job.disc_payload or {}
                        disc_payload_temp["post_paths"] = post_paths
                        disc_payload_temp["title_filename_map"] = title_filename_map
                        self.set_status(job, db, disc_payload=disc_payload_temp)
                        # Also set on job object directly for immediate access
                        job.post_paths = post_paths
                        db.flush()  # Ensure it's available for validation
                        log.info("resume_postprocess: job_id=%s Set post_paths (count=%s) and title_filename_map (count=%s) before validation", job_id, len(post_paths), len(title_filename_map))
                    except Exception as set_exc:
                        log.warning("resume_postprocess: job_id=%s Failed to set post_paths before validation: %s", job_id, set_exc)
                
                # Validate post-process output
                try:
                    from core.stage_validation import validate_transfer_prep_output
                    from core.transfer.path_resolution import resolve_transfer_prep_validation_root
                    log.info(
                        "resume_postprocess: job_id=%s running validate_transfer_prep_output (dest=%s)",
                        job_id, str(resolve_transfer_prep_validation_root(job, paths, db)),
                    )
                    validation_result = validate_transfer_prep_output(job, db, paths)
                    log.info(
                        "resume_postprocess: job_id=%s validate_transfer_prep_output valid=%s errors=%s warnings=%s",
                        job_id, validation_result.valid, validation_result.errors, validation_result.warnings
                    )
                    if not validation_result.valid:
                        errors_str = "; ".join(validation_result.errors)
                        validation_error = f"Post-process validation failed: {errors_str}"
                        self.add_log(job, db, validation_error)
                        log.error("resume_postprocess: job_id=%s Validation failed: %s (errors=%s)", job_id, validation_error, validation_result.errors)
                        if validation_result.warnings:
                            warnings_str = "; ".join(validation_result.warnings)
                            self.add_log(job, db, f"Post-process validation warnings: {warnings_str}")
                    else:
                        # Validation passed - clear any previous validation_error
                        validation_error = None
                        if validation_result.warnings:
                            warnings_str = "; ".join(validation_result.warnings)
                            self.add_log(job, db, f"Post-process validation warnings: {warnings_str}")
                            log.info("resume_postprocess: job_id=%s Validation passed with warnings: %s", job_id, warnings_str)
                        else:
                            self.add_log(job, db, "Post-process validation passed")
                            log.info("resume_postprocess: job_id=%s Validation passed successfully", job_id)
                except Exception as val_exc:
                    self.add_log(job, db, f"Warning: Post-process validation error: {val_exc}")
                    # Don't fail the job if validation check itself fails
            except Exception as exc:
                tb = traceback.format_exc(limit=10)
                validation_error = f"{type(exc).__name__}: {str(exc)}"
                error_msg = f"Post-process error during gather/validation: {validation_error}\n{tb}"
                self.add_log(job, db, error_msg)
                post_log(f"gather/validation: ERROR - {error_msg}", "error")
                log.error("resume_postprocess: job_id=%s FAILED at gather/validation exception: %s", job_id, validation_error, exc_info=True, extra={"job_id": job_id, "error_type": "gather_validation_exception", "exception_type": type(exc).__name__})

            try:
                summary = {
                    "job_id": str(job.id),
                    "disc_num": job.disc_num,
                    "mount_point": job.mount_point,
                    "mode": job.mode,
                    "status": "failed" if validation_error else "completed",
                    "post_paths": post_paths or None,
                    "final_hashes": final_hashes or None,
                    "error": validation_error,
                }
                (paths.metadata / "job_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            except Exception:
                pass

            if validation_error:
                self.add_log(job, db, f"Output validation failed during resume: {validation_error}")
                log.error("resume_postprocess: job_id=%s FAILED at final validation: %s (post_paths_count=%s)", job_id, validation_error, len(post_paths) if post_paths else 0, extra={"job_id": job_id, "error_type": "final_validation", "error_reason": validation_error, "post_paths_count": len(post_paths) if post_paths else 0})
                _post_postprocess_complete_callback(job_id, success=False, error_reason=validation_error)
                return

            disc_payload = job.disc_payload or {}
            try:
                title_keys: list[str] = []
                title_maps = _build_title_id_maps(job, disc_payload)
                id_to_title = title_maps.get("id_to_title", {})
                if id_to_title:
                    def _title_sort_key(t: Any) -> tuple[int, int]:
                        index_val = getattr(t, "index", None)
                        order_val = getattr(t, "order_index", None)
                        index_key = index_val if isinstance(index_val, int) else 9999
                        order_key = order_val if isinstance(order_val, int) else 9999
                        return (index_key, order_key)

                    titles_sorted = sorted(id_to_title.values(), key=_title_sort_key)
                    title_keys = [str(t.id) for t in titles_sorted if getattr(t, "id", None)]
                # Load disc_titles once so both maps get index-based alignment.
                try:
                    from api import models as db_models
                    disc_titles = (
                        db.query(db_models.DiscTitle)
                        .filter(db_models.DiscTitle.disc_id == disc_id).all()
                        if disc_id else []
                    )
                except Exception:
                    disc_titles = []
                title_output_map = _build_title_output_map(
                    title_keys, post_paths, disc_titles=disc_titles,
                )
                if title_output_map:
                    disc_payload["title_output_map"] = title_output_map
                    disc_payload["output_files"] = title_output_map
                if post_paths:
                    filename_map = disc_payload.get("title_filename_map") or {}
                    if disc_titles:
                        # Index-parse path: stable across full + selective rips.
                        from core.makemkv_output import map_mkv_filenames_to_title_ids
                        tid_to_rel = map_mkv_filenames_to_title_ids(
                            (rel for _k, rel in post_paths.items()),
                            disc_titles,
                        )
                        for tid, rel_path in tid_to_rel.items():
                            filename_map[str(tid)] = os.path.basename(rel_path)
                    else:
                        # Legacy positional fallback when disc_titles aren't loadable.
                        title_id_to_order = {
                            str(t.id): (
                                t.order_index if t.order_index is not None
                                else (t.index if t.index is not None else 9999)
                            )
                            for t in disc_titles if t.id
                        }
                        sorted_items = sorted(
                            post_paths.items(),
                            key=lambda x: (title_id_to_order.get(x[0], 9999), x[0]),
                        )
                        mkv_names_sorted = [rel_path for _, rel_path in sorted_items]
                        for idx, title_id in enumerate(title_keys):
                            if idx < len(mkv_names_sorted):
                                filename = os.path.basename(mkv_names_sorted[idx])
                                filename_map[str(title_id)] = filename
                    disc_payload["title_filename_map"] = filename_map
                disc_payload["post_paths"] = post_paths
                disc_payload["final_hashes"] = final_hashes
            except Exception:
                pass

            self.add_log(job, db, f"resume_postprocess: Post-processing completed successfully, reporting to API")
            # Clear recovery attempts on successful completion
            try:
                from core.failure_recovery import clear_recovery_attempts
                clear_recovery_attempts(job_id)
            except Exception:
                pass
            self.set_status(job, db, disc_payload=disc_payload)
            if getattr(job, "disc", None):
                job.disc.artifacts = {
                    "post_paths": post_paths,
                    "final_hashes": final_hashes,
                }
                db.commit()
                db.refresh(job)
            # Update file_path on DiscTitle rows to transient paths
            _pp_disc_id = getattr(job.disc, "id", None) if job.disc else None
            if _pp_disc_id and post_paths:
                _update_title_file_paths(db, _pp_disc_id, post_paths, "postprocess", base_dir=str(trans_root))
                # #448 — capture Matroska Segment UID for each finalised title.
                # Postprocess is the natural capture point: the file is in its
                # final muxed form and still on local storage. Capturing later
                # at transfer time could mean the file lives on an unreachable
                # remote destination. Failure here is non-fatal — a NULL UID
                # just makes downstream consumers fall back to heuristic match.
                try:
                    from core.mkv_identity import capture_segment_uids_for_titles
                    capture_segment_uids_for_titles(db, _pp_disc_id, post_paths, str(trans_root))
                    db.flush()
                except Exception as exc:
                    log.warning(
                        "resume_postprocess: segment_uid capture failed for job %s: %s",
                        job_id, exc,
                    )
            _post_postprocess_complete_callback(
                job_id,
                success=True,
                post_paths=post_paths or {},
                post_progress=100,
                disc_payload_updates=disc_payload,
            )
            self.add_log(job, db, f"resume_postprocess: Successfully completed post-processing for job {job_id}")
            # #365 Phase 2 § 6.1 — auto-dispatch the actual transfer so the
            # full sequence runs without waiting for the user to click
            # "Start Transfer" a second time. Two sibling helpers cover the
            # mode space (each no-ops for the other's modes):
            #   * remote → enqueues ``transfer_remote.delay(...)``
            #   * local → runs ``_execute_local_transfer_use_final_map``
            #     inline (same body the HTTP endpoint runs).
            # The ``POST /jobs/{id}/transfer`` endpoint stays as a recovery
            # path: a user click during the brief pre-transfer window hits
            # the existing 409 guard ("Transfer already in progress"); if
            # auto-dispatch fails the operator can retry manually.
            _maybe_auto_dispatch_remote_transfer(job_id, self)
            _maybe_auto_dispatch_local_transfer(job_id, self)
        except Exception as exc:
            tb = traceback.format_exc(limit=8)
            error_msg = f"ERROR during resume_postprocess: {tb}"
            self.add_log(job, db, error_msg)
            log.error("resume_postprocess: job_id=%s FAILED with unhandled exception: %s", job_id, str(exc), exc_info=True, extra={"job_id": job_id, "error_type": "unhandled_exception", "exception_type": type(exc).__name__})
            _post_postprocess_complete_callback(job_id, success=False, error_reason=f"Post-process failed: {str(exc)}")
            raise


# ────────────────────────────────────────────────────────────────
# Periodic maintenance tasks
# ────────────────────────────────────────────────────────────────

@celery_app.task(name='cleanup_zombies')
def cleanup_zombies():
    """
    Periodic task to reap zombie processes.
    
    While tini (PID 1) handles most zombie reaping, this provides defense-in-depth
    by periodically checking for and cleaning up any zombie processes that may
    have accumulated. This is particularly useful for grandchild processes spawned
    by MakeMKV (e.g., Java processes for blues.jar DRM component).
    """
    from core.utils import reap_zombie_processes
    
    count = reap_zombie_processes()
    if count > 0:
        log.info(f"Periodic zombie cleanup: reaped {count} zombie process(es)", extra={"zombie_count": count})
    else:
        log.debug("Periodic zombie cleanup: no zombies found")
    
    return {"reaped_count": count}


@celery_app.task(name='cleanup_job_mkv', acks_late=True)
def cleanup_job_mkv(job_id: str, reason: str):
    """
    Remove .mkv files from a job's raw and transient dirs and set transfer_source_cleaned.

    Called from Finish endpoint, transfer cleanup endpoint, and stale cleanup.
    Reason: user_finish, transfer_cleanup, stale_cleanup, or reconciliation.
    Idempotent: if no .mkv left, only sets transfer_source_cleaned = True.
    """
    from core.job_cleanup import job_has_mkv_files, remove_mkv_files_from_job

    db = database.SessionLocal()
    try:
        job = crud.get_job(db, job_id)
        if not job:
            return
        paths = JobPaths.for_id(job_id)
        if job_has_mkv_files(paths):
            try:
                remove_mkv_files_from_job(paths, reason=reason, write_manifest=True)
            except Exception as e:
                log.warning("cleanup_job_mkv: job %s remove_mkv failed: %s", job_id, e)
                return  # do not set transfer_source_cleaned so reconciler will retry
        job.transfer_source_cleaned = True
        db.commit()
        log.info("cleanup_job_mkv: job %s marked transfer_source_cleaned (reason=%s)", job_id, reason)
    finally:
        db.close()


@celery_app.task(name='probe_transfer_capabilities', acks_late=True)
def probe_transfer_capabilities(config_id: str):
    """Probe destination capabilities for a TransferConfig and cache the
    result on ``config.config_data['capabilities']`` (#635 commit B).

    Runs on the ``celery`` queue so it doesn't block rip/postprocess.
    Emits a ``transfer_config_capabilities_updated`` websocket event via
    Redis pub/sub so the FastAPI process forwards it to connected UIs.
    """
    from core.transfer.capabilities import probe as _probe
    from api import models as db_models
    from sqlalchemy.orm.attributes import flag_modified

    db = database.SessionLocal()
    try:
        config = db.query(db_models.TransferConfig).filter(
            db_models.TransferConfig.id == config_id
        ).first()
        if not config:
            log.warning("probe_transfer_capabilities: config %s not found", config_id)
            return
        caps = _probe(config, db=db)
        data = dict(config.config_data or {})
        data["capabilities"] = caps.to_dict()
        config.config_data = data
        flag_modified(config, "config_data")
        db.commit()
        log.info(
            "probe_transfer_capabilities: config=%s mode=%s caps=%s error=%s",
            config_id,
            getattr(config, "mode", None),
            {k: getattr(caps, k) for k in ("can_write_new", "can_overwrite_in_place", "can_delete", "can_rename")},
            caps.probe_error,
        )
        try:
            import redis as _redis
            _client = _redis.Redis.from_url(_redis_url, decode_responses=True)
            payload = json.dumps({
                "type": "transfer_config_capabilities_updated",
                "config_id": config_id,
                "capabilities": caps.to_dict(),
            })
            _client.publish("mkvauto:events", payload)
        except Exception as pub_exc:
            log.debug("probe_transfer_capabilities: websocket publish failed (non-fatal): %s", pub_exc)
    except Exception as exc:
        log.exception("probe_transfer_capabilities: unexpected error for config %s: %s", config_id, exc)
    finally:
        db.close()


@celery_app.task(name='reconcile_job_mkv_cleanup')
def reconcile_job_mkv_cleanup():
    """
    Periodic task: find completed/failed jobs with transfer_source_cleaned=False
    and process them inline (remove .mkv if any, set transfer_source_cleaned).
    One worker does all the leg work; no per-job task enqueue.
    """
    from api import models as db_models
    from core.job_cleanup import job_has_mkv_files, remove_mkv_files_from_job

    db = database.SessionLocal()
    try:
        # Only terminal jobs; exclude pending, running, validating
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        rows = (
            db.query(db_models.Job)
            .filter(
                db_models.Job.job_status.in_(["completed", "failed"]),
                db_models.Job.transfer_source_cleaned == False,  # noqa: E712
                db_models.Job.updated_at < cutoff,
            )
            .all()
        )
        processed = 0
        for job in rows:
            try:
                paths = JobPaths.for_id(str(job.id))
                if job_has_mkv_files(paths):
                    remove_mkv_files_from_job(paths, reason="reconciliation", write_manifest=True)
                job.transfer_source_cleaned = True
                db.commit()
                processed += 1
            except Exception as e:
                log.warning("reconcile_job_mkv_cleanup: job %s failed: %s", getattr(job, "id", None), e)
                db.rollback()
        if processed > 0:
            log.info("reconcile_job_mkv_cleanup: processed %d job(s)", processed)
        return {"processed": processed}
    finally:
        db.close()


# ── Transfer: Celery task for remote (rsync/SMB/NFS) transfers (#321/#50) ─────

_TRANSFER_PROGRESS_LAST_ACCEPT: dict[str, float] = {}
_TRANSFER_PROGRESS_RATE_LIMIT_SECONDS = 2


def _post_transfer_progress(job_id: str, *, transfer_progress: int) -> None:
    """Apply transfer-progress state directly via DB (#365 cleanup).

    Previously this function POSTed to the API's
    ``/jobs/{job_id}/transfer-progress`` endpoint on every progress tick
    (one HTTP roundtrip per tick × hundreds-to-thousands of ticks per
    multi-GB transfer). The worker and API live in the same process
    tree — the HTTP roundtrip bought us nothing beyond an extra failure
    mode (localhost guard, see #378). Now we open our own DB session
    and call ``apply_job_state`` directly.

    Self-rate-limited to one DB write per job per
    ``_TRANSFER_PROGRESS_RATE_LIMIT_SECONDS``. The API endpoint had
    the same throttle on its side (the worker fired every tick, the
    API rejected most); doing it here means the worker doesn't even
    open a DB session for throttled ticks.

    Also emits the WebSocket progress event so the frontend sees the
    update — mirrors the API endpoint's debounced progress emit.

    Imports are local to the function body to avoid circular imports.
    """
    now = time.time()
    last = _TRANSFER_PROGRESS_LAST_ACCEPT.get(job_id, 0.0)
    if now - last < _TRANSFER_PROGRESS_RATE_LIMIT_SECONDS:
        return
    _TRANSFER_PROGRESS_LAST_ACCEPT[job_id] = now

    from core.job_state import apply_job_state
    pct = max(0, min(100, transfer_progress))
    db = database.SessionLocal()
    try:
        job = crud.get_job(db, job_id)
        if not job:
            log.warning("transfer-progress: job %s not found", job_id)
            return
        if getattr(job, "transfer_state", None) != "running":
            # Matches API endpoint's 409 guard — drop the update.
            return
        apply_job_state(
            db, job,
            updates={"transfer_progress": pct, "transfer_state": "running"},
            reason="transfer_progress in-process",
            skip_context_changed=True,
        )
        # Mirror the API endpoint's debounced WebSocket progress emit so
        # the frontend's progress bar updates.
        try:
            from core.progress_emitter import emit_job_progress_debounced
            progress_data = {
                "rip_progress": getattr(job, "rip_progress", 0),
                "rip_phase": getattr(job, "rip_phase", None),
                # #604 / #605: ship stage states alongside progress for
                # consistency with the other rip/transfer-progress emit
                # sites. Without these, the in-process transfer-progress
                # path (skip_context_changed=True) leaves the frontend's
                # local jobStatus.transfer_state stuck at 'ready' through
                # the entire remote transfer.
                "rip_state": getattr(job, "rip_state", None),
                "post_state": getattr(job, "derived_post_state", None),
                "transfer_state": getattr(job, "transfer_state", None),
                "post_progress": getattr(job, "post_progress", 0),
                "transfer_progress": getattr(job, "transfer_progress", None),
                "per_title_progress": getattr(job, "per_title_progress", None),
                "current_title_progress": getattr(job, "current_title_progress", None),
                "current_title_id": getattr(job, "current_title_id", None),
                "current_title_number": getattr(job, "current_title_number", None),
            }
            emit_job_progress_debounced(job_id, progress_data)
        except Exception as exc:
            log.warning("transfer-progress: failed to emit WS progress for job %s: %s", job_id, exc)
    except Exception as exc:
        log.warning("transfer-progress in-process apply failed for job %s: %s", job_id, exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _post_transfer_complete_callback(
    job_id: str,
    success: bool,
    dest_paths: list | None = None,
    error_reason: str | None = None,
) -> None:
    """Apply transfer-complete state directly via DB (#365 cleanup).

    Previously this function POSTed to the API's
    ``/jobs/{job_id}/transfer-complete`` endpoint with retries. The
    worker and API run in the same process tree (single container in
    production, both bound to localhost in dev) — the HTTP roundtrip
    bought us nothing beyond an extra failure mode (the localhost
    guard rejecting non-127.0.0.1 callbacks, see #378). Now we open
    our own DB session and call ``_complete_transfer`` /
    ``_fail_transfer`` directly. Mirrors the
    ``_post_postprocess_complete_callback`` rewrite from PR #427.

    Imports are local to the function body to avoid the circular
    import ``workers.tasks`` ↔ ``api.routers.jobs``.

    Idempotency: if ``transfer_state`` is already ``"completed"``,
    the call is a no-op. Matches the HTTP endpoint's behaviour.

    The legacy ``POST /jobs/{job_id}/transfer-complete`` endpoint
    stays registered on the API for one release as a safety net for
    any in-flight Celery tasks queued under the old worker image
    across a deploy window.
    """
    from api.routers.jobs import _complete_transfer, _fail_transfer, _build_job_metadata
    db = database.SessionLocal()
    try:
        job = crud.get_job(db, job_id)
        if not job:
            log.warning("transfer-complete: job %s not found", job_id)
            return
        # Idempotency: refuse to re-finalize a job that's already done.
        transfer_state = getattr(job, "transfer_state", None)
        if transfer_state == "completed":
            return
        if success:
            job_metadata = _build_job_metadata(job)
            _complete_transfer(job, db, dest_paths or [], job_metadata)
        else:
            _fail_transfer(job, db, error_reason or "Transfer failed", dest_paths)
    except Exception as exc:
        log.error("transfer-complete in-process apply failed for job %s: %s", job_id, exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


@celery_app.task(bind=True, base=JobTask, name='transfer_remote', acks_late=True)
def transfer_remote(self, job_id: str, src_path: str, config_id: str):
    """
    Celery task: execute a remote transfer (rsync/SMB/NFS) and call back on completion.
    Replaces the inline BackgroundTasks.add_task(_run_background_transfer, ...) pattern. (#321/#50)
    """
    log.info("transfer_remote: task received job_id=%s config_id=%s", job_id, config_id)
    from pathlib import Path
    from core.transfer.utils import history as transfer_history
    try:
        with db_session() as db:
            job = crud.get_job(db, job_id)
            if not job:
                log.error("transfer_remote: job %s not found", job_id)
                return
            config = db.query(crud.models.TransferConfig).filter(
                crud.models.TransferConfig.id == config_id
            ).first()
            if not config:
                _post_transfer_complete_callback(job_id, False, error_reason="Transfer config not found")
                return
            src = Path(src_path)
            if not src.exists():
                _post_transfer_complete_callback(job_id, False, error_reason=f"Source path not found: {src}")
                return

            # #634: log the transfer start so ``transfer_history`` has a row
            # to complete/fail against. The API-level ``_run_background_transfer``
            # path (jobs.py:4494) has always done this inline; the celery path
            # skipped it, leaving every SMB/rsync/NFS transfer without an
            # audit row.
            history_id: str | None = None
            try:
                config_data = config.config_data or {}
                if config.mode in ("smb", "nfs"):
                    host = config_data.get("host", "")
                    share = config_data.get("share", "")
                    path = config_data.get("path", "")
                    dest_path_for_history = f"{config.mode}://{host}/{share}"
                    if path:
                        dest_path_for_history = f"{dest_path_for_history}/{path}".rstrip("/")
                elif config.mode == "rsync":
                    host = config_data.get("host", "")
                    path = config_data.get("path", "")
                    dest_path_for_history = f"rsync://{host}/{path}".rstrip("/") if path else f"rsync://{host}"
                else:
                    dest_path_for_history = config_data.get("transfer_dir", "") or str(src)
                history_id = transfer_history.log_transfer_start(
                    db,
                    job_id=job_id,
                    config_id=str(config.id),
                    mode=config.mode,
                    source_path=str(src),
                    dest_path=dest_path_for_history,
                )
            except Exception as hist_exc:
                log.warning("[%s] transfer_remote: failed to log transfer start: %s", job_id, hist_exc)

            from core.transfer.service import execute_transfer
            result = execute_transfer(
                db,
                job_id,
                src,
                config,
                progress_callback=lambda pct: _post_transfer_progress(job_id, transfer_progress=pct),
            )
            if not result.get("success"):
                error_msg = result.get("error") or "Transfer failed"
                if history_id:
                    try:
                        transfer_history.log_transfer_failed(db, history_id=history_id, error=error_msg)
                    except Exception as hist_exc:
                        log.warning("[%s] transfer_remote: failed to log transfer failure: %s", job_id, hist_exc)
                _post_transfer_complete_callback(job_id, False, error_reason=error_msg)
                return
            if history_id:
                try:
                    transfer_history.log_transfer_complete(
                        db,
                        history_id=history_id,
                        bytes_transferred=result.get("bytes_transferred", 0),
                        duration=result.get("duration", 0),
                        verified=result.get("verified", False),
                        hash_value=result.get("source_hash"),
                    )
                except Exception as hist_exc:
                    log.warning("[%s] transfer_remote: failed to log transfer completion: %s", job_id, hist_exc)
            dest_paths = [result.get("dest_path", "")]
            _post_transfer_complete_callback(job_id, True, dest_paths=dest_paths)
    except Exception as exc:
        log.error("transfer_remote: unhandled error for job %s: %s", job_id, exc, exc_info=True)
        _post_transfer_complete_callback(job_id, False, error_reason=str(exc))


@celery_app.task(bind=True, base=JobTask, name='generate_segment_previews', acks_late=True)
def generate_segment_previews(self, job_id: str):
    """Path A — post-exploratory-rip orchestration.

    Triggered from the rip-complete callback when the job is in
    `segment_reorder_state.stage == 'exploratory_ripping'`. Mounts the disc,
    parses MPLS for the playlist that was ripped, calls
    `core.segment_reorder.run_exploratory_postprocess` to generate previews,
    persists the manifest into segment_reorder_state.previews_manifest, and
    advances stage to `awaiting_segment_order` so the frontend can route
    the user to the segment-reorder page.

    Errors fail-soft: stage advances to `previews_failed` and we surface a
    notification telling the user to bail to manual selection (the
    exploratory rip artifacts on disk are still keepable).
    """
    from pathlib import Path as _Path
    from core.segment_reorder import run_exploratory_postprocess
    from core.disc_manager import get_cached_discs
    from core.notifications import emit_notification_sync
    from api import models as db_models

    log.info("generate_segment_previews: job_id=%s", job_id)
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            log.error("generate_segment_previews: job %s not found", job_id)
            return
        state = dict(getattr(job, "segment_reorder_state", None) or {})
        if not state:
            log.warning("generate_segment_previews: no segment_reorder_state on job %s; skipping", job_id)
            return

        exploratory_idx = state.get("exploratory_title_index")
        if exploratory_idx is None:
            log.error("generate_segment_previews: no exploratory_title_index on job %s", job_id)
            return

        # Resolve the .mpls filename for the exploratory title.
        # Cache is the fast path; DB is the fallback when the cache
        # has been evicted (typical after a long rip).
        mpls_filename: str | None = None
        cached = get_cached_discs() or []
        disc_info = next(
            (d for d in cached if d.get("mount_point") == job.mount_point), None,
        )
        if disc_info:
            titles = disc_info.get("titles") or {}
            title_meta = titles.get(exploratory_idx) or titles.get(str(exploratory_idx)) or {}
            mpls_filename = title_meta.get("source_file") or title_meta.get("file")
        if not mpls_filename:
            # Cache miss → DB fallback. disc_titles.index is what MakeMKV's
            # MSG:3307 calls "title id"; that's the same number Path A's
            # exploratory_title_index points at.
            disc_id = getattr(job, "disc_id", None)
            if disc_id:
                title_row = (
                    db.query(db_models.DiscTitle)
                    .filter(
                        db_models.DiscTitle.disc_id == disc_id,
                        db_models.DiscTitle.index == exploratory_idx,
                    )
                    .first()
                )
                if title_row and title_row.source_file:
                    mpls_filename = title_row.source_file
                    log.info(
                        "generate_segment_previews: resolved mpls=%s from DB (cache miss)",
                        mpls_filename,
                    )
        if not mpls_filename or not str(mpls_filename).lower().endswith(".mpls"):
            log.error(
                "generate_segment_previews: exploratory title %s has no .mpls source_file (resolved=%s)",
                exploratory_idx, mpls_filename,
            )
            state["stage"] = "previews_failed"
            state["error"] = f"could not resolve mpls filename for title {exploratory_idx}"
            job.segment_reorder_state = state
            db.commit()
            return

        # Find the rip output. Phase 1's selective-rip path with rip_set=[N]
        # writes one .mkv into raw/. We pick whatever .mkv is biggest there.
        paths = JobPaths.from_job(job, out_dir=str(DATA_ROOT))
        paths.ensure_layout()
        raw_dir = _Path(paths.raw)
        mkv_files = sorted(raw_dir.glob("*.mkv"), key=lambda p: p.stat().st_size, reverse=True)
        if not mkv_files:
            log.error("generate_segment_previews: no .mkv in %s", raw_dir)
            state["stage"] = "previews_failed"
            state["error"] = "no rip output .mkv found"
            job.segment_reorder_state = state
            db.commit()
            return
        rip_path = mkv_files[0]

        previews_dir = raw_dir / "previews"
        device_path = job.mount_point  # e.g. /dev/sr1

        log.info(
            "generate_segment_previews: job=%s rip=%s mpls=%s previews_dir=%s",
            job_id, rip_path, mpls_filename, previews_dir,
        )

        try:
            manifest = run_exploratory_postprocess(
                rip_path, previews_dir, device_path, str(mpls_filename),
            )
        except Exception as exc:
            log.exception("generate_segment_previews: orchestration failed for job %s", job_id)
            state["stage"] = "previews_failed"
            state["error"] = f"preview generation failed: {exc}"
            job.segment_reorder_state = state
            db.commit()
            return

        if not manifest:
            state["stage"] = "previews_failed"
            state["error"] = "MPLS parse returned no PlayItem durations"
            job.segment_reorder_state = state
            db.commit()
            try:
                emit_notification_sync(
                    "Couldn't generate segment previews. Please pick the canonical playlist manually.",
                    "warning", "segment_reorder_failed", job_id=job_id,
                )
            except Exception:
                pass
            return

        # Manifest persisted onto the job; frontend reads via workflow-context.
        state["stage"] = "awaiting_segment_order"
        state["previews_manifest"] = [s.to_dict() for s in manifest]
        job.segment_reorder_state = state
        db.commit()

        try:
            emit_notification_sync(
                f"{len(manifest)} segment previews ready — drag them into story order.",
                "info", "segment_reorder_ready", job_id=job_id,
            )
        except Exception:
            log.warning("generate_segment_previews: failed to emit ready notification", exc_info=True)


# Configure Celery Beat schedule for periodic tasks
celery_app.conf.beat_schedule = {
    'cleanup-zombies': {
        'task': 'cleanup_zombies',
        'schedule': crontab(minute='*/5'),  # Every 5 minutes
        'options': {'expires': 240},  # Task expires after 4 minutes (before next run)
    },
    'reconcile-job-mkv-cleanup': {
        'task': 'reconcile_job_mkv_cleanup',
        'schedule': crontab(hour=3, minute=0),  # Daily at 03:00
        'options': {'expires': 3600},
    },
}
