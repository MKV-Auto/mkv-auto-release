"""Video-library catalog and Dolby Vision 7 conversion queue (#894).

The library scan walks the user's transfer destination (a local folder, or
an SMB/NFS share the app mounts; see ``core.library_destination``, #902),
records every video file's format in ``library_media_files``, and marks Dolby Vision profile 7
MKVs ``queued``. ``convert_catalog_file`` converts one queued file in place
with ``core.dv_remux`` (verify, then replace).

Statuses: ``catalogued`` (nothing to do) | ``queued`` | ``converting`` |
``converted`` | ``failed`` | ``skipped`` (precondition not met, e.g. free
space; the reason is in ``error``) | ``unreadable`` (ffprobe can't read
the file: usually a damaged or incomplete file; nothing to convert). A rescan only re-probes a file whose
size or mtime changed, so repeat scans are cheap. A ``failed`` or
``skipped`` file is not re-queued by a rescan unless it changed; retrying is
an explicit action, so a broken file can't loop.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".wmv", ".webm"})
# Smaller files aren't catalogued. ``.ts`` is also TypeScript: a library
# share with a code folder (node_modules) holds thousands of tiny ones, and
# no video worth converting is this small.
MIN_VIDEO_BYTES = 1024 * 1024
# How often a running scan reports progress (``progress_cb``).
PROGRESS_EVERY_SECONDS = 30
# A file modified this recently may still be being written (a transfer in
# progress); it is skipped this scan and picked up by the next one.
RECENT_SECONDS = 300
COMMIT_EVERY = 50
# Longer than any conversion can run (extract + mkvmerge are bounded at 4 h
# each in core.dv_remux). A row still ``converting`` after this was
# interrupted, e.g. by a container restart; its original is untouched,
# because replacement only happens after verification.
STALE_CONVERTING_SECONDS = 9 * 3600

CATALOGUED = "catalogued"
QUEUED = "queued"
CONVERTING = "converting"
CONVERTED = "converted"
# convert_catalog_file's return value when a file stays queued for lack of
# disk space (#895); the task re-checks later.
WAITING_FOR_SPACE = "waiting_for_space"
WAITING_PREFIX = "Waiting for disk space: "
FAILED = "failed"
SKIPPED = "skipped"
UNREADABLE = "unreadable"
UNREADABLE_NOTE = "Couldn't read this file. It may be damaged or incomplete; check whether it plays."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def library_path_problem(path: str) -> Optional[str]:
    """Why ``path`` can't be a library folder, or None.

    It must exist inside the container, be on a mapped mount (not the
    container's own filesystem), and not be the app's data directory.
    """
    from core.storage_location import check_local_path, container_layer_message, missing_message
    from core.utils import get_mkvauto_data

    if not path or not str(path).strip():
        return "No folder given."
    check = check_local_path(path)
    if not check.exists:
        return missing_message(check.path)
    if check.on_container_layer:
        return container_layer_message(check.path)
    if str(check.path) == "/":
        return "The container's root folder can't be a library folder."
    try:
        data = Path(get_mkvauto_data()).resolve()
        if check.path == data or data in check.path.parents or check.path in data.parents:
            return f"{check.path} overlaps MKV-Auto's own data folder ({data})."
    except OSError:
        pass
    return None


# MPEG transport streams start with the 0x47 sync byte (``.m2ts`` after a
# 4-byte timestamp). A ``.ts`` file that doesn't is TypeScript, not video.
_TS_SYNC_OFFSETS = {".ts": 0, ".m2ts": 4, ".mts": 4}


def _looks_like_video(path: Path) -> bool:
    offset = _TS_SYNC_OFFSETS.get(path.suffix.lower())
    if offset is None:
        return True
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(1) == b"\x47"
    except OSError:
        return True   # let the probe decide


# The top-level folders MKV-Auto itself transfers into (core.disc: type_dir).
APP_FOLDERS = ("Movies", "Series")


def scan_folders(root: str) -> list[str]:
    """The folders under the destination ``root`` that the scan looks in.

    Only what MKV-Auto writes: ``Movies`` and ``Series``, plus the first folder
    of a custom path template when it's a fixed name. Everything else on the
    share (books, music, code, other people's files) is never walked.
    """
    from core import settings as _settings

    names = list(APP_FOLDERS)
    try:
        data = _settings.load_settings()
    except Exception:  # noqa: BLE001
        data = {}
    for key in ("path_template_movie", "path_template_series"):
        first = str(data.get(key) or "").strip().strip("/").split("/", 1)[0]
        if first and "{" not in first and first not in names:
            names.append(first)
    out = []
    for name in names:
        p = Path(root) / name
        if p.is_dir():
            out.append(str(p))
    return out


def _under(path: str, root: str) -> bool:
    root = str(root).rstrip(os.sep)
    return str(path).startswith(root + os.sep)


def _is_dv7_candidate(path: Path, dv_profile: Optional[int]) -> bool:
    return path.suffix.lower() == ".mkv" and dv_profile == 7


def _walk_videos(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Skip hidden folders, which includes in-flight conversion work dirs.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                yield Path(dirpath) / name


def scan_library(
    db: Any,
    roots: list[str],
    *,
    probe: Optional[Callable[[Path], Optional[dict]]] = None,
    now: Optional[float] = None,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> dict:
    """Catalog every video file under ``roots``; return a summary.

    ``progress_cb(files_seen)`` is called at most every
    ``PROGRESS_EVERY_SECONDS`` while the scan runs.

    ``summary["queued_ids"]`` lists rows in ``queued`` state (newly queued, or
    still queued from an earlier scan) for the caller to dispatch.
    """
    from api import models as db_models
    from core import dv_remux

    probe = probe or dv_remux.probe_media_info
    now = time.time() if now is None else now
    summary = {
        "roots": [], "files": 0, "probed": 0, "unchanged": 0, "recent": 0,
        "removed": 0, "dv7": 0, "unreadable": 0, "queued_ids": [], "errors": [],
    }
    pending = 0
    cutoff = datetime.fromtimestamp(now - STALE_CONVERTING_SECONDS, tz=timezone.utc)
    stale = db.query(db_models.LibraryMediaFile).filter(
        db_models.LibraryMediaFile.status == CONVERTING,
        db_models.LibraryMediaFile.updated_at < cutoff,
    ).all()
    for row in stale:
        row.status = FAILED
        row.progress = None
        row.error = "Interrupted (the app restarted mid-conversion). The original is untouched; retry to convert it."
        row.updated_at = _now()
    summary["interrupted"] = len(stale)
    all_rows = {r.path: r for r in db.query(db_models.LibraryMediaFile).all()}
    scanned_roots: list[str] = []
    for root in roots:
        problem = library_path_problem(root)
        if problem:
            summary["errors"].append(problem)
            summary["roots"].append({"path": root, "ok": False, "problem": problem})
            continue
        root_path = Path(root).resolve()
        scanned_roots.append(str(root_path))
        summary["roots"].append({"path": str(root_path), "ok": True, "problem": None})
        # By path, not recorded root: 1.7.0 recorded the whole share as the
        # root, and a path can only have one row.
        existing = {p: r for p, r in all_rows.items() if _under(p, str(root_path))}
        seen: set[str] = set()
        last_report = time.monotonic()
        for path in _walk_videos(root_path):
            key = str(path)
            try:
                st = path.stat()
            except OSError as exc:
                seen.add(key)
                summary["errors"].append(f"{path}: {exc}")
                continue
            if st.st_size < MIN_VIDEO_BYTES or not _looks_like_video(path):
                continue      # not a video: not catalogued, not counted
            seen.add(key)
            summary["files"] += 1
            if progress_cb is not None and time.monotonic() - last_report >= PROGRESS_EVERY_SECONDS:
                last_report = time.monotonic()
                try:
                    progress_cb(summary["files"])
                except Exception:  # noqa: BLE001 - progress must never break a scan
                    pass
            if now - st.st_mtime < RECENT_SECONDS:
                summary["recent"] += 1
                continue
            row = existing.get(key)
            # 1.7.0 recorded an unreadable file as ``failed``; probe those again.
            legacy_unreadable = row is not None and row.status == FAILED and row.dv_profile is None
            if row is not None:
                row.library_root = str(root_path)
            if (row is not None and row.size == st.st_size and row.mtime == st.st_mtime
                    and not legacy_unreadable):
                summary["unchanged"] += 1
                if row.status == UNREADABLE:
                    summary["unreadable"] += 1
                if row.status == QUEUED:
                    summary["queued_ids"].append(row.id)
                if row.dv_profile == 7:
                    summary["dv7"] += 1
                continue
            info = probe(path)
            summary["probed"] += 1
            if row is None:
                row = db_models.LibraryMediaFile(path=key, library_root=str(root_path))
                db.add(row)
                all_rows[key] = row
            row.size = st.st_size
            row.mtime = st.st_mtime
            row.scanned_at = _now()
            row.updated_at = _now()
            row.progress = None
            if info is None:
                row.video_codec = row.width = row.height = row.dv_profile = row.duration = None
                row.status = UNREADABLE
                row.error = UNREADABLE_NOTE
                summary["unreadable"] += 1
            else:
                row.video_codec = info.get("video_codec")
                row.width = info.get("width")
                row.height = info.get("height")
                row.dv_profile = info.get("dv_profile")
                row.duration = info.get("duration")
                row.error = None
                if _is_dv7_candidate(path, row.dv_profile):
                    row.status = QUEUED
                    summary["dv7"] += 1
                elif row.status != CONVERTED or row.dv_profile != 8:
                    row.status = CATALOGUED
            pending += 1
            if pending >= COMMIT_EVERY:
                db.commit()
                pending = 0
            if row.status == QUEUED:
                db.flush()
                summary["queued_ids"].append(row.id)
        for key, row in existing.items():
            if key in seen or row.status == CONVERTING:
                continue
            # Gone, or not a video after all (1.7.0 catalogued tiny files and
            # TypeScript ``.ts``): a walked-but-skipped file isn't in ``seen``.
            if not Path(key).exists() or (row.size is not None and row.size < MIN_VIDEO_BYTES) \
                    or not _looks_like_video(Path(key)):
                db.delete(row)
                summary["removed"] += 1
    # Rows outside the scanned folders (an earlier whole-share scan, or a
    # destination that changed) aren't part of the library any more.
    if scanned_roots:
        for path, row in all_rows.items():
            if row.status != CONVERTING and not any(_under(path, r) for r in scanned_roots):
                db.delete(row)
                summary["removed"] += 1
    db.commit()
    return summary


def scratch_dir() -> Path:
    """Local scratch for converting files that live on a network share (#896)."""
    from core.utils import get_mkvauto_tmp

    override = os.getenv("MKVAUTO_DV7_SCRATCH_DIR", "").strip()
    return Path(override) if override else Path(get_mkvauto_tmp()) / "dv7-scratch"


def plan_conversion(db: Any, file_id: str, path: Path, size: int) -> tuple[Optional[str], Optional[str]]:
    """Reserve the space a conversion needs (#895) and pick where it works.

    Returns ``(scratch, None)`` with ``scratch`` a directory for local-scratch
    mode (#896), ``(None, None)`` for in place, or ``(None, reason)`` when
    neither fits right now.

    - A file on a network share prefers local scratch: about 2x its size on
      the local disk (intermediate stream + new file) plus 1x next to the
      original for the copy back. Network traffic drops from ~4.6x to ~3x
      the file size.
    - Otherwise, or if scratch can't be reserved, in place: 2x next to it.
    """
    from core import space_reservations as sr
    from core import dv_remux
    from core.storage_location import is_network_path

    need = 2 * int(size)
    if is_network_path(path):
        sdir = scratch_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        scratch = sr.try_reserve(
            db, sr.KIND_DV7_SCRATCH, file_id, sdir, need,
            written_path=str(dv_remux.work_dir_for(path, sdir)),
        )
        if scratch.granted:
            push = sr.try_reserve(
                db, sr.KIND_DV7_PUSH, file_id, path.parent, int(size),
                written_path=str(dv_remux.push_temp_path(path)),
            )
            if push.granted:
                return str(sdir), None
            sr.release(db, sr.KIND_DV7_SCRATCH, file_id)
    inplace = sr.try_reserve(
        db, sr.KIND_DV7_INPLACE, file_id, path.parent, need,
        written_path=str(dv_remux.work_dir_for(path)),
    )
    if inplace.granted:
        return None, None
    return None, inplace.detail


def release_conversion_space(db: Any, file_id: str) -> None:
    from core import space_reservations as sr

    for kind in (sr.KIND_DV7_SCRATCH, sr.KIND_DV7_PUSH, sr.KIND_DV7_INPLACE):
        sr.release(db, kind, file_id)


def convert_catalog_file(
    db: Any,
    file_id: str,
    *,
    remux: Optional[Callable[..., Any]] = None,
    settings_getter: Optional[Callable[[], dict]] = None,
    planner: Optional[Callable[..., tuple]] = None,
    destination: Optional[Callable[[Any], Any]] = None,
) -> str:
    """Convert one ``queued`` catalog file in place. Returns the final status,
    or a short reason when nothing was done (not queued, disabled)."""
    from api import models as db_models
    from core import dv_remux
    from core import settings as _settings

    remux = remux or dv_remux.remux_dv7_to_dv81
    cfg = (settings_getter or _settings.get_dv7_conversion)()
    row = db.query(db_models.LibraryMediaFile).filter(db_models.LibraryMediaFile.id == file_id).first()
    if row is None:
        return "missing"
    if row.status != QUEUED:
        return f"not queued ({row.status})"
    if not cfg.get("enabled"):
        return "disabled"

    def _finish(status: str, error: Optional[str] = None) -> str:
        row.status = status
        row.error = error
        row.updated_at = _now()
        db.commit()
        return status

    path = Path(row.path)
    from core import library_destination

    dest = (destination or library_destination.ensure_root)(db)
    if not dest.available:
        return _finish(SKIPPED, dest.problem)
    if not _under(row.path, str(Path(dest.root).resolve())):
        return _finish(SKIPPED, "The transfer destination changed after this file was scanned. Scan again.")
    try:
        st = path.stat()
    except OSError:
        return _finish(SKIPPED, "The file is no longer there.")
    if st.st_size != row.size or st.st_mtime != row.mtime:
        return _finish(SKIPPED, "The file changed after it was scanned. Scan again.")

    scratch, waiting = (planner or plan_conversion)(db, file_id, path, st.st_size)
    if waiting:
        # Stays queued; the task tries again later (#895).
        row.error = WAITING_PREFIX + waiting
        row.updated_at = _now()
        db.commit()
        return WAITING_FOR_SPACE

    row.status = CONVERTING
    row.progress = 0.0
    row.error = None
    row.updated_at = _now()
    db.commit()

    def _progress(fraction: float) -> None:
        row.progress = round(float(fraction), 3)
        row.updated_at = _now()
        db.commit()

    try:
        result = remux(path, progress_cb=_progress, scratch_dir=scratch, check_space=False,
                       owner="library", journal_extra={"destination": dest.display})
    finally:
        release_conversion_space(db, file_id)
    if result.converted:
        st = path.stat()
        row.size = st.st_size
        row.mtime = st.st_mtime
        row.dv_profile = 8
        row.progress = 1.0
        row.converted_at = _now()
        return _finish(CONVERTED)
    row.progress = None
    return _finish(SKIPPED if result.status == "skipped" else FAILED, result.reason)


INTERRUPTED_NOTE = "Restarted after an interruption. The original was not changed."


def recover_interrupted(db: Any, *, destination: Optional[Callable[[Any], Any]] = None) -> list[str]:
    """Finish or roll back conversions a stopped container left half-done.

    Must run where no library conversion can be in progress: the convert
    worker (one at a time), at startup and before each conversion. For every
    unfinished swap in the journal it either keeps the fully swapped-in file
    or puts the original back (``core.dv_remux.recover``), and a ``converting``
    row with no journal entry never touched its original, so its leftovers
    are deleted. Interrupted files are queued again. Returns their ids.
    """
    from api import models as db_models
    from core import dv_remux, library_destination

    rows = db.query(db_models.LibraryMediaFile).filter(
        db_models.LibraryMediaFile.status == CONVERTING
    ).all()
    entries = dv_remux.journal_entries(owner="library")
    _wipe_scratch()
    if not rows and not entries:
        return []
    dest = (destination or library_destination.ensure_root)(db)
    if not dest.available:
        log.warning("recover_interrupted: library unavailable, leaving %s row(s) and %s journal "
                    "entr(ies) for later: %s", len(rows), len(entries), dest.problem)
        return []

    by_path = {r.path: r for r in rows}
    requeue: list[str] = []
    for entry in entries:
        if entry.get("destination") and entry["destination"] != dest.display:
            # The share at the mount point isn't the one this swap started on.
            log.error("recover_interrupted: %s was being converted on %s, but the destination is now %s; "
                      "left for when that share is the destination again",
                      entry["src"], entry["destination"], dest.display)
            by_path.pop(entry["src"], None)
            continue
        try:
            outcome = dv_remux.recover(entry)
        except OSError as exc:
            log.error("recover_interrupted: recovering %s failed, left as is: %s", entry["src"], exc)
            by_path.pop(entry["src"], None)
            continue
        row = by_path.pop(entry["src"], None) or db.query(db_models.LibraryMediaFile).filter(
            db_models.LibraryMediaFile.path == entry["src"]).first()
        if row is None:
            continue
        if outcome == dv_remux.RECOVERED_COMPLETED:
            st = Path(row.path).stat()
            row.size, row.mtime, row.dv_profile = st.st_size, st.st_mtime, 8
            row.status, row.progress, row.error = CONVERTED, 1.0, None
            row.converted_at = _now()
        elif outcome == dv_remux.RECOVERED_MISSING:
            row.status, row.progress = FAILED, None
            row.error = "Interrupted, and the file couldn't be found under any of its names."
        else:
            row.status, row.progress, row.error = QUEUED, None, INTERRUPTED_NOTE
            requeue.append(row.id)
        row.updated_at = _now()
        release_conversion_space(db, row.id)

    for row in by_path.values():
        # No journal entry: the swap never started, so the original never moved.
        src = Path(row.path)
        for leftover in (dv_remux.push_temp_path(src), dv_remux.work_dir_for(src)):
            try:
                if leftover.is_dir():
                    import shutil
                    shutil.rmtree(leftover, ignore_errors=True)
                elif leftover.exists():
                    leftover.unlink()
            except OSError as exc:
                log.warning("recover_interrupted: couldn't remove %s: %s", leftover, exc)
        row.status, row.progress, row.error = QUEUED, None, INTERRUPTED_NOTE
        row.updated_at = _now()
        release_conversion_space(db, row.id)
        requeue.append(row.id)
    db.commit()
    if requeue:
        log.info("recover_interrupted: re-queued %s interrupted conversion(s)", len(requeue))
    return requeue


def _wipe_scratch() -> None:
    """Remove conversion work dirs from local scratch (nothing is converting)."""
    import shutil
    from core import dv_remux

    root = scratch_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if name.startswith(dv_remux._WORK_DIR_PREFIX):
            shutil.rmtree(root / name, ignore_errors=True)


def retry_catalog_file(db: Any, file_id: str) -> Optional[str]:
    """Put a failed/skipped DV7 file back in the queue. Returns its id when
    re-queued, else None."""
    from api import models as db_models

    row = db.query(db_models.LibraryMediaFile).filter(db_models.LibraryMediaFile.id == file_id).first()
    if row is None or row.status not in (FAILED, SKIPPED) or row.dv_profile != 7:
        return None
    row.status = QUEUED
    row.error = None
    row.progress = None
    row.updated_at = _now()
    db.commit()
    return row.id


def catalog_summary(db: Any) -> dict:
    """Counts by status plus totals, for the Settings page."""
    from sqlalchemy import func
    from api import models as db_models

    counts = dict(
        db.query(db_models.LibraryMediaFile.status, func.count())
        .group_by(db_models.LibraryMediaFile.status).all()
    )
    total = sum(counts.values())
    dv7_total = db.query(func.count()).select_from(db_models.LibraryMediaFile).filter(
        db_models.LibraryMediaFile.dv_profile == 7
    ).scalar() or 0
    return {"total": total, "dv7": dv7_total, "by_status": counts}
