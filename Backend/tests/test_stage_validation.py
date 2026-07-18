"""
Unit tests for stage validation functions.
Tests validation logic without requiring actual disc ripping.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock
from sqlalchemy.orm import Session

from core.stage_validation import (
    ValidationResult,
    generate_expected_rip_output,
    validate_rip_output,
    generate_expected_finalize_output,
    validate_finalize_output,
    generate_expected_transfer_prep_output,
    validate_transfer_prep_output,
    generate_expected_transfer_output,
    validate_transfer_output,
)
from core.job_paths import JobPaths
from core.transfer.validation import calculate_file_hash


@pytest.fixture
def mock_job(tmp_path):
    """Create a mock job object for testing."""
    import uuid
    job = Mock()
    job.id = "test-job-123"
    job.disc_payload = {}
    job.ripped_files = None
    job.post_paths = None
    job.disc = Mock()
    job.disc.id = "test-disc-123"
    job.disc.disc_number = 1
    job.disc.disc_slug = "disc01"
    return job


@pytest.fixture
def mock_db(mock_job):
    """Create a mock database session with disc_titles."""
    import uuid
    db = Mock(spec=Session)
    
    # Mock DiscTitle objects with title_id
    # source_file matches expected MKV filenames in raw/ (stage_validation uses source_file for expected raw files)
    title1 = Mock()
    title1.id = str(uuid.uuid4())
    title1.source_file = "title_001.mkv"
    title1.comment = "Movie Name (2020).mkv"
    title1.title = "Movie Name"
    title1.index = 1
    title1.order_index = 1
    title1.mkv_size = None  # Tests set as needed for postprocess size validation
    
    title2 = Mock()
    title2.id = str(uuid.uuid4())
    title2.source_file = "title_002.mkv"
    title2.comment = "Movie Name (2020) - Extra.mkv"
    title2.title = "Extra Feature"
    title2.index = 2
    title2.order_index = 2
    title2.mkv_size = None
    
    disc_titles = [title1, title2]
    
    # Mock query
    mock_query = Mock()
    mock_query.filter.return_value.all.return_value = disc_titles
    db.query.return_value = mock_query
    
    return db


@pytest.fixture
def job_paths(tmp_path, mock_job):
    """Create JobPaths structure for testing."""
    paths = JobPaths(tmp_path, mock_job.id)
    paths.ensure_layout()
    return paths


@pytest.fixture
def sample_source_files(job_paths):
    """Create sample source MKV files in raw/ directory."""
    raw_dir = job_paths.raw
    
    # Create sample MKV files
    file1 = raw_dir / "title_001.mkv"
    file1.write_bytes(b"fake mkv content 1" * 100)
    
    file2 = raw_dir / "title_002.mkv"
    file2.write_bytes(b"fake mkv content 2" * 100)
    
    return {"title_001.mkv": file1, "title_002.mkv": file2}


@pytest.fixture
def sample_metadata_files(job_paths):
    """Create sample metadata files."""
    metadata_dir = job_paths.metadata
    
    # Create log files
    (metadata_dir / "makemkv.log").write_text("LOG: Rip completed")
    (metadata_dir / "makemkv_info.log").write_text("INFO: Disc scanned")
    
    # Create metadata files
    (metadata_dir / "titles_map.json").write_text(json.dumps({"1": {"file": "title_001.mkv"}}))
    (metadata_dir / "disc_info.json").write_text(json.dumps({"disc_hash": "test123"}))
    
    return metadata_dir


@pytest.fixture
def sample_finalize_files(job_paths, mock_job):
    """Create sample finalize output files."""
    finalize_dir = job_paths.finalize
    
    # Create disc JSON file
    disc_json = {
        "Index": mock_job.disc.disc_number,
        "Slug": mock_job.disc.disc_slug,
        "Name": "Test Disc",
        "Titles": []
    }
    (finalize_dir / f"{mock_job.disc.disc_slug}.json").write_text(json.dumps(disc_json))
    
    # Create TXT and summary files
    (finalize_dir / f"{mock_job.disc.disc_slug}.txt").write_text("Disc info")
    (finalize_dir / f"{mock_job.disc.disc_slug}-summary.txt").write_text("Summary")
    
    return finalize_dir


@pytest.fixture
def sample_postprocess_files(job_paths):
    """Create sample post-processed files in transient/."""
    transient_dir = job_paths.transient
    movies_dir = transient_dir / "Movies" / "Movie Name (2020)"
    movies_dir.mkdir(parents=True, exist_ok=True)
    
    # Create moved files
    file1 = movies_dir / "Movie Name (2020).mkv"
    file1.write_bytes(b"fake mkv content 1" * 100)
    
    other_dir = movies_dir / "Other"
    other_dir.mkdir(parents=True, exist_ok=True)
    file2 = other_dir / "Movie Name (2020) - Extra.mkv"
    file2.write_bytes(b"fake mkv content 2" * 100)
    
    return transient_dir


class TestValidationResult:
    """Test ValidationResult dataclass."""
    
    def test_validation_result_creation(self):
        """Test creating a ValidationResult."""
        result = ValidationResult(valid=True, errors=[], warnings=[], details={})
        assert result.valid is True
        assert result.errors == []
        assert result.warnings == []
        assert result.details == {}
    
    def test_validation_result_with_errors(self):
        """Test ValidationResult with errors."""
        result = ValidationResult(
            valid=False,
            errors=["File not found", "Hash mismatch"],
            warnings=["Unexpected file"],
            details={"file_count": 1}
        )
        assert result.valid is False
        assert len(result.errors) == 2
        assert len(result.warnings) == 1
        assert result.details["file_count"] == 1


class TestRipStageValidation:
    """Test rip stage validation."""
    
    def test_generate_expected_rip_output(self, mock_job, mock_db):
        """Test expected output generation for rip stage."""
        expected = generate_expected_rip_output(mock_job, mock_db)
        
        assert "raw_files" in expected
        assert "log_files" in expected
        assert "metadata_files" in expected
        assert "title_001.mkv" in expected["raw_files"]
        assert "title_002.mkv" in expected["raw_files"]
    
    def test_validate_rip_output_success(
        self, mock_job, mock_db, job_paths, sample_source_files, sample_metadata_files
    ):
        """Test successful rip validation."""
        # Add source hashes to disc_payload
        source_hashes = {}
        source_files = {}
        for filename, filepath in sample_source_files.items():
            source_hashes[filename] = calculate_file_hash(filepath)
            source_files[filename] = f"raw/{filename}"
        
        mock_job.disc_payload = {
            "source_hashes": source_hashes,
            "source_files": source_files,
        }
        
        result = validate_rip_output(mock_job, mock_db, job_paths)
        
        assert result.valid is True
        assert len(result.errors) == 0
        assert result.details["hashes_stored"] == 2
    
    def test_validate_rip_output_missing_files(
        self, mock_job, mock_db, job_paths, sample_metadata_files
    ):
        """Test rip validation when files are missing."""
        mock_job.disc_payload = {
            "source_hashes": {},
            "source_files": {},
        }
        
        result = validate_rip_output(mock_job, mock_db, job_paths)
        
        assert result.valid is False
        assert len(result.errors) > 0
        assert any("Missing" in err for err in result.errors)
    
    def test_validate_rip_output_missing_hashes(
        self, mock_job, mock_db, job_paths, sample_source_files
    ):
        """Test rip validation when hashes are missing."""
        mock_job.disc_payload = {
            "source_hashes": {},
            "source_files": {},
        }
        
        result = validate_rip_output(mock_job, mock_db, job_paths)
        
        assert result.valid is False
        assert any("hashes not stored" in err.lower() for err in result.errors)
    
    def test_validate_rip_output_zero_size_file(
        self, mock_job, mock_db, job_paths, sample_source_files
    ):
        """Test rip validation detects zero-size files."""
        # Create a zero-size file
        zero_file = job_paths.raw / "title_003.mkv"
        zero_file.write_bytes(b"")
        
        source_hashes = {}
        source_files = {}
        for filename, filepath in sample_source_files.items():
            source_hashes[filename] = calculate_file_hash(filepath)
            source_files[filename] = f"raw/{filename}"
        
        mock_job.disc_payload = {
            "source_hashes": source_hashes,
            "source_files": source_files,
        }
        
        result = validate_rip_output(mock_job, mock_db, job_paths)
        
        # Zero-size files should be detected as errors (corrupted files)
        assert result.valid is False, "Zero-size files should be detected as errors"
        assert any("zero size" in err.lower() or "corrupted" in err.lower() for err in result.errors)


class TestFinalizeStageValidation:
    """Test finalize disc stage validation."""
    
    def test_generate_expected_finalize_output(self, mock_job, mock_db):
        """Test expected output generation for finalize stage."""
        expected = generate_expected_finalize_output(mock_job, mock_db)
        
        assert "disc_json_files" in expected
        assert "disc_txt_files" in expected
        assert "disc_summary_files" in expected
        assert "disc01.json" in expected["disc_json_files"]
    
    def test_validate_finalize_output_success(
        self, mock_job, mock_db, job_paths, sample_finalize_files
    ):
        """Test successful finalize validation."""
        result = validate_finalize_output(mock_job, mock_db, job_paths)
        
        assert result.valid is True
        assert len(result.errors) == 0
    
    def test_validate_finalize_output_missing_json(
        self, mock_job, mock_db, job_paths
    ):
        """Test finalize validation when JSON is missing."""
        # Create only TXT file, missing JSON
        finalize_dir = job_paths.finalize
        (finalize_dir / "disc01.txt").write_text("Disc info")
        
        result = validate_finalize_output(mock_job, mock_db, job_paths)
        
        assert result.valid is False
        assert any("Missing" in err and "JSON" in err for err in result.errors)
    
    def test_validate_finalize_output_invalid_json(
        self, mock_job, mock_db, job_paths
    ):
        """Test finalize validation with invalid JSON."""
        finalize_dir = job_paths.finalize
        (finalize_dir / "disc01.json").write_text("invalid json content")
        
        result = validate_finalize_output(mock_job, mock_db, job_paths)
        
        assert result.valid is False
        assert any("Invalid JSON" in err or "JSON" in err for err in result.errors)


class TestTransferPrepStageValidation:
    """Test post-process stage validation."""
    
    def test_generate_expected_transfer_prep_output(self, mock_job, mock_db):
        """Test expected output generation for post-process stage."""
        import uuid
        # Get title_ids from mock_db
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        title2_id = str(disc_titles[1].id)
        
        # Set up job with post_paths (title_id keys) and source_hashes
        mock_job.post_paths = {
            title1_id: "Movies/Movie Name (2020)/Movie Name (2020).mkv",
            title2_id: "Movies/Movie Name (2020)/Other/Movie Name (2020) - Extra.mkv",
        }
        mock_job.disc_payload = {
            "post_paths": mock_job.post_paths,
            "source_hashes": {
                disc_titles[0].source_file: "hash1",
                disc_titles[1].source_file: "hash2",
            },
        }
        
        expected = generate_expected_transfer_prep_output(mock_job, mock_db)
        
        assert "expected_files" in expected
        assert "expected_hashes" in expected
        assert "expected_sizes" in expected
        assert len(expected["expected_files"]) == 2
        assert len(expected["expected_hashes"]) == 2
        # Verify keys are title_ids
        for key in expected["expected_files"].keys():
            assert len(key) == 36 and '-' in key, f"Expected UUID format, got {key}"

    def test_generate_expected_transfer_prep_output_skips_ignore_by_title_id(
        self, mock_job, mock_db
    ):
        """Stale post_paths key for an ignored title_id must not appear in expected_files."""
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        title2_id = str(disc_titles[1].id)
        disc_titles[0].type = "ignore"
        disc_titles[1].type = "MainMovie"

        mock_job.post_paths = {
            title1_id: "Movies/stale-ignored.mkv",
            title2_id: "Movies/keep.mkv",
        }
        mock_job.disc_payload = {
            "post_paths": mock_job.post_paths,
            "source_hashes": {},
        }

        expected = generate_expected_transfer_prep_output(mock_job, mock_db)

        assert title1_id not in expected["expected_files"]
        assert title2_id in expected["expected_files"]
    
    def test_validate_transfer_prep_output_success(
        self, mock_job, mock_db, job_paths, sample_postprocess_files
    ):
        """Test successful post-process validation."""
        # Get title_ids from mock_db
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        title2_id = str(disc_titles[1].id)
        
        # Calculate hashes for source files (before they were moved)
        # In real scenario, these would be calculated from raw/ before move
        # For test, we calculate from the moved files (content is same)
        source_hashes = {}
        movies_dir = sample_postprocess_files / "Movies" / "Movie Name (2020)"
        file1_path = movies_dir / "Movie Name (2020).mkv"
        file2_path = movies_dir / "Other" / "Movie Name (2020) - Extra.mkv"
        
        # Build post_paths and set mkv_size to match actual file sizes (size-based validation)
        post_paths = {}
        if file1_path.exists():
            source_hashes[disc_titles[0].source_file] = calculate_file_hash(file1_path)
            post_paths[title1_id] = "Movies/Movie Name (2020)/Movie Name (2020).mkv"
            disc_titles[0].mkv_size = file1_path.stat().st_size
        if file2_path.exists():
            source_hashes[disc_titles[1].source_file] = calculate_file_hash(file2_path)
            post_paths[title2_id] = "Movies/Movie Name (2020)/Other/Movie Name (2020) - Extra.mkv"
            disc_titles[1].mkv_size = file2_path.stat().st_size

        mock_job.post_paths = post_paths
        mock_job.disc_payload = {
            "post_paths": post_paths,
            "source_hashes": source_hashes,
        }
        
        result = validate_transfer_prep_output(mock_job, mock_db, job_paths)
        
        assert result.valid is True
        assert result.details["files_found"] == 2
    
    def test_validate_transfer_prep_output_size_mismatch(
        self, mock_job, mock_db, job_paths, sample_postprocess_files
    ):
        """Size vs mkv_size mismatch is recorded but does not fail validation when enforcement is off."""
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        file1_path = sample_postprocess_files / "Movies" / "Movie Name (2020)" / "Movie Name (2020).mkv"
        
        post_paths = {title1_id: "Movies/Movie Name (2020)/Movie Name (2020).mkv"}
        # Set mkv_size to a wrong value so it won't match actual file size
        disc_titles[0].mkv_size = 99999
        mock_job.post_paths = post_paths
        mock_job.disc_payload = {"post_paths": post_paths, "source_hashes": {}}
        
        result = validate_transfer_prep_output(mock_job, mock_db, job_paths)
        
        assert result.valid is True
        assert not any("Size mismatch" in err for err in result.errors)
        assert any("Size mismatch" in w and "not enforced" in w for w in result.warnings)
        assert len(result.details["size_mismatches"]) >= 1
    
    def test_validate_transfer_prep_output_missing_files(
        self, mock_job, mock_db, job_paths
    ):
        """Test post-process validation when files are missing."""
        # Get title_ids from mock_db
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        
        post_paths = {
            title1_id: "Movies/Movie Name (2020)/Missing File.mkv",
        }
        mock_job.post_paths = post_paths
        mock_job.disc_payload = {
            "post_paths": post_paths,
            "source_hashes": {
                disc_titles[0].source_file: "hash1",
            },
        }
        
        result = validate_transfer_prep_output(mock_job, mock_db, job_paths)
        
        assert result.valid is False
        assert any("not found" in err.lower() for err in result.errors)


class TestTransferStageValidation:
    """Test transfer stage validation."""
    
    def test_generate_expected_transfer_output(self, mock_job, mock_db):
        """Test expected output generation for transfer stage."""
        # Get title_ids from mock_db
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        
        post_paths = {
            title1_id: "Movies/Movie Name (2020)/Movie Name (2020).mkv",
        }
        mock_job.post_paths = post_paths
        mock_job.disc_payload = {
            "post_paths": post_paths,
            "final_hashes": {
                title1_id: "hash1",
            },
            "source_hashes": {
                disc_titles[0].source_file: "hash1",
            },
        }
        
        expected = generate_expected_transfer_output(mock_job, mock_db)
        
        assert "expected_files" in expected
        assert "expected_hashes" in expected
        assert len(expected["expected_files"]) == 1

    def test_generate_expected_transfer_output_skips_ignore_title_id(
        self, mock_job, mock_db
    ):
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        title2_id = str(disc_titles[1].id)
        disc_titles[0].type = "ignore"
        disc_titles[1].type = "MainMovie"

        post_paths = {
            title1_id: "Movies/ignored.mkv",
            title2_id: "Movies/keep.mkv",
        }
        mock_job.post_paths = post_paths
        mock_job.disc_payload = {
            "post_paths": post_paths,
            "final_hashes": {title2_id: "hash2"},
        }

        expected = generate_expected_transfer_output(mock_job, mock_db)

        assert title1_id not in expected["expected_files"]
        assert title2_id in expected["expected_files"]
        assert title1_id not in expected.get("expected_hashes", {})
    
    def test_validate_transfer_output_success(
        self, mock_job, mock_db, tmp_path, sample_postprocess_files
    ):
        """Test successful transfer validation."""
        # Get title_ids from mock_db
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        title2_id = str(disc_titles[1].id)
        
        # Copy files to destination (simulating transfer)
        dest_root = tmp_path / "transfer_dest"
        dest_movies = dest_root / "Movies" / "Movie Name (2020)"
        dest_movies.mkdir(parents=True, exist_ok=True)
        
        # Copy files
        import shutil
        for file in sample_postprocess_files.rglob("*.mkv"):
            rel_path = file.relative_to(sample_postprocess_files)
            dest_file = dest_root / rel_path
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dest_file)
        
        # Calculate hashes and build post_paths with title_id keys
        post_paths = {}
        final_hashes = {}
        source_hashes = {}
        file1_path = dest_movies / "Movie Name (2020).mkv"
        file2_path = dest_movies / "Other" / "Movie Name (2020) - Extra.mkv"
        
        if file1_path.exists():
            hash_val = calculate_file_hash(file1_path)
            post_paths[title1_id] = "Movies/Movie Name (2020)/Movie Name (2020).mkv"
            final_hashes[title1_id] = hash_val
            source_hashes[disc_titles[0].source_file] = hash_val
        
        if file2_path.exists():
            hash_val = calculate_file_hash(file2_path)
            post_paths[title2_id] = "Movies/Movie Name (2020)/Other/Movie Name (2020) - Extra.mkv"
            final_hashes[title2_id] = hash_val
            source_hashes[disc_titles[1].source_file] = hash_val
        
        mock_job.post_paths = post_paths
        mock_job.disc_payload = {
            "post_paths": post_paths,
            "final_hashes": final_hashes,
            "source_hashes": source_hashes,
        }
        
        result = validate_transfer_output(mock_job, mock_db, dest_root)
        
        assert result.valid is True
        assert result.details["files_found"] == 2
    
    def test_validate_transfer_output_hash_mismatch(
        self, mock_job, mock_db, tmp_path
    ):
        """Test transfer validation detects hash mismatches."""
        # Get title_ids from mock_db
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        
        # Create destination with files
        dest_root = tmp_path / "transfer_dest"
        dest_file = dest_root / "Movies" / "Movie Name (2020)" / "Movie Name (2020).mkv"
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(b"different content")
        
        post_paths = {
            title1_id: "Movies/Movie Name (2020)/Movie Name (2020).mkv",
        }
        mock_job.post_paths = post_paths
        mock_job.disc_payload = {
            "post_paths": post_paths,
            "final_hashes": {
                title1_id: "wrong_hash_that_doesnt_match",
            },
            "source_hashes": {
                disc_titles[0].source_file: "wrong_hash_that_doesnt_match",
            },
        }
        
        result = validate_transfer_output(mock_job, mock_db, dest_root)
        
        assert result.valid is False
        assert any("Hash mismatch" in err for err in result.errors)
    
    def test_validate_transfer_output_missing_files(
        self, mock_job, mock_db, tmp_path
    ):
        """Test transfer validation when files are missing."""
        # Get title_ids from mock_db
        disc_titles = mock_db.query.return_value.filter.return_value.all.return_value
        title1_id = str(disc_titles[0].id)
        
        dest_root = tmp_path / "transfer_dest"
        dest_root.mkdir()
        
        post_paths = {
            title1_id: "Movies/Movie Name (2020)/Missing.mkv",
        }
        mock_job.post_paths = post_paths
        mock_job.disc_payload = {
            "post_paths": post_paths,
            "final_hashes": {
                title1_id: "hash1",
            },
        }
        
        result = validate_transfer_output(mock_job, mock_db, dest_root)
        
        assert result.valid is False
        assert any("not found" in err.lower() for err in result.errors)

