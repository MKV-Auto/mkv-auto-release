"""
Integration tests for stage validation with full pipeline simulation.
Tests the complete flow without requiring actual disc ripping.
"""
import json
import pytest
import shutil
from pathlib import Path
from unittest.mock import Mock, MagicMock
from sqlalchemy.orm import Session

from core.stage_validation import (
    validate_rip_output,
    validate_finalize_output,
    validate_transfer_prep_output,
    validate_transfer_output,
)
from core.job_paths import JobPaths
from core.transfer.validation import calculate_file_hash


@pytest.fixture
def full_pipeline_setup(tmp_path):
    """
    Set up a complete pipeline simulation with all stages.
    Returns a dict with all paths and mock objects.
    """
    job_id = "test-job-integration"
    paths = JobPaths(tmp_path, job_id)
    paths.ensure_layout()
    
    # Create mock job
    import uuid
    job = Mock()
    job.id = job_id
    job.disc_payload = {}
    job.ripped_files = None
    job.post_paths = None
    job.disc = Mock()
    job.disc.id = "test-disc-integration"
    job.disc.disc_number = 1
    job.disc.disc_slug = "disc01"
    
    # Create mock DB with title_ids
    db = Mock(spec=Session)
    
    title1 = Mock()
    title1.id = str(uuid.uuid4())
    title1.source_file = "title_001.mkv"  # match MKV filenames written in raw/
    title1.comment = "Movie Name (2020).mkv"
    title1.title = "Movie Name"
    title1.index = 1
    title1.order_index = 1

    title2 = Mock()
    title2.id = str(uuid.uuid4())
    title2.source_file = "title_002.mkv"  # match MKV filenames written in raw/
    title2.comment = "Movie Name (2020) - Extra.mkv"
    title2.title = "Extra Feature"
    title2.index = 2
    title2.order_index = 2
    
    mock_query = Mock()
    mock_query.filter.return_value.all.return_value = [title1, title2]
    db.query.return_value = mock_query
    
    return {
        "job": job,
        "db": db,
        "paths": paths,
        "job_id": job_id,
    }


class TestFullPipelineIntegration:
    """Test complete pipeline validation flow."""
    
    def test_rip_to_finalize_validation_flow(self, full_pipeline_setup):
        """Test validation flow from rip through finalize."""
        setup = full_pipeline_setup
        job = setup["job"]
        db = setup["db"]
        paths = setup["paths"]
        
        # Stage 1: Rip - Create source files
        raw_dir = paths.raw
        file1 = raw_dir / "title_001.mkv"
        file2 = raw_dir / "title_002.mkv"
        file1.write_bytes(b"mkv content 1" * 1000)
        file2.write_bytes(b"mkv content 2" * 1000)
        
        # Get title_ids from mock DB
        disc_titles = db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        title2_id = str(disc_titles[1].id)
        
        # Calculate and store hashes (simulating rip completion)
        # ripped_files uses title_id keys; source_hashes uses file names for validate_rip_output
        ripped_files = {
            title1_id: "raw/title_001.mkv",
            title2_id: "raw/title_002.mkv",
        }
        source_hashes = {
            "title_001.mkv": calculate_file_hash(file1),
            "title_002.mkv": calculate_file_hash(file2),
        }
        source_files = {
            title1_id: "raw/title_001.mkv",
            title2_id: "raw/title_002.mkv",
        }
        
        # Create metadata files
        metadata_dir = paths.metadata
        (metadata_dir / "makemkv.log").write_text("LOG: Rip completed")
        (metadata_dir / "titles_map.json").write_text(json.dumps({"1": {"file": "title_001.mkv"}}))
        
        job.ripped_files = ripped_files
        job.disc_payload = {
            "ripped_files": ripped_files,
            "source_hashes": source_hashes,
            "source_files": source_files,
        }
        
        # Validate rip output
        rip_result = validate_rip_output(job, db, paths)
        assert rip_result.valid is True, f"Rip validation failed: {rip_result.errors}"
        
        # Stage 2: Finalize - Create finalize files
        finalize_dir = paths.finalize
        disc_json = {
            "Index": 1,
            "Slug": "disc01",
            "Name": "Test Disc",
            "Titles": []
        }
        (finalize_dir / "disc01.json").write_text(json.dumps(disc_json))
        (finalize_dir / "disc01.txt").write_text("Disc info")
        (finalize_dir / "disc01-summary.txt").write_text("Summary")
        
        # Validate finalize output
        finalize_result = validate_finalize_output(job, db, paths)
        assert finalize_result.valid is True, f"Finalize validation failed: {finalize_result.errors}"
    
    def test_postprocess_validation_with_hash_verification(self, full_pipeline_setup):
        """Test post-process validation with hash verification."""
        setup = full_pipeline_setup
        job = setup["job"]
        db = setup["db"]
        paths = setup["paths"]
        
        # Get title_ids from mock DB
        disc_titles = db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        title2_id = str(disc_titles[1].id)
        
        # Set up source files with hashes
        raw_dir = paths.raw
        file1 = raw_dir / "title_001.mkv"
        file2 = raw_dir / "title_002.mkv"
        file1.write_bytes(b"mkv content 1" * 1000)
        file2.write_bytes(b"mkv content 2" * 1000)
        
        # Build source_hashes using source_file keys (as stored during rip)
        source_hashes = {
            disc_titles[0].source_file: calculate_file_hash(file1),
            disc_titles[1].source_file: calculate_file_hash(file2),
        }
        
        # Simulate post-process: move files to transient
        transient_dir = paths.transient
        movies_dir = transient_dir / "Movies" / "Movie Name (2020)"
        movies_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy files (simulating rename/move)
        dest1 = movies_dir / "Movie Name (2020).mkv"
        other_dir = movies_dir / "Other"
        other_dir.mkdir(parents=True, exist_ok=True)
        dest2 = other_dir / "Movie Name (2020) - Extra.mkv"
        # Copy file1 to dest1 (title_001 -> Movie Name)
        shutil.copy2(file1, dest1)
        # Copy file2 to dest2 (title_002 -> Extra)
        shutil.copy2(file2, dest2)
        
        # Set up post_paths mapping (title_id keys) and mkv_size (validate_transfer_prep_output checks size)
        post_paths = {
            title1_id: "Movies/Movie Name (2020)/Movie Name (2020).mkv",
            title2_id: "Movies/Movie Name (2020)/Other/Movie Name (2020) - Extra.mkv",
        }
        disc_titles[0].mkv_size = dest1.stat().st_size
        disc_titles[1].mkv_size = dest2.stat().st_size
        
        job.post_paths = post_paths
        job.disc_payload = {
            "post_paths": post_paths,
            "source_hashes": source_hashes,
        }
        
        # Validate post-process output (file existence and size vs mkv_size)
        result = validate_transfer_prep_output(job, db, paths)
        assert result.valid is True, f"Post-process validation failed: {result.errors}"
        assert result.details["files_found"] == 2
        assert len(result.details["size_mismatches"]) == 0
    
    def test_transfer_validation_flow(self, full_pipeline_setup, tmp_path):
        """Test transfer validation flow."""
        setup = full_pipeline_setup
        job = setup["job"]
        db = setup["db"]
        
        # Get title_ids from mock DB
        disc_titles = db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        title2_id = str(disc_titles[1].id)
        
        # Set up source files in transient (after post-process)
        transient_dir = setup["paths"].transient
        movies_dir = transient_dir / "Movies" / "Movie Name (2020)"
        movies_dir.mkdir(parents=True, exist_ok=True)
        
        file1 = movies_dir / "Movie Name (2020).mkv"
        other_dir = movies_dir / "Other"
        other_dir.mkdir(parents=True, exist_ok=True)
        file2 = other_dir / "Movie Name (2020) - Extra.mkv"
        file1.write_bytes(b"mkv content 1" * 1000)
        file2.write_bytes(b"mkv content 2" * 1000)
        
        # Calculate hashes from source files (before transfer) - using title_id keys
        final_hashes = {
            title1_id: calculate_file_hash(file1),
            title2_id: calculate_file_hash(file2),
        }
        
        # Simulate transfer: copy to destination
        dest_root = tmp_path / "transfer_destination"
        dest_movies = dest_root / "Movies" / "Movie Name (2020)"
        dest_movies.mkdir(parents=True, exist_ok=True)
        
        # Copy files to destination (hashes should remain same)
        shutil.copy2(file1, dest_movies / "Movie Name (2020).mkv")
        dest_other = dest_movies / "Other"
        dest_other.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file2, dest_other / "Movie Name (2020) - Extra.mkv")
        
        # post_paths uses title_id keys
        post_paths = {
            title1_id: "Movies/Movie Name (2020)/Movie Name (2020).mkv",
            title2_id: "Movies/Movie Name (2020)/Other/Movie Name (2020) - Extra.mkv",
        }
        
        job.post_paths = post_paths
        job.disc_payload = {
            "post_paths": post_paths,
            "final_hashes": final_hashes,
            "source_hashes": {
                disc_titles[0].source_file: final_hashes[title1_id],
                disc_titles[1].source_file: final_hashes[title2_id],
            },
        }
        
        # Validate transfer output
        result = validate_transfer_output(job, db, dest_root)
        assert result.valid is True, f"Transfer validation failed: {result.errors}"
        assert result.details["files_found"] == 2
        assert result.details["files_validated"] == 2
    
    def test_hash_verification_across_stages(self, full_pipeline_setup):
        """Test that hashes remain consistent across rename/move operations."""
        setup = full_pipeline_setup
        job = setup["job"]
        db = setup["db"]
        paths = setup["paths"]
        
        # Create source file
        raw_dir = paths.raw
        source_file = raw_dir / "title_001.mkv"
        source_content = b"mkv content for hash test" * 1000
        source_file.write_bytes(source_content)
        
        # Calculate source hash
        source_hash = calculate_file_hash(source_file)
        
        # Move to post-process location (simulating rename)
        transient_dir = paths.transient
        movies_dir = transient_dir / "Movies" / "Movie Name (2020)"
        movies_dir.mkdir(parents=True, exist_ok=True)
        dest_file = movies_dir / "Movie Name (2020).mkv"
        shutil.copy2(source_file, dest_file)
        
        # Hash should be the same after move
        dest_hash = calculate_file_hash(dest_file)
        assert source_hash == dest_hash, "Hash should remain constant after file move"
        
        # Get title_id from mock DB
        disc_titles = db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        
        # Set up payload with post_paths (title_id keys) and mkv_size (validate_transfer_prep_output checks size)
        post_paths = {
            title1_id: "Movies/Movie Name (2020)/Movie Name (2020).mkv"
        }
        disc_titles[0].mkv_size = dest_file.stat().st_size
        job.post_paths = post_paths
        job.disc_payload = {
            "post_paths": post_paths,
            "source_hashes": {disc_titles[0].source_file: source_hash},
        }
        
        # Validate - file exists and size matches mkv_size
        result = validate_transfer_prep_output(job, db, paths)
        assert result.valid is True, f"Post-process validation failed: {result.errors}"
        assert result.details["files_found"] == 1


class TestValidationFailureScenarios:
    """Test validation failure scenarios and error reporting."""
    
    def test_missing_files_at_each_stage(self, full_pipeline_setup):
        """Test validation detects missing files at each stage."""
        setup = full_pipeline_setup
        job = setup["job"]
        db = setup["db"]
        paths = setup["paths"]
        
        # Rip validation - no files
        job.disc_payload = {"source_hashes": {}, "source_files": {}}
        result = validate_rip_output(job, db, paths)
        assert result.valid is False
        assert any("Missing" in err for err in result.errors)
        
        # Finalize validation - no JSON file
        result = validate_finalize_output(job, db, paths)
        assert result.valid is False
        assert any("Missing" in err or "JSON" in err for err in result.errors)
        
        # Post-process validation - files don't exist
        # Get title_id from mock DB
        disc_titles = db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        
        post_paths = {
            title1_id: "Movies/Missing/File.mkv"
        }
        job.post_paths = post_paths
        job.disc_payload = {
            "post_paths": post_paths,
            "source_hashes": {disc_titles[0].source_file: "hash1"},
        }
        result = validate_transfer_prep_output(job, db, paths)
        assert result.valid is False
        assert any("not found" in err.lower() for err in result.errors)
    
    def test_size_mismatch_detection_postprocess(self, full_pipeline_setup):
        """Size mismatch vs mkv_size is recorded as warning + details, not a hard failure."""
        setup = full_pipeline_setup
        job = setup["job"]
        db = setup["db"]
        paths = setup["paths"]
        
        # Get title_id from mock DB
        disc_titles = db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        
        # Create file in transient
        transient_dir = paths.transient
        movies_dir = transient_dir / "Movies" / "Movie Name (2020)"
        movies_dir.mkdir(parents=True, exist_ok=True)
        file1 = movies_dir / "Movie Name (2020).mkv"
        file1.write_bytes(b"different content" * 1000)
        actual_size = file1.stat().st_size
        
        # Set mkv_size to a wrong value so size validation fails
        disc_titles[0].mkv_size = 99999
        post_paths = {
            title1_id: "Movies/Movie Name (2020)/Movie Name (2020).mkv"
        }
        job.post_paths = post_paths
        job.disc_payload = {
            "post_paths": post_paths,
            "source_hashes": {disc_titles[0].source_file: "any"},
        }
        
        result = validate_transfer_prep_output(job, db, paths)
        assert result.valid is True
        assert any("Size mismatch" in w for w in result.warnings)
        assert len(result.details["size_mismatches"]) > 0
        assert result.details["size_mismatches"][0]["expected"] == 99999
        assert result.details["size_mismatches"][0]["actual"] == actual_size

