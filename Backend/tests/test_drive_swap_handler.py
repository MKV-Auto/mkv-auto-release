"""Tests for ``core.drive_swap_handler.check_and_handle_swaps``.

Verifies the orchestration: identity_map cache update on every call, swaps
detected against the cached prior, affected jobs marked failed with a
descriptive error_reason, healthy jobs untouched.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from api import models
from core.drive_identity import DriveIdentity
from core import drive_swap_handler


@pytest.fixture(autouse=True)
def _isolated_cache():
    """Each test starts from a clean module-global cache."""

    drive_swap_handler.reset_cache_for_tests()
    yield
    drive_swap_handler.reset_cache_for_tests()


def _id(serial: str) -> DriveIdentity:
    return DriveIdentity(
        by_id_serial=serial,
        vendor="V",
        model="M",
        bus="b",
        by_id_name="",
        hardware_name=None,
        identity_source="by-id",
    )


def _make_disc(SessionLocal) -> str:
    with SessionLocal() as db:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=str(uuid.uuid4()).replace("-", "")[:32].upper(),
        )
        db.add(disc)
        db.commit()
        return disc.id


def _make_job(SessionLocal, disc_id: str, **kwargs) -> str:
    defaults: dict = dict(
        disc_num="0",
        mount_point="/dev/sr1",
        mode="copy",
        job_status="running",
        rip_state="running",
        dismissed=False,
        drive_by_id_serial="PIONEER",
    )
    defaults.update(kwargs)
    with SessionLocal() as db:
        job = models.Job(id=str(uuid.uuid4()), disc_id=disc_id, **defaults)
        db.add(job)
        db.commit()
        return job.id


class TestFirstCallSeedsCache:
    """The first call after backend start (or test reset) treats the
    observation as a fresh seed — no swaps are reported."""

    def test_first_call_returns_no_swaps(self, test_db):
        identity_map = {"/dev/sr1": _id("PIONEER"), "/dev/sr2": _id("ASUS")}
        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value=identity_map,
        ):
            with test_db() as db:
                swaps = drive_swap_handler.check_and_handle_swaps(db)

        assert swaps == []

    def test_cache_populated_after_first_call(self, test_db):
        identity_map = {"/dev/sr1": _id("PIONEER")}
        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value=identity_map,
        ):
            with test_db() as db:
                drive_swap_handler.check_and_handle_swaps(db)

        # A second call with the same map should still return no swaps,
        # but the cache must be populated.
        assert drive_swap_handler._last_identity_map["/dev/sr1"].by_id_serial == "PIONEER"


class TestSwapDetectedAndJobsFail:
    def test_swap_fails_matching_active_job(self, test_db):
        disc_id = _make_disc(test_db)
        jid = _make_job(test_db, disc_id, drive_by_id_serial="PIONEER")

        # Seed: PIONEER at sr1
        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value={"/dev/sr1": _id("PIONEER")},
        ):
            with test_db() as db:
                drive_swap_handler.check_and_handle_swaps(db)

        # Now the kernel reassigned sr1 to ASUS.
        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value={"/dev/sr1": _id("ASUS")},
        ):
            with test_db() as db:
                swaps = drive_swap_handler.check_and_handle_swaps(db)

        assert len(swaps) == 1
        assert swaps[0].previous_serial == "PIONEER"
        assert swaps[0].current_serial == "ASUS"

        # The job should now be failed with a descriptive error_reason.
        with test_db() as db:
            failed = db.query(models.Job).filter_by(id=jid).one()
        assert failed.rip_state == "failed"
        assert failed.job_status == "failed"
        assert "swapped" in (failed.error_reason or "").lower()
        assert "PIONEER" in (failed.error_reason or "")
        assert "ASUS" in (failed.error_reason or "")

    def test_unaffected_jobs_remain_healthy(self, test_db):
        disc_id = _make_disc(test_db)
        affected = _make_job(test_db, disc_id, drive_by_id_serial="PIONEER")
        # Different drive's job — must NOT be touched by a sr1 swap.
        other = _make_job(test_db, disc_id, mount_point="/dev/sr2",
                          drive_by_id_serial="OTHER")

        # Seed identity map.
        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value={
                "/dev/sr1": _id("PIONEER"),
                "/dev/sr2": _id("OTHER"),
            },
        ):
            with test_db() as db:
                drive_swap_handler.check_and_handle_swaps(db)

        # Swap on sr1 only.
        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value={
                "/dev/sr1": _id("ASUS"),
                "/dev/sr2": _id("OTHER"),
            },
        ):
            with test_db() as db:
                drive_swap_handler.check_and_handle_swaps(db)

        with test_db() as db:
            a = db.query(models.Job).filter_by(id=affected).one()
            o = db.query(models.Job).filter_by(id=other).one()
        assert a.rip_state == "failed"
        assert o.rip_state == "running"

    def test_dismissed_jobs_not_failed(self, test_db):
        """Dismissed jobs are no longer user-visible; not worth touching them
        on swap detection (they're not actively writing to the device)."""

        disc_id = _make_disc(test_db)
        jid = _make_job(test_db, disc_id, drive_by_id_serial="PIONEER", dismissed=True)

        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value={"/dev/sr1": _id("PIONEER")},
        ):
            with test_db() as db:
                drive_swap_handler.check_and_handle_swaps(db)

        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value={"/dev/sr1": _id("ASUS")},
        ):
            with test_db() as db:
                drive_swap_handler.check_and_handle_swaps(db)

        with test_db() as db:
            j = db.query(models.Job).filter_by(id=jid).one()
        assert j.rip_state == "running"

    def test_terminal_state_jobs_not_failed(self, test_db):
        """Already-completed and already-failed jobs are skipped."""

        disc_id = _make_disc(test_db)
        completed = _make_job(test_db, disc_id, rip_state="completed",
                              job_status="running", drive_by_id_serial="PIONEER")

        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value={"/dev/sr1": _id("PIONEER")},
        ):
            with test_db() as db:
                drive_swap_handler.check_and_handle_swaps(db)

        with patch(
            "core.drive_swap_handler.build_identity_map",
            return_value={"/dev/sr1": _id("ASUS")},
        ):
            with test_db() as db:
                drive_swap_handler.check_and_handle_swaps(db)

        with test_db() as db:
            c = db.query(models.Job).filter_by(id=completed).one()
        assert c.rip_state == "completed"
        assert c.error_reason is None
