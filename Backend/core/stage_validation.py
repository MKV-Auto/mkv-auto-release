"""
Stage validation utilities for verifying expected output structures and file integrity.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

from core.transfer.validation import calculate_file_hash
from core.job_paths import JobPaths

log = logging.getLogger(__name__)

# Post-process output: transient file size vs disc_titles.mkv_size (raw) is not a reliable success signal after remux.
_ENFORCE_POSTPROCESS_OUTPUT_MKV_SIZE = False


def _per_rip_mkv_count_or_walk(job: Any, transient_dir: Path) -> int:
    """Per-rip-aware count of MKVs at the job's destination (#365 step 5a).

    Prefer ``job.post_paths`` (or ``disc_payload.post_paths``) when
    UUID-keyed — that's the authoritative per-rip mapping written by
    ``StageState.postprocess_complete``. Only fall back to walking
    ``transient_dir`` when no persisted per-rip signal exists.

    Under #365 step 3b's MKVAUTO_RENAME_DIRECT_TO_DEST flag,
    ``transient_dir`` may resolve to a shared library — the walk would
    over-count by hundreds. The per-rip preference makes this safe in
    the common case (jobs that have post_paths set); the walk fallback
    keeps legacy / very-fresh-resume jobs working.
    """
    def _keys_are_uuids(d):
        return d and all(len(str(k)) == 36 and "-" in str(k) for k in (d or {}))

    persisted = getattr(job, "post_paths", None) or {}
    if _keys_are_uuids(persisted):
        return len(persisted)
    restored = (getattr(job, "disc_payload", None) or {}).get("post_paths") or {}
    if _keys_are_uuids(restored):
        return len(restored)
    if transient_dir.exists():
        return len(list(transient_dir.rglob("*.mkv")))
    return 0


@dataclass
class ValidationResult:
    """
    Result of a validation operation.
    
    Tests: tests/test_stage_validation.py::TestValidationResult
    Update tests if fields are added/removed/modified.
    """
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def generate_expected_rip_output(job, db) -> Dict[str, Any]:
    """
    Generate expected output structure for rip stage.
    
    Returns:
        Dict with expected files, structure, and metadata
    
    Tests: tests/test_stage_validation.py::TestRipStageValidation::test_generate_expected_rip_output
    Update tests if return structure changes.
    """
    expected = {
        "raw_files": [],
        "log_files": [],
        "metadata_files": [],
    }
    
    try:
        from api import models as db_models
        
        disc = getattr(job, "disc", None)
        if not disc:
            return expected
        
        disc_id = disc.id
        disc_titles = db.query(db_models.DiscTitle).filter(
            db_models.DiscTitle.disc_id == disc_id
        ).all()
        
        # Expected MKV files in raw/
        for title in disc_titles:
            if title.source_file:
                expected["raw_files"].append(title.source_file)
        
        # Expected log files
        expected["log_files"] = ["makemkv.log", "makemkv_info.log"]
        
        # Expected metadata files
        expected["metadata_files"] = ["titles_map.json", "disc_info.json"]
        
    except Exception as exc:
        log.warning(f"Error generating expected rip output: {exc}")
    
    return expected


def validate_rip_output(job, db, paths: Optional[JobPaths] = None) -> ValidationResult:
    """
    Validate rip stage output against expected structure.
    
    Tests: tests/test_stage_validation.py::TestRipStageValidation (5 tests)
    Update tests if validation logic, error messages, or function signature changes.
    
    Checks:
    - All expected MKV files exist in raw/
    - Log files exist
    - File sizes > 0 (not corrupted)
    - All expected files have been hashed (stored in disc_payload)
    """
    errors = []
    warnings = []
    details = {}
    
    try:
        if paths is None:
            paths = JobPaths.from_job(job)
        
        raw_dir = paths.raw
        metadata_dir = paths.metadata
        
        if not raw_dir.exists():
            errors.append(f"Raw directory not found: {raw_dir}")
            return ValidationResult(valid=False, errors=errors, warnings=warnings, details=details)
        
        # Get expected structure
        expected = generate_expected_rip_output(job, db)
        expected_raw_files = set(expected.get("raw_files", []))
        
        # Check actual files in raw/
        actual_raw_files = set()
        for file_path in raw_dir.glob("*.mkv"):
            actual_raw_files.add(file_path.name)
        
        # Check for missing files
        missing_files = expected_raw_files - actual_raw_files
        if missing_files:
            errors.append(f"Missing expected MKV files: {', '.join(missing_files)}")
        
        # Check for unexpected files (warnings only)
        unexpected_files = actual_raw_files - expected_raw_files
        if unexpected_files:
            warnings.append(f"Unexpected MKV files found: {', '.join(unexpected_files)}")
        
        # Verify file sizes > 0
        for file_name in actual_raw_files:
            file_path = raw_dir / file_name
            if file_path.exists():
                size = file_path.stat().st_size
                if size == 0:
                    errors.append(f"File {file_name} has zero size (corrupted)")
                details[f"file_size_{file_name}"] = size
        
        # Check log files exist
        for log_file in expected.get("log_files", []):
            log_path = metadata_dir / log_file if metadata_dir.exists() else raw_dir / log_file
            if not log_path.exists():
                warnings.append(f"Log file not found: {log_file}")
        
        # Verify hashes are stored in disc_payload
        disc_payload = job.disc_payload or {}
        source_hashes = disc_payload.get("source_hashes", {})
        source_files = disc_payload.get("source_files", {})
        
        if not source_hashes:
            errors.append("Source hashes not stored in disc_payload")
        else:
            # Check all raw files have hashes
            for file_name in actual_raw_files:
                if file_name not in source_hashes:
                    errors.append(f"Hash not stored for {file_name}")
        
        details["expected_files"] = list(expected_raw_files)
        details["actual_files"] = list(actual_raw_files)
        details["hashes_stored"] = len(source_hashes)
        
    except Exception as exc:
        errors.append(f"Validation error: {exc}")
        log.error(f"Rip validation failed: {exc}", exc_info=True)
    
    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        details=details
    )


def generate_expected_finalize_output(job, db) -> Dict[str, Any]:
    """
    Generate expected output structure for finalize disc stage.
    
    Returns:
        Dict with expected files and structure
    
    Tests: tests/test_stage_validation.py::TestFinalizeStageValidation::test_generate_expected_finalize_output
    Update tests if return structure changes.
    """
    expected = {
        "disc_json_files": [],
        "disc_txt_files": [],
        "disc_summary_files": [],
    }
    
    try:
        from api import models as db_models
        
        disc = getattr(job, "disc", None)
        if not disc:
            return expected
        
        disc_number = getattr(disc, "disc_number", None) or 1
        disc_slug = getattr(disc, "disc_slug", None) or f"disc{disc_number:02d}"
        
        # Expected files: discNN.json, discNN.txt, discNN-summary.txt
        expected["disc_json_files"] = [f"{disc_slug}.json"]
        expected["disc_txt_files"] = [f"{disc_slug}.txt"]
        expected["disc_summary_files"] = [f"{disc_slug}-summary.txt"]
        
    except Exception as exc:
        log.warning(f"Error generating expected finalize output: {exc}")
    
    return expected


def validate_finalize_output(job, db, paths: Optional[JobPaths] = None) -> ValidationResult:
    """
    Validate finalize disc stage output.
    
    Tests: tests/test_stage_validation.py::TestFinalizeStageValidation (4 tests)
    Update tests if validation logic, error messages, or function signature changes.
    
    Checks:
    - Expected JSON, TXT, and summary files exist
    - JSON structure is valid
    """
    errors = []
    warnings = []
    details = {}
    
    try:
        if paths is None:
            paths = JobPaths.from_job(job)
        
        finalize_dir = paths.finalize
        
        if not finalize_dir.exists():
            errors.append(f"Finalize directory not found: {finalize_dir}")
            return ValidationResult(valid=False, errors=errors, warnings=warnings, details=details)
        
        # Get expected structure
        expected = generate_expected_finalize_output(job, db)
        
        # Check JSON files
        expected_json = set(expected.get("disc_json_files", []))
        actual_json = {f.name for f in finalize_dir.glob("*.json")}
        
        missing_json = expected_json - actual_json
        if missing_json:
            errors.append(f"Missing expected JSON files: {', '.join(missing_json)}")
        
        # Validate JSON structure
        for json_file in expected_json:
            json_path = finalize_dir / json_file
            if json_path.exists():
                try:
                    with open(json_path, "r") as f:
                        json_data = json.load(f)
                    # Basic structure validation
                    if not isinstance(json_data, dict):
                        errors.append(f"JSON file {json_file} is not a valid object")
                    else:
                        # Check for required fields
                        if "Index" not in json_data:
                            warnings.append(f"JSON file {json_file} missing 'Index' field")
                        if "Titles" not in json_data:
                            warnings.append(f"JSON file {json_file} missing 'Titles' field")
                except json.JSONDecodeError as exc:
                    errors.append(f"Invalid JSON in {json_file}: {exc}")
                except Exception as exc:
                    errors.append(f"Error reading {json_file}: {exc}")
        
        # Check TXT files
        expected_txt = set(expected.get("disc_txt_files", []))
        actual_txt = {f.name for f in finalize_dir.glob("*.txt") if not f.name.endswith("-summary.txt")}
        
        missing_txt = expected_txt - actual_txt
        if missing_txt:
            warnings.append(f"Missing expected TXT files: {', '.join(missing_txt)}")
        
        # Check summary files
        expected_summary = set(expected.get("disc_summary_files", []))
        actual_summary = {f.name for f in finalize_dir.glob("*-summary.txt")}
        
        missing_summary = expected_summary - actual_summary
        if missing_summary:
            warnings.append(f"Missing expected summary files: {', '.join(missing_summary)}")
        
        details["expected_json"] = list(expected_json)
        details["actual_json"] = list(actual_json)
        details["expected_txt"] = list(expected_txt)
        details["actual_txt"] = list(actual_txt)
        
    except Exception as exc:
        errors.append(f"Validation error: {exc}")
        log.error(f"Finalize validation failed: {exc}", exc_info=True)
    
    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        details=details
    )


def generate_expected_transfer_prep_output(job, db) -> Dict[str, Any]:
    """
    Generate expected output structure for post-process stage.
    
    Returns:
        Dict with expected file paths and hashes
    
    Tests: tests/test_stage_validation.py::TestTransferPrepStageValidation::test_generate_expected_transfer_prep_output
    Update tests if return structure changes.
    """
    expected = {
        "expected_files": {},   # title_id -> expected_path
        "expected_hashes": {},  # title_id -> expected_hash (legacy; prefer expected_sizes)
        "expected_sizes": {},   # title_id -> mkv_size (from disc_titles)
        "directory_structure": [],
    }
    
    try:
        disc_payload = job.disc_payload or {}
        source_hashes = disc_payload.get("source_hashes", {})
        
        # Get list of ignored source files from DiscTitle records
        ignored_source_files = set()
        disc_id = None
        try:
            from api import models as db_models
            disc = getattr(job, "disc", None)
            if disc:
                disc_id = disc.id
                disc_titles = db.query(db_models.DiscTitle).filter(
                    db_models.DiscTitle.disc_id == disc_id
                ).all()
                for title in disc_titles:
                    # Skip titles with type="ignore" - these are not moved to transient
                    if title.type and str(title.type).strip().lower() == "ignore":
                        if title.source_file:
                            ignored_source_files.add(title.source_file)
        except Exception as exc:
            log.warning(f"Error querying ignored titles: {exc}")
        
        # Use job.post_paths if available (contains actual renamed filenames after post-processing with title_id keys)
        # Otherwise fall back to discovering from transient directory or using disc_payload
        actual_post_paths = {}
        job_post_paths = getattr(job, "post_paths", None)

        # Check both job.post_paths (direct attribute) and disc_payload.post_paths
        # job.post_paths might be more up-to-date if it was just set
        job_post_paths_direct = getattr(job, "post_paths", None)
        if job_post_paths_direct and isinstance(job_post_paths_direct, dict):
            # job.post_paths has title_id keys -> relative_path (most reliable)
            actual_post_paths = job_post_paths_direct
            log.debug("generate_expected_transfer_prep_output: Using job.post_paths directly (count=%s)", len(actual_post_paths))
        elif job_post_paths:
            # job.post_paths from getattr (might be from disc_payload)
            actual_post_paths = job_post_paths
            log.debug("generate_expected_transfer_prep_output: Using job.post_paths from getattr (count=%s)", len(actual_post_paths))
        else:
            # No job.post_paths - this should not happen if rename_outputs worked correctly
            log.warning("generate_expected_transfer_prep_output: No job.post_paths found - rename_outputs may have failed")
            actual_post_paths = {}
        
        # When post_paths came from gather_final_outputs (e.g. files_already_moved / reverted job),
        # keys can be renamed filenames, not title_ids. expected_sizes is keyed by title_id, so we must
        # resolve filename keys to title_id using title_filename_map (title_id -> filename) or disc_titles.
        def _is_uuid_like(k):
            s = str(k)
            return len(s) == 36 and "-" in s
        if actual_post_paths and not all(_is_uuid_like(k) for k in actual_post_paths):
            disc_payload = getattr(job, "disc_payload", None) or {}
            title_filename_map = disc_payload.get("title_filename_map") or {}
            filename_to_title_id = {}
            if isinstance(title_filename_map, dict):
                filename_to_title_id = {str(v): str(k) for k, v in title_filename_map.items() if v and k}
            # Fallback for reverted jobs: match each discovered basename to a disc_title by title substring (best-effort)
            if not filename_to_title_id and disc_id:
                try:
                    from api import models as db_models
                    disc_titles = db.query(db_models.DiscTitle).filter(
                        db_models.DiscTitle.disc_id == disc_id
                    ).all()
                    non_ignore = [(str(t.id), (t.title or "").strip()) for t in disc_titles if t.id and str(getattr(t, "type", "") or "").strip().lower() != "ignore"]
                    for key, rel_path in list(actual_post_paths.items()):
                        basename = os.path.basename(rel_path) if rel_path else str(key)
                        base_lower = basename.lower()
                        for tid, tit in non_ignore:
                            if tit and tit.lower() in base_lower:
                                filename_to_title_id[basename] = tid
                                filename_to_title_id[str(key)] = tid
                                break
                except Exception:
                    pass
            normalized = {}
            for key, rel_path in actual_post_paths.items():
                basename = os.path.basename(rel_path) if rel_path else str(key)
                title_id = filename_to_title_id.get(str(key)) or filename_to_title_id.get(basename)
                if title_id:
                    normalized[title_id] = rel_path
                else:
                    log.debug("generate_expected_transfer_prep_output: could not map filename key %r to title_id, skipping", key)
            if normalized:
                actual_post_paths = normalized
                log.info("Job %s: generate_expected_transfer_prep_output: normalized %s filename-keyed entries to title_id keys", getattr(job, "id", None), len(normalized))
        
        # Build title_id -> source_file mapping to check ignored files
        title_id_to_source_file: Dict[str, str] = {}
        title_id_ignore: Set[str] = set()
        if disc_id:
            try:
                from api import models as db_models
                disc_titles = db.query(db_models.DiscTitle).filter(
                    db_models.DiscTitle.disc_id == disc_id
                ).all()
                for title in disc_titles:
                    if title.id:
                        tid = str(title.id)
                        if title.source_file:
                            title_id_to_source_file[tid] = title.source_file
                        if title.type and str(title.type).strip().lower() == "ignore":
                            title_id_ignore.add(tid)
            except Exception:
                pass
        
        # Build title_id -> mkv_size for expected_sizes
        if disc_id:
            try:
                from api import models as db_models
                disc_titles = db.query(db_models.DiscTitle).filter(
                    db_models.DiscTitle.disc_id == disc_id
                ).all()
                skipped_ignore = 0
                skipped_none = []
                for t in disc_titles:
                    if t.id and t.type and str(t.type).strip().lower() == "ignore":
                        skipped_ignore += 1
                        continue
                    if t.id and t.mkv_size is not None:
                        expected["expected_sizes"][str(t.id)] = t.mkv_size
                    elif t.id:
                        skipped_none.append((str(t.id), getattr(t, "source_file", None)))
                log.info(
                    "Job %s: generate_expected_transfer_prep_output expected_sizes: disc_id=%s expected_sizes_count=%s skipped_ignore=%s skipped_mkv_size_none=%s",
                    getattr(job, "id", None), disc_id, len(expected["expected_sizes"]),
                    skipped_ignore, skipped_none[:10]
                )
            except Exception as exc:
                log.warning("generate_expected_transfer_prep_output: error building expected_sizes: %s", exc)

        # Build expected file mapping, excluding ignored files
        # actual_post_paths keys are always title_ids (UUIDs) - no mapping needed
        for title_id, rel_path in actual_post_paths.items():
            # title_id is already a UUID, no mapping needed
            tid_key = str(title_id)
            if tid_key in title_id_ignore:
                continue
            source_file = title_id_to_source_file.get(tid_key)
            if source_file and source_file in ignored_source_files:
                continue
            expected["expected_files"][tid_key] = rel_path
            # source_hashes may use title_id keys (new format) or source_file keys (legacy)
            if tid_key in source_hashes:
                expected["expected_hashes"][tid_key] = source_hashes[tid_key]
            elif source_file and source_file in source_hashes:
                expected["expected_hashes"][tid_key] = source_hashes[source_file]
        
        # Extract unique directory paths
        dirs = set()
        for title_id, rel_path in actual_post_paths.items():
            tid_key = str(title_id)
            if tid_key in title_id_ignore:
                continue
            source_file = title_id_to_source_file.get(tid_key)
            if source_file and source_file in ignored_source_files:
                continue
            path_obj = Path(rel_path)
            # Add all parent directories
            for parent in path_obj.parents:
                if parent != Path("."):
                    dirs.add(str(parent))
        
        expected["directory_structure"] = sorted(dirs)
        
    except Exception as exc:
        log.warning(f"Error generating expected postprocess output: {exc}")
    
    return expected


def validate_transfer_preconditions(job, db, paths: Optional[JobPaths] = None) -> ValidationResult:
    """
    Validate preconditions for starting post-process stage.

    Checks:
    - Files already in transient (files_already_moved) - if so, validation passes
    - Source directory discovery: raw (from JobPaths), ripped_files (job + disc_payload merge) for mapping
    - Source hash structure (warnings if missing/incomplete)
    - File count (excluding ignored); ``mkv_size`` unset for ripped titles is a **warning** (postprocess refreshes sizes after quiescence when possible)

    Returns:
        ValidationResult with valid=True if postprocess can start, False otherwise
    """
    errors = []
    warnings = []
    details = {}
    
    try:
        if paths is None:
            paths = JobPaths.from_job(job)

        # Mirror validate_transfer_prep_output: resolve to wherever rename
        # writes (transient/ under flag-off, config.transfer_dir under
        # flag-on local). Otherwise this preflight walks transient/ even
        # when files-already-moved cases under flag-on have already
        # placed files at the library.
        from core.transfer.path_resolution import resolve_transfer_prep_validation_root
        transient_dir = resolve_transfer_prep_validation_root(job, paths, db)

        # Check files_already_moved scenario first. Use per-rip count
        # helper (#365 step 5a) — under the MKVAUTO_RENAME_DIRECT_TO_DEST
        # flag, transient_dir may be a shared library that always has
        # MKVs from prior rips.
        transient_mkv_count = _per_rip_mkv_count_or_walk(job, transient_dir)
        if transient_mkv_count > 0:
            details["files_already_moved"] = True
            details["transient_mkv_count"] = transient_mkv_count
            log.info(f"Job {job.id}: Pre-flight validation: Files already at destination ({transient_mkv_count} MKV files), skipping source directory validation")
            return ValidationResult(valid=True, errors=errors, warnings=warnings, details=details)
        
        details["files_already_moved"] = False
        
        # Source directory discovery: use raw directory
        source_dir = None
        directories_checked = []
        
        # Check raw directory
        raw_path = paths.raw
        exists = raw_path.exists()
        accessible = False
        mkv_count = 0
        if exists:
            try:
                accessible = os.access(raw_path, os.R_OK)
                if accessible:
                    mkv_count = sum(1 for _ in raw_path.rglob("*.mkv"))
            except Exception:
                pass
        directories_checked.append({
            "path": str(raw_path),
            "type": "raw",
            "exists": exists,
            "accessible": accessible,
            "mkv_count": mkv_count
        })
        if exists and accessible and mkv_count > 0:
            source_dir = raw_path
            details["selected_source_dir"] = "raw"
            details["selected_path"] = str(raw_path)
            log.info(f"Job {job.id}: Pre-flight validation: Selected raw: {raw_path} (found {mkv_count} MKV files)")
        
        details["directories_checked"] = directories_checked
        
        # If no valid source directory found, return error
        if source_dir is None:
            errors.append("No valid source directory found with MKV files. Checked: " + ", ".join([f"{d['type']} ({'exists' if d['exists'] else 'missing'}, {d['mkv_count']} MKV files)" for d in directories_checked]))
            return ValidationResult(valid=False, errors=errors, warnings=warnings, details=details)
        
        # Validate source_hashes structure
        disc_payload = job.disc_payload or {}
        source_hashes = disc_payload.get("source_hashes", {})
        
        if not source_hashes:
            warnings.append("source_hashes not found in disc_payload - will recalculate all hashes during postprocess")
        elif not isinstance(source_hashes, dict):
            warnings.append(f"source_hashes has invalid structure (expected dict, got {type(source_hashes).__name__}) - will recalculate all hashes")
        else:
            # Count expected source files
            expected_source_files = set()
            try:
                from core.disc import Disc
                disc = Disc(job.disc_num, job.mount_point)
                if hasattr(job, "disc") and job.disc and job.disc.disc_info:
                    titles_map = job.disc.disc_info.get("titles") or job.disc.disc_info.get("titles_map")
                    if isinstance(titles_map, dict):
                        for title_info in titles_map.values():
                            if isinstance(title_info, dict) and "source_file" in title_info:
                                expected_source_files.add(title_info["source_file"])
                elif disc_payload:
                    titles_map = disc_payload.get("titles") or disc_payload.get("tracks")
                    if isinstance(titles_map, dict):
                        for title_info in titles_map.values():
                            if isinstance(title_info, dict) and "source_file" in title_info:
                                expected_source_files.add(title_info["source_file"])
            except Exception as exc:
                log.warning(f"Error determining expected source files: {exc}")
            
            if expected_source_files:
                missing_hashes = expected_source_files - set(source_hashes.keys())
                if missing_hashes:
                    warnings.append(f"{len(missing_hashes)}/{len(expected_source_files)} source files missing hashes - will recalculate those")
                else:
                    log.info(f"Job {job.id}: Pre-flight validation: All {len(source_hashes)} source files have cached hashes")
            else:
                log.info(f"Job {job.id}: Pre-flight validation: {len(source_hashes)} source files have cached hashes")
        
        details["source_hashes_present"] = bool(source_hashes and isinstance(source_hashes, dict))
        details["source_hash_count"] = len(source_hashes) if isinstance(source_hashes, dict) else 0
        
        # File count validation
        if source_dir:
            actual_count = sum(1 for _ in source_dir.rglob("*.mkv"))
            details["source_files_found"] = actual_count
            
            # Expected count comes from the JOB's own manifest — never from
            # Disc(job.disc_num, job.mount_point): that is DRIVE-keyed
            # live/cached info, i.e. whatever disc sits in the tray NOW.
            # After a disc swap it described a different disc entirely and
            # this check failed jobs whose files were all present (#864 —
            # RE Extinction UHD: 122 ripped, 122 on disk, "expected" 124
            # from the wrong disc). Precedence: DiscDB-hit selection map
            # (job-scoped) > ripped_files > the job's own disc_titles rows.
            expected_count = 0
            missing_names: list = []
            try:
                ripped = getattr(job, "ripped_files", None)
                if (
                    disc_payload.get("discdb_hit")
                    and isinstance(disc_payload.get("title_filename_map"), dict)
                    and disc_payload["title_filename_map"]
                ):
                    expected_count = len(disc_payload["title_filename_map"])
                elif isinstance(ripped, dict) and ripped:
                    expected_count = len(ripped)
                else:
                    _disc_id = getattr(job.disc, "id", None) if getattr(job, "disc", None) else None
                    if _disc_id:
                        from api import models as db_models
                        expected_count = (
                            db.query(db_models.DiscTitle)
                            .filter(db_models.DiscTitle.disc_id == _disc_id)
                            .count()
                        )
                # Receipts (#853 rule 1): name what is actually absent, so a
                # shortfall claim can be verified instead of trusted.
                if isinstance(ripped, dict) and ripped:
                    _present = {p.name for p in source_dir.rglob("*.mkv")}
                    missing_names = sorted(
                        str(rel) for rel in ripped.values()
                        if str(rel).rsplit("/", 1)[-1] not in _present
                    )[:10]
            except Exception as exc:
                log.warning(f"Error determining expected file count: {exc}")

            details["source_files_expected"] = expected_count

            if expected_count > 0:
                if actual_count < expected_count:
                    _missing_note = f" (missing: {', '.join(missing_names)})" if missing_names else ""
                    errors.append(
                        f"Found only {actual_count}/{expected_count} MKV files in source directory {source_dir}{_missing_note}"
                    )
                elif actual_count > expected_count:
                    warnings.append(f"Found {actual_count} MKV files, expected {expected_count} (extra files may be ignored)")
            elif actual_count == 0:
                errors.append(f"No MKV files found in source directory {source_dir}")

            # Pre-flight raw file checks: existence, non-zero size; mkv_size unset → warning (resume_postprocess syncs after quiescence when ripped_files exists)
            disc_id = getattr(job.disc, "id", None) if getattr(job, "disc", None) else None
            if disc_id and source_dir and actual_count > 0:
                try:
                    from api import models as db_models
                    from core.utils import is_dev_mode
                    all_titles = db.query(db_models.DiscTitle).filter(
                        db_models.DiscTitle.disc_id == disc_id
                    ).all()
                    titles_excl_ignore = [t for t in all_titles if not (t.type and str(t.type).strip().lower() == "ignore")]
                    ignore_title_ids = {str(t.id) for t in all_titles if t.id and (t.type and str(t.type).strip().lower() == "ignore")}
                    ripped: dict = {}
                    payload_ripped = (job.disc_payload or {}).get("ripped_files") if getattr(job, "disc_payload", None) else None
                    if isinstance(payload_ripped, dict):
                        ripped.update(payload_ripped)
                    job_r = getattr(job, "ripped_files", None) or {}
                    if isinstance(job_r, dict):
                        ripped.update(job_r)
                    # Only require mkv_size for titles we actually ripped (in job.ripped_files)
                    ripped_title_ids = {str(k) for k in ripped.keys()}
                    # Log disc_titles summary: id, source_file, mkv_size for each (non-ignore) title
                    for t in titles_excl_ignore:
                        log.info(
                            "Job %s: Pre-flight mkv_size: disc_id=%s title_id=%s source_file=%s mkv_size=%s",
                            job.id, disc_id, t.id, getattr(t, "source_file", None), t.mkv_size
                        )
                    title_id_to_mkv_size = {str(t.id): t.mkv_size for t in titles_excl_ignore if t.id and t.mkv_size is not None}
                    log.info(
                        "Job %s: Pre-flight title_id_to_mkv_size: count=%s sample=%s",
                        job.id, len(title_id_to_mkv_size),
                        dict(list(title_id_to_mkv_size.items())[:5]) if title_id_to_mkv_size else {}
                    )
                    for t in titles_excl_ignore:
                        if t.id and t.mkv_size is None and str(t.id) in ripped_title_ids:
                            warnings.append(f"mkv_size not set for title {t.id} (source_file={t.source_file})")
                    rel_to_title = {v: k for k, v in ripped.items()}
                    log.info(
                        "Job %s: Pre-flight ripped_files: keys_count=%s rel_to_title_sample=%s",
                        job.id, len(ripped), dict(list(rel_to_title.items())[:5]) if rel_to_title else {}
                    )
                    for mkv in source_dir.rglob("*.mkv"):
                        rel = str(mkv.relative_to(source_dir))
                        title_id = rel_to_title.get(rel)
                        if title_id is None:
                            for t in titles_excl_ignore:
                                if t.source_file and mkv.name == t.source_file:
                                    title_id = str(t.id)
                                    break
                        if title_id is None:
                            # File not in job.ripped_files and not in disc_titles (e.g. extra titles from full disc).
                            # Do not fail: only require that ripped_files entries match; treat extra as warning.
                            log.info("Job %s: Pre-flight compare: rel=%s title_id=None (unmatched, skipping)", job.id, rel)
                            warnings.append(f"Extra file not in job selection: {rel}")
                            continue
                        if title_id in ignore_title_ids:
                            log.info("Job %s: Pre-flight compare: rel=%s title_id=%s (ignore title, skip)", job.id, rel, title_id)
                            continue
                        # Only require mkv_size for titles we actually ripped (in job.ripped_files)
                        if title_id not in ripped_title_ids:
                            log.info("Job %s: Pre-flight compare: rel=%s title_id=%s (not in ripped_files, skip)", job.id, rel, title_id)
                            continue
                        actual = mkv.stat().st_size
                        exp = title_id_to_mkv_size.get(title_id)
                        log.info(
                            "Job %s: Pre-flight raw file: rel=%s title_id=%s mkv_size=%s actual_stat=%s",
                            job.id, rel, title_id, exp, actual,
                        )
                        if exp is None:
                            warnings.append(f"mkv_size not set for title {title_id} (file {rel})")
                            continue
                        if actual <= 0:
                            errors.append(f"Source file has zero size: {rel} (title {title_id})")
                        elif is_dev_mode() and 1 <= actual <= 20 * 1024 and exp and actual != exp:
                            pass
                except Exception as exc:
                    log.warning(f"Error during pre-flight file size check: {exc}")
                    errors.append(f"Pre-flight file size check failed: {exc}")

        details["transient_directory_exists"] = transient_dir.exists()
        # Per-rip-aware count (#365 step 5a): same rationale as the
        # files_already_moved check above.
        details["transient_files_count"] = _per_rip_mkv_count_or_walk(job, transient_dir)
        
    except Exception as exc:
        errors.append(f"Pre-flight validation error: {exc}")
        log.error(f"Post-process pre-flight validation failed: {exc}", exc_info=True)
    
    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        details=details
    )


def validate_transfer_prep_output(job, db, paths: Optional[JobPaths] = None) -> ValidationResult:
    """
    Validate post-process stage output.

    Checks:
    - All expected files exist at expected paths
    - Optionally file sizes vs disc_titles.mkv_size (disabled: raw byte size is not comparable to remuxed transient output).

    Looks at whichever path ``rename_outputs`` wrote to via
    :func:`core.transfer.path_resolution.resolve_transfer_prep_validation_root`
    so the validator stays in lockstep with the
    ``MKVAUTO_RENAME_DIRECT_TO_DEST`` flag (#365). Under the production
    default this resolves to ``paths.transient`` (pre-collapse layout);
    under the opt-in flag with a local-mode TransferConfig it resolves
    to ``config.transfer_dir``. Without this resolution the validator
    would look at the empty transient/ under flag-on and report
    "0 of N expected files" while the files actually exist at the
    library destination.
    """
    errors = []
    warnings = []
    details = {}

    try:
        if paths is None:
            paths = JobPaths.from_job(job)

        from core.transfer.path_resolution import resolve_transfer_prep_validation_root
        transient_dir = resolve_transfer_prep_validation_root(job, paths, db)
        log.info(f"Job {job.id}: Post-process validation: Checking directory: {transient_dir}")

        if not transient_dir.exists():
            errors.append(f"Post-process output directory not found: {transient_dir}")
            return ValidationResult(valid=False, errors=errors, warnings=warnings, details=details)

        expected = generate_expected_transfer_prep_output(job, db)
        expected_files = expected.get("expected_files", {})
        expected_sizes = expected.get("expected_sizes", {})

        ripped = {}
        payload_ripped = (job.disc_payload or {}).get("ripped_files") if getattr(job, "disc_payload", None) else None
        if isinstance(payload_ripped, dict):
            ripped.update(payload_ripped)
        job_r = getattr(job, "ripped_files", None) or {}
        if isinstance(job_r, dict):
            ripped.update(job_r)
        ripped_title_ids = {str(k) for k in ripped.keys()}

        log.info(f"Job {job.id}: Post-process validation: Expected {len(expected_files)} files")

        files_found = 0
        size_mismatches = []

        for title_id, expected_rel_path in expected_files.items():
            expected_path = transient_dir / expected_rel_path
            log.debug(f"Job {job.id}: Post-process validation: Checking file: {expected_rel_path} (title_id: {title_id})")

            if not expected_path.exists():
                errors.append(f"Expected file not found: {expected_rel_path} (title_id: {title_id})")
                continue

            files_found += 1
            try:
                actual_size = expected_path.stat().st_size
            except Exception as exc:
                errors.append(f"Failed to stat {expected_rel_path}: {exc}")
                continue

            exp_size = expected_sizes.get(title_id)
            log.info(
                "Job %s: Post-process validate: title_id=%s path=%s exp_size=%s actual_size=%s",
                job.id, title_id, expected_rel_path, exp_size, actual_size
            )
            if exp_size is None:
                if str(title_id) in ripped_title_ids:
                    if _ENFORCE_POSTPROCESS_OUTPUT_MKV_SIZE:
                        err = f"mkv_size not set for title {title_id} (file {expected_rel_path}); cannot validate size"
                        errors.append(err)
                        log.warning("Job %s: Post-process validate: %s", job.id, err)
                    else:
                        w = f"mkv_size not set for title {title_id} (file {expected_rel_path}); skipping size check"
                        warnings.append(w)
                        log.info("Job %s: Post-process validate: %s", job.id, w)
                continue
            if actual_size != exp_size:
                size_mismatches.append({"file": expected_rel_path, "title_id": title_id, "expected": exp_size, "actual": actual_size})
                mismatch_msg = (
                    f"Size mismatch for {expected_rel_path} (title_id: {title_id}): expected {exp_size}, got {actual_size}"
                )
                if _ENFORCE_POSTPROCESS_OUTPUT_MKV_SIZE:
                    errors.append(mismatch_msg)
                else:
                    warnings.append(mismatch_msg + " (not enforced)")
                    log.info("Job %s: Post-process validate: %s", job.id, mismatch_msg)

        details["expected_count"] = len(expected_files)
        details["files_found"] = files_found
        details["size_mismatches"] = size_mismatches

        log.info(f"Job {job.id}: Post-process validation: Found {files_found}/{len(expected_files)} files")
        
        # If expected_files is empty, check if there are actually files at the destination.
        # This handles the case where filename mapping failed but files were successfully moved.
        # Per-rip count (#365 step 5a) so the check doesn't false-pass when
        # transient_dir is a shared library with unrelated MKVs.
        if len(expected_files) == 0:
            actual_count_at_dest = _per_rip_mkv_count_or_walk(job, transient_dir)
            if actual_count_at_dest > 0:
                # Files exist but weren't in expected_files - this is a mapping issue, not a validation failure
                warnings.append(f"Found {actual_count_at_dest} files at destination but expected_files is empty (filename mapping may have failed)")
                log.warning(f"Job {job.id}: Post-process validation: Expected 0 files but found {actual_count_at_dest} files at destination - filename mapping may have failed")
                # Don't fail validation if files exist - the mapping issue is a warning, not an error
                # Validation should pass if files were successfully moved, even if we can't map them
            else:
                # No files expected and none found - this is valid (all files might be ignored)
                log.info(f"Job {job.id}: Post-process validation: Expected 0 files and found 0 files (all files may be ignored)")
        elif files_found < len(expected_files):
            error_msg = f"Only found {files_found} of {len(expected_files)} expected files"
            errors.append(error_msg)
            log.error(f"Job {job.id}: Post-process validation: {error_msg}")
        
    except Exception as exc:
        errors.append(f"Validation error: {exc}")
        log.error(f"Post-process validation failed: {exc}", exc_info=True)
    
    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        details=details
    )


def generate_expected_transfer_output(job, db) -> Dict[str, Any]:
    """
    Generate expected output structure for transfer stage.
    
    Returns:
        Dict with expected file paths and hashes at destination
    
    Tests: tests/test_stage_validation.py::TestTransferStageValidation::test_generate_expected_transfer_output
    Update tests if return structure changes.
    """
    expected = {
        "expected_files": {},  # title_id -> expected_dest_path
        "expected_hashes": {},  # title_id -> expected_hash
        "directory_structure": [],
    }
    
    try:
        # Get post_paths from job (preferred) or disc_payload (fallback)
        # Keys are now title_id, not source_file
        post_paths = getattr(job, "post_paths", None) or {}
        disc_payload = job.disc_payload or {}
        if not post_paths:
            post_paths = disc_payload.get("post_paths", {})
        
        final_hashes = disc_payload.get("final_hashes", {})
        source_hashes = disc_payload.get("source_hashes", {})
        # ripped_files maps title_id → rip output filename ("test_t1.mkv",
        # or in prod "<disc>_tNN.mkv"). final_hashes / source_hashes are often
        # keyed by that filename rather than by title_id, so we use this map
        # as the bridge when the direct title_id lookup misses below.
        ripped_files = (
            getattr(job, "ripped_files", None) or disc_payload.get("ripped_files") or {}
        )
        if not isinstance(ripped_files, dict):
            ripped_files = {}
        
        ignore_title_ids: Set[str] = set()
        try:
            from api import models as db_models
            disc = getattr(job, "disc", None)
            if disc:
                for t in db.query(db_models.DiscTitle).filter(db_models.DiscTitle.disc_id == disc.id).all():
                    if t.id and t.type and str(t.type).strip().lower() == "ignore":
                        ignore_title_ids.add(str(t.id))
        except Exception:
            pass

        # Build expected file mapping (should mirror transient structure)
        # post_paths keys are title_id
        for title_id, rel_path in post_paths.items():
            tid_key = str(title_id)
            if tid_key in ignore_title_ids:
                continue
            expected["expected_files"][tid_key] = rel_path
            # Prefer final_hashes (keyed by title_id), fallback to source_hashes (may be keyed by source_file)
            if tid_key in final_hashes:
                expected["expected_hashes"][tid_key] = final_hashes[tid_key]
            elif tid_key in source_hashes:
                expected["expected_hashes"][tid_key] = source_hashes[tid_key]
            else:
                # Try title_id → rip output filename → hash. gather_final_outputs
                # returns final_hashes keyed by the on-disk filename
                # (``test_t1.mkv`` in E2E, ``<disc>_tNN.mkv`` in prod), but
                # post_paths is keyed by title_id; the ripped_files map
                # bridges them.
                _ripped_name = ripped_files.get(tid_key) or ripped_files.get(title_id)
                if _ripped_name and _ripped_name in final_hashes:
                    expected["expected_hashes"][tid_key] = final_hashes[_ripped_name]
                elif _ripped_name and _ripped_name in source_hashes:
                    expected["expected_hashes"][tid_key] = source_hashes[_ripped_name]
                else:
                    # Last resort: source_file lookup (matches when the
                    # rip output happens to be named after the segment).
                    try:
                        from api import models as db_models
                        disc = getattr(job, "disc", None)
                        if disc:
                            disc_titles = db.query(db_models.DiscTitle).filter(
                                db_models.DiscTitle.disc_id == disc.id,
                                db_models.DiscTitle.id == title_id
                            ).first()
                            if disc_titles and disc_titles.source_file and disc_titles.source_file in source_hashes:
                                expected["expected_hashes"][tid_key] = source_hashes[disc_titles.source_file]
                    except Exception:
                        pass
        
        # Extract directory structure
        dirs = set()
        for title_id, rel_path in post_paths.items():
            if str(title_id) in ignore_title_ids:
                continue
            path_obj = Path(rel_path)
            for parent in path_obj.parents:
                if parent != Path("."):
                    dirs.add(str(parent))
        
        expected["directory_structure"] = sorted(dirs)
        
    except Exception as exc:
        log.warning(f"Error generating expected transfer output: {exc}")
    
    return expected


def validate_transfer_output(job, db, destination_path: Path) -> ValidationResult:
    """
    Validate transfer stage output.
    
    Tests: tests/test_stage_validation.py::TestTransferStageValidation (4 tests)
    Update tests if validation logic, error messages, or function signature changes.
    
    Checks:
    - All expected files exist at destination
    - File hashes match expected hashes
    """
    errors = []
    warnings = []
    details = {}
    
    try:
        if not destination_path.exists():
            errors.append(f"Destination path not found: {destination_path}")
            return ValidationResult(valid=False, errors=errors, warnings=warnings, details=details)
        
        # Get expected structure
        expected = generate_expected_transfer_output(job, db)
        expected_files = expected.get("expected_files", {})
        expected_hashes = expected.get("expected_hashes", {})
        
        if not expected_hashes:
            errors.append("Expected hashes not available for validation")
            return ValidationResult(valid=False, errors=errors, warnings=warnings, details=details)
        
        # Check each expected file
        files_found = 0
        files_validated = 0
        hash_mismatches = []
        
        for title_id, expected_rel_path in expected_files.items():
            expected_path = destination_path / expected_rel_path

            # Library transfers pass the library subroot (``transfer-dest/Movies``)
            # as dest_path, but expected_rel_path still carries the library
            # prefix (``Movies/...``) because post_paths is relative to the
            # transient root. If the literal join misses, retry with the
            # library prefix stripped — that matches the actual on-disk layout.
            if not expected_path.exists():
                dest_basename = destination_path.name
                rel_parts = Path(expected_rel_path).parts
                if rel_parts and rel_parts[0] == dest_basename:
                    alt_path = destination_path / Path(*rel_parts[1:])
                    if alt_path.exists():
                        expected_path = alt_path

            if not expected_path.exists():
                errors.append(f"Expected file not found at destination: {expected_rel_path} (title_id: {title_id})")
                continue

            files_found += 1
            
            # Verify hash matches expected
            expected_hash = expected_hashes.get(title_id)
            if expected_hash:
                try:
                    actual_hash = calculate_file_hash(expected_path)
                    if actual_hash != expected_hash:
                        hash_mismatches.append({
                            "file": expected_rel_path,
                            "title_id": title_id,
                            "expected": expected_hash,
                            "actual": actual_hash,
                        })
                        errors.append(
                            f"Hash mismatch at destination for {expected_rel_path} (title_id: {title_id}): "
                            f"expected {expected_hash[:16]}..., got {actual_hash[:16]}..."
                        )
                    else:
                        files_validated += 1
                except Exception as exc:
                    errors.append(f"Failed to calculate hash for {expected_rel_path}: {exc}")
            else:
                warnings.append(f"No expected hash for {title_id}, skipping hash validation")
        
        details["expected_count"] = len(expected_files)
        details["files_found"] = files_found
        details["files_validated"] = files_validated
        details["hash_mismatches"] = hash_mismatches
        
        if files_found < len(expected_files):
            errors.append(f"Only found {files_found} of {len(expected_files)} expected files at destination")
        
    except Exception as exc:
        errors.append(f"Validation error: {exc}")
        log.error(f"Transfer validation failed: {exc}", exc_info=True)
    
    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        details=details
    )

