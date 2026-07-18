"""
Phase A+B of rip verification: quiescence (stat stability) then ffprobe readiness.

Hashing and mkv_size sync (Phase C) must run only after this module's gates pass.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger(__name__)

# Reuse interval / max wait from rip incomplete-file wait loops
_RIP_INTERVAL = "MKVAUTO_RIP_SHORT_INTERVAL_SECONDS"
_RIP_MAX_WAIT = "MKVAUTO_RIP_SHORT_WAIT_SECONDS"
_RIP_PROBE_PARALLEL = "MKVAUTO_RIP_PROBE_MAX_PARALLEL"
# Seconds with no file size increase before treating raw MKVs as quiescent (after copy subprocess exited).
_RIP_QUIESCENCE_STABLE = "MKVAUTO_RIP_QUIESCENCE_STABLE_SECONDS"
_RIP_SKIP_FFPROBE = "MKVAUTO_RIP_VERIFY_SKIP_FFPROBE"


def mkv_sizes_by_relpath(workdir: Path) -> dict[str, int]:
    """All ``*.mkv`` under ``workdir`` → relative POSIX path → size in bytes."""
    root = workdir.resolve()
    out: dict[str, int] = {}
    for p in workdir.rglob("*.mkv"):
        if not p.is_file():
            continue
        try:
            rel = str(p.resolve().relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = str(p.relative_to(workdir)).replace("\\", "/")
        try:
            out[rel] = p.stat().st_size
        except OSError:
            continue
    return out


def wait_ripped_mkvs_quiescent(
    rip_root: Path,
    rel_paths: Sequence[str],
    *,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """
    Block until every listed relative MKV exists, is non-empty, and file sizes do not
    increase for ``MKVAUTO_RIP_QUIESCENCE_STABLE_SECONDS`` (default **15**).

    (Incomplete-rip waiting still uses ``MKVAUTO_RIP_SHORT_*`` in rip_verification_impl.)
    """
    rels = sorted({str(r).replace("\\", "/") for r in rel_paths if r})
    if not rels:
        return

    wait_interval_sec = max(1, int(os.getenv(_RIP_INTERVAL, "15")))
    stable_no_growth_sec = max(1, int(os.getenv(_RIP_QUIESCENCE_STABLE, "15")))
    wait_max_total_sec = int(os.getenv(_RIP_MAX_WAIT, "0"))

    root = rip_root.resolve()
    start_time = time.monotonic()
    prev_sizes: dict[str, int] | None = None
    stable_since: float | None = None

    def _snapshot() -> dict[str, int]:
        snap: dict[str, int] = {}
        for rel in rels:
            full = (root / rel).resolve()
            if full.is_file():
                try:
                    snap[rel] = full.stat().st_size
                except OSError:
                    snap[rel] = -1
            else:
                snap[rel] = -1
        return snap

    while True:
        time.sleep(wait_interval_sec)
        cur = _snapshot()
        all_ok = all(cur.get(r, -1) > 0 for r in rels)
        if not all_ok:
            prev_sizes = cur
            stable_since = None
            if wait_max_total_sec > 0 and (time.monotonic() - start_time) >= wait_max_total_sec:
                raise RuntimeError(
                    f"Timeout waiting for raw MKV files to exist and be non-empty under {rip_root}"
                )
            continue

        if prev_sizes is None:
            prev_sizes = cur
            stable_since = time.monotonic()
            continue

        grew = any(cur.get(r, -1) > prev_sizes.get(r, -1) for r in rels)
        if grew:
            if log_fn:
                log_fn("Raw MKV(s) still growing; waiting for quiescence before verify.")
            prev_sizes = cur
            stable_since = time.monotonic()
            continue

        prev_sizes = cur
        if stable_since is None:
            stable_since = time.monotonic()
        if (time.monotonic() - stable_since) >= stable_no_growth_sec:
            return

        if wait_max_total_sec > 0 and (time.monotonic() - start_time) >= wait_max_total_sec:
            raise RuntimeError(
                f"Timeout waiting for raw MKV size quiescence under {rip_root} "
                f"(no growth for {stable_no_growth_sec}s not satisfied within max wait)"
            )


def probe_raw_mkv_ready(path: Path) -> tuple[bool, str]:
    """
    Return (True, "") if ffprobe sees a plausible complete MKV (video stream + positive duration).

    Uses the same ffprobe stack as metadata scan; failures often indicate truncated writes.
    """
    from core.ffprobe_metadata import scan_file_metadata

    p = Path(path)
    if not p.is_file():
        return False, f"not a file: {p}"

    res = scan_file_metadata(p)
    if res is None:
        return False, "ffprobe metadata returned no result"
    if res.warning:
        return False, res.warning
    vcount = (res.stream_counts or {}).get("video") or 0
    if vcount < 1:
        return False, "no video stream in container"
    dur = res.format.get("duration")
    try:
        dur_f = float(dur) if dur is not None else 0.0
    except (TypeError, ValueError):
        dur_f = 0.0
    if dur_f <= 0.0:
        return False, "duration missing or non-positive"
    return True, ""


def probe_ripped_mkvs_ready(
    rip_root: Path,
    title_id_to_rel: dict[str, str],
    *,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """
    Run ffprobe readiness on each ripped path. Parallelism from MKVAUTO_RIP_PROBE_MAX_PARALLEL (default 4).

    Set ``MKVAUTO_RIP_VERIFY_SKIP_FFPROBE=1`` to skip (tests / emergency only).
    """
    if not title_id_to_rel:
        return True, ""

    if os.getenv(_RIP_SKIP_FFPROBE, "").strip().lower() in ("1", "true", "yes", "on"):
        log.warning("rip_raw_ready: skipping ffprobe gate (%s=1)", _RIP_SKIP_FFPROBE)
        return True, ""

    max_workers = max(1, int(os.getenv(_RIP_PROBE_PARALLEL, "4")))
    root = rip_root.resolve()
    items = list(title_id_to_rel.items())

    def _one(item: tuple[str, str]) -> tuple[str, str, bool, str]:
        tid, rel = item
        full = (root / rel).resolve()
        ok, err = probe_raw_mkv_ready(full)
        return tid, rel, ok, err

    if len(items) == 1:
        tid, rel, ok, err = _one(items[0])
        if not ok:
            return False, f"title {tid} ({rel}): {err}"
        return True, ""

    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as ex:
        futures = {ex.submit(_one, it): it for it in items}
        for fut in as_completed(futures):
            tid, rel, ok, err = fut.result()
            if not ok:
                errors.append(f"title {tid} ({rel}): {err}")
                if log_fn:
                    log_fn(f"ffprobe readiness failed: title {tid} ({rel}): {err}")

    if errors:
        return False, "; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else "")
    return True, ""
