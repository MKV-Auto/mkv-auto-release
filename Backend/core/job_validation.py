"""
Validation functions for job recovery and troubleshooting.
Validates post-processing outputs and preview generation.
"""
import logging
import os
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from sqlalchemy.orm import Session

from core.transfer.validation import calculate_file_hash
from core.job_paths import JobPaths

log = logging.getLogger(__name__)


# Removed: validate_postprocessing() - replaced by validate_transfer_prep_output() in stage_validation.py


def validate_previews(job, db: Session) -> Tuple[bool, List[str]]:
    """
    Validate preview generation by checking:
    1. Preview files exist for each output_file or title entry
    2. Preview manifest files (.m3u8) exist
    3. Preview segment files (.ts) exist
    
    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors = []
    
    try:
        paths = JobPaths.from_job(job)
        preview_root = paths.previews
        
        if not preview_root.exists():
            errors.append(f"Preview directory not found: {preview_root}")
            return False, errors
        
        # Get expected previews from disc_payload
        disc_payload = job.disc_payload or {}
        previews = disc_payload.get("previews", {})
        if not isinstance(previews, dict):
            errors.append("No preview metadata found in disc_payload")
            return False, errors
        
        tracks = previews.get("tracks", {})
        if not isinstance(tracks, dict):
            errors.append("No track metadata found in previews")
            return False, errors
        
        # Get post_paths (preferred) or ripped_files (fallback) to map track keys to output files
        # Both use title_id keys now
        post_paths = getattr(job, "post_paths", None) or disc_payload.get("post_paths") or {}
        ripped_files = getattr(job, "ripped_files", None) or disc_payload.get("ripped_files") or {}
        file_paths = post_paths if post_paths else ripped_files
        
        # Check each track has preview files
        for track_key, track_info in tracks.items():
            if not isinstance(track_info, dict):
                continue
            
            manifest_path = track_info.get("manifest")
            if not manifest_path:
                errors.append(f"Track {track_key} has no manifest path")
                continue
            
            # Resolve manifest path
            if manifest_path.startswith("previews/"):
                # Relative path
                preview_file = preview_root / manifest_path.replace("previews/", "")
            else:
                # Absolute or relative to preview root
                preview_file = preview_root / manifest_path
            
            if not preview_file.exists():
                errors.append(f"Preview manifest not found for {track_key}: {preview_file}")
                continue
            
            # Check manifest file is readable
            try:
                manifest_content = preview_file.read_text()
                # Check for segment references
                if ".ts" not in manifest_content:
                    errors.append(f"Preview manifest for {track_key} appears empty or invalid")
            except Exception as exc:
                errors.append(f"Failed to read preview manifest for {track_key}: {exc}")
        
        # Check if all expected tracks have previews (match by source rel_path when available)
        # file_paths keys are title_id, values are relative paths
        expected_sources = set(file_paths.values()) if file_paths else set()
        actual_sources = set()
        for track_key, track_info in tracks.items():
            if not isinstance(track_info, dict):
                continue
            source = track_info.get("source")
            if source:
                actual_sources.add(source)
            else:
                # Fallback: track_key might be title_id, check if it's in file_paths
                rel = file_paths.get(track_key)
                if rel:
                    actual_sources.add(rel)
        missing_sources = expected_sources - actual_sources
        if missing_sources:
            errors.append(f"Missing previews for tracks: {', '.join(sorted(missing_sources))}")
        
    except Exception as exc:
        errors.append(f"Validation error: {exc}")
        log.error(f"Preview validation failed: {exc}", exc_info=True)
    
    return len(errors) == 0, errors


# Removed: store_source_hashes() - replaced by hash calculation at end of rip stage
# Removed: store_output_hashes() - replaced by validation using source_hashes
# Removed: validate_postprocessing() - replaced by validate_transfer_prep_output() in stage_validation.py

