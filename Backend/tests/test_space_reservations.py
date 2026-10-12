"""#895: heavy tasks reserve disk space; admission counts what others hold."""
from __future__ import annotations

import uuid
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

from api import models
from core import space_reservations as sr

GB = 1024 ** 3
_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture
def disk(monkeypatch, tmp_path):
    """Pretend the filesystem has ``state['free']`` bytes free; floor 10 GB."""
    state = {"free": 140 * GB}
    monkeypatch.setattr(sr.shutil, "disk_usage", lambda p: _Usage(983 * GB, 983 * GB - state["free"], state["free"]))
    monkeypatch.setenv("MKVAUTO_TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.delenv("MKVAUTO_SPACE_RESERVE_FLOOR_BYTES", raising=False)
    return state


def test_second_rip_cannot_claim_space_the_first_already_holds(test_db, disk, tmp_path):
    """The #875 shape by another route: two 80 GB rips on 140 GB free."""
    db = test_db()
    try:
        a = sr.try_reserve(db, sr.KIND_RIP, "/dev/sr0", tmp_path, 80 * GB)
        b = sr.try_reserve(db, sr.KIND_RIP, "/dev/sr1", tmp_path, 80 * GB)
        assert a.granted and not b.granted
        assert b.reserved_by_others == 80 * GB and b.floor == 10 * GB
        assert "reserved by other running work" in b.detail
        sr.release(db, sr.KIND_RIP, "/dev/sr0")
        assert sr.try_reserve(db, sr.KIND_RIP, "/dev/sr1", tmp_path, 80 * GB).granted
    finally:
        db.close()


def test_floor_is_kept_free_for_the_database(test_db, disk, tmp_path):
    db = test_db()
    try:
        assert not sr.try_reserve(db, sr.KIND_RIP, "/dev/sr0", tmp_path, 135 * GB).granted
        assert sr.try_reserve(db, sr.KIND_RIP, "/dev/sr0", tmp_path, 130 * GB).granted
    finally:
        db.close()


def test_reservation_shrinks_as_its_output_lands(test_db, disk, tmp_path):
    out = tmp_path / "raw"
    out.mkdir()
    db = test_db()
    try:
        assert sr.try_reserve(db, sr.KIND_RIP, "/dev/sr0", tmp_path, 1000, written_path=str(out)).granted
        (out / "t00.mkv").write_bytes(b"x" * 400)
        row = db.query(models.SpaceReservation).one()
        assert sr.outstanding_bytes(row) == 600
        (out / "t01.mkv").write_bytes(b"x" * 900)
        assert sr.outstanding_bytes(row) == 0
    finally:
        db.close()


def test_re_reserving_replaces_rather_than_adds(test_db, disk, tmp_path):
    db = test_db()
    try:
        sr.try_reserve(db, sr.KIND_RIP, "/dev/sr0", tmp_path, 50 * GB)
        sr.try_reserve(db, sr.KIND_RIP, "/dev/sr0", tmp_path, 60 * GB)
        rows = db.query(models.SpaceReservation).all()
        assert len(rows) == 1 and rows[0].bytes_reserved == 60 * GB
    finally:
        db.close()


def test_stale_rip_reservation_is_swept_after_the_grace_window(test_db, disk, tmp_path):
    db = test_db()
    try:
        sr.try_reserve(db, sr.KIND_RIP, "/dev/sr0", tmp_path, 80 * GB)
        assert sr.sweep_stale(db) == 0                      # inside the grace window
        row = db.query(models.SpaceReservation).one()
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.commit()
        # A running rip on that drive keeps it.
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}")
        db.add(disc)
        db.flush()
        job = models.Job(id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0", mount_point="/dev/sr0",
                         job_status="running", rip_state="running")
        db.add(job)
        db.commit()
        assert sr.sweep_stale(db) == 0
        job.rip_state = "failed"
        db.commit()
        assert sr.sweep_stale(db) == 1 and db.query(models.SpaceReservation).count() == 0
    finally:
        db.close()


def test_floor_is_configurable(monkeypatch):
    monkeypatch.setenv("MKVAUTO_SPACE_RESERVE_FLOOR_BYTES", str(5 * GB))
    assert sr.floor_bytes() == 5 * GB
    monkeypatch.setenv("MKVAUTO_SPACE_RESERVE_FLOOR_BYTES", "lots")
    assert sr.floor_bytes() == sr.DEFAULT_FLOOR_BYTES
