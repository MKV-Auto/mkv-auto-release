"""
Job-level cleanup: remove .mkv and preview files from job directories.

Used by the cleanup_job_mkv Celery task and the reconcile_job_mkv_cleanup
periodic task. Does not touch the DB; callers set transfer_source_cleaned.

Cleanup removes:
- .mkv files from raw/ and transient/ directories
- All preview files (.m3u8 manifests, .ts segments) from previews/ directory

Both raw and transient are cleaned together when the job reaches a terminal
state (user Finish, transfer cleanup, stale/startup cleanup, or reconciliation).

**Post-5d (#365) note:** for local-mode jobs the rename step writes
directly to ``config.transfer_dir`` and ``paths.transient`` stays empty,
so the transient walk is typically a no-op for those — the correct
behaviour because the library files are the rip's final output and
must not be deleted by job_cleanup. For remote modes (rsync/smb/nfs)
``paths.transient`` still holds the local staging copy and the walk
removes it as expected.

Cleaning up partial library writes from a *failed* local-mode job
(e.g. rename succeeded for some files before the job failed at
validation) is **out of scope** for this module — library management
is a separate concern from per-job working-directory cleanup. The
devmode revert endpoints (``/reset-postprocess``, ``/restore-postprocess``)
do handle that case via ``_clear_per_rip_postprocess_output``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from core.job_paths import JobPaths

log = logging.getLogger(__name__)

MANIFEST_FILENAME = "cleaned_up_files.json"


def job_has_cleanable_files(paths: JobPaths, *, include_previews: bool = True) -> bool:
    """
    Return True if the job has any files that should be cleaned up.

    Checks:
    - paths.raw and paths.transient for *.mkv files (rglob)
    - paths.previews for any files (when include_previews=True)

    Used by reconciler and cleanup task to decide "run removal" vs "only mark cleaned."
    """
    # Check for .mkv files in raw and transient
    for dir_path in (paths.raw, paths.transient):
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        try:
            if any(dir_path.rglob("*.mkv")):
                return True
        except OSError as e:
            log.warning("job_cleanup: could not scan %s: %s", dir_path, e)

    # Check for preview files
    if include_previews:
        previews_dir = paths.previews
        if previews_dir.exists() and previews_dir.is_dir():
            try:
                if any(previews_dir.rglob("*")):
                    # rglob("*") matches files and dirs; check for at least one file
                    for item in previews_dir.rglob("*"):
                        if item.is_file():
                            return True
            except OSError as e:
                log.warning("job_cleanup: could not scan %s: %s", previews_dir, e)

    return False


# Backward-compatible alias
def job_has_mkv_files(paths: JobPaths) -> bool:
    """
    Return True if paths.raw or paths.transient contain any *.mkv file,
    or if paths.previews contains any preview files.

    Backward-compatible wrapper around job_has_cleanable_files.
    """
    return job_has_cleanable_files(paths, include_previews=True)


def _collect_file_entry(fpath: Path, root: Path) -> dict:
    """Collect metadata for a file before deletion."""
    stat = fpath.stat()
    try:
        rel_path = fpath.relative_to(root)
    except ValueError:
        rel_path = fpath
    return {
        "path": str(rel_path),
        "name": fpath.name,
        "size_bytes": stat.st_size,
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def remove_mkv_files_from_job(
    paths: JobPaths,
    *,
    reason: str,
    write_manifest: bool = True,
    include_previews: bool = True,
) -> tuple[int, Path | None]:
    """
    Remove .mkv files from raw and transient directories, and optionally
    all preview files from the previews directory.

    Collects stats (path, name, size_bytes, mtime_iso) for each file before unlink.
    If write_manifest, writes cleaned_up_files.json under paths.root / "metadata".

    When include_previews is True (default), also removes all files from
    paths.previews (.m3u8 manifests, .ts segments, etc.) and cleans up
    empty subdirectories.

    Returns (count_removed, manifest_path or None).
    Defensive: missing dirs skipped; unlink errors logged and continued.
    Idempotent: second run finds no files, removes nothing.
    """
    if not isinstance(paths, JobPaths):
        raise TypeError("paths must be JobPaths")

    files_removed: list[dict] = []
    count_removed = 0
    root = paths.root

    # Remove .mkv files from raw and transient
    for dir_path in (paths.raw, paths.transient):
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        try:
            for fpath in dir_path.rglob("*.mkv"):
                if not fpath.is_file():
                    continue
                try:
                    entry = _collect_file_entry(fpath, root)
                    fpath.unlink()
                    files_removed.append(entry)
                    count_removed += 1
                except OSError as e:
                    log.warning("job_cleanup: could not remove %s: %s", fpath, e)
        except OSError as e:
            log.warning("job_cleanup: could not scan %s: %s", dir_path, e)

    # Remove preview files (.m3u8, .ts segments, etc.)
    if include_previews:
        previews_dir = paths.previews
        if previews_dir.exists() and previews_dir.is_dir():
            try:
                # Collect all files first, then remove
                for fpath in previews_dir.rglob("*"):
                    if not fpath.is_file():
                        continue
                    try:
                        entry = _collect_file_entry(fpath, root)
                        fpath.unlink()
                        files_removed.append(entry)
                        count_removed += 1
                    except OSError as e:
                        log.warning("job_cleanup: could not remove preview file %s: %s", fpath, e)

                # Remove empty subdirectories (bottom-up)
                for dirpath in sorted(previews_dir.rglob("*"), reverse=True):
                    if dirpath.is_dir():
                        try:
                            dirpath.rmdir()  # only removes empty dirs
                        except OSError:
                            pass  # not empty or permission error — skip
            except OSError as e:
                log.warning("job_cleanup: could not scan previews %s: %s", previews_dir, e)

    manifest_path: Path | None = None
    if write_manifest:
        metadata_dir = paths.metadata
        try:
            metadata_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = metadata_dir / MANIFEST_FILENAME
            payload = {
                "reason": reason,
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "files": files_removed,
            }
            manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("job_cleanup: could not write manifest %s: %s", manifest_path, e)
            manifest_path = None

    return count_removed, manifest_path
