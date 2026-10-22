"""Rip-failure classification: disc defect vs drive fault, with receipts (#731).

Sense codes alone cannot split the two — HARDWARE ERROR 44/07 appeared in
BOTH classes during the 2026-09 incidents. What one failed attempt CAN show,
and what it cannot (#883):

- **A wedged drive.** One hard read error, retried hundreds of times at a
  single offset, then every LATER stream failing at its first byte — streams
  that were never read and cannot all be defective at byte 0. That is the
  drive having stopped answering after a fault or reset, and only a
  power-cycle clears it. Every September failure of RE: Apocalypse UHD and
  Star Wars Ep. III has this shape, with the first error at a DIFFERENT
  place each time (00357 @ 3.16 GB, 00357 @ 0.16 GB, 00367 @ 3.56 GB,
  00300 @ 0.14 GB) — and both discs later read completely, error-free. The
  "N errors at one offset" pile-up is MakeMKV's retry loop wherever the drive
  first faltered; it is not evidence about the disc. This analyzer used to
  call exactly that a pressed dead zone and tell the user to exchange a
  good disc.
- **A disc defect needs the same spot to fail again**, in another attempt:
  on a second drive it is the disc ("exchange it"); twice on the same drive
  it is only *suspect* ("clean it and try another drive"), because a
  marginal drive can repeat on a marginal spot.
- **A drive failing several different discs** points at the drive.

History only counts what it can verify (#876):
  * a prior failure is evidence only if ITS log recorded read errors —
    "no usable optical drives", a revoked task, or a clean-log stall never
    read the disc and say nothing about it;
  * a disc that has ever been read to completion (MSG 5036, or a
    completed/skipped rip) is not defective, whatever happens later;
  * a failure with zero read errors of its own cannot be called a disc
    defect by history alone.

Every verdict carries receipts (#853 rule 1: verified facts, never
inference) and an `indeterminate` escape hatch that keeps the generic
message rather than guessing confidently.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

# MakeMKV robot/message line: Error 'Scsi error - HARDWARE ERROR:4407'
# occurred while reading '/BDMV/STREAM/00357.m2ts' at offset '3164209152'
_READ_ERROR_RE = re.compile(
    r"Error '(?P<err>[^']+)' occurred while reading '(?P<path>[^']+)' at offset '(?P<offset>\d+)'"
)

# Thresholds — deliberately conservative; below them the verdict is
# indeterminate and nothing changes for the user.
WEDGE_MIN_STREAMS_AT_BYTE0 = 3       # other streams failing at byte 0 after a hard error
SAME_SPOT_TOLERANCE_BYTES = 128 * 1024 * 1024  # "same place" across attempts, same stream
SCATTER_MIN_UNIQUE_OFFSETS = 8        # scattered-looking error spread…
SCATTER_MAX_REPEAT = 2                # …with no offset repeating more than this
DRIVE_CORROBORATION_MIN_DISCS = 2     # other distinct discs failed on this drive
HISTORY_MAX_PRIOR_JOBS = 20           # recent jobs for this disc inspected for evidence
DRIVE_HISTORY_MAX_JOBS = 50           # recent failures on this drive inspected for evidence

# MakeMKV's own end-of-copy line (MSG 5036): "Copy complete. N titles saved."
# A job row can still say rip_state='failed' after this — the 2026-09-19 disc
# read fully and was then killed by the host's disk filling up (#875).
# Only 5036 counts: a copy with failed titles ends with MSG 5037 "Copy
# complete. N titles saved, M failed." — the same words, but the opposite of
# a completed read (every September wedge log ends that way, #883). A live
# log carries "[timestamp] " prefixes until the rip persists the raw copy.
_COPY_COMPLETE_RE = re.compile(r"^(?:\[[^\]]*\] )?MSG:5036,", re.MULTILINE)

FIXED_LOCATION_REMEDY = (
    "The same spot on the disc has failed in more than one drive, so another "
    "retry as-is cannot succeed — clean the disc and, if it fails there "
    "again, exchange it."
)
SAME_SPOT_ONE_DRIVE_REMEDY = (
    "This spot has now failed more than once in this drive — clean the disc "
    "and try it in another drive; if it fails there at the same spot, "
    "exchange it."
)
DRIVE_FAULT_REMEDY = (
    "This looks like the drive, not the disc — check its power supply and "
    "USB cable, or power-cycle the drive, then retry."
)
# In a hung-drive rip error (core.utils.MakeMKVDriveHungError); keys the verdict below.
DRIVE_HUNG_MARKER = "could not be stopped"
DRIVE_HUNG_REMEDY = (
    "Unplug the drive's USB cable (and its power, if it has its own) for about "
    "10 seconds and plug it back in. Nothing on this drive will work until "
    "then. Then retry the disc."
)
DRIVE_WEDGED_REMEDY = (
    "The drive stopped responding partway through, so the disc itself is "
    "probably fine — power-cycle the drive (unplug its power and USB for "
    "about 10 seconds), then retry. If it fails again at the same spot, "
    "clean the disc and try another drive."
)
Classification = Literal["disc_defect", "suspect_disc", "drive_fault", "indeterminate"]

_LABELS = {
    "disc_defect": "disc defect",
    "suspect_disc": "possible disc defect",
    "drive_fault": "drive fault",
}


@dataclass
class RipFailureVerdict:
    classification: Classification
    failure_kind: Optional[str]           # "precondition" | "transient" | None
    receipts: list[str] = field(default_factory=list)
    remedy: Optional[str] = None

    def augment(self, error_reason: str) -> str:
        """Append the verdict, receipts, and remedy to the raw error text."""
        if self.classification == "indeterminate":
            return error_reason
        label = _LABELS[self.classification]
        parts = [error_reason.rstrip(), f"Diagnosis: {label} ({'; '.join(self.receipts)})."]
        if self.remedy:
            parts.append(self.remedy)
        return " ".join(parts)


def _parse_read_errors(log_text: str) -> list[tuple[str, str, int]]:
    return [
        (m.group("err"), m.group("path"), int(m.group("offset")))
        for m in _READ_ERROR_RE.finditer(log_text)
    ]


def _is_stream(path: str) -> bool:
    """A disc file (e.g. /BDMV/STREAM/00357.m2ts), not the raw device path
    MakeMKV also reports (/dev/sr0) for the same failed read."""
    return not path.startswith("/dev/")


def _file_of(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _hard_error_spots(errors: list[tuple[str, str, int]]) -> list[tuple[str, int]]:
    """Distinct (file, offset) places where a read failed past byte 0."""
    seen: dict[tuple[str, int], None] = {}
    for _err, path, offset in errors:
        if offset > 0 and _is_stream(path):
            seen.setdefault((_file_of(path), offset), None)
    return list(seen)


def _wedge_after_first_error(errors: list[tuple[str, str, int]]) -> tuple[Optional[tuple[str, int]], list[str]]:
    """(first hard error spot, OTHER streams that then failed at byte 0).

    After a drive faults it fails every later read immediately, so streams it
    has not touched yet each fail at offset 0. The first spot's own file is
    excluded: MakeMKV re-opening that same file is not new evidence.
    """
    first: Optional[tuple[str, int]] = None
    later_at_zero: dict[str, None] = {}
    for _err, path, offset in errors:
        if not _is_stream(path):
            continue
        name = _file_of(path)
        if first is None:
            if offset > 0:
                first = (name, offset)
            continue
        if offset == 0 and name != first[0]:
            later_at_zero.setdefault(name, None)
    return first, list(later_at_zero)


# A failure that says something about the drive: MakeMKV went silent mid-read.
_STALL_RE = re.compile(r"No output from MakeMKV for \d+s", re.IGNORECASE)


def _is_drive_evidence(job: Any) -> bool:
    """Did this failed job actually exercise the drive and fail at it?

    Only read errors in its own log, or a stall, count. An eject, a
    cancellation, "no usable optical drives", a revoked task or a setup error
    never read anything and say nothing about the drive (2026-10-08: the ASUS
    was called faulty on the strength of a user's eject).
    """
    if _STALL_RE.search(getattr(job, "error_reason", None) or ""):
        return True
    text = _read_progress_log(job)
    return bool(text and _hard_error_spots(_parse_read_errors(text)))


def _same_spot(a: tuple[str, int], b: tuple[str, int]) -> bool:
    return a[0] == b[0] and abs(a[1] - b[1]) <= SAME_SPOT_TOLERANCE_BYTES


def _gb(offset: int) -> str:
    return f"{offset / 1e9:.2f} GB"


def _is_true_by_id_serial(serial: Optional[str]) -> bool:
    """Only TRUE by-id serials count as distinct drives. Fallback identities
    embed their source ("sysfs:VENDOR:MODEL:srN", "unknown:srN") — the same
    physical drive recorded under two representations must not fabricate a
    cross-drive condemnation (seen validating against prod: one Pioneer
    counted twice)."""
    return bool(serial) and ":" not in serial


@dataclass
class _PriorFailure:
    serial: Optional[str]
    spots: list[tuple[str, int]]


@dataclass
class _History:
    # Prior failed attempts on THIS disc whose own logs recorded read errors
    # past byte 0 (verified evidence, not infrastructure), with where.
    prior_failures: list[_PriorFailure] = field(default_factory=list)
    # Any other job on this disc read it to completion.
    disc_read_completed: bool = False
    # Distinct OTHER discs this drive failed on.
    other_discs: int = 0


def _history(job: Any, db: Any) -> _History:
    """Evidence from this disc's and this drive's recorded history (#731, #876)."""
    hist = _History()
    try:
        from api import models

        disc_id = getattr(job, "disc_id", None)
        serial = getattr(job, "drive_by_id_serial", None)
        if disc_id:
            prior = (
                db.query(models.Job)
                .filter(models.Job.disc_id == disc_id, models.Job.id != job.id)
                .order_by(models.Job.created_at.desc())
                .limit(HISTORY_MAX_PRIOR_JOBS)
                .all()
            )
            for p in prior:
                if getattr(p, "rip_state", None) in ("completed", "skipped"):
                    hist.disc_read_completed = True
                    continue
                if getattr(p, "rip_state", None) != "failed":
                    continue
                text = _read_progress_log(p)
                if text and _COPY_COMPLETE_RE.search(text):
                    hist.disc_read_completed = True
                    continue
                spots = _hard_error_spots(_parse_read_errors(text)) if text else []
                if spots:
                    p_serial = getattr(p, "drive_by_id_serial", None)
                    hist.prior_failures.append(_PriorFailure(
                        serial=p_serial if _is_true_by_id_serial(p_serial) else None,
                        spots=spots,
                    ))
        if serial:
            rows = (
                db.query(models.Job)
                .filter(
                    models.Job.drive_by_id_serial == serial,
                    models.Job.rip_state == "failed",
                    models.Job.disc_id != disc_id,
                    models.Job.disc_id.isnot(None),
                )
                .order_by(models.Job.created_at.desc())
                .limit(DRIVE_HISTORY_MAX_JOBS)
                .all()
            )
            hist.other_discs = len({
                r.disc_id for r in rows if r.disc_id and _is_drive_evidence(r)
            })
    except Exception as exc:
        logger.warning("rip_failure_analysis: history lookup failed: %s", exc)
    return hist


def _read_progress_log(job: Any) -> str:
    try:
        from core.job_paths import JobPaths

        path = JobPaths.for_id(str(job.id)).raw / "makemkv_progress.log"
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        logger.debug("rip_failure_analysis: no progress log for job %s: %s",
                     getattr(job, "id", None), exc)
    return ""


def analyze_rip_failure(job: Any, db: Any, log_text: Optional[str] = None) -> RipFailureVerdict:
    """Classify a failed rip. Never raises; falls back to indeterminate."""
    try:
        # --- drive hung: MakeMKV couldn't even be killed (#909) ---
        # Nothing else in the log matters; no retry works until it's replugged.
        if DRIVE_HUNG_MARKER in (getattr(job, "error_reason", None) or ""):
            return RipFailureVerdict(
                classification="drive_fault",
                failure_kind="transient",
                receipts=["MakeMKV was stuck on a read the drive never answered"],
                remedy=DRIVE_HUNG_REMEDY,
            )

        text = log_text if log_text is not None else _read_progress_log(job)
        errors = _parse_read_errors(text)
        offsets = Counter(offset for _e, _p, offset in errors)
        same_offset_max = max(offsets.values()) if offsets else 0
        hist = _history(job, db)
        spots = _hard_error_spots(errors)
        first_spot, later_at_zero = _wedge_after_first_error(errors)
        serial = getattr(job, "drive_by_id_serial", None)

        # --- disc defect: the same spot failing again in another attempt ---
        # A disc read to completion is not defective: if it now fails, even
        # at one spot, the drive is what changed (#876).
        if not hist.disc_read_completed and spots:
            repeats = [
                pf for pf in hist.prior_failures
                if any(_same_spot(here, there) for here in spots for there in pf.spots)
            ]
            if repeats:
                drives = {pf.serial for pf in repeats if pf.serial}
                if _is_true_by_id_serial(serial):
                    drives.add(serial)
                where = spots[0]
                receipt = (
                    f"{where[0]} failed near {_gb(where[1])} again — the same spot as "
                    f"{len(repeats)} earlier attempt(s), on {len(drives) or 1} drive(s)"
                )
                if len(drives) >= 2:
                    return RipFailureVerdict(
                        classification="disc_defect",
                        failure_kind="precondition",
                        receipts=[receipt],
                        remedy=FIXED_LOCATION_REMEDY,
                    )
                return RipFailureVerdict(
                    classification="suspect_disc",
                    failure_kind="transient",
                    receipts=[receipt],
                    remedy=SAME_SPOT_ONE_DRIVE_REMEDY,
                )

        # --- drive wedged: a hard error, then untouched streams failing at byte 0 ---
        if first_spot and len(later_at_zero) >= WEDGE_MIN_STREAMS_AT_BYTE0:
            return RipFailureVerdict(
                classification="drive_fault",
                failure_kind="transient",
                receipts=[
                    f"after a read error in {first_spot[0]} near {_gb(first_spot[1])}, "
                    f"{len(later_at_zero)} other streams failed at their first byte — "
                    "the drive stopped answering"
                ],
                remedy=DRIVE_WEDGED_REMEDY,
            )

        # --- drive fault: scattered errors AND this drive failing other discs ---
        scattered = (
            len(offsets) >= SCATTER_MIN_UNIQUE_OFFSETS
            and same_offset_max <= SCATTER_MAX_REPEAT
        )
        if hist.other_discs >= DRIVE_CORROBORATION_MIN_DISCS and (scattered or not errors):
            receipts = [
                f"this drive has failed {hist.other_discs} other disc(s) with read "
                "errors or stalls",
            ]
            if scattered:
                receipts.append(
                    f"errors scattered across {len(offsets)} different offsets with no repetition"
                )
            return RipFailureVerdict(
                classification="drive_fault",
                failure_kind="transient",
                receipts=receipts,
                remedy=DRIVE_FAULT_REMEDY,
            )

        return RipFailureVerdict(classification="indeterminate", failure_kind=None)
    except Exception as exc:
        logger.warning("rip_failure_analysis: analysis failed for job %s: %s",
                       getattr(job, "id", None), exc)
        return RipFailureVerdict(classification="indeterminate", failure_kind=None)
