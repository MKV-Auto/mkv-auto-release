"""
Split preview vs metadata/detection for raw MKV titles (DiscDB miss path).

preview_raw_titles runs HLS preview + mkv_size; detect_raw_titles runs ffprobe metadata_scan
and optional padding/junk detection. Imported from workers.tasks with lazy inner imports to
avoid circular imports.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from api import crud
from core import settings
from core.bitrate_plausibility import evaluate_low_bitrate_decoy
from core.duration_sanity import evaluate_duration_short
from core.ffmpeg_detection import FFMPEG_DETECTION_CONFIDENCE_THRESHOLD, detect_padding_junk, is_detection_disabled
from core.ffprobe_metadata import is_metadata_scan_disabled, scan_file_metadata
from core.job_paths import JobPaths

log = logging.getLogger(__name__)


def run_preview_raw_titles_phase(
    task_self: Any,
    job_id: str,
    title_keys: list[str] | None,
    rel_path_overrides: dict[str, str] | None,
) -> list[str]:
    """
    HLS preview + mkv_size only. Returns title IDs that had a raw file and DiscTitle row
    (eligible for a follow-up detect_raw_titles task).
    """
    from api import models as db_models
    from workers.tasks import DATA_ROOT, _safe_track_folder, db_session, ffmpeg_semaphore

    eligible: list[str] = []
    eligible_lock = threading.Lock()
    log.info(
        "preview_raw_titles phase started",
        extra={"job_id": job_id, "title_keys": title_keys},
    )
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            return []
        paths = JobPaths.from_job(job, out_dir=str(DATA_ROOT))
        paths.ensure_layout()
        input_root = paths.raw
        if not input_root.exists():
            task_self.add_log(job, db, "preview_raw_titles: raw directory not found")
            return []
        ripped = getattr(job, "ripped_files", None) or (job.disc_payload or {}).get("ripped_files") or {}
        path_map = {**ripped, **(rel_path_overrides or {})}
        keys = title_keys if title_keys is not None else list(path_map.keys())
        if not keys:
            task_self.add_log(job, db, "preview_raw_titles: no title keys")
            return []
        cfg = settings.get_preview_dict()
        duration_sec = max(1, int(cfg.get("duration_seconds", 120)))
        max_parallel = max(1, int(cfg.get("max_parallel", os.cpu_count() or 1)))
        status_lock = threading.Lock()

        def do_preview(title_id: str) -> None:
            nonlocal job
            rel = path_map.get(title_id) or (job.disc_payload or {}).get("title_filename_map", {}).get(title_id)
            if not rel:
                with status_lock:
                    with db_session() as th_db:
                        j = crud.get_job(th_db, job_id)
                        if not j:
                            return
                        payload = deepcopy(j.disc_payload or {})
                        backlog = list(payload.get("preview_backlog") or [])
                        backlog.append({"title_id": title_id, "rel_path": None})
                        payload["preview_backlog"] = backlog
                        task_self.set_status(j, th_db, disc_payload=payload)
                return
            cand = (input_root / rel).resolve()
            if not cand.exists():
                cand = (paths.root / rel).resolve()
            if not cand.exists():
                log.warning("preview_raw_titles: file not found for %s: %s", title_id, rel)
                return
            with db_session() as th_db:
                j = crud.get_job(th_db, job_id)
                tr = th_db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == title_id).first() if j else None
            if not tr:
                with status_lock:
                    with db_session() as th_db:
                        j = crud.get_job(th_db, job_id)
                        if not j:
                            return
                        payload = deepcopy(j.disc_payload or {})
                        backlog = list(payload.get("preview_backlog") or [])
                        backlog.append({"title_id": title_id, "rel_path": rel})
                        payload["preview_backlog"] = backlog
                        task_self.set_status(j, th_db, disc_payload=payload)
                return
            out_dir = paths.previews / _safe_track_folder(title_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            manifest = out_dir / "preview.m3u8"
            seg_pat = out_dir / "segment_%03d.ts"
            cmd = [
                "ffmpeg", "-y", "-ss", "0", "-t", str(duration_sec), "-i", str(cand),
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
                "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                "-hls_segment_filename", str(seg_pat), str(manifest),
            ]
            try:
                with ffmpeg_semaphore(max_parallel):
                    subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            except (TimeoutError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                log.warning("preview_raw_titles: preview ffmpeg failed for %s: %s", title_id, e)
            prev_status = "completed" if (manifest.exists() if manifest else False) else "failed"
            prev_err = None if prev_status == "completed" else "Failed to generate preview"
            with status_lock:
                with db_session() as th_db:
                    j = crud.get_job(th_db, job_id)
                    if not j:
                        return
                    payload = deepcopy(j.disc_payload or {})
                    prevs = payload.get("previews") or {}
                    tracks = (prevs.get("tracks") or {}).copy() if isinstance(prevs.get("tracks"), dict) else {}
                    tracks[title_id] = {
                        "status": prev_status,
                        "manifest": f"previews/{_safe_track_folder(title_id)}/preview.m3u8" if prev_status == "completed" else None,
                        "error": prev_err,
                        "title_id": title_id,
                        "source": rel,
                    }
                    payload["previews"] = {"status": prevs.get("status", "running"), "tracks": tracks, "updated_at": datetime.utcnow().isoformat()}
                    task_self.set_status(j, th_db, disc_payload=payload)
            disk_sz = None
            if cand.exists():
                try:
                    disk_sz = cand.stat().st_size
                except OSError:
                    disk_sz = None
            stored = getattr(tr, "mkv_size", None)
            if disk_sz is not None:
                sz = disk_sz if stored is None else max(int(stored), int(disk_sz))
            else:
                sz = stored
            if sz is not None:
                with status_lock:
                    with db_session() as th_db:
                        tit = th_db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == title_id).first()
                        if tit:
                            tit.mkv_size = sz
                            th_db.flush()
                            th_db.commit()
            with eligible_lock:
                eligible.append(title_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
            list(pool.map(do_preview, keys))
    log.info("preview_raw_titles phase finished", extra={"job_id": job_id, "eligible": len(eligible)})
    return eligible


def run_detect_raw_titles_phase(
    task_self: Any,
    job_id: str,
    title_keys: list[str] | None,
    rel_path_overrides: dict[str, str] | None,
) -> None:
    """ffprobe metadata_scan + padding/junk detection for raw MKVs."""
    from api import models as db_models
    from workers.tasks import DATA_ROOT, db_session

    log.info(
        "detect_raw_titles phase started",
        extra={"job_id": job_id, "title_keys": title_keys},
    )
    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            return
        paths = JobPaths.from_job(job, out_dir=str(DATA_ROOT))
        paths.ensure_layout()
        input_root = paths.raw
        if not input_root.exists():
            task_self.add_log(job, db, "detect_raw_titles: raw directory not found")
            return
        ripped = getattr(job, "ripped_files", None) or (job.disc_payload or {}).get("ripped_files") or {}
        path_map = {**ripped, **(rel_path_overrides or {})}
        keys = title_keys if title_keys is not None else list(path_map.keys())
        if not keys:
            task_self.add_log(job, db, "detect_raw_titles: no title keys")
            return
        cfg = settings.get_preview_dict()
        max_parallel = max(1, int(cfg.get("max_parallel", os.cpu_count() or 1)))
        detection_enabled = not is_detection_disabled()
        metadata_scan_enabled = not is_metadata_scan_disabled()
        if not detection_enabled and not metadata_scan_enabled:
            log.info("detect_raw_titles: metadata and detection disabled, nothing to do")
            return
        status_lock = threading.Lock()

        def do_detect(title_id: str) -> None:
            nonlocal job
            rel = path_map.get(title_id) or (job.disc_payload or {}).get("title_filename_map", {}).get(title_id)
            if not rel:
                return
            cand = (input_root / rel).resolve()
            if not cand.exists():
                cand = (paths.root / rel).resolve()
            if not cand.exists():
                log.warning("detect_raw_titles: file not found for %s: %s", title_id, rel)
                return
            with db_session() as th_db:
                j = crud.get_job(th_db, job_id)
                tr = th_db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == title_id).first() if j else None
            if not tr:
                return
            sz = None
            if cand.exists():
                try:
                    sz = cand.stat().st_size
                except OSError:
                    sz = None
            if sz is None:
                sz = getattr(tr, "mkv_size", None)
            resolution = None
            if metadata_scan_enabled:
                try:
                    meta_res = scan_file_metadata(cand)
                    if meta_res is not None:
                        meta_dict = meta_res.to_dict()
                        video_hints = meta_dict.get("video_hints") or {}
                        width = video_hints.get("width")
                        height = video_hints.get("height")
                        if width is not None and height is not None:
                            resolution = (int(width), int(height))
                        # Issue #374: post-ffprobe obfuscation checks.
                        # Two complementary signals:
                        #   - duration_short: declared duration is much
                        #     shorter than what ffprobe reads — catches
                        #     the case where MakeMKV under-reports a
                        #     long m2ts (when the rip preserved the full
                        #     content).
                        #   - low_bitrate_decoy: bitrate is implausibly
                        #     low for the resolution (e.g. 4K HEVC @ 1
                        #     Mbps) — catches the post-rip remnant where
                        #     MakeMKV trimmed the m2ts but the resulting
                        #     MKV is still obviously not real picture
                        #     content (Midway 00001.mpls).
                        # The two are independent — a title can fire one
                        # without the other.
                        declared_duration = getattr(tr, "duration", None)
                        fmt = meta_dict.get("format") or {}
                        actual_duration = fmt.get("duration")
                        actual_bit_rate = fmt.get("bit_rate")
                        duration_reason = evaluate_duration_short(
                            declared=declared_duration,
                            actual=actual_duration,
                        )
                        bitrate_reason = evaluate_low_bitrate_decoy(
                            bit_rate=actual_bit_rate,
                            width=width,
                            height=height,
                        )
                        post_ffprobe_reason = duration_reason or bitrate_reason
                        with status_lock:
                            with db_session() as th_db:
                                j = crud.get_job(th_db, job_id)
                                tit = th_db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == title_id).first() if j else None
                                if tit:
                                    tit.metadata_scan = meta_dict
                                    if post_ffprobe_reason is not None:
                                        # Don't downgrade stronger reasons —
                                        # segment_set_sibling / path_a_decoy
                                        # convey relational context that
                                        # post-ffprobe reasons can't. Set only
                                        # if no other reason wins.
                                        existing_reason = getattr(tit, "obfuscation_reason", None)
                                        if not existing_reason:
                                            tit.obfuscation_reason = post_ffprobe_reason
                                        tit.obfuscation_flag = True
                                        if duration_reason is not None:
                                            try:
                                                tit.duration = int(round(float(actual_duration)))
                                            except (TypeError, ValueError):
                                                pass
                                    th_db.flush()
                                if not j:
                                    return
                                payload = deepcopy(j.disc_payload or {})
                                meta_results = (payload.get("metadata_results") or {}).copy()
                                meta_results[title_id] = meta_dict
                                payload["metadata_results"] = meta_results
                                task_self.set_status(j, th_db, disc_payload=payload)
                except Exception as meta_ex:
                    log.warning("detect_raw_titles: metadata scan failed for title %s: %s", title_id, meta_ex)
            if detection_enabled:
                try:
                    res = detect_padding_junk(
                        cand,
                        duration=getattr(tr, "duration", None),
                        size_bytes=sz,
                        resolution=resolution,
                    )
                    with status_lock:
                        with db_session() as th_db:
                            j = crud.get_job(th_db, job_id)
                            tit = th_db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == title_id).first() if j else None
                            if tit:
                                tit.detection_flags = res.to_flags_dict()
                                tit.detection_confidence = res.confidence
                                tit.detection_warning = res.confidence >= FFMPEG_DETECTION_CONFIDENCE_THRESHOLD
                                if tit.detection_warning and (tit.type is None or tit.type == ""):
                                    # FFmpeg-based padding/decoy detection is
                                    # automated; source='auto' keeps user_type
                                    # NULL so the chip system surfaces the
                                    # row for review instead of treating it as
                                    # a user-confirmed ignore.
                                    from api.crud import set_title_type
                                    set_title_type(tit, "ignore", source="auto")
                                elif (
                                    not tit.detection_warning
                                    and (tit.auto_type or "").strip().lower() == "ignore"
                                    and tit.user_type is None
                                    and getattr(tit, "obfuscation_reason", None) is None
                                    and getattr(tit, "subsumed_by_title_id", None) is None
                                    and tit.active is not False
                                ):
                                    # #517 — inverse path: a clean re-run heals a
                                    # stale detection-sourced auto-ignore. Guards
                                    # ensure we never touch user decisions,
                                    # obfuscation-driven ignores, m2ts subsumption,
                                    # or demoted duplicate secondaries — those have
                                    # their own markers and owners.
                                    from api.crud import set_title_type
                                    set_title_type(tit, None, source="auto")
                                th_db.flush()
                            if not j:
                                return
                            payload = deepcopy(j.disc_payload or {})
                            det = (payload.get("detection_results") or {}).copy() if isinstance(payload.get("detection_results"), dict) else {}
                            det[title_id] = {
                                "confidence": res.confidence,
                                "warning": res.confidence >= FFMPEG_DETECTION_CONFIDENCE_THRESHOLD,
                                "warnings": res.warnings,
                                "flags": res.to_flags_dict(),
                            }
                            payload["detection_results"] = det
                            task_self.set_status(j, th_db, disc_payload=payload)
                except Exception as det_ex:
                    log.warning("detect_raw_titles: detection failed for title %s: %s", title_id, det_ex)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
            list(pool.map(do_detect, keys))
    log.info("detect_raw_titles phase finished", extra={"job_id": job_id})
