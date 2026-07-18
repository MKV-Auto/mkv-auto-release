# Phase 6 — Integration test checklist

Use this to plan the Phase 6 integration subset: which tests to mark `@pytest.mark.integration`, and whether they already use the three mocks (MockDrive, MockMKV, test_db).

**Run the integration subset:** `pytest -m integration`

**Phase 6 definition:** FastAPI app + workers (in-process or mocked) with **MockDrive, MockMKV, test_db**. No real `/dev/sr*`, makemkvcon, or Postgres. Hit `/discs/`, `/jobs/rip`, `/drives/`, etc. via TestClient.

---

## Candidate tests and three-mocks audit

| Test file | Fixtures / environment | MockDrive | MockMKV | test_db | Notes |
|-----------|------------------------|-----------|---------|---------|-------|
| **test_comprehensive_api** | `e2e_test_environment`, `enhanced_fake_drive_manager`, `mock_makemkv` | ✓ (enhanced_fake) | ✓ (mock_makemkv) | ✓ (via e2e) | Ready. Mark `@pytest.mark.integration` on class or module. |
| **test_e2e_api_endpoints** | `mock_drive`, `test_db`, `mock_celery_tasks`, local `mock_makemkv` | ✓ | ✓ (local) | ✓ | Ready. Mark `@pytest.mark.integration`. |
| **test_drive_gatekeeper_e2e** | `e2e_test_environment` | ✓ (via e2e) | ✓ (mock_makemkv) | ✓ (via e2e) | Ready. Mark `@pytest.mark.integration`. |
| **test_drive_operations_comprehensive** | `e2e_test_environment`, `enhanced_fake_drive_manager` | ✓ (via e2e) | ✓ (via e2e) | ✓ (via e2e) | Ready. Mark `@pytest.mark.integration`. |
| **test_drive_gatekeeper** → **TestEndToEndScenarios** | `test_db`, `cached_discs`, `sample_disc_hash`; patches `rip_disc`, `get_disc_info` | ✗ (patches) | ✗ (patches) | ✓ | Already `@pytest.mark.integration`. Uses patches instead of mock_drive/mock_mkv; acceptable for gatekeeper-focused flows. |
| **test_rip_flow** | `mock_drive`, `mock_mkv`, `test_db` in rip tests; `test_db` + DummyDisc in postprocess | ✓ (some) | ✓ (some) | ✓ | Mixed. Rip tests: all three ✓. Postprocess-only: test_db ✓, drive/MKV N/A (DummyDisc). Mark rip tests or whole module; document postprocess as N/A for drive/MKV. |
| **test_disc_manager_integration** | `test_db`, local `mock_drive_manager` (patches _drive_operations) | ✓ (local equiv) | N/A (no rip) | ✓ | Ready. mock_drive_manager is drive mock; no MKV. Mark `@pytest.mark.integration`. |
| **test_resume_postprocess_integration** | `job_with_rip_done_for_postprocess(test_db, ...)`, DummyDisc | N/A | N/A | ✓ | Postprocess-only; no drive/MKV. Mark `@pytest.mark.integration`; document drive/MKV N/A. |
| **test_stage_validation_integration** | `full_pipeline_setup(tmp_path)`: Mock(Session), Mock job, JobPaths, files | ✗ | ✗ | ✗ (Mock) | Uses Mock(Session), not test_db. Consider migrating to test_db for Phase 6; drive/MKV N/A (validation only). |
| **test_rip_hash_integration** | `mock_rip_workdir` (tmp_path, files); no API/workers | N/A | N/A | ✗ | Hash logic only; no API, no workers. Optional for `-m integration`; or keep as unit/component. |
| **test_jobs_workflow_step** | `e2e_test_environment`, `test_db` | ✓ (via e2e) | ✓ (via e2e) | ✓ (via e2e) | Ready. Mark `@pytest.mark.integration`. |

---

## Tests that do *not* use `mock_drive` (by design)

- **test_drive_manager_endpoints**: Patches `get_drives`, `hash_media_disc`, `run_makemkv` at a low level to exercise real `_drive_operations`. Do **not** mark as integration; keep separate.
- **test_drive_operations_comprehensive**: Does use `e2e_test_environment` (MockDrive, MockMKV, test_db) — include in integration. (It is in the table above.)

---

## Phase 6 actions (Phase 6 completion)

1. **Mark** — Done. The “Ready” and “acceptable” rows are marked with `@pytest.mark.integration` (module-level `pytestmark` or class-level for `TestEndToEndScenarios`).
2. **Optional — Deferred:** Migrate `test_stage_validation_integration` from `Mock(Session)` to `test_db` for consistency; then add `@pytest.mark.integration`. Leave unmarked until then.
3. **Document** — Done. `docs/TESTING.md` has an “Integration tests” section; `Backend/tests/README_INTEGRATION.md` describes when to use integration vs component vs E2E.
4. **Add README** — Done. `Backend/tests/README_INTEGRATION.md` documents that `--run-integration` is for network (TMDB), not for the integration subset.

---

## Run commands

| Command | Purpose |
|---------|---------|
| `pytest -m "not requires_uds"` | Default; full suite. Can be slow. |
| `pytest -m integration` | Integration subset only (~128 tests). |
| `pytest --run-integration` | Include network-dependent tests (e.g. TMDB); does not select the integration subset. |
| `pytest tests/component/ tests/test_comprehensive_api.py tests/test_rip_flow.py tests/test_stage_validation_integration.py -q` | Smaller baseline if full run times out. |
