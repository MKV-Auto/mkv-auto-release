# Comprehensive Test Suite Documentation

This document describes the comprehensive test suite for MKV-Auto, covering all backend API calls, frontend systems, drive operations, and parsing functionality.

## Test Structure

### Backend Tests

#### 1. `test_comprehensive_api.py`
**Purpose**: Tests all backend API endpoints

**Coverage**:
- **Jobs API** (`/jobs/*`):
  - Starting rips (`POST /jobs/rip`)
  - Getting job status (`GET /jobs/{job_id}/status`)
  - Getting current job (`GET /jobs/current`)
  - Listing jobs (`GET /jobs`)
  - Transfer operations (`POST /jobs/{job_id}/transfer`)
  - Resume operations (`POST /jobs/{job_id}/resume`)
  - Post-processing (`POST /jobs/{job_id}/postprocess`)
  - Labeling (`POST /jobs/{job_id}/label`)
  - Artifacts retrieval (`GET /jobs/{job_id}/artifacts`)
  - Tracks retrieval (`GET /jobs/{job_id}/tracks`)
  - Preview operations (`POST /jobs/{job_id}/previews/regenerate`, `DELETE /jobs/{job_id}/previews`)

- **Discs API** (`/discs/*`):
  - Listing discs (`GET /discs/`)
  - Getting disc info (`GET /discs/{disc_num}/info`)
  - Refreshing disc info (`POST /discs/{disc_num}/refresh`)
  - Getting current disc (`GET /discs/current`)

- **System API** (`/system/*`):
  - MakeMKV information (`GET /system/makemkv`)
  - Storage information (`GET /system/storage`)
  - Transfer configurations (`GET /system/transfer/configs`, `POST /system/transfer/configs`)
  - Preview configuration (`GET /system/preview/config`)
  - Discord configuration (`GET /system/discord/config`)

- **Releases API** (`/releases/*`):
  - Listing releases (`GET /releases`)
  - Getting release by slug (`GET /releases/{slug}`)
  - Listing release discs (`GET /releases/{slug}/discs`)
  - Getting disc by hash (`GET /releases/disc/by-hash`)
  - Listing boxsets (`GET /releases/boxsets`)

- **Movies API** (`/movies/*`):
  - Listing movies (`GET /movies`)
  - Getting movie by ID (`GET /movies/{movie_id}`)
  - Creating movies (`POST /movies`)
  - Looking up movies from TMDB (`POST /movies/lookup`)

- **DiscDB API** (`/discdb/*`):
  - Searching DiscDB (`GET /discdb/search`)
  - Getting DiscDB detail (`GET /discdb/detail`)
  - Hashing for DiscDB (`POST /discdb/hash`)

- **Drives API** (`/drives/*`):
  - Getting drives (`GET /drives/drives`)
  - Getting disc info (`GET /drives/discinfo`)
  - Refreshing disc info (`POST /drives/discinfo/refresh`)
  - Scanning disc info (`POST /drives/discinfo/scan`)
  - Hashing disc (`POST /drives/discinfo/hash`)
  - Ejecting disc (`POST /drives/disc/eject`)

- **Health Checks**:
  - Health endpoint (`GET /healthz`)
  - Readiness endpoint (`GET /readyz`)

#### 2. `test_drive_operations_comprehensive.py`
**Purpose**: Tests all drive operations (hashing, info scanning, ripping)

**Coverage**:
- **Hash Operations**:
  - Successful hashing
  - Lock acquisition and release
  - Concurrent operation prevention
  - Error handling

- **Info Scan Operations**:
  - Successful info scanning
  - Lock acquisition and release
  - Concurrent operation prevention
  - Caching behavior
  - Refresh functionality

- **Rip Operations**:
  - Lock acquisition and release
  - Concurrent operation prevention
  - Cross-operation locking (rip prevents hash/info)

- **Operation Locks**:
  - Lock timeout behavior
  - Lock release on error
  - Operation active checking
  - Concurrent operations on different discs

- **Gatekeeper Integration**:
  - Getting disc info through gatekeeper
  - Starting rips with lock respect

#### 3. `test_parsing_comprehensive.py`
**Purpose**: Tests all parsing functionality

**Coverage**:
- **Info Log Parsing**:
  - Basic info log parsing
  - Title metadata extraction
  - Multiple title parsing
  - Duration format parsing (various formats)
  - Resolution inference
  - Stream information (SINFO) parsing

- **Copy Log Parsing**:
  - Basic copy log parsing
  - Skipped titles detection
  - Disc label extraction
  - Title count extraction

- **Disc Payload Hydration**:
  - Basic payload hydration
  - Resolution inference in hydration
  - Cached payload handling
  - Label inference from various fields

- **Edge Cases**:
  - Empty log handling
  - Malformed log handling
  - Unicode character handling
  - Special character handling
  - Edge case duration formats

- **Integration**:
  - Full parse and hydrate flow

### Frontend Tests

#### 1. `ripping.service.spec.ts`
**Purpose**: Tests the RippingService Angular service

**Coverage**:
- Starting rips
- Getting job status
- Getting current job
- Labeling jobs
- Transfer operations
- Resuming jobs
- Error handling (duplicate rips, etc.)

#### 2. `drive.service.spec.ts`
**Purpose**: Tests the DriveService Angular service

**Coverage**:
- Getting drives
- Getting disc info
- Refreshing disc info
- Drive selection
- Error handling

## Running the Tests

### Backend Tests

```bash
cd Backend
source .venv/bin/activate
pytest tests/test_comprehensive_api.py -v
pytest tests/test_drive_operations_comprehensive.py -v
pytest tests/test_parsing_comprehensive.py -v
```

Run all comprehensive tests:
```bash
pytest tests/test_comprehensive_*.py -v
```

### Frontend Tests

```bash
cd Frontend
npm test
```

Run specific test suites:
```bash
npm test -- --include='**/ripping.service.spec.ts'
npm test -- --include='**/drive.service.spec.ts'
```

## Test Fixtures and Mocks

### Backend Fixtures

The tests use `conftest_e2e.py` which provides:
- `e2e_test_environment`: Database session and test environment setup
- `mock_celery_sync`: Synchronous Celery task execution for testing
- `enhanced_fake_drive_manager`: Mocked drive operations implemented with **MockDrive** (`tests.fixtures.mock_drive`); patches 4 ops, seeds real `disc_cache`; keeps real `scan_disc_info`/`hash_disc` for `test_drive_operations_comprehensive`. See `docs/TESTING.md` and `Backend/tests/fixtures/README.md`.
- `mock_makemkv`: Mocked MakeMKV via **MockMKV** (`tests.fixtures.mock_mkv`); replaces `run_makemkv` at `core.utils`, `core.disc`, `api.crud`. Use real `Disc` with `mock_makemkv` for rip flows. See `docs/TESTING.md`.

### Frontend Mocks

The frontend tests use Angular's `HttpClientTestingModule` to mock HTTP requests and responses.

## Test Coverage Goals

### Backend API Coverage
- ✅ All `/jobs/*` endpoints
- ✅ All `/discs/*` endpoints
- ✅ All `/system/*` endpoints
- ✅ All `/releases/*` endpoints
- ✅ All `/movies/*` endpoints
- ✅ All `/discdb/*` endpoints
- ✅ All `/drives/*` endpoints
- ✅ Health check endpoints

### Drive Operations Coverage
- ✅ Hash operations (success, locking, concurrency)
- ✅ Info scan operations (success, locking, caching)
- ✅ Rip operations (locking, concurrency)
- ✅ Operation locks (timeout, release, active checking)
- ✅ Gatekeeper integration

### Parsing Coverage
- ✅ Info log parsing (all fields)
- ✅ Copy log parsing (all fields)
- ✅ Disc payload hydration
- ✅ Edge cases (empty, malformed, unicode, special chars)
- ✅ Integration flows

### Frontend Coverage
- ✅ RippingService (all methods)
- ✅ DriveService (all methods)
- ✅ Error handling
- ✅ Observable behavior

## Adding New Tests

### Backend API Test

```python
def test_new_endpoint(self, client, e2e_test_environment):
    """Test description."""
    response = client.get("/new/endpoint")
    assert response.status_code == 200
    data = response.json()
    assert "expected_field" in data
```

### Drive Operation Test

```python
def test_new_operation(self, e2e_test_environment):
    """Test description."""
    with patch('core._drive_operations.run_makemkv') as mock_makemkv:
        mock_makemkv.return_value = "expected output"
        result = new_operation("1", "/dev/sr0")
        assert result is not None
```

### Parsing Test

```python
def test_parse_new_format(self):
    """Test description."""
    log = "new format log content"
    result = parse_new_format(log)
    assert result is not None
    assert "expected_field" in result
```

### Frontend Service Test

```typescript
it('should perform new operation', () => {
  const mockResponse = { field: 'value' };
  
  service.newOperation().subscribe(response => {
    expect(response.field).toBe('value');
  });
  
  const req = httpMock.expectOne(`${apiUrl}/endpoint`);
  expect(req.request.method).toBe('GET');
  req.flush(mockResponse);
});
```

## Continuous Integration

These tests should be run as part of CI/CD pipelines:
1. On every pull request
2. Before merging to main
3. On scheduled nightly runs

## Troubleshooting

### Backend Tests Failing

1. **Database Issues**: Ensure test database is properly set up
2. **Mock Issues**: Check that mocks are correctly configured
3. **Import Errors**: Ensure all dependencies are installed

### Frontend Tests Failing

1. **Angular Version**: Ensure Angular version matches test setup
2. **HTTP Mocks**: Verify `HttpTestingController` is properly configured
3. **Observable Issues**: Check that observables are properly subscribed

## Test Fixtures

### `sample_job`
Creates a real job in the test database for use in tests that need a valid job ID. This replaces hardcoded job IDs and ensures tests use actual database records.

### `unique_disc_hash`
Generates a unique disc hash for tests that need isolation from other tests. Useful for concurrent operation tests and tests that need to avoid conflicts.

## Test Patterns

### Error Handling Tests
All endpoints should have tests for:
- Missing required fields (422 validation errors)
- Invalid input values (400 bad request)
- Non-existent resources (404 not found)
- Invalid state transitions (400/409 bad request/conflict)

### Concurrent Operation Tests
Tests verify that concurrent requests are handled correctly, with proper locking and conflict detection. The `test_concurrent_rip_requests` test demonstrates this.

### Performance Tests
Basic performance checks ensure operations complete in reasonable time. The `test_start_rip_performance` test verifies rip initiation completes in < 2 seconds.

### Parametrized Tests
Similar endpoints are tested using `@pytest.mark.parametrize` to reduce code duplication and ensure consistent testing across similar operations.

### Integration Workflow Tests
Full end-to-end workflows are tested to ensure the entire system works together correctly. The `test_full_rip_workflow` test demonstrates this.

## Recent Improvements

### Added (Latest Update)
- ✅ Test fixtures (`sample_job`, `unique_disc_hash`) for better test data management
- ✅ Error handling tests for all major endpoints
- ✅ Concurrent operation tests
- ✅ Performance tests
- ✅ Edge case tests (empty strings, long hashes)
- ✅ Full workflow integration tests
- ✅ Parametrized tests for similar endpoints
- ✅ Improved assertion messages for better debugging
- ✅ Replaced hardcoded job IDs with real fixtures

## Future Enhancements

- [ ] Add E2E tests using Playwright
- [ ] Add performance/load tests
- [ ] Add security tests
- [ ] Add accessibility tests for frontend
- [ ] Add visual regression tests
- [ ] Add more state transition tests
- [ ] Add timeout and retry mechanism tests

