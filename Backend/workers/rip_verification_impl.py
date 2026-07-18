# Post-copy verification: hashing, payload merge, rip-verification-complete callback.
# Imported after workers.tasks is fully loaded to avoid circular imports.

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from workers.rip_raw_ready import mkv_sizes_by_relpath, probe_ripped_mkvs_ready, wait_ripped_mkvs_quiescent

log = logging.getLogger(__name__)


def run_rip_verification_for_job(job_task: Any, job_id: str) -> None:
    from api import models as db_models

    from workers.tasks import (
        RIP_PROGRESS_COPY_END,
        JobPaths,
        _backfill_preview_title_ids,
        _build_title_id_maps,
        _build_title_output_map,
        _ensure_previews_map,
        _normalize_ripped_files_to_title_ids,
        _post_rip_progress,
        _post_rip_verification_complete_callback,
        _sync_disc_title_mkv_sizes_from_ripped,
        _update_title_file_paths,
        crud,
        db_session,
        is_disc_read_error,
        makemkv_mkv_rel_path_sort_key,
    )

    func_logger = logging.getLogger("workers.rip_verification")

    with db_session() as db:
        job = crud.get_job(db, job_id)
        if not job:
            func_logger.warning("rip_verification: job %s not found", job_id)
            return
        if getattr(job, "rip_state", None) in ("completed", "skipped"):
            func_logger.info("rip_verification skip job_id=%s rip already done", job_id)
            return
        if getattr(job, "rip_state", None) != "running":
            func_logger.warning(
                "rip_verification skip job_id=%s rip_state=%s", job_id, getattr(job, "rip_state", None)
            )
            return
        if getattr(job, "rip_phase", None) != "verification":
            func_logger.warning(
                "rip_verification skip job_id=%s rip_phase=%s (expected verification)",
                job_id,
                getattr(job, "rip_phase", None),
            )
            return

        if job.disc_id:
            job.disc = db.query(db_models.Disc).filter(db_models.Disc.id == job.disc_id).first()

        paths = JobPaths.from_job(job, None)
        rip_workdir = paths.raw
        job_root = paths.root

        title_id_maps = _build_title_id_maps(job, job.disc_payload or {})
        id_to_title = title_id_maps.get("id_to_title", {})
        title_keys: list[str] = []
        if id_to_title:
            def _title_sort_key(t: Any) -> tuple[int, int]:
                index_val = getattr(t, "index", None)
                order_val = getattr(t, "order_index", None)
                index_key = index_val if isinstance(index_val, int) else 9999
                order_key = order_val if isinstance(order_val, int) else 9999
                return (index_key, order_key)

            titles_sorted = sorted(id_to_title.values(), key=_title_sort_key)
            title_keys = [str(t.id) for t in titles_sorted if getattr(t, "id", None)]

        needs_label = bool((job.disc_payload or {}).get("label_required")) and not bool(
            (job.disc_payload or {}).get("label_ready")
        )
        disc_id = getattr(job.disc, "id", None) if job.disc else None

        try:
            if needs_label:
                _run_miss_label_verification(
                    job_task,
                    job,
                    db,
                    paths,
                    rip_workdir,
                    job_root,
                    title_keys,
                    disc_id,
                )
            else:
                _run_hit_verification(
                    job_task,
                    job,
                    db,
                    paths,
                    rip_workdir,
                    title_keys,
                    disc_id,
                )
        except Exception as exc:
            func_logger.exception("rip_verification failed job_id=%s: %s", job_id, exc)
            err_type = "disc_read" if is_disc_read_error(str(exc)) else None
            _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
            _post_rip_verification_complete_callback(
                str(job.id), success=False, error_reason=str(exc), error_type=err_type
            )


def _run_miss_label_verification(
    job_task: Any,
    job: Any,
    db: Any,
    paths: Any,
    rip_workdir: Any,
    job_root: Any,
    title_keys: list[str],
    disc_id: str | None,
) -> None:
    from api import models as db_models

    from workers.tasks import (
        RIP_PROGRESS_COPY_END,
        _backfill_preview_title_ids,
        _build_title_id_maps,
        _build_title_output_map,
        _ensure_previews_map,
        _normalize_ripped_files_to_title_ids,
        _post_rip_progress,
        _post_rip_verification_complete_callback,
        _sync_disc_title_mkv_sizes_from_ripped,
        _update_title_file_paths,
        makemkv_mkv_rel_path_sort_key,
    )

    payload = {**(job.disc_payload or {}), "label_required": True, "label_ready": False}

    _post_rip_progress(str(job.id), rip_phase="verification")
    last_verification_progress = [RIP_PROGRESS_COPY_END]
    VERIFICATION_THROTTLE_PCT = 3

    def _verification_progress_cb(verification_pct: int, _filename: str) -> None:
        combined = RIP_PROGRESS_COPY_END + int(verification_pct * (100 - RIP_PROGRESS_COPY_END) / 100)
        combined = min(100, max(last_verification_progress[0], combined))
        if combined >= 100 or (combined - last_verification_progress[0]) >= VERIFICATION_THROTTLE_PCT:
            last_verification_progress[0] = combined
            _post_rip_progress(str(job.id), rip_progress=combined)

    title_filename_map: dict[str, Any] = dict((job.disc_payload or {}).get("title_filename_map") or {})
    filename_to_title_id: dict[str, str] = {}
    for tid, fn in title_filename_map.items():
        if fn and tid:
            filename_to_title_id[str(fn)] = str(tid)

    try:
        ripped_src0, _ = job_task.gather_final_outputs(
            rip_workdir, disc_id=disc_id, db=db, skip_hashes=True
        )
    except (ValueError, FileNotFoundError):
        ripped_src0 = {}

    ripped_files = _normalize_ripped_files_to_title_ids(
        db, ripped_src0, disc_id=disc_id, filename_to_title_id=filename_to_title_id
    )
    final_hashes: dict[str, str] = {}

    expected_count_miss = len(title_keys) if title_keys else 0
    wait_interval_sec = int(os.getenv("MKVAUTO_RIP_SHORT_INTERVAL_SECONDS", "15"))
    stable_no_growth_sec = int(os.getenv("MKVAUTO_RIP_SHORT_STABLE_SECONDS", "600"))
    wait_max_total_sec = int(os.getenv("MKVAUTO_RIP_SHORT_WAIT_SECONDS", "0"))
    count_ok_miss = True
    if expected_count_miss > 0 and len(ripped_files) < expected_count_miss and stable_no_growth_sec > 0:
        job_task.add_log(
            job,
            db,
            f"Incomplete rip: {len(ripped_files)}/{expected_count_miss} titles on disk; "
            f"waiting for writes to finish (will fail after {stable_no_growth_sec}s with no file size increase).",
        )

        start_time = time.monotonic()
        last_growth_time = time.monotonic()
        prev_sizes = mkv_sizes_by_relpath(rip_workdir)
        count_ok_miss = False
        while True:
            time.sleep(min(wait_interval_sec, max(1, stable_no_growth_sec - (time.monotonic() - last_growth_time))))
            try:
                ripped_source_new, _ = job_task.gather_final_outputs(
                    rip_workdir, disc_id=disc_id, db=db, skip_hashes=True
                )
                ripped_normalized_new = _normalize_ripped_files_to_title_ids(
                    db, ripped_source_new, disc_id=disc_id, filename_to_title_id=filename_to_title_id
                )
                if len(ripped_normalized_new) >= expected_count_miss:
                    ripped_files = ripped_normalized_new
                    count_ok_miss = True
                    break
            except (ValueError, FileNotFoundError):
                pass
            cur_sizes = mkv_sizes_by_relpath(rip_workdir)
            any_growth = any(cur_sizes.get(n, 0) > prev_sizes.get(n, 0) for n in prev_sizes) or len(
                cur_sizes
            ) > len(prev_sizes)
            if any_growth:
                last_growth_time = time.monotonic()
                job_task.add_log(job, db, "Files still growing, continuing to wait.")
            prev_sizes = cur_sizes
            if not count_ok_miss and (time.monotonic() - last_growth_time) >= stable_no_growth_sec:
                break
            if wait_max_total_sec > 0 and (time.monotonic() - start_time) >= wait_max_total_sec:
                break
        if not count_ok_miss:
            try:
                ripped_source_final, _ = job_task.gather_final_outputs(
                    rip_workdir, disc_id=disc_id, db=db, skip_hashes=True
                )
                ripped_final = _normalize_ripped_files_to_title_ids(
                    db, ripped_source_final, disc_id=disc_id, filename_to_title_id=filename_to_title_id
                )
                if len(ripped_final) >= expected_count_miss:
                    ripped_files = ripped_final
                    count_ok_miss = True
            except (ValueError, FileNotFoundError):
                pass
        if not count_ok_miss:
            msg = (
                f"Incomplete rip: only {len(ripped_files)}/{expected_count_miss} titles on disk "
                f"after {stable_no_growth_sec}s with no file size increase; MakeMKV may have exited early or gone defunct."
            )
            job_task.add_log(job, db, msg)
            _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
            _post_rip_verification_complete_callback(str(job.id), success=False, error_reason=msg)
            return

    if not ripped_files:
        msg = "Rip verification: no MKV outputs found under raw/"
        job_task.add_log(job, db, msg)
        _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
        _post_rip_verification_complete_callback(str(job.id), success=False, error_reason=msg)
        return

    try:
        wait_ripped_mkvs_quiescent(
            rip_workdir,
            list(ripped_files.values()),
            log_fn=lambda m: job_task.add_log(job, db, m),
        )
    except RuntimeError as exc:
        job_task.add_log(job, db, f"Rip verification: quiescence wait failed: {exc}")
        _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
        _post_rip_verification_complete_callback(str(job.id), success=False, error_reason=str(exc))
        return

    ok_probe, probe_err = probe_ripped_mkvs_ready(
        rip_workdir,
        ripped_files,
        log_fn=lambda m: job_task.add_log(job, db, m),
    )
    if not ok_probe:
        job_task.add_log(job, db, f"Rip verification: ffprobe readiness failed: {probe_err}")
        _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
        _post_rip_verification_complete_callback(
            str(job.id), success=False, error_reason=f"ffprobe readiness failed: {probe_err}"
        )
        return

    ripped_files, final_hashes = job_task.gather_final_outputs(
        rip_workdir, disc_id=disc_id, db=db, progress_cb=_verification_progress_cb
    )
    ripped_files = _normalize_ripped_files_to_title_ids(
        db, ripped_files, disc_id=disc_id, filename_to_title_id=filename_to_title_id
    )
    _sync_disc_title_mkv_sizes_from_ripped(
        db,
        rip_workdir,
        ripped_files,
        disc_id,
        on_error=lambda msg: job_task.add_log(job, db, msg),
    )
    db.commit()
    _post_rip_progress(str(job.id), rip_progress=100, clear_rip_phase=True, is_final=True)

    backlog = list((job.disc_payload or {}).get("preview_backlog") or [])
    drained: list[dict[str, Any]] = []
    for be in backlog:
        tid, rp = be.get("title_id"), be.get("rel_path")
        if not tid or not rp:
            continue
        if db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == tid).first():
            drained.append(be)
    remaining_backlog = [e for e in backlog if e.get("title_id") not in {d.get("title_id") for d in drained}]
    # Resolve {title_id: rel_path} by parsing the MakeMKV `_tNN` index from the
    # MKV filename and matching against `disc_titles.index`. For selective rips
    # the file list is sparse, so the legacy positional zip mis-aligned titles
    # to files; the index-parse path is correct for both full and selective rips.
    disc_titles_for_map = list(getattr(job.disc, "titles", None) or []) if job.disc else []
    title_output_map = _build_title_output_map(
        title_keys, ripped_files, disc_titles=disc_titles_for_map,
    )
    try:
        if disc_titles_for_map:
            from core.makemkv_output import map_mkv_filenames_to_title_ids
            tid_to_rel = map_mkv_filenames_to_title_ids(
                (rel for _k, rel in ripped_files.items()),
                disc_titles_for_map,
            )
            for tid, rel_path in tid_to_rel.items():
                title_filename_map[str(tid)] = os.path.basename(rel_path)
        else:
            # Legacy fallback: positional zip when no disc titles are loaded.
            sorted_items = sorted(ripped_files.items(), key=lambda x: makemkv_mkv_rel_path_sort_key(x[1]))
            mkv_names_sorted = [rel_path for _, rel_path in sorted_items]
            for idx, title_id in enumerate(title_keys):
                if idx < len(mkv_names_sorted):
                    filename = os.path.basename(mkv_names_sorted[idx])
                    title_filename_map[str(title_id)] = filename
    except Exception:
        pass

    source_hashes = final_hashes.copy()
    source_files = ripped_files.copy()
    preview_maps = _build_title_id_maps(job, payload)
    preview_paths: dict[str, str] = {}
    for title_id, rel_path in ripped_files.items():
        preview_paths[title_id] = rel_path
    payload = _ensure_previews_map(payload, preview_paths, preview_maps)
    payload["ripped_files"] = ripped_files
    payload["final_hashes"] = final_hashes
    payload["source_hashes"] = source_hashes
    payload["source_files"] = source_files
    if title_output_map:
        payload["title_output_map"] = title_output_map
    payload["title_filename_map"] = title_filename_map
    payload["preview_backlog"] = remaining_backlog
    payload = _backfill_preview_title_ids(payload)

    existing_ripped = getattr(job, "ripped_files", None) or {}
    if not isinstance(existing_ripped, dict):
        existing_ripped = {}
    merged_ripped = {**existing_ripped, **ripped_files}
    job_task.set_status(job, db, disc_payload=payload, ripped_files=merged_ripped)

    _disc_id = getattr(job.disc, "id", None) if job.disc else None
    if _disc_id and merged_ripped:
        raw_dir = str(job_root / "raw") if job_root else None
        _update_title_file_paths(db, _disc_id, merged_ripped, "rip", base_dir=raw_dir)

    try:
        from core.stage_validation import validate_rip_output

        validation_result = validate_rip_output(job, db, paths)
        if not validation_result.valid:
            errors_str = "; ".join(validation_result.errors)
            job_task.add_log(job, db, f"Rip validation failed: {errors_str}")
        elif validation_result.warnings:
            warnings_str = "; ".join(validation_result.warnings)
            job_task.add_log(job, db, f"Rip validation warnings: {warnings_str}")
    except Exception as exc:
        job_task.add_log(job, db, f"Warning: Rip validation error: {exc}")

    all_keys = list(preview_paths.keys()) + [d.get("title_id") for d in drained if d.get("title_id")]
    overrides = {d["title_id"]: d["rel_path"] for d in drained if d.get("rel_path")} or None

    _post_rip_verification_complete_callback(
        str(job.id),
        success=True,
        ripped_files=merged_ripped,
        source_hashes=source_hashes,
        preview_detect_keys=all_keys or None,
        preview_detect_overrides=overrides,
    )


def _run_hit_verification(
    job_task: Any,
    job: Any,
    db: Any,
    paths: Any,
    rip_workdir: Any,
    title_keys: list[str],
    disc_id: str | None,
) -> None:
    from workers.tasks import (
        RIP_PROGRESS_COPY_END,
        _normalize_ripped_files_to_title_ids,
        _post_rip_progress,
        _post_rip_verification_complete_callback,
        _sync_disc_title_mkv_sizes_from_ripped,
        _update_title_file_paths,
    )

    _post_rip_progress(str(job.id), rip_phase="verification")
    last_verification_progress_hit = [RIP_PROGRESS_COPY_END]
    VERIFICATION_THROTTLE_PCT = 3

    def _verification_progress_cb_hit(verification_pct: int, _filename: str) -> None:
        combined = RIP_PROGRESS_COPY_END + int(verification_pct * (100 - RIP_PROGRESS_COPY_END) / 100)
        combined = min(100, max(last_verification_progress_hit[0], combined))
        if combined >= 100 or (combined - last_verification_progress_hit[0]) >= VERIFICATION_THROTTLE_PCT:
            last_verification_progress_hit[0] = combined
            _post_rip_progress(str(job.id), rip_progress=combined)

    filename_to_title_id: dict[str, str] = {}
    previews = (job.disc_payload or {}).get("previews") or {}
    tracks = previews.get("tracks") if isinstance(previews, dict) else {}
    if isinstance(tracks, dict):
        for tid, t in tracks.items():
            if tid and isinstance(t, dict) and t.get("source"):
                filename_to_title_id[str(t["source"])] = str(tid)
    title_filename_map = (job.disc_payload or {}).get("title_filename_map") or {}
    if isinstance(title_filename_map, dict):
        for tid, fn in title_filename_map.items():
            if fn and tid:
                filename_to_title_id.setdefault(str(fn), str(tid))

    try:
        ripped_files_source, _ = job_task.gather_final_outputs(
            rip_workdir, disc_id=disc_id, db=db, skip_hashes=True
        )
    except (ValueError, FileNotFoundError):
        ripped_files_source = {}

    if disc_id and db and ripped_files_source:
        _t_mkv = re.compile(r"_t(\d+)\.mkv$", re.IGNORECASE)
        try:
            from api import models as db_models

            titles = (
                db.query(db_models.DiscTitle)
                .filter(db_models.DiscTitle.disc_id == disc_id)
                .order_by(
                    db_models.DiscTitle.index.asc().nulls_last(),
                    db_models.DiscTitle.order_index.asc().nulls_last(),
                    db_models.DiscTitle.id.asc(),
                )
                .all()
            )
            sorted_ids = [str(t.id) for t in titles if t.id]
            for rp in ripped_files_source.values():
                base = os.path.basename(str(rp))
                m = _t_mkv.search(base)
                if m and sorted_ids:
                    idx = int(m.group(1))
                    if 0 <= idx < len(sorted_ids):
                        filename_to_title_id[base] = sorted_ids[idx]
        except Exception as exc:
            log.debug("rip_verification hit: _tNN map job %s: %s", job.id, exc)

    ripped_normalized = _normalize_ripped_files_to_title_ids(
        db, ripped_files_source, disc_id=disc_id, filename_to_title_id=filename_to_title_id
    )

    expected_count = len(title_keys) if title_keys else 0
    wait_interval_sec = int(os.getenv("MKVAUTO_RIP_SHORT_INTERVAL_SECONDS", "15"))
    stable_no_growth_sec = int(os.getenv("MKVAUTO_RIP_SHORT_STABLE_SECONDS", "600"))
    wait_max_total_sec = int(os.getenv("MKVAUTO_RIP_SHORT_WAIT_SECONDS", "0"))
    if expected_count > 0 and len(ripped_normalized) < expected_count and stable_no_growth_sec > 0:
        job_task.add_log(
            job,
            db,
            f"Incomplete rip: {len(ripped_normalized)}/{expected_count} titles on disk; "
            f"waiting for writes to finish (will fail after {stable_no_growth_sec}s with no file size increase).",
        )

        start_time = time.monotonic()
        last_growth_time = time.monotonic()
        prev_sizes = mkv_sizes_by_relpath(rip_workdir)
        count_ok = False
        while True:
            time.sleep(min(wait_interval_sec, max(1, stable_no_growth_sec - (time.monotonic() - last_growth_time))))
            try:
                ripped_files_source_new, _ = job_task.gather_final_outputs(
                    rip_workdir, disc_id=disc_id, db=db, skip_hashes=True
                )
                ripped_normalized_new = _normalize_ripped_files_to_title_ids(
                    db, ripped_files_source_new, disc_id=disc_id, filename_to_title_id=filename_to_title_id
                )
                if len(ripped_normalized_new) >= expected_count:
                    ripped_normalized = ripped_normalized_new
                    ripped_files_source = ripped_files_source_new
                    count_ok = True
                    break
            except (ValueError, FileNotFoundError):
                pass
            cur_sizes = mkv_sizes_by_relpath(rip_workdir)
            any_growth = any(cur_sizes.get(n, 0) > prev_sizes.get(n, 0) for n in prev_sizes) or len(cur_sizes) > len(
                prev_sizes
            )
            if any_growth:
                last_growth_time = time.monotonic()
                job_task.add_log(job, db, "Files still growing, continuing to wait.")
            prev_sizes = cur_sizes
            if not count_ok and (time.monotonic() - last_growth_time) >= stable_no_growth_sec:
                break
            if wait_max_total_sec > 0 and (time.monotonic() - start_time) >= wait_max_total_sec:
                break
        if not count_ok:
            try:
                ripped_files_source_final, _ = job_task.gather_final_outputs(
                    rip_workdir, disc_id=disc_id, db=db, skip_hashes=True
                )
                ripped_normalized_final = _normalize_ripped_files_to_title_ids(
                    db, ripped_files_source_final, disc_id=disc_id, filename_to_title_id=filename_to_title_id
                )
                if len(ripped_normalized_final) >= expected_count:
                    ripped_normalized = ripped_normalized_final
                    ripped_files_source = ripped_files_source_final
                    count_ok = True
            except (ValueError, FileNotFoundError):
                pass
            if not count_ok:
                msg = (
                    f"Incomplete rip: only {len(ripped_normalized)}/{expected_count} titles on disk "
                    f"after {stable_no_growth_sec}s with no file size increase; MakeMKV may have exited early or gone defunct."
                )
                job_task.add_log(job, db, msg)
                _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
                _post_rip_verification_complete_callback(str(job.id), success=False, error_reason=msg)
                return

    if not ripped_normalized:
        msg = "Rip verification: no MKV outputs found under raw/"
        job_task.add_log(job, db, msg)
        _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
        _post_rip_verification_complete_callback(str(job.id), success=False, error_reason=msg)
        return

    try:
        wait_ripped_mkvs_quiescent(
            rip_workdir,
            list(ripped_normalized.values()),
            log_fn=lambda m: job_task.add_log(job, db, m),
        )
    except RuntimeError as exc:
        job_task.add_log(job, db, f"Rip verification: quiescence wait failed: {exc}")
        _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
        _post_rip_verification_complete_callback(str(job.id), success=False, error_reason=str(exc))
        return

    ok_probe, probe_err = probe_ripped_mkvs_ready(
        rip_workdir,
        ripped_normalized,
        log_fn=lambda m: job_task.add_log(job, db, m),
    )
    if not ok_probe:
        job_task.add_log(job, db, f"Rip verification: ffprobe readiness failed: {probe_err}")
        _post_rip_progress(str(job.id), clear_rip_phase=True, is_final=True)
        _post_rip_verification_complete_callback(
            str(job.id), success=False, error_reason=f"ffprobe readiness failed: {probe_err}"
        )
        return

    ripped_files_source, final_hashes_source = job_task.gather_final_outputs(
        rip_workdir, disc_id=disc_id, db=db, progress_cb=_verification_progress_cb_hit
    )
    ripped_normalized = _normalize_ripped_files_to_title_ids(
        db, ripped_files_source, disc_id=disc_id, filename_to_title_id=filename_to_title_id
    )
    path_to_orig_key = {rp: k for k, rp in ripped_files_source.items()}
    source_hashes_hit: dict[str, str] = {}
    for tid, rp in ripped_normalized.items():
        orig_key = path_to_orig_key.get(rp)
        if orig_key is not None and final_hashes_source and orig_key in final_hashes_source:
            source_hashes_hit[tid] = final_hashes_source[orig_key]
    if ripped_normalized:
        _sync_disc_title_mkv_sizes_from_ripped(
            db,
            rip_workdir,
            ripped_normalized,
            disc_id,
            on_error=lambda msg: job_task.add_log(job, db, msg),
        )
    db.commit()
    _post_rip_progress(str(job.id), rip_progress=100, clear_rip_phase=True, is_final=True)

    if disc_id and ripped_normalized:
        raw_dir = str(rip_workdir) if rip_workdir else None
        _update_title_file_paths(db, disc_id, ripped_normalized, "rip", base_dir=raw_dir)

    job_task.set_status(job, db, ripped_files=ripped_normalized)

    _post_rip_verification_complete_callback(
        str(job.id),
        success=True,
        ripped_files=ripped_normalized,
        source_hashes=source_hashes_hit,
    )
