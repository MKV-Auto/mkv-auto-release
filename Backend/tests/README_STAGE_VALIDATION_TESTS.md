# Stage Validation Test Bed

This test bed provides comprehensive testing for the stage validation and progress tracking system without requiring actual disc ripping.

## ⚠️ IMPORTANT: Test Maintenance

**Before modifying any code in `core/stage_validation.py` or related validation functions in `workers/tasks.py`, please read:**

- **[TEST_MAINTENANCE_GUIDE.md](./TEST_MAINTENANCE_GUIDE.md)** - Complete guide on updating tests when code changes
- **`TEST_COVERAGE_SUMMARY.md` (historical)** - Overview of what's tested

**Rule**: When you modify a function, you MUST update its corresponding tests to reflect new inputs, outputs, or behavior. Function docstrings include test references (e.g., `Tests: tests/test_*.py::TestClass`).

## Test Files

### 1. `test_stage_validation.py`
**Unit tests for validation functions**

Tests individual validation functions in isolation:
- `ValidationResult` dataclass
- `generate_expected_*_output()` functions (rip, finalize, post-process, transfer)
- `validate_*_output()` functions for each stage
- Error detection (missing files, hash mismatches, invalid JSON, etc.)
- Success scenarios with proper file structures

**Key Features:**
- Uses mocked job and database objects
- Creates temporary file structures using pytest fixtures
- Tests validation logic without external dependencies

### 2. `test_stage_validation_integration.py`
**Integration tests for full pipeline**

Tests the complete workflow across stages:
- Rip → Finalize validation flow
- Post-process validation with hash verification
- Transfer validation flow
- Hash verification across rename/move operations
- Validation failure scenarios

**Key Features:**
- Simulates complete pipeline from rip through transfer
- Tests hash consistency across file operations
- Validates error detection and reporting

### 3. `test_hash_calculation_storage.py`
**Hash calculation and storage tests**

Tests the hash calculation and storage mechanism:
- Hash calculation consistency
- Hash storage structure in `disc_payload`
- Hash verification after file moves
- Integration with rip completion workflow

**Key Features:**
- Tests hash calculation accuracy
- Validates hash storage format
- Ensures hashes remain consistent after file operations

### 4. `test_gather_final_outputs_cached_hashes.py`
**Tests for gather_final_outputs cached_hashes parameter**

Tests the hash caching functionality:
- Using cached hashes instead of recalculating
- Calculating hashes when cache is not provided
- Partial cache handling (some files cached, some not)
- Both method and module-level function versions

**Key Features:**
- Verifies hash_file is not called when cached_hashes provided
- Tests fallback to calculation when cache missing
- Ensures correctness of cached vs calculated hashes

### 5. `test_rip_hash_integration.py`
**Tests for hash calculation integration in rip_disc**

Tests the hash calculation logic at end of rip:
- Hash storage in `disc_payload` structure
- Handling of missing files
- Files in subdirectories
- Error handling

**Key Features:**
- Tests the actual integration pattern used in rip_disc
- Validates disc_payload structure format
- Tests edge cases (missing files, subdirectories)

### 6. `test_postprocess_progress_tracking.py`
**Tests for post-process progress tracking**

Tests progress callback functionality:
- Rename progress (0-50%)
- Hash verification progress (50-100%)
- Combined rename + hash progress
- Edge cases (zero files, files already moved)

**Key Features:**
- Tests progress calculation logic
- Verifies monotonic progress updates
- Tests different scenarios (with/without rename phase)

### 7. `test_rollback_on_validation_failure.py`
**Tests for rollback functionality when validation fails**

Tests the rollback/checkpoint system:
- Checkpoint creation before stages
- Validation failure detection at each stage
- File backup and restore functionality
- Directory structure preservation during restore
- Rollback infrastructure availability

**Key Features:**
- Tests that validation failures are properly detected (triggers rollback)
- Verifies checkpoint/backup functions exist and are callable
- Tests file backup/restore without requiring actual disc ripping
- Validates rollback infrastructure is in place for all stages

### 8. `test_e2e_api_endpoints.py`
**End-to-end API endpoint tests for the full pipeline**

Tests each pipeline stage via HTTP API endpoints using FastAPI TestClient:
- Rip stage completion via `POST /jobs/rip`
- Label stage via `POST /disc/{disc_id}/label`
- Finalize stage via `POST /disc/{disc_id}/finalize`
- Post-process stage via `POST /jobs/{job_id}/postprocess` (comprehensive verification)
- Transfer stage via `POST /jobs/{job_id}/transfer`
- Full pipeline from rip through post-process

**Key Features:**
- Tests via HTTP endpoints (realistic API usage)
- Comprehensive completion verification (state + files + progress + hashes)
- Post-processing completion tracking (detailed progress monitoring)
- Partial completion recovery testing
- Failure detection and error handling
- Uses mocked Celery tasks (executes synchronously)
- No actual disc ripping required

**Completion Verification:**
- Job state transitions (`pending -> running -> completed`)
- File system validation (files exist at expected locations)
- Hash verification (file integrity)
- Progress tracking (progress reaches 100%)
- Payload verification (`disc_payload` fields populated)

## Running the Tests

### Prerequisites
```bash
cd Backend
source .venv/bin/activate  # or your virtual environment
pip install pytest pytest-asyncio
```

### Run All Stage Validation Tests
```bash
# Run all validation-related tests
pytest tests/test_stage_validation*.py tests/test_hash_calculation*.py tests/test_gather_final_outputs*.py tests/test_rip_hash*.py tests/test_postprocess_progress*.py tests/test_rollback*.py tests/test_e2e*.py -v

# Or use a pattern
pytest tests/test_*validation*.py tests/test_*hash*.py tests/test_*progress*.py tests/test_*rollback*.py tests/test_e2e*.py -v
```

### Run Specific Test File
```bash
# Unit tests only
pytest tests/test_stage_validation.py -v

# Integration tests only
pytest tests/test_stage_validation_integration.py -v

# Hash calculation tests only
pytest tests/test_hash_calculation_storage.py -v

# Cached hashes functionality
pytest tests/test_gather_final_outputs_cached_hashes.py -v

# Rip hash integration
pytest tests/test_rip_hash_integration.py -v

# Progress tracking
pytest tests/test_postprocess_progress_tracking.py -v

# Rollback on validation failure
pytest tests/test_rollback_on_validation_failure.py -v

# E2E API endpoint tests
pytest tests/test_e2e_api_endpoints.py -v
```

### Run Specific Test Class or Test Function
```bash
# Run all tests in a class
pytest tests/test_stage_validation.py::TestRipStageValidation -v

# Run a specific test
pytest tests/test_stage_validation.py::TestRipStageValidation::test_validate_rip_output_success -v
```

### Run with Coverage
```bash
pytest tests/test_stage_validation*.py --cov=core.stage_validation --cov-report=html
```

## Test Structure

### Fixtures

The tests use pytest fixtures to set up test environments:

- **`mock_job`**: Creates a mock job object with disc information
- **`mock_db`**: Creates a mock database session with disc_titles
- **`job_paths`**: Sets up the JobPaths directory structure
- **`sample_source_files`**: Creates sample MKV files in `raw/` directory
- **`sample_metadata_files`**: Creates log and metadata files
- **`sample_finalize_files`**: Creates finalize output files (JSON, TXT)
- **`sample_postprocess_files`**: Creates post-processed files in `transient/`
- **`full_pipeline_setup`**: Complete setup for integration tests
- **`mock_rip_environment`**: Environment for hash calculation tests

### Test Classes

**test_stage_validation.py:**
1. **`TestValidationResult`**: Tests the ValidationResult dataclass
2. **`TestRipStageValidation`**: Tests rip stage validation
3. **`TestFinalizeStageValidation`**: Tests finalize disc validation
4. **`TestPostProcessStageValidation`**: Tests post-process validation
5. **`TestTransferStageValidation`**: Tests transfer validation

**test_stage_validation_integration.py:**
6. **`TestFullPipelineIntegration`**: Integration tests across stages
7. **`TestValidationFailureScenarios`**: Tests error detection

**test_hash_calculation_storage.py:**
8. **`TestHashCalculation`**: Tests hash calculation
9. **`TestHashStorage`**: Tests hash storage structure
10. **`TestHashVerificationAfterRip`**: Tests hash verification
11. **`TestHashCalculationIntegration`**: Tests hash calculation in rip workflow

**test_gather_final_outputs_cached_hashes.py:**
12. **`TestGatherFinalOutputsCachedHashes`**: Tests cached_hashes parameter

**test_rip_hash_integration.py:**
13. **`TestRipHashCalculationIntegration`**: Tests hash calculation integration

**test_postprocess_progress_tracking.py:**
14. **`TestPostProcessProgressTracking`**: Tests progress tracking functions

**test_rollback_on_validation_failure.py:**
15. **`TestRipValidationRollback`**: Tests rollback for rip validation failures
16. **`TestPostProcessValidationRollback`**: Tests rollback for post-process validation failures
17. **`TestFinalizeValidationRollback`**: Tests rollback for finalize validation failures
18. **`TestTransferValidationRollback`**: Tests rollback for transfer validation failures
19. **`TestRollbackIntegration`**: Integration tests for rollback across stages

**test_e2e_api_endpoints.py:**
20. **`TestRipStageE2E`**: Tests rip stage via API endpoint
21. **`TestLabelStageE2E`**: Tests label stage via API endpoint
22. **`TestFinalizeStageE2E`**: Tests finalize stage via API endpoint
23. **`TestPostProcessStageE2E`**: Tests post-process stage via API endpoint (comprehensive)
24. **`TestTransferStageE2E`**: Tests transfer stage via API endpoint
25. **`TestFullPipelineE2E`**: Tests complete pipeline via API endpoints

## What Gets Tested

### Rip Stage
- ✅ Expected output generation (MKV files, logs, metadata)
- ✅ File existence validation
- ✅ Hash storage validation
- ✅ Zero-size file detection
- ✅ Missing file detection
- ✅ Hash calculation at end of rip
- ✅ Hash storage in `disc_payload` structure
- ✅ Hash calculation with subdirectories

### Finalize Disc Stage
- ✅ Expected output generation (JSON, TXT, summary files)
- ✅ JSON structure validation
- ✅ Missing file detection
- ✅ Invalid JSON detection

### Post-Process Stage
- ✅ Expected output generation (file paths, hashes)
- ✅ File existence at expected paths
- ✅ Hash verification (files should have same hash after rename/move)
- ✅ Hash mismatch detection
- ✅ Missing file detection
- ✅ Progress tracking (0-50% rename, 50-100% hash verification)
- ✅ Progress tracking when files already moved
- ✅ Using cached hashes from rip stage (no recalculation)

### Transfer Stage
- ✅ Expected output generation (destination structure)
- ✅ File existence at destination
- ✅ Hash verification at destination
- ✅ Hash mismatch detection
- ✅ Missing file detection
- ✅ Progress tracking (already implemented, verified)

### Hash System
- ✅ Hash calculation consistency
- ✅ Hash storage in `disc_payload`
- ✅ Hash verification across file operations
- ✅ Hash format validation
- ✅ Cached hash usage in `gather_final_outputs`
- ✅ Partial cache handling
- ✅ Hash calculation fallback when cache missing

### Progress Tracking
- ✅ Post-process rename progress (0-50%)
- ✅ Post-process hash progress (50-100%)
- ✅ Combined progress tracking
- ✅ Edge cases (zero files, files already moved)
- ✅ Monotonic progress updates

## Adding New Tests

To add a new test:

1. **For unit tests**: Add to appropriate test class in `test_stage_validation.py`
2. **For integration tests**: Add to `test_stage_validation_integration.py`
3. **For hash tests**: Add to `test_hash_calculation_storage.py`

Example:
```python
def test_my_new_scenario(self, mock_job, mock_db, job_paths):
    """Test description."""
    # Set up test data
    # Run validation
    result = validate_rip_output(mock_job, mock_db, job_paths)
    # Assert results
    assert result.valid is True
```

## Troubleshooting

### Tests Fail with Import Errors
- Ensure virtual environment is activated
- Run `pip install -r requirements.txt` (or equivalent)

### Tests Fail with Database Errors
- Tests use mocked database objects - real DB not needed
- Check that fixtures are properly set up

### Tests Fail with File Not Found
- Tests use `tmp_path` fixture for temporary directories
- Check that fixtures create required file structures

### Hash Tests Fail
- Ensure `calculate_file_hash` function works correctly
- Check that file content is consistent between operations

## Mocking vs Real Files

The test bed uses a hybrid approach:
- **Mocked**: Job objects, database sessions, disc objects
- **Real Files**: Actual file creation in temporary directories for validation
- **Real Hashes**: Actual SHA256 hash calculation for accuracy

This provides:
- Fast test execution (no actual ripping)
- Accurate validation (real file operations)
- Isolated tests (temporary directories, mocked dependencies)

