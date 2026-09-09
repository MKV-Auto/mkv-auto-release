"""#731: rip-failure classification — disc defect vs drive fault, with receipts.

Fixtures reproduce the 2026-09 live incidents: the RE Apocalypse UHD fixed-
offset pattern (336 errors at one offset + consecutive offset-0 streams),
the cross-drive condemnation experiment, and the scattered drive-fault shape.
"""
from __future__ import annotations

import uuid

import pytest

from api import models
from core.rip_failure_analysis import RipFailureVerdict, analyze_rip_failure


def _err_line(path: str, offset: int, err: str = "Scsi error - HARDWARE ERROR:4407") -> str:
    return f"Error '{err}' occurred while reading '{path}' at offset '{offset}'\n"


def _mk_job(session, *, serial="PIONEER123", disc=None):
    if disc is None:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:12]}")
        session.add(disc)
        session.flush()
    job = models.Job(
        id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0",
        mount_point="/dev/sr0", job_status="failed", rip_state="failed",
        drive_by_id_serial=serial,
    )
    session.add(job)
    session.commit()
    return job, disc


def test_fixed_offset_pattern_is_disc_defect(test_db):
    """The Apocalypse specimen: hundreds of errors at ONE offset plus a
    consecutive offset-0 stream run — a pressed dead zone, not a drive."""
    log = _err_line("/BDMV/STREAM/00357.m2ts", 3164209152) * 336
    for n in range(356, 368):
        log += _err_line(f"/BDMV/STREAM/{n:05d}.m2ts", 0) * 14

    with test_db() as db:
        job, _ = _mk_job(db)
        v = analyze_rip_failure(job, db, log_text=log)
    assert v.classification == "disc_defect"
    assert v.failure_kind == "precondition"
    joined = "; ".join(v.receipts)
    assert "336 read errors at the identical offset 3164209152" in joined
    assert "00356" in joined and "00367" in joined
    assert "exchange" in v.remedy


def test_cross_drive_history_condemns_the_disc(test_db):
    """Same disc failed on another drive serial → disc, definitively —
    even with no useful log (the ASUS silent-stall case)."""
    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="PIONEER123", disc=disc)  # prior failure, other drive
        v = analyze_rip_failure(job, db, log_text="")
    assert v.classification == "disc_defect"
    assert "1 other drive(s)" in "; ".join(v.receipts)


def test_fallback_serial_representations_do_not_fabricate_cross_drive(test_db):
    """The same physical drive recorded under a sysfs-era fallback identity
    must not count as a second drive (prod validation caught one Pioneer
    counted twice across its identity representations)."""
    with test_db() as db:
        job, disc = _mk_job(db, serial="1958040110900395")
        _mk_job(db, serial="sysfs:PIONEER:BD-RW__BDR-XD06U:sr0", disc=disc)
        _mk_job(db, serial="unknown:sr1", disc=disc)
        v = analyze_rip_failure(job, db, log_text="")
    assert v.classification == "indeterminate"


def test_scattered_errors_with_drive_history_is_drive_fault(test_db):
    log = "".join(
        _err_line(f"/BDMV/STREAM/{n:05d}.m2ts", 1000000 + n * 777)
        for n in range(1, 12)
    )
    with test_db() as db:
        job, _ = _mk_job(db, serial="FLAKY_DRIVE")
        _mk_job(db, serial="FLAKY_DRIVE")  # two OTHER discs failed on it
        _mk_job(db, serial="FLAKY_DRIVE")
        v = analyze_rip_failure(job, db, log_text=log)
    assert v.classification == "drive_fault"
    assert v.failure_kind == "transient"
    assert "failed 2 other disc(s)" in "; ".join(v.receipts)
    assert "power" in v.remedy


def test_scattered_errors_without_history_is_indeterminate(test_db):
    """The pattern alone must not condemn a drive — first failure on a
    fresh drive keeps the generic message."""
    log = "".join(
        _err_line(f"/BDMV/STREAM/{n:05d}.m2ts", 1000000 + n * 777)
        for n in range(1, 12)
    )
    with test_db() as db:
        job, _ = _mk_job(db)
        v = analyze_rip_failure(job, db, log_text=log)
    assert v.classification == "indeterminate"
    assert v.failure_kind is None


def test_few_errors_are_indeterminate(test_db):
    log = _err_line("/BDMV/STREAM/00080.m2ts", 12345) * 2
    with test_db() as db:
        job, _ = _mk_job(db)
        v = analyze_rip_failure(job, db, log_text=log)
    assert v.classification == "indeterminate"


def test_augment_appends_diagnosis_and_remedy(test_db):
    log = _err_line("/BDMV/STREAM/00357.m2ts", 99) * 25
    with test_db() as db:
        job, _ = _mk_job(db)
        v = analyze_rip_failure(job, db, log_text=log)
    out = v.augment("MakeMKV copy finished with 24 title(s) failed.")
    assert out.startswith("MakeMKV copy finished")
    assert "Diagnosis: disc defect" in out
    assert "retry cannot succeed" in out
    # Indeterminate leaves the text untouched.
    assert RipFailureVerdict("indeterminate", None).augment("x") == "x"
