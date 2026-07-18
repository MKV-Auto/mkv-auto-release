# Drive Gatekeeper Testing Framework

This document describes the comprehensive test suite for the Drive Gatekeeper consolidation feature.

## Overview

The test suite (`test_drive_gatekeeper.py`) provides comprehensive coverage for:

1. **Duplicate Prevention** - Ensures only one rip job can be active per disc
2. **Hash-Based Detection** - Skips unnecessary scans for already-scanned discs
3. **Recovery Mechanism** - Handles failed scans and allows retry
4. **State Management** - Verifies only gatekeeper can modify rip state
5. **Integration** - Tests integration with `/jobs/rip` endpoint
6. **Celery Task Verification** - Ensures canonical execution (abort duplicates)

## Running the Tests

### Run All Gatekeeper Tests

```bash
cd Backend
pytest tests/test_drive_gatekeeper.py -v
```

### Run Specific Test Classes

```bash
# Test duplicate prevention
pytest tests/test_drive_gatekeeper.py::TestDuplicatePrevention -v

# Test hash-based detection
pytest tests/test_drive_gatekeeper.py::TestHashBasedDetection -v

# Test recovery mechanism
pytest tests/test_drive_gatekeeper.py::TestRecoveryMechanism -v

# Test state management
pytest tests/test_drive_gatekeeper.py::TestStateManagement -v
```

### Run Integration Tests

```bash
# End-to-end scenarios (marked with @pytest.mark.integration)
pytest tests/test_drive_gatekeeper.py::TestEndToEndScenarios -v --run-integration
```

### Run with Coverage

```bash
pytest tests/test_drive_gatekeeper.py --cov=core.drive_gatekeeper --cov-report=html
```

## Test Structure

### TestDuplicatePrevention

Tests that ensure duplicate rip requests are prevented:

- `test_can_start_rip_returns_false_when_job_exists` - Verifies duplicate detection
- `test_can_start_rip_returns_true_when_no_job_exists` - Verifies no false positives
- `test_can_start_rip_handles_lock_timeout` - Tests race condition handling
- `test_start_rip_prevents_duplicate_creation` - Ensures existing job is returned
- `test_concurrent_rip_requests_only_one_succeeds` - Multi-threaded duplicate prevention

### TestHashBasedDetection

Tests hash-based disc detection to skip unnecessary scans:

- `test_get_disc_info_returns_cached_when_scan_completed` - Uses cached data when available
- `test_get_disc_info_scans_when_disc_not_found` - Performs scan when disc not in DB
- `test_get_disc_info_refreshes_when_requested` - Forces refresh even if cached
- `test_get_disc_info_raises_error_when_scan_failed` - Handles previously failed scans

### TestRecoveryMechanism

Tests recovery from failed scans:

- `test_recover_failed_scan_retries_scan` - Successful recovery flow
- `test_recover_failed_scan_increments_attempts` - Tracks retry attempts

### TestStateManagement

Tests that only gatekeeper can modify rip state:

- `test_update_rip_state_updates_job` - Verifies state updates work correctly
- `test_update_rip_state_handles_failed_state` - Handles error states
- `test_get_drive_state_returns_active_operations` - Queries active operations from DB

### TestIntegrationWithJobsEndpoint

Tests integration with the `/jobs/rip` API endpoint:

- `test_jobs_rip_endpoint_uses_gatekeeper` - Verifies endpoint uses gatekeeper
- `test_jobs_rip_endpoint_rejects_duplicate` - Verifies duplicate rejection

### TestCeleryTaskCanonicalExecution

Tests that Celery tasks verify canonical execution:

- `test_rip_disc_aborts_if_task_id_mismatch` - Aborts non-canonical tasks

### TestEdgeCases

Tests edge cases and error handling:

- `test_start_rip_handles_missing_disc_hash` - Missing required parameters
- `test_start_rip_handles_hash_mismatch` - Hash validation
- `test_update_rip_state_handles_missing_job` - Missing job handling
- `test_get_disc_info_handles_scan_failure` - Scan failure handling

### TestEndToEndScenarios

End-to-end integration tests:

- `test_full_rip_flow_with_duplicate_prevention` - Complete rip flow
- `test_hash_based_detection_skips_unnecessary_scan` - Full hash detection flow
- `test_recovery_flow_for_failed_scan` - Complete recovery flow

## Key Test Scenarios

### Scenario 1: Concurrent Rip Requests

**Goal**: Ensure only one rip job is created when multiple requests arrive simultaneously.

**Test**: `test_concurrent_rip_requests_only_one_succeeds`

**What it verifies**:
- Multiple threads attempting to start a rip for the same disc
- Only one job is created
- All threads return the same job ID
- Only one Celery task is dispatched

### Scenario 2: Hash-Based Cache Hit

**Goal**: Skip unnecessary scans when disc is already in database.

**Test**: `test_get_disc_info_returns_cached_when_scan_completed`

**What it verifies**:
- Disc exists in DB with `scan_state='completed'`
- `get_disc_info()` returns cached data
- Drive manager is NOT called
- Response includes all expected fields

### Scenario 3: Failed Scan Recovery

**Goal**: Allow retry of previously failed scans.

**Test**: `test_recover_failed_scan_retries_scan`

**What it verifies**:
- Disc with `scan_state='failed'` can be recovered
- `recover_failed_scan()` increments `scan_attempts`
- Successful recovery updates `scan_state='completed'`
- Error is cleared on success

### Scenario 4: Duplicate Task Abortion

**Goal**: Ensure duplicate Celery tasks abort early.

**Test**: `test_rip_disc_aborts_if_task_id_mismatch`

**What it verifies**:
- Task with mismatched `celery_task_id` aborts
- Task verifies canonical execution before proceeding
- Non-canonical tasks don't execute rip logic

## Mocking Strategy

The tests use extensive mocking to:

1. **Avoid External Dependencies**: Drive manager calls are mocked
2. **Control Test Data**: Disc payloads are controlled via fixtures
3. **Isolate Components**: Each test focuses on gatekeeper logic
4. **Simulate Race Conditions**: Threading tests simulate concurrent requests

## Fixtures

- `gatekeeper` - DriveGatekeeper instance with test DB session
- `sample_disc_hash` - Test disc hash
- `sample_disc_payload` - Sample disc information payload
- `existing_disc` - Pre-existing disc record in DB
- `existing_job` - Pre-existing active job for duplicate testing

## Continuous Integration

These tests should be run:

1. **Before Committing**: Run locally to catch regressions
2. **In CI/CD**: Automated on every pull request
3. **Before Release**: Full test suite before tagging releases

## Troubleshooting

### Tests Failing Due to Database Locks

If you see `OperationalError` related to locks:
- Tests use SQLite which has limited locking support
- Some lock timeout tests may behave differently on SQLite vs Postgres
- This is expected and tests account for it

### Mock Assertions Failing

If mocks aren't being called as expected:
- Check that patches are applied to the correct import path
- Verify that the code path actually uses the mocked function
- Use `pytest -v` to see which assertions are failing

### Threading Tests Flaky

If concurrent tests are flaky:
- Add small delays between thread starts
- Increase test timeout if needed
- Verify database isolation between threads

## Future Enhancements

Potential additions to the test suite:

1. **Performance Tests**: Measure duplicate prevention overhead
2. **Stress Tests**: High concurrency scenarios (100+ threads)
3. **Database Migration Tests**: Verify migration correctness
4. **API Contract Tests**: Verify OpenAPI schema compliance
5. **Monitoring Tests**: Verify logging and metrics collection

