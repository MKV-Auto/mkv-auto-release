"""#731/#883: rip-failure classification — disc defect vs drive fault, with receipts.

Fixtures reproduce the 2026-09 live incidents. The four September failures
(RE: Apocalypse UHD ×3, Star Wars Ep. III) are the real logs' shapes: one
hard error retried at one spot, then every untouched stream failing at byte
0. Both discs later read completely, error-free — those are wedged drives,
which this analyzer used to call pressed disc defects (#883).
"""
from __future__ import annotations

import uuid

import pytest

from api import models
from core.job_paths import JobPaths
from core.rip_failure_analysis import (
    DRIVE_WEDGED_REMEDY,
    FIXED_LOCATION_REMEDY,
    SAME_SPOT_ONE_DRIVE_REMEDY,
    RipFailureVerdict,
    analyze_rip_failure,
)


def _err_line(path: str, offset: int, err: str = "Scsi error - HARDWARE ERROR:4407") -> str:
    return f"Error '{err}' occurred while reading '{path}' at offset '{offset}'\n"


def _mk_job(session, *, serial="PIONEER123", disc=None, rip_state="failed", log=None, error_reason=None):
    if disc is None:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:12]}")
        session.add(disc)
        session.flush()
    job = models.Job(
        id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0",
        mount_point="/dev/sr0",
        job_status="completed" if rip_state == "completed" else "failed",
        rip_state=rip_state,
        drive_by_id_serial=serial,
        error_reason=error_reason,
    )
    session.add(job)
    session.commit()
    if log is not None:
        raw = JobPaths.for_id(str(job.id)).raw
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "makemkv_progress.log").write_text(log, encoding="utf-8")
    return job, disc


@pytest.fixture(autouse=True)
def _jobs_root(tmp_path, monkeypatch):
    """Prior jobs' progress logs live under the jobs root; isolate it."""
    monkeypatch.setenv("MKVAUTO_JOBS_DIR", str(tmp_path / "jobs"))


COPY_COMPLETE = 'MSG:5036,260,1,"Copy complete. 11 titles saved.","Copy complete. %1 titles saved.","11"\n'
NO_DRIVES = (
    'MSG:5042,0,0,"The program can\'t find any usable optical drives.",'
    '"The program can\'t find any usable optical drives."\n'
)


APOCALYPSE_LATER = ["00080", "00081", "00082"] + [f"{n:05d}" for n in range(351, 368)]
EP3_LATER = ["00011", "00122", "00123", "00720", "00301", "00302"]


def _wedge_log(first: str, offset: int, later: list[str], saved_failed: tuple[int, int]) -> str:
    """One hard error retried at one spot (alternating with the raw device's
    Remote I/O errors, as MakeMKV logs it), then the drive stops answering:
    each untouched stream fails at its first byte, 7 tries apiece."""
    log = ""
    for _ in range(40):
        log += _err_line(f"/BDMV/STREAM/{first}.m2ts", offset) * 7
        log += _err_line("/dev/sr0", offset, err="Posix error - Remote I/O error") * 5
    log += _err_line(f"/BDMV/STREAM/{first}.m2ts", 0) * 21
    for n in later:
        if n != first:
            log += _err_line(f"/BDMV/STREAM/{n}.m2ts", 0) * 7
    saved, failed = saved_failed
    log += (f'MSG:5037,516,2,"Copy complete. {saved} titles saved, {failed} failed.",'
            f'"Copy complete. %1 titles saved, %2 failed.","{saved}","{failed}"\n')
    return log


# The four recorded September failures (job, first hard error, later streams).
SEPTEMBER = {
    "apocalypse-0906-f8aae869": ("00357", 3164209152, APOCALYPSE_LATER, (116, 24)),
    "apocalypse-0907-e6336c3a": ("00357", 159252480, APOCALYPSE_LATER, (116, 24)),
    "apocalypse-0916-f804f6f4": ("00367", 3560577024, ["00357"] + APOCALYPSE_LATER, (116, 24)),
    "ep3-0919-fa2b0b30": ("00300", 143523840, EP3_LATER, (3, 8)),
}


@pytest.mark.parametrize("name", list(SEPTEMBER))
def test_september_failures_are_a_wedged_drive_not_the_disc(test_db, name):
    """#883: each of these discs later read 100% error-free. The analyzer
    must point at the drive and must never tell the user to exchange it."""
    first, offset, later, saved_failed = SEPTEMBER[name]
    with test_db() as db:
        job, _ = _mk_job(db, serial="1958040110900395")
        v = analyze_rip_failure(job, db, log_text=_wedge_log(first, offset, later, saved_failed))
    assert v.classification == "drive_fault"
    assert v.failure_kind == "transient"
    assert f"{first}.m2ts" in v.receipts[0]
    assert "other streams failed at their first byte" in v.receipts[0]
    assert v.remedy == DRIVE_WEDGED_REMEDY
    out = v.augment("MakeMKV copy finished with 24 title(s) failed.")
    assert "Diagnosis: drive fault" in out
    assert "exchange" not in out.split("If it fails again")[0]
    assert "disc defect" not in out


def test_september_apocalypse_sequence_never_convicts_the_disc(test_db):
    """The real sequence on one Pioneer: three attempts, each first failing
    somewhere else. A moving first error is no evidence against the disc —
    and the earlier logs' MSG 5037 ("Copy complete. 116 titles saved, 24
    failed.") is not a completed read either."""
    with test_db() as db:
        disc = None
        for name in list(SEPTEMBER)[:2]:
            first, offset, later, sf = SEPTEMBER[name]
            _, disc = _mk_job(db, serial="1958040110900395", disc=disc,
                              log=_wedge_log(first, offset, later, sf))
        job, _ = _mk_job(db, serial="1958040110900395", disc=disc)
        first, offset, later, sf = SEPTEMBER["apocalypse-0916-f804f6f4"]
        v = analyze_rip_failure(job, db, log_text=_wedge_log(first, offset, later, sf))
    assert v.classification == "drive_fault"


def test_a_lone_pile_up_at_one_offset_is_not_a_disc_defect(test_db):
    """Hundreds of errors at one offset are MakeMKV retrying wherever the
    drive first faltered — on a first attempt that says nothing either way."""
    with test_db() as db:
        job, _ = _mk_job(db)
        v = analyze_rip_failure(job, db, log_text=_err_line("/BDMV/STREAM/00357.m2ts", 3164209152) * 336)
    assert v.classification == "indeterminate"


def test_same_spot_on_two_drives_is_a_disc_defect(test_db):
    """A genuine fixed defect: the same place fails in a second drive. A
    partial copy (MSG 5037) before it must not veto the verdict."""
    prior = _err_line("/BDMV/STREAM/00200.m2ts", 2_000_000_000) * 7 + (
        'MSG:5037,516,2,"Copy complete. 10 titles saved, 1 failed.",'
        '"Copy complete. %1 titles saved, %2 failed.","10","1"\n'
    )
    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="PIONEER123", disc=disc, log=prior)
        # 4 MiB away on the other drive — the same place within read granularity.
        v = analyze_rip_failure(job, db, log_text=_err_line("/BDMV/STREAM/00200.m2ts", 2_004_194_304) * 3)
    assert v.classification == "disc_defect"
    assert v.failure_kind == "precondition"
    receipt = "; ".join(v.receipts)
    assert "00200.m2ts failed near 2.00 GB again" in receipt
    assert "on 2 drive(s)" in receipt
    assert v.remedy == FIXED_LOCATION_REMEDY


def test_same_spot_twice_on_one_drive_is_only_suspect(test_db):
    """A marginal drive can repeat on a marginal spot — one drive alone
    cannot condemn the disc; the remedy is another drive, not an exchange."""
    spot = _err_line("/BDMV/STREAM/00200.m2ts", 2_000_000_000) * 7
    with test_db() as db:
        job, disc = _mk_job(db, serial="PIONEER123")
        _mk_job(db, serial="PIONEER123", disc=disc, log=spot)
        v = analyze_rip_failure(job, db, log_text=spot)
    assert v.classification == "suspect_disc"
    assert v.failure_kind == "transient"
    assert v.remedy == SAME_SPOT_ONE_DRIVE_REMEDY
    out = v.augment("Rip failed.")
    assert "Diagnosis: possible disc defect (" in out


def test_a_different_spot_in_the_same_stream_is_not_a_repeat(test_db):
    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="PIONEER123", disc=disc,
                log=_err_line("/BDMV/STREAM/00357.m2ts", 3164209152) * 7)
        v = analyze_rip_failure(job, db, log_text=_err_line("/BDMV/STREAM/00357.m2ts", 159252480) * 7)
    assert v.classification == "indeterminate"


def test_a_repeat_after_the_disc_was_read_completely_is_not_a_disc_defect(test_db):
    """#876 veto still wins: the disc has been read end-to-end, so whatever
    is failing now, it isn't the disc."""
    spot = _err_line("/BDMV/STREAM/00200.m2ts", 2_000_000_000) * 7
    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="PIONEER123", disc=disc, log=spot)
        _mk_job(db, serial="PIONEER123", disc=disc, rip_state="completed")
        v = analyze_rip_failure(job, db, log_text=spot)
    assert v.classification not in ("disc_defect", "suspect_disc")


def test_prior_failure_that_never_read_the_disc_is_not_evidence(test_db):
    """#876: "no usable optical drives", a revoked task, or a missing log say
    nothing about the disc — they must not count as another drive's verdict."""
    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="PIONEER123", disc=disc, log=NO_DRIVES)
        _mk_job(db, serial="LGWH16NS40", disc=disc)  # revoked mid-rip, no log left
        v = analyze_rip_failure(job, db, log_text=_err_line("/BDMV/STREAM/00200.m2ts", 5555) * 3)
    assert v.classification == "indeterminate"


def test_silent_stall_is_not_a_disc_defect_by_history_alone(test_db):
    """A failure with zero read errors of its own cannot be convicted by
    another drive's history — even history with real read errors."""
    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="PIONEER123", disc=disc,
                log=_err_line("/BDMV/STREAM/00300.m2ts", 143523840) * 336)
        v = analyze_rip_failure(job, db, log_text="PRGV:48790,24454,65536\n")
    assert v.classification != "disc_defect"


def test_a_completed_read_vetoes_disc_defect(test_db):
    """A disc that MakeMKV has read to the end is not defective. The job row
    can still say rip_state='failed' — #875 killed exactly such a job after
    its copy completed — so the log's "Copy complete" counts too."""
    fixed_offset = _err_line("/BDMV/STREAM/00357.m2ts", 3164209152) * 336
    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="ASUS_BW16", disc=disc, log="PRGV:1,2,3\n" + COPY_COMPLETE)
        v = analyze_rip_failure(job, db, log_text=fixed_offset)
    assert v.classification != "disc_defect"

    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="PIONEER123", disc=disc, rip_state="completed")
        v = analyze_rip_failure(job, db, log_text=fixed_offset)
    assert v.classification != "disc_defect"


def test_876_prod_history_is_not_called_a_disc_defect(test_db):
    """The exact 2026-09-19 Star Wars Ep. III history. The user was told
    "disc defect … exchange it" for a disc that had read end-to-end
    error-free in the same drive three hours earlier."""
    pioneer, asus = "1958040110900395", "AAAABBBB000E"
    pioneer_errors = (
        _err_line("/BDMV/STREAM/00300.m2ts", 143523840) * 336
        + _err_line("/dev/sr0", 143523840, err="Posix error - Remote I/O error") * 240
        + _err_line("/BDMV/STREAM/00011.m2ts", 0) * 7
    )
    with test_db() as db:
        current, disc = _mk_job(db, serial=asus)                     # 53fc7f19
        _mk_job(db, serial=pioneer, disc=disc, log=NO_DRIVES)         # 882a91ac
        _mk_job(db, serial=pioneer, disc=disc, log=pioneer_errors)    # fa2b0b30 (then revoked)
        _mk_job(db, serial=asus, disc=disc,
                log="PRGV:1,2,3\n" + COPY_COMPLETE)                  # 539402c4 (then #875)
        # Tonight: zero read errors, MakeMKV just went silent.
        v = analyze_rip_failure(current, db, log_text="PRGV:48790,24454,65536\n")
    assert v.classification != "disc_defect"
    assert "exchange" not in v.augment("No output from MakeMKV for 300s; rip aborted as stalled.")


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
        # two OTHER discs failed on it, each with drive evidence
        _mk_job(db, serial="FLAKY_DRIVE", log=_err_line("/BDMV/STREAM/00010.m2ts", 777_000) * 3)
        _mk_job(db, serial="FLAKY_DRIVE", error_reason=STALL)
        v = analyze_rip_failure(job, db, log_text=log)
    assert v.classification == "drive_fault"
    assert v.failure_kind == "transient"
    assert "failed 2 other disc(s)" in "; ".join(v.receipts)
    assert "power" in v.remedy


STALL = "No output from MakeMKV for 300s; rip aborted as stalled."


def test_failures_that_never_read_the_disc_do_not_condemn_the_drive(test_db):
    """2026-10-08, RC on prod: the ASUS was called a faulty drive because it
    had "failed 2 other discs" — one a silent stall, the other a user's
    eject. Ejects, cancellations and "no drives" say nothing about a drive."""
    with test_db() as db:
        job, _ = _mk_job(db, serial="AAAABBBB000E")
        _mk_job(db, serial="AAAABBBB000E", error_reason=STALL)            # Apocalypse 9/7
        _mk_job(db, serial="AAAABBBB000E", error_reason="Job failed: disc ejected")
        _mk_job(db, serial="AAAABBBB000E", log=NO_DRIVES)
        _mk_job(db, serial="AAAABBBB000E", error_reason="Celery task was terminated")
        v = analyze_rip_failure(job, db, log_text="PRGV:28483,47273,65536\n")
    assert v.classification == "indeterminate"

    with test_db() as db:
        job, _ = _mk_job(db, serial="STALLING_DRIVE")
        _mk_job(db, serial="STALLING_DRIVE", error_reason=STALL)
        _mk_job(db, serial="STALLING_DRIVE", error_reason=STALL)
        v = analyze_rip_failure(job, db, log_text="PRGV:28483,47273,65536\n")
    assert v.classification == "drive_fault"
    assert "failed 2 other disc(s) with read errors or stalls" in v.receipts[0]


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
    spot = _err_line("/BDMV/STREAM/00357.m2ts", 99_000_000) * 25
    with test_db() as db:
        job, disc = _mk_job(db, serial="ASUS_BW16")
        _mk_job(db, serial="PIONEER123", disc=disc, log=spot)
        v = analyze_rip_failure(job, db, log_text=spot)
    out = v.augment("MakeMKV copy finished with 24 title(s) failed.")
    assert out.startswith("MakeMKV copy finished")
    assert "Diagnosis: disc defect" in out
    assert "cannot succeed" in out
    # Indeterminate leaves the text untouched.
    assert RipFailureVerdict("indeterminate", None).augment("x") == "x"
