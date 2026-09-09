"""Rip-failure classification: disc defect vs drive fault, with receipts (#731).

Sense codes alone cannot split the two — HARDWARE ERROR 44/07 appeared in
BOTH classes during the 2026-09 incidents. The PATTERN decides:

- A pressed **disc defect** fails at a fixed location: many errors at one
  identical byte offset, and/or a run of consecutive streams each failing
  at offset 0 (everything laid out beyond the dead zone). Specimen:
  RE Apocalypse UHD — 336 errors at offset 3164209152 on 00357.m2ts, then
  streams 00356–00367 at offset 0, ×14 retries each.
- A **drive/link fault** scatters: errors at many different offsets with no
  repetition, and the same drive failing multiple DIFFERENT discs.
- **Cross-history is definitive in both directions**: the same disc failing
  on two different drive serials condemns the disc (that exact experiment
  ran live on 2026-09-07: the Pioneer errored fast, the ASUS silently
  ground into a stall — same disc, same region); one drive failing several
  different discs condemns the drive.

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
_STREAM_NUM_RE = re.compile(r"(\d+)\.m2ts$", re.IGNORECASE)

# Thresholds — deliberately conservative; below them the verdict is
# indeterminate and nothing changes for the user.
SAME_OFFSET_DEFECT_THRESHOLD = 20     # errors at ONE identical offset
OFFSET0_RUN_DEFECT_THRESHOLD = 3      # consecutive streams failing at byte 0
SCATTER_MIN_UNIQUE_OFFSETS = 8        # scattered-looking error spread…
SCATTER_MAX_REPEAT = 2                # …with no offset repeating more than this
DRIVE_CORROBORATION_MIN_DISCS = 2     # other distinct discs failed on this drive
Classification = Literal["disc_defect", "drive_fault", "indeterminate"]


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
        label = "disc defect" if self.classification == "disc_defect" else "drive fault"
        parts = [error_reason.rstrip(), f"Diagnosis: {label} ({'; '.join(self.receipts)})."]
        if self.remedy:
            parts.append(self.remedy)
        return " ".join(parts)


def _parse_read_errors(log_text: str) -> list[tuple[str, str, int]]:
    return [
        (m.group("err"), m.group("path"), int(m.group("offset")))
        for m in _READ_ERROR_RE.finditer(log_text)
    ]


def _max_consecutive_offset0_run(errors: list[tuple[str, str, int]]) -> tuple[int, list[int]]:
    """Longest run of CONSECUTIVE stream numbers that each errored at offset 0."""
    nums = set()
    for _err, path, offset in errors:
        if offset != 0:
            continue
        m = _STREAM_NUM_RE.search(path)
        if m:
            nums.add(int(m.group(1)))
    if not nums:
        return 0, []
    ordered = sorted(nums)
    best_len, best_start, run_len, run_start = 1, ordered[0], 1, ordered[0]
    for prev, cur in zip(ordered, ordered[1:]):
        if cur == prev + 1:
            run_len += 1
        else:
            run_len, run_start = 1, cur
        if run_len > best_len:
            best_len, best_start = run_len, run_start
    return best_len, list(range(best_start, best_start + best_len))


def _history_corroboration(job: Any, db: Any) -> tuple[int, int]:
    """(distinct OTHER drive serials this disc failed on,
        distinct OTHER discs this drive failed) — from recorded rip failures."""
    other_serials = 0
    other_discs = 0
    try:
        from api import models

        disc_id = getattr(job, "disc_id", None)
        serial = getattr(job, "drive_by_id_serial", None)
        if disc_id:
            rows = (
                db.query(models.Job.drive_by_id_serial)
                .filter(
                    models.Job.disc_id == disc_id,
                    models.Job.rip_state == "failed",
                    models.Job.id != job.id,
                    models.Job.drive_by_id_serial.isnot(None),
                )
                .distinct()
                .all()
            )
            # Only TRUE by-id serials count as distinct drives. Fallback
            # identities embed their source ("sysfs:VENDOR:MODEL:srN",
            # "unknown:srN") — the same physical drive recorded under two
            # representations must not fabricate a cross-drive condemnation
            # (seen validating against prod: one Pioneer counted twice).
            other_serials = len({
                r[0] for r in rows
                if r[0] and r[0] != serial and ":" not in r[0]
            })
        if serial:
            rows = (
                db.query(models.Job.disc_id)
                .filter(
                    models.Job.drive_by_id_serial == serial,
                    models.Job.rip_state == "failed",
                    models.Job.disc_id != disc_id,
                    models.Job.disc_id.isnot(None),
                )
                .distinct()
                .all()
            )
            other_discs = len({r[0] for r in rows if r[0]})
    except Exception as exc:
        logger.warning("rip_failure_analysis: history lookup failed: %s", exc)
    return other_serials, other_discs


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
        text = log_text if log_text is not None else _read_progress_log(job)
        errors = _parse_read_errors(text)
        offsets = Counter(offset for _e, _p, offset in errors)
        same_offset_max = max(offsets.values()) if offsets else 0
        worst_offset = max(offsets, key=offsets.get) if offsets else None
        run_len, run_streams = _max_consecutive_offset0_run(errors)
        other_serials, other_discs = _history_corroboration(job, db)

        receipts: list[str] = []

        # --- disc defect: fixed-location failure or cross-drive history ---
        is_defect = False
        if same_offset_max >= SAME_OFFSET_DEFECT_THRESHOLD:
            is_defect = True
            receipts.append(
                f"{same_offset_max} read errors at the identical offset {worst_offset}"
            )
        if run_len >= OFFSET0_RUN_DEFECT_THRESHOLD:
            is_defect = True
            receipts.append(
                f"{run_len} consecutive streams ({run_streams[0]:05d}–{run_streams[-1]:05d}) "
                "each unreadable from byte 0"
            )
        if other_serials >= 1:
            is_defect = True
            receipts.append(
                f"this disc also failed on {other_serials} other drive(s)"
            )
        if is_defect:
            return RipFailureVerdict(
                classification="disc_defect",
                failure_kind="precondition",
                receipts=receipts,
                remedy=(
                    "The failure reproduces at a fixed location on the disc, so a retry "
                    "cannot succeed — clean the disc and, if it fails again, exchange it."
                ),
            )

        # --- drive fault: scattered errors AND this drive failing other discs ---
        scattered = (
            len(offsets) >= SCATTER_MIN_UNIQUE_OFFSETS
            and same_offset_max <= SCATTER_MAX_REPEAT
        )
        if other_discs >= DRIVE_CORROBORATION_MIN_DISCS and (scattered or not errors):
            receipts = [
                f"this drive has failed {other_discs} other disc(s)",
            ]
            if scattered:
                receipts.append(
                    f"errors scattered across {len(offsets)} different offsets with no repetition"
                )
            return RipFailureVerdict(
                classification="drive_fault",
                failure_kind="transient",
                receipts=receipts,
                remedy=(
                    "This looks like the drive, not the disc — check its power supply and "
                    "USB cable, or power-cycle the drive, then retry."
                ),
            )

        return RipFailureVerdict(classification="indeterminate", failure_kind=None)
    except Exception as exc:
        logger.warning("rip_failure_analysis: analysis failed for job %s: %s",
                       getattr(job, "id", None), exc)
        return RipFailureVerdict(classification="indeterminate", failure_kind=None)
