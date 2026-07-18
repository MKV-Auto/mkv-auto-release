# Test Fixtures

Fixtures in this folder and in `conftest_backend.py` / `conftest_e2e.py` provide I/O mocks and test doubles for backend tests.

## MockDrive

**Module:** `tests.fixtures.mock_drive.MockDrive`  
**Fixtures:** `mock_drive` (`conftest_backend.py`), `enhanced_fake_drive_manager` (`conftest_e2e.py`)

Test double for drive and disc-info I/O. Implements the same surface as `core._drive_operations` and writes to the real `core.disc_cache`. See **`docs/TESTING.md`** for config (`drives`, `discinfo_payload`, `failures`), `disc_cache` behavior, and when to use it.

### How to request

- **`mock_drive`**: Add `mock_drive` to your test’s fixture parameters. It patches all 8 drive ops and seeds `disc_cache` for the default drive. Use in unit/component tests that need drive/disc behavior without hardware.
- **`enhanced_fake_drive_manager`**: Used by E2E tests via `e2e_test_environment`. Patches 4 ops and keeps real `scan_disc_info` / `hash_disc` for lock tests. You typically request `e2e_test_environment`, which includes it.

### Typical combinations

- **`mock_drive`** alone: API or workflow tests that need list_drives / get_disc_info / validate_disc_info etc. without real drives.
- **`mock_drive` + `mock_mkv`**: Rip flows where both drive and MakeMKV are faked; use real `Disc`, not DummyDisc.
- **`mock_drive` + DB fixtures** (e.g. `e2e_db`, `test_db` when available): Tests that need disc cache consistency and DB state.

`test_drive_manager_endpoints` and `test_drive_operations_comprehensive` do **not** use `mock_drive`; they patch at a lower level. See `docs/TESTING.md`.

## MockMKV

**Module:** `tests.fixtures.mock_mkv.MockMKV`  
**Fixtures:** `mock_mkv` (`conftest_backend.py`), `mock_makemkv` (`conftest_e2e.py`)

Test double for `run_makemkv` (MakeMKV boundary). Writes MKV files and a `parse_log`-compatible log; returns `(log_str, pid)`. Use with **real** `Disc` for rip-path tests instead of `fake_disc`/DummyDisc. See **`docs/TESTING.md`** for config (`titles`, `progress`, `failures`) and patch points.

### How to request

- **`mock_mkv`**: Add `mock_mkv` to your test’s fixture parameters. Patches `core.utils.run_makemkv`, `core.disc.run_makemkv`, `api.crud.run_makemkv`. Use for rip flows (e.g. `test_rip_flow`, component tests).
- **`mock_makemkv`**: Used by `e2e_test_environment` for e2e rip flows. Same behavior as `mock_mkv`.

### Typical combinations

- **`mock_drive` + `mock_mkv`**: Full rip path (create_job → rip_disc → Disc.rip) without hardware or makemkvcon.

## test_db (MockDatabase)

**Fixture:** `test_db` (`conftest_backend.py`)

Canonical test DB for backend tests. SQLite on `tmp_path`; patches both `api.database.SessionLocal` and `workers.tasks.database.SessionLocal`. No real Postgres.

### How to request

- Add `test_db` to your test’s fixture parameters: `def test_foo(test_db):`
- Use `with test_db() as session:` or `session = test_db(); ...; session.close()`
- Prefer `test_db` over any local `db` or `db_session` fixture in new tests.

### Typical combinations

- **`test_db`** with **`mock_drive`** and/or **`mock_mkv`**: API, workflow, transfer, and integration tests that need DB state without real Postgres.
