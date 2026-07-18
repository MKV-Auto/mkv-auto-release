# Phase 4 — Unit Test Matrix

## Backend

| Component | Unit tests? | Test file(s) | Depth | Gaps and next steps |
|-----------|-------------|--------------|-------|---------------------|
| **core.utils** (slugify, build_release_slug) | Y | test_slug_utils.py | low | 2 tests; add parse_*, coerce_duration, infer_resolution if not in test_parsing_comprehensive. |
| **core.utils** (parse_*, coerce_duration, infer_resolution) | Y | test_parsing_comprehensive.py | high | 19 tests; good. |
| **core.utils** (calculate_required_rip_space_bytes, estimate_preview_size_bytes) | Y | test_disk_space_preflight.py | low | 3 tests; env monkeypatch. |
| **core.utils** (move_with_progress) | Y | test_utils_move.py | low | 2 tests; monkeypatch os.rename, hashlib. |
| **core.utils** (hash_file) | Y | test_hash_calculation_storage, test_gather_final_outputs_cached_hashes, test_hash_progress_integration | med | Exercised indirectly; no dedicated unit for hash_file alone. |
| **core.path_templates** | Y | test_path_templates.py | med | 10 tests; resolve, validate, get_available_variables. |
| **core.job_paths** | Y | test_job_paths.py | med | JobPaths, resolve_jobs_root; Mock job, tmp_path. |
| **core.job_state** | Y | test_job_state_guard.py | med | 4 tests; validate_job_state_transition, StateViolation; SimpleNamespace. |
| **core.job_validation** | Y | test_job_validation.py | low | validate_previews; Mock job, tmp_path, manifests. |
| **core.disc** (Disc class) | Y | test_disc.py | low | Disc.get_movie_data; pure, no I/O. Touched in test_rip_flow, test_postprocess_partial_processing (component). |
| **core.disc_cache** | Y | test_disc_cache.py | low | get, set_payload, set, clear, clear_key; patch _persist, _find_makemkvcon. |
| **core.disc_locks** | Y | test_disc_locks.py | med | FileLock, _is_makemkvcon_running; monkeypatch get_mkvauto_tmp, tmp_path. |
| **core.disc_manager** | Y | test_disc_manager.py | med-high | parse_info_log, query_discdb, get_disc_info, refresh_disc_info, get_disc_hash, list_discs; mocked _list_drives, _get_disc_info, cache, discdb. |
| **core._drive_operations** (access control) | Y | test_drive_operations_access_control.py | med | list_drives, get_disc_info, scan_disc_info, hash_disc, handle_*; patch inspect.stack, get_drives. Full ops need drive (component). |
| **core.drive_gatekeeper** | N | test_drive_gatekeeper.py (component, test_db) | — | **Hard to unit-test** in isolation; DB + Celery; keep as component. |
| **core.drive_manager_client** | N | — | — | **Unit-testable** for parse/format; HTTP best as component. |
| **core.stage_validation** | Y | test_stage_validation.py | high | 19 tests; Mock job, Session, JobPaths, calculate_file_hash. Some reclassified as unit-with-mocks. |
| **core.stage_backup** | Y | test_stage_backup.py, test_rollback_on_validation_failure.py | med-high | backup_files, restore_files, get_stage_backup_dir unit-like (tmp_path); create_stage_backup/restore use test_db. Rollback covers create/restore checkpoints. |
| **core.ffmpeg_detection** | Y | test_ffmpeg_detection.py | high | 16 tests; patch _get_file_metadata, tmp_path. |
| **core.settings** (ffmpeg_detection) | Y | test_settings_ffmpeg_detection.py | low | get/set_ffmpeg_detection; patch load_settings/save_settings. |
| **core.tmdb_scraper** | Y | test_tmdb_scraper.py | med | _parse_runtime_to_minutes, parse_tmdb_url pure; scrape_* need --run-integration (network). |
| **core.importbuddy_prefill** (parse_copy_log) | Y | test_parsing_comprehensive.py | med | In 19-test suite. |
| **core.discdb_finalize** | Y | test_discdb_finalize.py | low | _write_film_metadata metadata.json format; tmp_path, patch TMDB scrapers. |
| **core.discdb_import** | Y | test_discdb_import.py | low | _normalize_mount, _collect_files, hash_log_file; patch _calculate_hash. |
| **core.devmode** | Y | test_devmode.py | low | _format_list, _format_mismatches, build_validation_report, _gather_files, compare_directories. |
| **core.devmode_backup** | Y | test_devmode_backup.py | low | get_backup_root, get_stage_backup_dir; create/restore/cleanup need DB (component). |
| **core.discord_config** | Y | test_discord_config.py | low | load/save/get_webhook_url; patch core.settings. |
| **core.preview_config** | Y | test_preview_config.py | low | load/save; patch core.settings. |
| **core.storage_detection** | Y | test_storage_detection.py | low | get_local_storage_info, get_storage_info; SMB/NFS/rsync error branches; patch get_decrypted_credentials. |
| **core.transfer_config** | Y | test_transfer_config.py | low | save/load round-trip, env default; monkeypatch _CONFIG_FILE, get_mkvauto_data. |
| **core.transfer_history** | N | test_transfer_history.py (component, test_db) | — | **Component**; log_*, get_* use DB. |
| **core.transfer.validation** (calculate_file_hash, verify_transferred_file) | Y | test_transfer_verification.py, test_hash_calculation_storage.py | med-high | tmp_path; consistency, different files, verify after move. |
| **core.transfer.utils.conflicts** | Y | test_transfer_conflicts.py | high | 9 tests; check_conflict, resolve_conflict, generate_unique_name; tmp_path. |
| **core.transfer.utils.deduplication** | Y | test_transfer_deduplication.py | med | 8 tests; tmp_path, Mock config, calculate_file_hash. |
| **core.transfer.utils.credentials** (encrypt/decrypt) | Y | test_transfer_credentials.py | med | 4 pure; 2 with test_db (store/get). |
| **core.transfer.monitoring** (SpeedTracker, calculate_speed) | Y | test_transfer_speed.py | med | 6 tests; no DB. |
| **core.transfer.monitoring** (should_cleanup, cleanup_source_safe) | Y | test_transfer_cleanup.py | med | 7 tests; Mock config, tmp_path. |
| **core.transfer.service** (ProgressThrottle, enumerate_transfer_files, verify_transferred_files_batch) | Y | test_transfer_hash_progress.py | med-high | Unit-style; tmp_path, Mock. |
| **core.transfer.service** (resolve_path_template) | Y | test_transfer_service.py | low | 1 pure test. |
| **core.transfer.service** (get_active_config, create_config, activate_config, etc.) | N | test_transfer_service.py (component, test_db) | — | **Component**; full CRUD via test_db. |
| **core.transfer.config** | N | — | — | May overlap transfer.service; **unit-testable** for template/validation. |
| **core.transfer.protocols** | N | — | — | **Hard to unit-test** without I/O; prefer component with mocked connection. |
| **core.transfer.utils.history** | N | — | — | **Unit-testable** for format; DB as component. |
| **core.transfer.utils.error_handler** | Y | test_transfer_error_handler.py | low | categorize_error, can_retry_automatically, can_retry, get_transfer_error_details; mock Session/Job. |
| **core.transfer.utils.notifications** | Y | test_transfer_notifications.py | low | should_notify, notify_transfer_started/completed/failed; patch _send_notification. |
| **core.failure_recovery** | Y | test_failure_recovery.py | low | should_attempt_recovery, get_recovery_strategy; SimpleNamespace job; clear _recovery_attempts. |
| **core.progress_emitter** | Y | test_progress_emitter.py | low | emit_job_progress_debounced debounce and _pending_progress merge; patch _emit_progress_async, run_coroutine_threadsafe. |
| **core.redis_cache** | Y | test_redis_cache.py | low | get, set, invalidate, is_stale, _make_key; mock get_redis_client. |
| **core.websocket_manager** | N | — | — | **Hard to unit-test**; async + real connections; component. |
| **core.logging_utils** | N | — | — | **Unit-testable** for format/filter. |
| **core.makemkv_updater** | N | — | — | **Hard to unit-test**; downloads; component. |
| **core.makemkv_state** | Y | test_makemkv_state.py | low | is_disabled, set_disabled, get_reason, clear_disabled. |
| **core.makemkv_update_jobs** | Y | test_makemkv_update_jobs.py | low | get_job only; defer start_update_job, _stream_from_root_helper to component. |
| **api.scan_guard** | Y | test_scan_guard.py | med | try_start, complete, expiry; monkeypatch time; async. |
| **api.routers.jobs** (_derive_pipeline) | Y | test_jobs_pipeline.py | low | 3 tests; _derive_pipeline; SimpleNamespace. |
| **api.export_import** | Y | test_export_import.py | low | _model_to_dict; pure serialization. |
| **api.crud** | Y | test_crud_helpers.py | low | Pure helpers: _normalize_format, _format_rank, _disc_name_sluggify, _format_slug, _best_format, _title_case. CRUD remains component. |
| **workers.tasks** (gather_final_outputs) | Y | test_gather_final_outputs_cached_hashes.py | med-high | Cached hashes, recalc; Mock Session, patch hash_file. |
| **workers.tasks** (rip hash storage) | Y | test_rip_hash_integration.py | med | Simulated disc_payload; hash_file, structure. |
| **workers.tasks** (postprocess progress 0–50/50–100) | Y | test_postprocess_progress_tracking.py | med | 5 tests; pure progress math. |
| **workers.tasks** (_build_title_output_map) | Y | test_title_ordering.py | med | Mock db, disc_titles; ordering, missing titles. |
| **workers.tasks** (_build_title_id_maps, _ensure_previews_map, _resolve_preview_*) | Y | test_preview_title_id_mapping.py | med | 3 tests; SimpleNamespace; pure. |
| **workers.tasks** (DebouncedRippedFilesCommit) | Y | test_incremental_ripped_files.py | med | Mock Job, db; threshold, time, flush, merge. |
| **workers.tasks** (_safe_track_folder, _backfill_preview_title_ids) | Y | test_workers_pure_helpers.py | low | Pure helpers; 14 tests. |
| **workers.tasks** (preview_and_detect, rip_disc, etc.) | N | test_rip_flow, test_e2e_api_endpoints, etc. (component/integration) | — | **Component/integration**; full unit extraction not done. |
| **parsing.disc_parser** (hydrate_disc_payload, parse_info_log, label_flags) | Y | test_disc_parser.py, test_parsing_comprehensive.py | med | test_apply_scan_tracks_persists uses SQLite+api.models → integration; hydrate/parse unit-like. |
| **drive_manager.main** (_read_sys_block_size) | Y | test_drive_manager_disc_size.py | low | 1 test; tmp_path /sys/block. |
| **drive_manager.uds_server** | Y | test_uds_server.py | med | **Component** (real socket, threading); start/stop, insert/eject events. |

### Backend: unit-style tests reclassified as integration/component

- **test_stage_validation_integration.py**: Full pipeline; Mock + JobPaths + SQLite-style; **integration**.
- **test_hash_calculation_storage** (TestHashCalculationIntegration): Simulates rip; **unit** (tmp_path, hash_file).
- **test_disc_parser::test_apply_scan_tracks_persists_titles_and_streams**: create_engine, sessionmaker, api.models, crud → **integration**.
- **test_tmdb_scraper** (scrape_tmdb_page, scrape_tmdb_cast_page): Require `--run-integration`; **integration** when run with network.
- **test_transfer_credentials** (encrypt_and_store, get_decrypted): use test_db → **component**.
- **test_transfer_health.py**, **test_transfer_history.py**: test_db → **component**.
- **test_transfer_service** (all except test_resolve_path_template): test_db → **component**.
- **test_stage_backup::create_stage_backup** (and similar): test_db → **component**; `backup_files`-only tests remain unit-like.

### Backend: no dedicated unit tests — unit-testable vs hard

| Module | Unit-testable? | Notes |
|--------|----------------|-------|
| core.job_validation | Y | Pure rules. |
| core.disc (selected methods) | Y | Pure helpers; rip() via mocks. |
| core.disc_cache | Y | get/set/clear with mocked backend. |
| core.discdb_finalize (format) | Y | _write_film_metadata. |
| core.discdb_import (parsing) | Y | Parsing only. |
| core.devmode | Y | Toggles/overrides. |
| core.discord_config | Y | load/save/validate, mocked FS. |
| core.preview_config | Y | load/save/validate. |
| core.storage_detection | Y | Mocked mount/stat. |
| core.transfer.config | Y | Template/validation. |
| core.transfer.utils.history (format) | Y | Format only. |
| core.transfer.utils.error_handler | Y | Retry/backoff. |
| core.transfer.utils.notifications (format) | Y | Message formatting. |
| core.failure_recovery | Y | Decision logic. |
| core.progress_emitter | Y | Mocked WebSocket. |
| core.redis_cache | Y | Mocked Redis. |
| core.makemkv_state | Y | State parsing. |
| core.makemkv_update_jobs | Y | Job-update logic. |
| core.drive_manager_client (parse/format) | Y | Parse/format only. |
| api.export_import | Y | Serialization. |
| core.drive_gatekeeper | N | DB + Celery; component. |
| core.websocket_manager | N | Async + connections. |
| core.makemkv_updater | N | Downloads. |
| core.transfer.protocols | N | I/O-heavy; component. |

---

## Frontend

| Component / Service / Page | Has spec? | Depth | Gaps and next steps |
|----------------------------|-----------|-------|---------------------|
| **AppComponent** | Y | low | should create, router-outlet smoke. |
| **JobStatusDisplayComponent** | Y | med | create, onActionClick (emit / null / disabled), getStageProgress. Gaps: template/change detection, more CTA states. |
| **BoxsetSelectorComponent** | Y | med | create, ngOnInit+listBoxsets, boxsetSelected, boxsetToggled, ngOnDestroy. Gaps: listBoxsets error, empty options. |
| **MovieSelectorComponent** | Y | med | create, ngOnInit+getMovies, movieSelected, movieCleared, tmdbUrlLookup, ngOnDestroy. Gaps: getMovies error. |
| **DriveSelectorComponent** | Y | low | create, onChange, driveSelected, localStorage, no drives (onChange does not emit). |
| **HistoryComponent** | Y | low-med | devReportUrl, devStatusLabel, devStatusClass. Gaps: main load/display, PipelineMap. |
| **RipperPageComponent** | Y | med | create, titleOrder$, previewTrackKey, loading/scanning, init, observables; CTA tests removed. Gaps: more flows, WorkflowActions. |
| **HistoryPageComponent** | Y | med | load on init, transferJob, resumeJob+toast, discIconState. Gaps: getHistory/listReleases (stubbed), error handling. |
| **ErrorRecoveryService** | Y | high | classifyError, getUserMessage, getErrorInfo, retryWithBackoff. Good coverage. |
| **JobService** | Y | med | job.service.spec.ts; startRip, getJobStatus, transferJob, completeWorkflowStep, completeLabel, error path. |
| **MetadataService** | Y | med | getMovies, getMovie, createMovie; listReleases, getRelease; listBoxsets, createBoxset; createAndLinkMovie. HttpTestingController. Gaps: getBoxset, createAndLinkRelease/Boxset, error handling. |
| **DriveService** | Y | med | drives$, discInfo$, refreshDiscInfo, selectDrive, getDrives, currentSelected; refreshDiscInfo 500 error. |
| **SettingsService** | Y | low | saveSettings, getSettings, null when empty. Gaps: shape validation, migration. |
| **WorkflowService** | Y | high | Context, setContextByCard, updateContext, title seq gating, Observables, startRip, canNavigateToStep, computeFurthestStep, validateStepCompletion (titles, postprocess), determineWorkflowStep, applyMetadataSelection; getJobStatus error surfaced. |
| **ComboboxComponent** | Y | low | create; items/selectedItemId, filteredItems; onSelectItem, onClear, onToggle, onClose; onTmdbLookup valid/invalid. Mock MobileService. |
| **DevmodeFloatingButtonComponent** | Y | low | create; toggleExpanded. Mock Router, Job, System, Toast, Workflow. |
| **DevmodeMenuComponent** | Y | low | create; revertOptions when jobStatus has post_state completed. Mock Job, Metadata, Toast, Workflow, Router. |
| **DiscCarouselComponent** | Y | low | create; allCards, isCardActive, onCardClick; getDiscTitle, getDiscMeta. |
| **DiscInfoComponent** | Y | low | create; displayKey, progressFor, hasTitles; onStartRip, onToggleDiscMode. Mock Logger. |
| **DiscLabelComponent** | Y | low | create, missingDiscFormat, onDiscFormatChange (labelChanged). |
| **FilmLabelComponent** | Y | low | film-label.component.spec.ts; create, displayFilmName, filmSelected on lookup. |
| **LabelShellComponent** | Y | low | label-shell.component.spec.ts; create, release/disc visibility from inputs. |
| **LoadingCardComponent** | Y | low | create; message input. |
| **MakemkvUpdaterComponent** | N | — | **Component**: check/install flow; mock SystemService. |
| **MobileDrawerComponent** | Y | low | create; title; onClose emit. OverlayModule. |
| **PreviewPlayerComponent** | N | — | **Component**: HLS/player; mock or E2E. |
| **PreviewViewerComponent** | N | — | **Component**: HLS/player. |
| **ReleaseLabelComponent** | Y | low | release-label.component.spec.ts; create, groupSelected, getSelectedReleaseDisplayName. |
| **ReleaseSelectorComponent** | Y | low | create; onReleaseCleared; _validateReleaseYear, isFormValid. Mock Metadata, Mobile, Logger. |
| **StageProgressBarComponent** | Y | low | create; getProgressValue, isStageCompletedCheck, isStageFuture; stageGridTemplate. |
| **TitleLabelComponent** | Y | low | title-label.component.spec.ts; create, onTitleChange emit, isIgnored. |
| **TitleModalComponent** | Y | low | create; onClose; formatDuration, formatSize, isIgnored; openPreview, closePreview. |
| **UnfinishedJobsComponent** | Y | low | create; getJobCardTitle, getJobProductionYear, getJobResolutionFormat; onJobClick. |
| **WorkflowActionBarComponent** | Y | low | workflow-action-bar.component.spec.ts; create, CTA, continue/back emit, canGoBack. |
| **WorkflowBreadcrumbComponent** | Y | low | create, getDisplayName, getStepIndex, isMuted, onStepClick, toggleDropdown, closeDropdown. |
| **WorkflowLabelingComponent** | Y | low | workflow-labeling.component.spec.ts; create, step from context, film step smoke. |
| **CardCarouselComponent** | Y | low | create; trackByCardId, isCardActive, onCardSelected (setSelectedCard, setContextByCard). Mock Workflow, Metadata, Logger. |
| **WorkflowActionsComponent** | Y | low | create; context$, stageTimeline$, canContinue$, canGoBack$; onContinue, onBack. Mock Workflow, Mobile, Logger. |
| **transfer-config/*** (path-template-editor, -form, -list, -health, -history) | Y | low | path-template-editor: templateChange, updatePreview, getVariableDescription. transfer-config-form: formData, ngOnInit, onSubmit, onCancel. transfer-config-list: getHealthStatusText, getHealthBadgeClass, onEdit. Health/history: **component** for API. |
| **DiscdbSearchComponent** | N | — | **Component**: search, API; mock DiscdbService. |
| **PreviewTestComponent** | N | — | **E2E** or manual. |
| **SettingsPageComponent** | N | — | **Unit-testable**: form, save; mock SystemService. |
| **ShellComponent** | N | — | **Unit-testable**: layout, nav. |
| **RipDetailsModalComponent** | N | — | **Unit-testable**: open/close, content. |
| **SettingsModalComponent** | N | — | **Unit-testable**: form, save. |
| **DiscdbService** | Y | low | search (results, 4xx/5xx); detail (object, 4xx/5xx). Mock fetch. |
| **LoggerService** | Y | low | create; error, warn, info, debug, log do not throw. Mock System, HttpClient. |
| **MobileService** | Y | low | create; isMobile, isMobile$ from window.innerWidth. |
| **SystemService** | Y | low | system.service.spec.ts; getRsyncConfig, getDevMode, getStorageSummary, getRegistrationStatus. |
| **ToastService** | Y | low | show adds to toasts$; dismiss removes; refreshDiscordConfig. Mock System, Logger. |
| **LabelFormService** | Y | low | buildLabelForm, buildMetadataPayload, buildReleasePatchPayload, validateLabelForm, hasLabelContent. Mock Logger. |
| **state/*** (reducers, actions, meta-reducers) | Y | low | settings.actions: setSelectedDrive. settings.reducer: initialState, on setSelectedDrive. local-storage.reducer: meta-reducer wrapper, setItem. |

---

## Prioritized backlog (P0–P2)

**P0 — High-impact, unit-testable, missing or very shallow**

- **Backend:** `core.job_validation` (pure rules); `workers.tasks` pure helpers beyond current coverage; `api.export_import` (serialization); `core.disc` selected pure methods.
- **Frontend:** `JobService` (expand beyond placeholder); `WorkflowLabelingComponent`; `WorkflowActionBarComponent`; `SystemService`; `LabelShellComponent`; `FilmLabelComponent`; `ReleaseLabelComponent`; `TitleLabelComponent`.

**P1 — Important, partially covered or medium effort**

- **Backend:** `core.disc_cache`; `core.discdb_finalize` (format); `core.discdb_import` (parsing); `core.devmode`, `core.devmode_backup`; `core.discord_config`, `core.preview_config`; `core.storage_detection`; `core.transfer.utils.error_handler`, `core.transfer.utils.notifications`; `core.failure_recovery`; `core.progress_emitter`; `core.redis_cache`; `core.makemkv_state`, `core.makemkv_update_jobs`.
- **Frontend:** `MetadataService` (getBoxset, createAndLinkRelease/Boxset, errors); `DriveService` (getDrives, currentSelected, errors); `WorkflowService` (errors, more validateStepCompletion); `WorkflowBreadcrumbComponent`; `DiscLabelComponent`; `AppComponent` (minimal smoke); `DriveSelectorComponent` (expand).

**P2 — Nice-to-have or better covered by integration/E2E**

- **Backend:** `core.drive_gatekeeper`, `core.websocket_manager`, `core.makemkv_updater`, `core.transfer.protocols`; `api.crud` (CRUD via component).
- **Frontend:** `ComboboxComponent`, `DevmodeFloatingButtonComponent`, `DevmodeMenuComponent`, `DiscCarouselComponent`, `DiscInfoComponent`, `LoadingCardComponent`, `MobileDrawerComponent`, `ReleaseSelectorComponent`, `StageProgressBarComponent`, `TitleModalComponent`, `UnfinishedJobsComponent`, `CardCarouselComponent`, `WorkflowActionsComponent`, `transfer-config/*`, `DiscdbService`, `LoggerService`, `MobileService`, `ToastService`, `LabelFormService`, `state/*`; `PreviewPlayerComponent`, `PreviewViewerComponent`, `MakemkvUpdaterComponent`, `DiscdbSearchComponent`, `PreviewTestComponent` (E2E/manual).

---

## Phase 4 coverage (optional)

- **Backend:** `pytest --cov=core --cov=api --cov-report=term-missing` (requires `pytest-cov`: `pip install pytest-cov`). Use `--cov-report=html` for `htmlcov/`.
- **Frontend:** `ng test --code-coverage` (or `npm test -- --code-coverage`). Output under `coverage/`.
- See `docs/TESTING.md` § "Coverage (optional)" for usage and interpretation.

## Phase 4 gate (unit-only, for Phase 5 transition)

To verify **unit tests** pass (component/integration/E2E deferred to Phase 5+), run:

```bash
cd Backend && source .venv/bin/activate && pytest tests/ \
  --ignore=tests/test_e2e_api_endpoints.py \
  --ignore=tests/test_drive_gatekeeper_e2e.py \
  --ignore=tests/test_drive_gatekeeper.py \
  --ignore=tests/test_drive_manager_endpoints.py \
  --ignore=tests/test_drive_operations_comprehensive.py \
  --ignore=tests/test_rip_flow.py \
  --ignore=tests/test_stage_validation_integration.py \
  --ignore=tests/test_uds_server.py \
  --ignore=tests/test_boxset_relationships.py \
  --ignore=tests/test_comprehensive_api.py \
  -q
```

Frontend: `cd Frontend && npm test -- --watch=false --browsers=ChromeHeadless`
