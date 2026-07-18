# Backend Tests

This directory contains the backend test suite for MKV-Auto, organized by test type.

## Quick Links

- **[QUICK_REFERENCE.md](QUICK_REFERENCE.md)** - Quick guide for updating tests when modifying code
- **[TEST_MAINTENANCE_GUIDE.md](TEST_MAINTENANCE_GUIDE.md)** - Detailed test-to-code mapping and maintenance procedures

## Running Tests

### All Tests
```bash
cd Backend
source .venv/bin/activate
pytest
```

### Specific Test File
```bash
pytest tests/test_<name>.py -v
```

### Specific Test
```bash
pytest tests/test_<name>.py::TestClass::test_method -v
```

### With Coverage
```bash
pytest --cov=. --cov-report=term
```

## Test Organization

### Unit Tests
Test individual functions and classes in isolation with mocked dependencies:
- `test_slug_utils.py` - String utilities (slugify, build_release_slug)
- `test_parsing_comprehensive.py` - Parsing functions (parse_*, coerce_duration, infer_resolution)
- `test_path_templates.py` - Path template resolution and validation
- `test_job_paths.py` - Job path utilities
- `test_job_state_guard.py` - State transition validation
- `test_disc_cache.py` - Disc information caching
- `test_disc_locks.py` - File-based locking mechanism
- `test_stage_validation.py` - Stage output validation logic
- `test_ffmpeg_detection.py` - FFmpeg metadata detection

### Component Tests
Test interactions between multiple components with real/in-memory databases:
- `test_disc_manager.py` - Disc manager operations with mocked dependencies
- `test_drive_operations_comprehensive.py` - Drive operations with access control
- `test_stage_backup.py` - Stage backup and restore operations
- `test_rip_flow.py` - Ripping workflow components
- `test_postprocess_partial_processing.py` - Post-processing pipeline

### Integration Tests
Test complete workflows with database and multiple services:
- `test_disc_manager_integration.py` - Full disc manager integration
- `test_stage_validation_integration.py` - End-to-end stage validation
- `test_resume_postprocess_integration.py` - Post-process resume workflows
- `test_hash_progress_integration.py` - Hash calculation with progress tracking
- `test_rip_hash_integration.py` - Ripping with hash generation

### Stress / concurrent tests
- `test_pool_concurrent.py` - Concurrent requests to coordinator and jobs endpoints; guards against DB pool exhaustion and session-across-await regressions. Marked `integration` and `slow`. Run with `pytest tests/test_pool_concurrent.py -v`.

### End-to-End Tests
Test complete user workflows through the API with mocked external dependencies:
- `test_drive_gatekeeper_e2e.py` - Full rip workflows, duplicate prevention
- `test_comprehensive_api.py` - All API endpoints

See [README_E2E_TESTING.md](README_E2E_TESTING.md) for E2E framework details.

### API Tests
Test API endpoints and routing:
- `test_e2e_api_endpoints.py` - API endpoint coverage
- `test_discs_api_endpoints.py` - Disc management API
- `test_drive_manager_endpoints.py` - Drive manager API

## Test Fixtures

Fixtures are organized by scope:

- **Session**: `fixtures/pytest_plugins.py` - Shared session-scope fixtures
- **Function**: `conftest.py` - Per-test fixtures (database, job factories)
- **E2E**: `conftest_e2e.py` - E2E environment with comprehensive mocking

See `fixtures/README.md` for fixture details.

**Integration tests that run rip/postprocess tasks directly:** The worker does not set `rip_state`/`post_state`/`phase`; the API applies them via HTTP callbacks. Tests that invoke `rip_disc` or `resume_postprocess` synchronously must request the `stage_callback_mocks` fixture and run the task inside `with stage_callback_mocks:` so that rip-complete and postprocess-complete are simulated (state applied via StageState in the test DB). See `conftest_backend.py` (`_make_stage_callback_fake_requests`, `stage_callback_mocks`).

## Test Categories by Feature

### Disc Operations
- `test_disc.py`, `test_disc_cache.py`, `test_disc_locks.py`
- `test_disc_manager.py`, `test_disc_manager_integration.py`
- `test_disc_numbering.py`, `test_disc_parser.py`
- `test_disc_workflow_unlink.py`

### Drive Operations
- `test_drive_operations_access_control.py`
- `test_drive_operations_comprehensive.py`
- `test_drive_manager_endpoints.py`
- `test_drive_gatekeeper.py`, `test_drive_gatekeeper_e2e.py`

### Job Workflow
- `test_jobs_pipeline.py`, `test_jobs_workflow_step.py`
- `test_job_paths.py`, `test_job_state_guard.py`, `test_job_validation.py`
- `test_job_workflow_context_titles.py`
- `test_rip_flow.py`, `test_rip_with_detection.py`

### Post-Processing
- `test_postprocess_error_handling.py`
- `test_postprocess_partial_processing.py`
- `test_postprocess_progress_tracking.py`
- `test_resume_postprocess_integration.py`

### Transfer
- `test_transfer_*.py` (12 files covering all transfer features)

### Stage Management
- `test_stage_validation.py`, `test_stage_validation_integration.py`
- `test_stage_backup.py`
- `test_rollback_on_validation_failure.py`

### Settings & Configuration
- `test_settings_*.py` - Various settings modules
- `test_preview_config.py`, `test_discord_config.py`
- `test_transfer_config.py`

### Metadata & Parsing
- `test_parsing_comprehensive.py`
- `test_ffmpeg_detection.py`, `test_ffprobe_metadata.py`
- `test_preview_*.py` - Preview generation and management
- `test_discdb_*.py` - DiscDB integration

### Hashing
- `test_hash_calculation_storage.py`
- `test_hash_progress_integration.py`
- `test_gather_final_outputs_cached_hashes.py`
- `test_rip_hash_integration.py`

### Import/Export
- `test_export_import.py`
- `test_discdb_import.py`

### DevMode
- `test_devmode.py`, `test_devmode_backup.py`
- `test_devseed.py`

## Test Maintenance

**When you modify code, update the corresponding tests!**

1. Check the function's docstring for a `Tests:` reference
2. Find and update the referenced test file
3. Run the tests to verify changes
4. See [QUICK_REFERENCE.md](QUICK_REFERENCE.md) for details

## Additional Documentation

- **[README_COMPREHENSIVE_TEST_SUITE.md](README_COMPREHENSIVE_TEST_SUITE.md)** - Overview of all test files and their coverage
- **[README_E2E_TESTING.md](README_E2E_TESTING.md)** - E2E testing framework and fixtures
- **[README_INTEGRATION.md](README_INTEGRATION.md)** - Integration test patterns
- **[README_DRIVE_GATEKEEPER_TESTS.md](README_DRIVE_GATEKEEPER_TESTS.md)** - Drive gatekeeper test specifics
- **[README_STAGE_VALIDATION_TESTS.md](README_STAGE_VALIDATION_TESTS.md)** - Stage validation test details
- **[archive/](archive/)** - Historical test documentation (PHASE docs, coverage matrices)

## Related Documentation

- `docs/TESTING.md` (development repo) - Overall testing strategy and pyramid
- `docs/TESTING_CHECKLIST.md` (development repo) - Pre-merge testing checklist
