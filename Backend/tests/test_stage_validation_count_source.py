"""#864: the postprocess file-count check counts from the JOB's manifest.

The old expected_count came from ``Disc(job.disc_num, job.mount_point)`` —
drive-keyed live/cached info, i.e. whatever disc sits in the tray NOW. After
a disc swap it described a different disc and failed jobs whose files were
all present (prod: RE Extinction UHD — 122 ripped, 122 on disk, "expected"
124 from the wrong disc).
"""
from __future__ import annotations

import pytest

from core.job_paths import JobPaths
from core.stage_validation import validate_transfer_preconditions
from tests.postprocess_fixtures import job_with_rip_done_for_postprocess
from api import models


class _DriveBomb:
    """Instantiating this means the validator consulted the drive — the bug."""

    def __init__(self, *a, **k):
        raise AssertionError("validator must never read Disc(disc_num, mount_point) (#864)")


def test_complete_rip_passes_even_when_drive_holds_another_disc(test_db, tmp_path, monkeypatch):
    job_id, _title_id, paths = job_with_rip_done_for_postprocess(
        test_db, tmp_path, monkeypatch, num_titles=3, use_dummy_disc=False
    )
    # The tray now "holds" a different disc with a different title count —
    # the validator must never even look.
    monkeypatch.setattr("core.disc.Disc", _DriveBomb)

    session = test_db()
    try:
        job = session.query(models.Job).filter(models.Job.id == job_id).first()
        result = validate_transfer_preconditions(job, session, paths)
    finally:
        session.close()
    assert result.valid, result.errors
    assert result.details.get("source_files_expected") == 3
    assert result.details.get("source_files_found") == 3


def test_genuinely_missing_file_fails_with_receipts(test_db, tmp_path, monkeypatch):
    job_id, _title_id, paths = job_with_rip_done_for_postprocess(
        test_db, tmp_path, monkeypatch, num_titles=3, use_dummy_disc=False
    )
    monkeypatch.setattr("core.disc.Disc", _DriveBomb)
    (paths.raw / "test_t2.mkv").unlink()

    session = test_db()
    try:
        job = session.query(models.Job).filter(models.Job.id == job_id).first()
        result = validate_transfer_preconditions(job, session, paths)
    finally:
        session.close()
    assert not result.valid
    joined = "; ".join(result.errors)
    assert "2/3 MKV files" in joined
    # #853 rule 1: the claim carries receipts — WHICH file is absent.
    assert "test_t2.mkv" in joined
