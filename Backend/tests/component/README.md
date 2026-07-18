# Component tests

Component tests exercise one subsystem with I/O replaced by mocks: API plus `mock_drive` / `mock_mkv` / `test_db`. No real optical drives or MakeMKV.

## What is covered

- **`/discs/`**: list, `GET /discs/{disc_num}/info`, `POST /discs/{disc_num}/refresh`
- **`/drives/`**: `GET /drives/drives`, `GET /drives/discinfo`, `POST /drives/disc/eject`

Rip and postprocess smoke tests may be added later; they would use `mock_drive`, `mock_mkv`, and `test_db`.

## Fixtures

- **`mock_drive`** (from `conftest_backend`): Replaces `_drive_operations` and seeds `disc_cache`. Use for any test that hits `/discs/` or `/drives/` without real hardware.
- **`mock_mkv`**: Use when the test runs rip (MakeMKV) code paths.
- **`test_db`**: SQLite on `tmp_path`; patches `api.database.SessionLocal` and `workers.tasks.database.SessionLocal`.

## How to run

```bash
pytest Backend/tests/component/
# or from Backend:
pytest tests/component/
```

## Adding a component test

1. Request `mock_drive` (and `mock_mkv`, `test_db` as needed).
2. Avoid `fake_disc` / DummyDisc for **rip** paths; for postprocess-only flows a minimal `rename_outputs` stub is acceptable.
3. For rip-involving tests, use `mock_mkv` and a real `Disc` (and `mock_drive`) instead of DummyDisc.

## Other suites

- **`test_drive_manager_endpoints`** and **`test_drive_operations_comprehensive`** do **not** use `mock_drive`; they patch at a lower level (`get_drives`, `hash_media_disc`, `run_makemkv`).
- **UDS tests** (`test_uds_server`): Tests marked `@pytest.mark.requires_uds` are excluded from the default run. Run them with: `pytest -m requires_uds`.
