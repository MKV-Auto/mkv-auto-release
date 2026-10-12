"""Disk-space reservations: heavy tasks reserve what they will write (#895).

Point-in-time free-space checks pass independently: two rips can each see
140 GB free and together write 160 GB onto the disk that also holds Postgres
(the #875 outage by another route). Here every heavy task reserves its
estimated bytes on the filesystem it writes to before it starts, and is
admitted only if

    free - (other tasks' outstanding reservations on that filesystem) - floor >= need

A reservation's *outstanding* amount is what it reserved minus what it has
already written to its output directory, so it shrinks as data lands and the
same bytes are never counted twice (once as used space, once as reserved).

Decisions are serialized with a file lock under the app's tmp dir (every
worker runs in the same container), so two tasks can't both claim the same
space. Reservations are rows in ``space_reservations`` and survive restarts;
``sweep_stale`` drops ones whose task is no longer active.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

DEFAULT_FLOOR_BYTES = 10 * 1024 ** 3
# A reservation younger than this is never swept: its task may still be
# being created (a rip reserves before its job row exists).
STALE_GRACE = timedelta(minutes=10)

KIND_RIP = "rip"
KIND_DV7_INPLACE = "dv7_inplace"
KIND_DV7_SCRATCH = "dv7_scratch"
KIND_DV7_PUSH = "dv7_push"
KIND_DV7_POSTPROCESS = "dv7_postprocess"


def floor_bytes() -> int:
    raw = os.getenv("MKVAUTO_SPACE_RESERVE_FLOOR_BYTES", "").strip()
    try:
        return max(0, int(float(raw))) if raw else DEFAULT_FLOOR_BYTES
    except ValueError:
        return DEFAULT_FLOOR_BYTES


def _existing_ancestor(path: str | Path) -> Path:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def fs_key(path: str | Path) -> str:
    """Identifies the filesystem ``path`` lives on (its device id)."""
    return str(os.stat(_existing_ancestor(path)).st_dev)


def _dir_bytes(path: Optional[str]) -> int:
    if not path:
        return 0
    total = 0
    p = Path(path)
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    for dirpath, _dirs, files in os.walk(p):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def outstanding_bytes(row: Any) -> int:
    """Reserved minus already written (never negative)."""
    return max(0, int(row.bytes_reserved or 0) - _dir_bytes(row.written_path))


@contextlib.contextmanager
def _decision_lock() -> Iterator[None]:
    """Serialize reserve decisions across processes (all run in one container)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX dev hosts
        yield
        return
    from core.utils import get_mkvauto_tmp

    lock_dir = Path(get_mkvauto_tmp())
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_dir / "space_reservations.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass
class Decision:
    granted: bool
    need: int
    free: int
    reserved_by_others: int
    floor: int
    detail: str

    @property
    def available(self) -> int:
        return self.free - self.reserved_by_others - self.floor


def _gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def try_reserve(
    db: Any,
    kind: str,
    ref_id: str,
    path: str | Path,
    need_bytes: int,
    *,
    written_path: Optional[str] = None,
) -> Decision:
    """Reserve ``need_bytes`` on ``path``'s filesystem for (kind, ref_id).

    Re-reserving the same (kind, ref_id) replaces the earlier amount. Commits.
    """
    from api import models as db_models

    need = max(0, int(need_bytes))
    floor = floor_bytes()
    with _decision_lock():
        sweep_stale(db)
        key = fs_key(path)
        free = shutil.disk_usage(_existing_ancestor(path)).free
        others = (
            db.query(db_models.SpaceReservation)
            .filter(db_models.SpaceReservation.fs_key == key)
            .all()
        )
        reserved_by_others = sum(
            outstanding_bytes(r) for r in others if not (r.kind == kind and r.ref_id == str(ref_id))
        )
        available = free - reserved_by_others - floor
        if need > available:
            detail = (
                f"Not enough disk space on {path}: this needs {_gb(need)}, "
                f"{_gb(free)} is free, {_gb(reserved_by_others)} is already reserved by "
                f"other running work, and {_gb(floor)} is kept free for the database."
            )
            return Decision(False, need, free, reserved_by_others, floor, detail)
        row = (
            db.query(db_models.SpaceReservation)
            .filter(db_models.SpaceReservation.kind == kind,
                    db_models.SpaceReservation.ref_id == str(ref_id))
            .first()
        )
        now = datetime.now(timezone.utc)
        if row is None:
            row = db_models.SpaceReservation(kind=kind, ref_id=str(ref_id), created_at=now)
            db.add(row)
        row.path = str(path)
        row.fs_key = key
        row.bytes_reserved = need
        row.written_path = written_path
        row.updated_at = now
        db.commit()
        return Decision(True, need, free, reserved_by_others, floor, "")


def attach_written_path(db: Any, kind: str, ref_id: str, written_path: str) -> None:
    """Point a reservation at the directory its task writes to (so it shrinks)."""
    from api import models as db_models

    row = (
        db.query(db_models.SpaceReservation)
        .filter(db_models.SpaceReservation.kind == kind,
                db_models.SpaceReservation.ref_id == str(ref_id))
        .first()
    )
    if row is not None:
        row.written_path = written_path
        row.updated_at = datetime.now(timezone.utc)
        db.commit()


def release(db: Any, kind: str, ref_id: str) -> bool:
    from api import models as db_models

    n = (
        db.query(db_models.SpaceReservation)
        .filter(db_models.SpaceReservation.kind == kind,
                db_models.SpaceReservation.ref_id == str(ref_id))
        .delete(synchronize_session=False)
    )
    db.commit()
    return bool(n)


def _still_active(db: Any, row: Any) -> bool:
    from api import models as db_models

    if row.kind == KIND_RIP:
        return db.query(db_models.Job).filter(
            db_models.Job.mount_point == row.ref_id,
            db_models.Job.rip_state.in_(("pending", "running")),
        ).first() is not None
    if row.kind in (KIND_DV7_INPLACE, KIND_DV7_SCRATCH, KIND_DV7_PUSH):
        lib = db.query(db_models.LibraryMediaFile).filter(
            db_models.LibraryMediaFile.id == row.ref_id
        ).first()
        return lib is not None and lib.status == "converting"
    if row.kind == KIND_DV7_POSTPROCESS:
        job_id = str(row.ref_id).split(":", 1)[0]
        job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
        return job is not None and job.job_status in ("running", "validating")
    return True


def sweep_stale(db: Any) -> int:
    """Drop reservations whose task is no longer active (crash, restart)."""
    from api import models as db_models

    cutoff = datetime.now(timezone.utc) - STALE_GRACE
    dropped = 0
    for row in db.query(db_models.SpaceReservation).all():
        created = row.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created is not None and created > cutoff:
            continue
        if not _still_active(db, row):
            db.delete(row)
            dropped += 1
    if dropped:
        db.commit()
        log.info("space_reservations: swept %s stale reservation(s)", dropped)
    return dropped
