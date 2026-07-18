# Test Maintenance Guide

## Overview

This guide ensures that when code is modified, corresponding tests are also updated to reflect changes in input/output expectations, function signatures, or behavior.

## Critical Rule: Update Tests When Code Changes

**⚠️ IMPORTANT**: When modifying any function that has tests, you MUST:
1. Identify which tests cover the modified code
2. Update tests to reflect new inputs/outputs/behavior
3. Run the test suite to verify changes
4. Ensure all tests still pass

## Test-to-Code Mapping

### `core/stage_validation.py`

#### `ValidationResult` dataclass
- **Tests**: `test_stage_validation.py::TestValidationResult`
- **When to update**: If you add/remove/modify fields in `ValidationResult`
- **What to update**: 
  - Test assertions that check field existence
  - Test data structures used to create `ValidationResult` instances

#### `generate_expected_rip_output(job, db) -> Dict[str, Any]`
- **Tests**: `test_stage_validation.py::TestRipStageValidation::test_generate_expected_rip_output`
- **When to update**: If you change the expected output structure (keys, nested structures, file patterns)
- **What to update**:
  - Expected keys in the returned dictionary
  - File naming patterns or structures
  - Mock job/db setup if function requires different job/db attributes

#### `validate_rip_output(job, db, paths=None) -> ValidationResult`
- **Tests**: `test_stage_validation.py::TestRipStageValidation` (5 tests)
- **When to update**: If you change validation logic, add/remove checks, or change error messages
- **What to update**:
  - `test_validate_rip_output_success` - Update if success criteria change
  - `test_validate_rip_output_missing_files` - Update if missing file detection logic changes
  - `test_validate_rip_output_missing_hashes` - Update if hash validation requirements change
  - `test_validate_rip_output_zero_size_file` - Update if zero-size file handling changes
  - Test fixtures if function signature changes (e.g., new parameters)

#### `generate_expected_finalize_output(job, db) -> Dict[str, Any]`
- **Tests**: `test_stage_validation.py::TestFinalizeStageValidation::test_generate_expected_finalize_output`
- **When to update**: If expected finalize output structure changes
- **What to update**: Expected dictionary structure and keys

#### `validate_finalize_output(job, db, paths=None) -> ValidationResult`
- **Tests**: `test_stage_validation.py::TestFinalizeStageValidation` (4 tests)
- **When to update**: If validation logic, checks, or error messages change
- **What to update**: Success/failure test cases, error message assertions

#### `generate_expected_postprocess_output(job, db) -> Dict[str, Any]`
- **Tests**: `test_stage_validation.py::TestPostProcessStageValidation::test_generate_expected_postprocess_output`
- **When to update**: If expected post-process output structure changes

#### `validate_postprocess_output(job, db, paths=None) -> ValidationResult`
- **Tests**: `test_stage_validation.py::TestPostProcessStageValidation` (4 tests)
- **When to update**: If validation logic changes, especially hash verification logic

#### `generate_expected_transfer_output(job, db) -> Dict[str, Any]`
- **Tests**: `test_stage_validation.py::TestTransferStageValidation::test_generate_expected_transfer_output`
- **When to update**: If expected transfer output structure changes

#### `validate_transfer_output(job, db, destination_path) -> ValidationResult`
- **Tests**: `test_stage_validation.py::TestTransferStageValidation` (4 tests)
- **When to update**: If validation logic or destination_path parameter usage changes

### `workers/tasks.py`

#### `gather_final_outputs()` (method and module-level)
- **Tests**: `test_gather_final_outputs_cached_hashes.py` (4 tests)
- **When to update**: 
  - If you change the `cached_hashes` parameter behavior
  - If you modify how hashes are calculated or used
  - If function signature changes (add/remove parameters)
- **What to update**:
  - Test function calls to match new signature
  - Test assertions about hash calculation/caching behavior
  - Mock setups if dependencies change

#### Hash calculation at end of `rip_disc`
- **Tests**: `test_rip_hash_integration.py` (3 tests)
- **When to update**: 
  - If hash calculation logic changes
  - If storage structure in `disc_payload` changes
  - If hash storage location/format changes
- **What to update**:
  - Expected `disc_payload` structure
  - Hash calculation assertions
  - File path handling tests if structure changes

#### Progress tracking callbacks (post-process)
- **Tests**: `test_postprocess_progress_tracking.py` (5 tests)
- **When to update**: 
  - If progress calculation logic changes (percentages, ranges)
  - If progress callback signature changes
  - If progress phases are redefined (e.g., rename 0-50%, hash 50-100%)
- **What to update**:
  - Progress percentage assertions
  - Progress range tests
  - Callback invocation tests

### Integration Tests

#### `test_stage_validation_integration.py`
- **Tests**: Full pipeline integration tests (6 tests)
- **When to update**: 
  - If stage-to-stage transitions change
  - If data structures passed between stages change
  - If validation flow changes
- **What to update**:
  - Pipeline simulation fixtures
  - Stage transition assertions
  - Data structure assertions between stages

#### `test_hash_calculation_storage.py`
- **Tests**: Hash system tests (9 tests)
- **When to update**: 
  - If hash calculation algorithm changes
  - If hash storage format changes
  - If hash verification logic changes
- **What to update**:
  - Hash format assertions
  - Storage structure tests
  - Verification logic tests

## Code Change Checklist

When modifying code, use this checklist:

### Before Making Changes
- [ ] Identify which functions you're modifying
- [ ] Find the corresponding test files (use this guide)
- [ ] Read the existing tests to understand current expectations

### During Changes
- [ ] Update function signature in code if needed
- [ ] Update corresponding test function calls
- [ ] Update test assertions if behavior/output changes
- [ ] Update test fixtures if dependencies change
- [ ] Update test data if input/output structures change

### After Changes
- [ ] Run the specific test file: `pytest tests/test_<filename>.py -v`
- [ ] Run all validation tests: `pytest tests/test_stage_validation*.py tests/test_hash*.py tests/test_*progress*.py tests/test_gather*.py -v`
- [ ] Verify all tests pass
- [ ] Check for any new test failures that indicate missing updates
- [ ] Update this guide if you've added new functions/tests

## Common Scenarios

### Scenario 1: Adding a New Parameter to a Function

**Example**: Adding `verify_integrity: bool = True` to `validate_rip_output()`

**Actions Required**:
1. Update function signature in `core/stage_validation.py`
2. Update all test calls in `test_stage_validation.py::TestRipStageValidation`
3. Add tests for new parameter behavior (when `verify_integrity=False`)
4. Update test fixtures if needed

### Scenario 2: Changing Return Value Structure

**Example**: Changing `generate_expected_rip_output()` to return additional keys

**Actions Required**:
1. Update function implementation
2. Update `test_generate_expected_rip_output` to check for new keys
3. Update `validate_rip_output()` if it uses the expected output
4. Update all tests that use the expected output structure

### Scenario 3: Changing Validation Logic

**Example**: Making zero-size file detection more lenient (warning instead of error)

**Actions Required**:
1. Update validation function
2. Update `test_validate_rip_output_zero_size_file` to expect warning instead of error
3. Update error message assertions if messages change
4. Run tests to ensure behavior is correct

### Scenario 4: Modifying Hash Calculation

**Example**: Changing hash algorithm from SHA256 to SHA512

**Actions Required**:
1. Update hash calculation functions
2. Update all tests that check hash format/length
3. Update `test_hash_calculation_storage.py` tests
4. Update hash verification tests if algorithm affects verification

### Scenario 5: Changing Progress Calculation

**Example**: Changing rename progress from 0-50% to 0-40%

**Actions Required**:
1. Update progress calculation in `workers/tasks.py`
2. Update `test_rename_progress_tracking_0_to_50_percent` (and rename it!)
3. Update hash progress to start at 40% instead of 50%
4. Update `test_hash_progress_tracking_50_to_100_percent` accordingly
5. Update combined progress tests

## Test Naming Conventions

When adding new tests, follow these conventions:

- Unit tests: `test_<function_name>_<scenario>`
- Integration tests: `test_<stage>_to_<stage>_<scenario>`
- Error scenarios: `test_<function_name>_<error_type>`
- Success scenarios: `test_<function_name>_success`

## Running Tests

### Run specific test file
```bash
cd Backend
source .venv/bin/activate
pytest tests/test_stage_validation.py -v
```

### Run specific test class
```bash
pytest tests/test_stage_validation.py::TestRipStageValidation -v
```

### Run specific test
```bash
pytest tests/test_stage_validation.py::TestRipStageValidation::test_validate_rip_output_success -v
```

### Run all validation-related tests
```bash
pytest tests/test_stage_validation*.py tests/test_hash*.py tests/test_*progress*.py tests/test_gather*.py -v
```

## Quick Reference: Function → Test File Mapping

**Full inventory:** See `Backend/tests/PHASE4_UNIT_TEST_MATRIX.md` for the Phase 4 unit test matrix, reclassified unit/component/integration tests, and prioritized backlog.

| Function / area | Test File | Test Class/Method |
|-----------------|-----------|-------------------|
| `ValidationResult` | `test_stage_validation.py` | `TestValidationResult` |
| `generate_expected_rip_output` | `test_stage_validation.py` | `TestRipStageValidation::test_generate_expected_rip_output` |
| `validate_rip_output` | `test_stage_validation.py` | `TestRipStageValidation` (5 tests) |
| `generate_expected_finalize_output` | `test_stage_validation.py` | `TestFinalizeStageValidation::test_generate_expected_finalize_output` |
| `validate_finalize_output` | `test_stage_validation.py` | `TestFinalizeStageValidation` (4 tests) |
| `generate_expected_postprocess_output` | `test_stage_validation.py` | `TestPostProcessStageValidation::test_generate_expected_postprocess_output` |
| `validate_postprocess_output` | `test_stage_validation.py` | `TestPostProcessStageValidation` (4 tests) |
| `generate_expected_transfer_output` | `test_stage_validation.py` | `TestTransferStageValidation::test_generate_expected_transfer_output` |
| `validate_transfer_output` | `test_stage_validation.py` | `TestTransferStageValidation` (4 tests) |
| `gather_final_outputs()` | `test_gather_final_outputs_cached_hashes.py` | `TestGatherFinalOutputsCachedHashes` (4 tests) |
| Hash calculation in `rip_disc` | `test_rip_hash_integration.py` | `TestRipHashCalculationIntegration` (3 tests) |
| Progress tracking | `test_postprocess_progress_tracking.py` | `TestPostProcessProgressTracking` (5 tests) |
| Pipeline integration | `test_stage_validation_integration.py` | `TestFullPipelineIntegration`, `TestValidationFailureScenarios` |
| Hash system | `test_hash_calculation_storage.py` | Multiple classes (9 tests) |
| `slugify`, `build_release_slug` | `test_slug_utils.py` | — |
| `path_templates` (resolve, validate, get_available_variables) | `test_path_templates.py` | — |
| `validate_job_state_transition`, `StateViolation` | `test_job_state_guard.py` | — |
| `calculate_required_rip_space_bytes`, `estimate_preview_size_bytes` | `test_disk_space_preflight.py` | — |
| `core.transfer.utils.conflicts` | `test_transfer_conflicts.py` | — |
| `calculate_file_hash`, `verify_transferred_file` | `test_transfer_verification.py` | — |
| `core.transfer.utils.credentials` (encrypt/decrypt) | `test_transfer_credentials.py` | 4 pure tests |
| `_derive_pipeline` | `test_jobs_pipeline.py` | — |
| `core.transfer_config` save/load | `test_transfer_config.py` | — |
| `SpeedTracker`, `calculate_speed` | `test_transfer_speed.py` | — |
| `should_cleanup`, `cleanup_source_safe` | `test_transfer_cleanup.py` | — |
| `core.transfer.utils.deduplication` | `test_transfer_deduplication.py` | — |
| `core.utils` parsing, `importbuddy_prefill.parse_copy_log` | `test_parsing_comprehensive.py` | — |
| `core.ffmpeg_detection` | `test_ffmpeg_detection.py` | — |
| `JobPaths`, `resolve_jobs_root` | `test_job_paths.py` | — |
| `core.disc_locks` | `test_disc_locks.py` | — |
| `api.scan_guard` (try_start, complete, expiry) | `test_scan_guard.py` | — |
| `core.settings` get/set_ffmpeg_detection | `test_settings_ffmpeg_detection.py` | — |
| `core.stage_backup` (backup_files, restore, checkpoints) | `test_stage_backup.py`, `test_rollback_on_validation_failure.py` | — |
| `_build_title_output_map` | `test_title_ordering.py` | — |
| `_build_title_id_maps`, `_ensure_previews_map`, `_resolve_preview_*` | `test_preview_title_id_mapping.py` | — |
| `DebouncedRippedFilesCommit` | `test_incremental_ripped_files.py` | — |
| `ProgressThrottle`, `enumerate_transfer_files`, `verify_transferred_files_batch` | `test_transfer_hash_progress.py` | — |
| `core.disc_manager` (parse_info_log, get_disc_info, list_discs, …) | `test_disc_manager.py` | — |
| `core._drive_operations` (access control) | `test_drive_operations_access_control.py` | — |
| `drive_manager.main._read_sys_block_size` | `test_drive_manager_disc_size.py` | — |
| `drive_manager.uds_server` | `test_uds_server.py` | — |
| `validate_previews` | `test_job_validation.py` | — |
| `_safe_track_folder`, `_backfill_preview_title_ids` | `test_workers_pure_helpers.py` | — |
| `_model_to_dict` | `test_export_import.py` | — |
| `api.crud` pure helpers (_normalize_format, _format_rank, _disc_name_sluggify, _format_slug, _best_format, _title_case) | `test_crud_helpers.py` | — |
| `Disc.get_movie_data` | `test_disc.py` | `TestDiscGetMovieData` |
| `core.disc_cache` get, set_payload, set, clear, clear_key | `test_disc_cache.py` | — |
| `core.discdb_finalize._write_film_metadata` | `test_discdb_finalize.py` | — |
| `core.discdb_import` _normalize_mount, _collect_files, hash_log_file | `test_discdb_import.py` | — |
| `core.devmode` _format_list, _format_mismatches, build_validation_report, _gather_files, compare_directories | `test_devmode.py` | — |
| `core.devmode_backup` get_backup_root, get_stage_backup_dir | `test_devmode_backup.py` | — |
| `core.discord_config` load, save, get_webhook_url | `test_discord_config.py` | — |
| `core.preview_config` load, save | `test_preview_config.py` | — |
| `core.storage_detection` get_storage_info, get_local_storage_info, get_smb/nfs/rsync (error branches) | `test_storage_detection.py` | — |
| `core.transfer.utils.error_handler` categorize_error, can_retry_automatically, can_retry, get_transfer_error_details | `test_transfer_error_handler.py` | — |
| `core.transfer.utils.notifications` should_notify, notify_transfer_started/completed/failed | `test_transfer_notifications.py` | — |
| `core.failure_recovery` should_attempt_recovery, get_recovery_strategy | `test_failure_recovery.py` | — |
| `core.progress_emitter` emit_job_progress_debounced | `test_progress_emitter.py` | — |
| `core.redis_cache` get, set, invalidate, is_stale, _make_key | `test_redis_cache.py` | — |
| `core.makemkv_state` is_disabled, set_disabled, get_reason, clear_disabled | `test_makemkv_state.py` | — |
| `core.makemkv_update_jobs` get_job | `test_makemkv_update_jobs.py` | — |

### Frontend: component → spec

| Component | Spec file |
|-----------|-----------|
| WorkflowActionBarComponent | `workflow-action-bar.component.spec.ts` |
| WorkflowLabelingComponent | `workflow-labeling.component.spec.ts` |
| LabelShellComponent | `label-shell.component.spec.ts` |
| FilmLabelComponent | `film-label.component.spec.ts` |
| ReleaseLabelComponent | `release-label.component.spec.ts` |
| TitleLabelComponent | `title-label.component.spec.ts` |
| JobService | `job.service.spec.ts` |
| SystemService | `system.service.spec.ts` |
| WorkflowBreadcrumbComponent | `workflow-breadcrumb.component.spec.ts` |
| DiscLabelComponent | `disc-label.component.spec.ts` |
| AppComponent | `app.component.spec.ts` |
| DriveSelectorComponent | `drive-selector.component.spec.ts` |
| ComboboxComponent | `combobox.component.spec.ts` |
| DevmodeFloatingButtonComponent | `devmode-floating-button.component.spec.ts` |
| DevmodeMenuComponent | `devmode-menu.component.spec.ts` |
| DiscCarouselComponent | `disc-carousel.component.spec.ts` |
| DiscInfoComponent | `disc-info.component.spec.ts` |
| LoadingCardComponent | `loading-card.component.spec.ts` |
| MobileDrawerComponent | `mobile-drawer.component.spec.ts` |
| ReleaseSelectorComponent | `release-selector.component.spec.ts` |
| StageProgressBarComponent | `stage-progress-bar.component.spec.ts` |
| TitleModalComponent | `title-modal.component.spec.ts` |
| UnfinishedJobsComponent | `unfinished-jobs.component.spec.ts` |
| CardCarouselComponent | `card-carousel.component.spec.ts` |
| WorkflowActionsComponent | `workflow-actions.component.spec.ts` |
| PathTemplateEditorComponent | `path-template-editor.component.spec.ts` |
| TransferConfigFormComponent | `transfer-config-form.component.spec.ts` |
| TransferConfigListComponent | `transfer-config-list.component.spec.ts` |
| DiscdbService | `discdb.service.spec.ts` |
| LoggerService | `logger.service.spec.ts` |
| MobileService | `mobile.service.spec.ts` |
| ToastService | `toast.service.spec.ts` |
| LabelFormService | `label-form.service.spec.ts` |
| settings.actions | `settings.actions.spec.ts` |
| settings.reducer | `settings.reducer.spec.ts` |
| localStorageMetaReducer | `local-storage.reducer.spec.ts` |

## Reminders

1. **Always run tests after code changes** - Even small changes can break tests
2. **Update tests first if behavior intentionally changes** - Tests should reflect desired behavior
3. **Don't delete tests without understanding why** - Tests exist for a reason
4. **Add new tests for new functionality** - Don't just modify existing tests
5. **Keep this guide updated** - If you add new functions/tests, document them here

## Need Help?

If you're unsure which tests to update:
1. Search for the function name in test files: `grep -r "function_name" tests/`
2. Check test file names - they usually indicate what they test
3. Run all tests and see which ones fail - failing tests indicate what needs updating
4. Read the test code - it shows what the function is expected to do

