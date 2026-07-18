"""Regression test for the create_job → drive_by_id_serial gap caught by
the 2026-06-17 live verification of the multi-drive PR cluster.

PR #549 added the column and the policy gate; the drive_swap_handler from
#551 fails jobs whose ``drive_by_id_serial`` matches the previous identity
at a swapped mount_point. But the job-creation flow in ``crud.create_job``
was never updated to write the column — every new job was NULL, and the
swap detector could never match. This test guards against regression.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from api import crud, models
from core.drive_identity import DriveIdentity


def _make_payload() -> dict:
    return {
        "disc_hash": "TESTHASH0000000000000000000000FF",
        "disc_num": "1",
        "mount_point": "/dev/sr1",
        "info_title": "Test Disc",
        "discdb_hit": False,
    }


def _id(source: str, serial: str) -> DriveIdentity:
    return DriveIdentity(
        by_id_serial=serial,
        vendor="V",
        model="M",
        bus="b",
        by_id_name="",
        hardware_name=None,
        identity_source=source,  # type: ignore[arg-type]
    )


class TestDriveByIdSerialPersisted:
    """``Job.drive_by_id_serial`` must reflect the resolved by-id identity."""

    def test_by_id_identity_persisted(self, test_db, monkeypatch):
        # Bypass payload hydration / sanitization side effects.
        monkeypatch.setattr(crud, "_hydrate_payload", lambda *a, **k: a[2])
        monkeypatch.setattr(crud, "_sanitize_unicode_for_db", lambda p: p)

        with patch(
            "core.drive_identity.resolve_drive_identity",
            return_value=_id("by-id", "PIONEER-1958040110900395"),
        ):
            with test_db() as db:
                job = crud.create_job(
                    db,
                    disc_num="1",
                    mount_point="/dev/sr1",
                    payload=_make_payload(),
                )
                assert job.drive_by_id_serial == "PIONEER-1958040110900395"

    def test_sysfs_fallback_persisted(self, test_db, monkeypatch):
        """Even when identity falls back to sysfs, we still record the
        synthetic serial so the swap detector has something to compare
        against on subsequent enumerations."""

        monkeypatch.setattr(crud, "_hydrate_payload", lambda *a, **k: a[2])
        monkeypatch.setattr(crud, "_sanitize_unicode_for_db", lambda p: p)

        with patch(
            "core.drive_identity.resolve_drive_identity",
            return_value=_id("sysfs", "sysfs:PIONEER:BD-RW:sr1"),
        ):
            with test_db() as db:
                job = crud.create_job(
                    db,
                    disc_num="1",
                    mount_point="/dev/sr1",
                    payload=_make_payload(),
                )
                assert job.drive_by_id_serial == "sysfs:PIONEER:BD-RW:sr1"

    def test_unknown_identity_persisted_as_null(self, test_db, monkeypatch):
        """If we can't resolve identity at all, NULL is correct — the policy
        gate would already have refused the rip in production, so reaching
        create_job with ``unknown`` means something's wrong with the layer
        below; do NOT pollute the column with the synthetic ``unknown:srN``
        placeholder."""

        monkeypatch.setattr(crud, "_hydrate_payload", lambda *a, **k: a[2])
        monkeypatch.setattr(crud, "_sanitize_unicode_for_db", lambda p: p)

        with patch(
            "core.drive_identity.resolve_drive_identity",
            return_value=_id("unknown", "unknown:sr1"),
        ):
            with test_db() as db:
                job = crud.create_job(
                    db,
                    disc_num="1",
                    mount_point="/dev/sr1",
                    payload=_make_payload(),
                )
                assert job.drive_by_id_serial is None

    def test_identity_resolution_failure_does_not_block_job(self, test_db, monkeypatch):
        """An unexpected exception from the identity layer must not abort
        job creation — Job rows continue to be created with NULL serial so
        the rest of the rip pipeline can still proceed."""

        monkeypatch.setattr(crud, "_hydrate_payload", lambda *a, **k: a[2])
        monkeypatch.setattr(crud, "_sanitize_unicode_for_db", lambda p: p)

        def boom(*_a, **_k):
            raise RuntimeError("simulated identity-layer fault")

        with patch("core.drive_identity.resolve_drive_identity", side_effect=boom):
            with test_db() as db:
                job = crud.create_job(
                    db,
                    disc_num="1",
                    mount_point="/dev/sr1",
                    payload=_make_payload(),
                )
                assert job.drive_by_id_serial is None
                assert job.mount_point == "/dev/sr1"
