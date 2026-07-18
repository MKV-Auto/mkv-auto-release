# End-to-End Testing Framework

This document describes the end-to-end testing framework and its variants.

## Overview

**Backend E2E-style tests** (pytest): `e2e_test_environment`, TestClient, no frontend. API + workers + three mocks; no real Redis, Postgres, or hardware. See [Fixtures](#fixtures) and [Usage](#usage).

**Full-stack E2E** (Playwright): Angular + FastAPI + Playwright. The E2E backend is started via [Backend/scripts/run_e2e_backend.py](../scripts/run_e2e_backend.py) against a **test Postgres** on host port **5433** and a **test Redis** on host port **6380** (dedicated ports so a co-located production container's Postgres/5432 and Redis/6379 are not touched — issue #378). See [Full-stack E2E and the three mocks](#full-stack-e2e-and-the-three-mocks) and [docs/TESTING.md](../../../docs/TESTING.md).

The pytest E2E framework provides complete mocking of:
- **Redis/Celery**: Tasks execute synchronously instead of being queued
- **Database**: SQLite in-memory database for fast, isolated tests
- **Drive/Disc Operations**: Mocked makemkv, disc scanning, and drive operations
- **Disc Cache**: In-memory cache for disc information

The pytest variant and the Playwright variant use different stacks; the pytest framework above is in-process SQLite, while full-stack Playwright uses real Postgres + Redis (see [Full-stack E2E and the three mocks](#full-stack-e2e-and-the-three-mocks)).

## Files

- `tests/conftest_e2e.py`: Comprehensive mocking fixtures
- `tests/test_drive_gatekeeper_e2e.py`: End-to-end test scenarios

## Usage

### Running E2E Tests

```bash
cd Backend
source .venv/bin/activate
pytest tests/test_drive_gatekeeper_e2e.py -v
```

### Running Specific Test

```bash
pytest tests/test_drive_gatekeeper_e2e.py::TestEndToEndRipFlow::test_duplicate_prevention_e2e -v
```

## Fixtures

### `e2e_test_environment`

The main fixture that sets up the entire test environment. It combines:
- `e2e_db`: SQLite database session. **Deprecated:** implemented as an alias for `test_db`; prefer `test_db` in new tests.
- `mock_celery`: Synchronous Celery task execution
- `mock_redis`: In-memory Redis mock
- `enhanced_fake_drive_manager`: Mocked drive operations (MockDrive; patches 4 ops, seeds real `disc_cache`; see `docs/TESTING.md`)
- `mock_makemkv`: Mocked MakeMKV via **MockMKV** (`tests.fixtures.mock_mkv`); replaces `run_makemkv`. Use real `Disc` for rip flows. See `docs/TESTING.md`.

Disc cache is **real**; `enhanced_fake_drive_manager` seeds it at fixture setup.

### `mock_celery`

Mocks Celery to execute tasks synchronously instead of queuing them. Tasks are executed immediately when `apply_async()` is called.

### `mock_redis`

Provides an in-memory Redis mock that stores data in a dictionary. No actual Redis connection is required.

### `enhanced_fake_drive_manager`

Drive fixture implemented with **MockDrive** (`tests.fixtures.mock_drive`). It patches 4 ops on `core._drive_operations`: `list_drives`, `get_disc_info`, `refresh_disc_info`, `validate_disc_info` (plus `api.routers.jobs.validate_disc_info`). It keeps **real** `scan_disc_info` and `hash_disc` so `test_drive_operations_comprehensive` can exercise locking. Seeds real `disc_cache` at setup and clears it to avoid cross-test leaks. See `docs/TESTING.md` and `Backend/tests/fixtures/README.md`.

### `mock_makemkv`

Implemented with **MockMKV** (`tests.fixtures.mock_mkv`). Replaces `run_makemkv` at `core.utils`, `core.disc`, `api.crud`; writes MKV files and a `parse_log`-compatible log, returns `(log_str, pid)`. Use real `Disc` for rip flows. No actual `makemkvcon` commands are executed. See `docs/TESTING.md`.

## Test Scenarios

### `TestEndToEndRipFlow`

Tests the complete flow from API endpoint to job completion:
- `test_complete_rip_flow_from_api`: Full rip flow from API to completion
- `test_duplicate_prevention_e2e`: Duplicate request prevention
- `test_hash_based_detection_e2e`: Hash-based disc detection
- `test_gatekeeper_to_celery_task_flow`: Gatekeeper to Celery task execution

### `TestConcurrentOperationsE2E`

Tests concurrent operations:
- `test_concurrent_rip_requests_e2e`: Multiple simultaneous rip requests

### `TestStateManagementE2E`

Tests state management:
- `test_gatekeeper_state_updates_persist`: State persistence across operations

### `TestErrorHandlingE2E`

Tests error handling:
- `test_failed_scan_recovery_e2e`: Recovery from failed scans
- `test_hash_mismatch_handling_e2e`: Hash mismatch handling

## Mocking Strategy

### Celery Tasks

Celery tasks are mocked to execute synchronously. When `apply_async()` is called, the task function is executed immediately instead of being queued. This allows testing the full task execution flow without requiring a Celery worker.

### Database

SQLite is used for all database operations. Each test gets a fresh database instance, ensuring test isolation. The database schema is created automatically from the models.

### Drive Operations

Drive operations are mocked using **MockDrive** via the `enhanced_fake_drive_manager` fixture (and in other tests via the `mock_drive` fixture). MockDrive provides:
- Mocked disc information (from configurable `discinfo_payload`)
- Mocked drive listing
- Mocked disc validation and info/refresh/scan
- Writes to real `core.disc_cache` for consistency with `list_discs` / `get_cached_discs`
- No actual hardware access

`test_drive_manager_endpoints` and `test_drive_operations_comprehensive` do not use these fixtures; they patch at a lower level. See `docs/TESTING.md`.

### Makemkv

Makemkv operations are mocked to create fake output files and simulate progress. No actual `makemkvcon` commands are executed.

## Full-stack E2E and the three mocks

When the **E2E backend** is started via [Backend/scripts/run_e2e_backend.py](../scripts/run_e2e_backend.py), the same three mocks are applied at process startup via [Backend/scripts/e2e_bootstrap.py](../scripts/e2e_bootstrap.py):

- **MockDrive**: Patches `core._drive_operations` (list_drives, get_disc_info, refresh_disc_info, validate_disc_info, scan_disc_info, hash_disc, handle_disc_eject, handle_disc_insert) and `api.routers.jobs.validate_disc_info`. Seeds `core.disc_cache` with a disc at `disc_num=1`, `mount_point=/dev/sr0` so `get_cached_discs` and `/api/coordinator/initial-state` work.
- **MockMKV**: Patches `core.utils.run_makemkv`, `core.disc.run_makemkv`, `api.crud.run_makemkv`. Writes MKV files and emits progress (TCOUNT, PRGV) so the rip path and progress_emitter run without `makemkvcon`.
- **Postgres** (DB): `DATABASE_URL` defaults to `postgresql+psycopg2://postgres:e2e_pass@localhost:5433/e2e`. The `e2e-full.js` driver provisions an empty `postgres:16-alpine` container on host port **5433**; `run_e2e_backend.py` drops and recreates the `public` schema each run and builds tables via `Base.metadata.create_all` (skips Alembic for parity speed — the prod path still runs migrations). SQLite is supported as a fallback (`E2E_DATABASE_URL=sqlite:///...`) but deadlocks postprocess under eager Celery + nested `SessionLocal` and is not the canonical stack.
- **Celery broker**: `e2e_bootstrap.py` forces `broker_url="memory://"` so eager `apply_async` calls execute in-process and never publish to any host Redis. This blocks a co-located prod worker from picking up test tasks (#378).
- **Stage-callback intercept**: `e2e_bootstrap.py` patches `workers.tasks.requests` so the rip → rip_verification → postprocess callback chain applies state via `StageState` in-process instead of round-tripping through HTTP. This eliminates the eager-Celery + single-uvicorn-worker self-deadlock the original infra hit.

A **test Redis** runs on **localhost:6380** (Docker `redis:alpine` or `redis-server --port 6380 --daemonize yes`). The API's Redis subscriber (`psubscribe progress_updates:*`) and the progress_emitter's publish path use it for WebSocket progress; **WebSocket progress** is tested in [Frontend/e2e/rip-happy.spec.ts](../../../Frontend/e2e/rip-happy.spec.ts). The [e2e:full](../../../Frontend/package.json) script provisions Postgres + Redis, starts the E2E backend, serves the frontend, runs Playwright, and tears everything down. See [docs/TESTING.md](../../../docs/TESTING.md) for `npm run e2e` vs `npm run e2e:full`, the `E2E_ALLOW_PROD_CONTAINER` override, env vars, and journeys.

## Limitations

1. **Task Execution**: Celery tasks execute synchronously, so timing-related issues may not be caught
2. **Concurrency**: While concurrent requests are tested, true distributed task execution is not simulated
3. **Hardware**: No actual disc drives or hardware operations are tested

## Future Enhancements

- Add more comprehensive error scenario tests
- Test task retry mechanisms
- Test orphaned task cleanup
- Add performance benchmarks

