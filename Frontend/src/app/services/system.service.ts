import { Injectable } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, shareReplay } from 'rxjs';
import { environment } from '../environments/environment';

export interface MakeMKVInfo {
  version: string | null;
  binary_path: string;
}

export interface MakeMKVUpdateRequest {
  version?: string | null;
  build_ffmpeg?: boolean;
  ffmpeg_advanced_features?: boolean;
  install_prefix?: string | null;
}

export interface MakeMKVUpdateResponse {
  version: string;
  ffmpeg_built: boolean;
  logs: string[];
}

export interface MakeMKVUpdateJobResponse {
  jobId: string;
}

export interface MakeMKVUpdateJobStatus {
  jobId: string;
  status: string;  // pending, running, completed, failed
  logs: string[];
  error?: string | null;
  version?: string | null;
  createdAt: string;
  updatedAt: string;
}

/** Response from GET /system/makemkv/update/active: in-progress job if any. */
export interface MakeMKVUpdateActiveResponse {
  active: boolean;
  jobId?: string | null;
  status?: string | null;
  logs?: string[] | null;
  error?: string | null;
  version?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface MakeMKVRegistrationStatus {
  expired: boolean;
  message?: string | null;
  currentKey?: string | null;
}

export interface StorageInfo {
  path: string;
  total: number;
  used: number;
  free: number;
}
export interface StorageSummary {
  data_root: StorageInfo;
  transfer_root: StorageInfo;
}
export interface StorageDirEntry {
  name: string;
  path: string;
  is_dir: boolean;
}
export interface MkdirRequest {
  path: string;
  name: string;
}

export interface RsyncConfig {
  host: string;
  user: string;
  path: string;
  port?: number;
  bwlimit?: number | null;
}

export interface RsyncConfigResponse {
  config?: RsyncConfig | null;
  hasKey: boolean;
}
export interface RsyncValidateResponse {
  status: string;
  message?: string;
}

export interface TransferConfig {
  mode: 'local' | 'rsync';
  transfer_dir?: string | null;
  output_dir?: string | null;
}

export interface TransferConfigCreate {
  mode: 'local' | 'rsync' | 'smb' | 'nfs';
  name?: string | null;
  transfer_dir?: string | null;
  output_dir?: string | null;
  path_template?: string | null;
  config_data?: Record<string, any> | null;
  conflict_resolution?: 'overwrite' | 'skip' | 'rename' | 'fail';
  health_check_interval_minutes?: number | null;
  credentials?: Record<string, string> | null;
}

export interface TransferConfigUpdate {
  name?: string | null;
  transfer_dir?: string | null;
  output_dir?: string | null;
  path_template?: string | null;
  config_data?: Record<string, any> | null;
  conflict_resolution?: 'overwrite' | 'skip' | 'rename' | 'fail';
  health_check_interval_minutes?: number | null;
  credentials?: Record<string, string> | null;
}

export interface TransferCapabilities {
  can_write_new: boolean;
  can_overwrite_in_place: boolean;
  can_delete: boolean;
  can_rename: boolean;
  probed_at: string;
  probe_error?: string | null;
  notes?: Record<string, any> | null;
}

export interface TransferConfigSummary {
  id: string;
  mode: string;
  name?: string | null;
  is_active: boolean;
  transfer_dir?: string | null;
  path_template?: string | null;
  conflict_resolution: string;
  health_check_interval_minutes?: number | null;
  health_status?: string | null;
  capabilities?: TransferCapabilities | null;
  created_at: string;
  updated_at: string;
}

export interface TransferConfigRecord {
  id: string;
  mode: string;
  name?: string | null;
  is_active: boolean;
  transfer_dir?: string | null;
  output_dir?: string | null;
  path_template?: string | null;
  config_data?: Record<string, any> | null;
  conflict_resolution: string;
  health_check_interval_minutes?: number | null;
  capabilities?: TransferCapabilities | null;
  created_at: string;
  updated_at: string;
}

export interface ValidationResult {
  success: boolean;
  message: string;
  errors?: string[] | null;
}

export interface TransferHistorySummary {
  id: string;
  job_id?: string | null;
  transfer_config_id?: string | null;
  mode: string;
  source_path: string;
  destination_path: string;
  status: string;
  bytes_transferred?: number | null;
  transfer_duration_seconds?: number | null;
  average_speed_mbps?: number | null;
  verification_status?: string | null;
  was_deduplicated: boolean;
  created_at: string;
  // #593: human-readable identity resolved server-side via
  // Job → Disc → Release → Movie. All null for orphaned rows
  // (job deleted with SET NULL on FK).
  movie_name?: string | null;
  release_name?: string | null;
  release_year?: number | null;
  disc_name?: string | null;
}

export interface TransferStatistics {
  total_transfers: number;
  completed: number;
  failed: number;
  deduplicated: number;
  success_rate: number;
  average_speed_mbps: number;
  total_bytes_transferred: number;
  period_days: number;
}

export interface HealthCheckResult {
  check_type: string;
  status: string;
  message?: string | null;
  response_time_ms?: number | null;
}

export interface TransferHealthStatus {
  overall?: HealthCheckResult | null;
  connectivity?: HealthCheckResult | null;
  authentication?: HealthCheckResult | null;
  permissions?: HealthCheckResult | null;
  space?: HealthCheckResult | null;
}

export interface ChannelPair {
  in_app: boolean;
  discord: boolean;
}

export interface InformativeCategoryChannels {
  in_app: boolean;
  discord: boolean;
}

/** Mirrors Backend notification_preferences (discord.settings). */
export interface NotificationPreferences {
  informative: {
    enabled: boolean;
    categories: Record<string, InformativeCategoryChannels>;
  };
  action_required: ChannelPair;
  errors: ChannelPair;
}

export interface DiscordConfig {
  webhook_url?: string | null;
  enabled: boolean;
  notification_preferences?: NotificationPreferences;
}

const INFORMATIVE_CATEGORY_KEYS = [
  'rip_start',
  'rip_complete',
  'job_completed',
  'per_title',
  'previews_ready',
  'transfer_started',
] as const;

/** Default prefs when API omits nested fields (should not happen after GET /system/discord/config). */
export function defaultNotificationPreferences(): NotificationPreferences {
  const categories: Record<string, InformativeCategoryChannels> = {};
  for (const k of INFORMATIVE_CATEGORY_KEYS) {
    categories[k] = { in_app: true, discord: true };
  }
  return {
    informative: { enabled: false, categories },
    action_required: { in_app: true, discord: true },
    errors: { in_app: true, discord: true },
  };
}

export function mergeDiscordConfig(cfg: DiscordConfig): DiscordConfig {
  const base = defaultNotificationPreferences();
  const p = cfg.notification_preferences;
  if (!p) {
    return { ...cfg, notification_preferences: base };
  }
  const mergedCats = { ...base.informative.categories };
  if (p.informative?.categories) {
    for (const k of Object.keys(p.informative.categories)) {
      const row = p.informative.categories[k];
      mergedCats[k] = {
        in_app: row?.in_app ?? true,
        discord: row?.discord ?? true,
      };
    }
  }
  return {
    ...cfg,
    notification_preferences: {
      informative: {
        enabled: p.informative?.enabled ?? false,
        categories: mergedCats,
      },
      action_required: {
        in_app: p.action_required?.in_app ?? true,
        discord: p.action_required?.discord ?? true,
      },
      errors: {
        in_app: p.errors?.in_app ?? true,
        discord: p.errors?.discord ?? true,
      },
    },
  };
}

export interface MediaServerConfig {
  media_server: 'plex' | 'jellyfin';
}

/** Counts returned by the backend when ``POST /system/tmdb/config`` runs
 *  the no-key → key backfill. Lets the UI confirm "Found N suggestions
 *  for discs you already have" instead of leaving the user wondering. */
export interface TmdbBackfillSummary {
  scanned: number;
  updated: number;
  seeded: number;
}

export interface TmdbConfigResponse {
  api_key_set: boolean;
  /** #610: the persisted key value, echoed so the Settings → TMDB input
   *  field can pre-populate. null when no key is configured. Mirrors how
   *  MakeMKV registration echoes currentKey. */
  api_key?: string | null;
  /** Only present on POST responses when the key transitioned from
   *  unconfigured → configured. */
  backfill?: TmdbBackfillSummary | null;
}

/** Copy settings: DiscDB prefill toggle and eject-on-finish toggle. */
export interface DiscDbLookupConfig {
  discdb_miss_workflow_with_prefill: boolean;
  eject_on_finish: boolean;
}

/** Auto-rip toggle (#331): rip on insert for both DiscDB hits and misses. */
export interface AutoRipConfig {
  auto_rip_enabled: boolean;
}

export interface PreviewConfig {
  duration_seconds: number;
  max_parallel: number;
  // Server-derived ceiling (os.cpu_count()). UI binds the slider's [max] to
  // this so the thumb always agrees with the persisted value, regardless of
  // the browser host's CPU count. Optional on writes; populated on reads.
  max_parallel_ceiling?: number;
}

export interface DevModeStatus {
  enabled: boolean;
  repo_url: string;
  branch: string;
  repo_path: string;
  export_root: string;
}

export interface ImportSummary {
  movies_imported: number;
  releases_imported: number;
  discs_imported: number;
  jobs_imported: number;
  disc_titles_imported: number;
  title_streams_imported: number;
  boxsets_imported: number;
  boxset_releases_imported: number;
  movies_skipped: number;
  releases_skipped: number;
  discs_skipped: number;
  jobs_skipped: number;
  disc_titles_skipped: number;
  title_streams_skipped: number;
  boxsets_skipped: number;
  boxset_releases_skipped: number;
  errors: string[];
}

export type DiscWorkflowBlockReason =
  | 'none'
  | 'makemkv_not_installed'
  | 'registration_required'
  | 'makemkv_error';

export type MakeMKVDownloadState = 'missing' | 'downloading' | 'ready' | 'failed';

/**
 * MakeMKV source-tarball pre-download state (#625). Populated on container
 * startup so the Setup Assistant can link to the real EULA text before the
 * user consents to install.
 */
export interface MakeMKVDownloadStatus {
  state: MakeMKVDownloadState;
  version: string | null;
  downloaded_at: string | null;
  error: string | null;
  started_at?: string | null;
}

export interface MakeMKVHealth {
  installed: boolean;
  valid: boolean;
  can_rip: boolean;
  version: string | null;
  missing_components: string[];
  error: string | null;
  binary_path?: string;
  /** True when disc scan/rip should be gated (install, key, or drive warmup failure). */
  disc_workflow_blocked?: boolean;
  disc_workflow_block_reason?: DiscWorkflowBlockReason;
  /** Source-tar pre-download progress for the Setup Assistant EULA link (#625). */
  download?: MakeMKVDownloadStatus;
}

export interface SystemHealth {
  makemkv: MakeMKVHealth;
  workers: {
    status: string;
    worker_count: number;
    active_workers: string[];
    issues: string[];
  };
  storage: {
    path: string;
    total_gb: number;
    free_gb: number;
    used_percent: number;
  };
}

@Injectable({ providedIn: 'root' })
export class SystemService {
  private readonly apiUrl = environment.apiBase ?? 'http://localhost:8000';

  constructor(private http: HttpClient) {}

  getLatestMakeMKV(): Observable<{ version: string }> {
    return this.http.get<{ version: string }>(`${this.apiUrl}/system/makemkv/latest`);
  }

  getMakeMKVInfo(): Observable<MakeMKVInfo> {
    return this.http.get<MakeMKVInfo>(`${this.apiUrl}/system/makemkv`);
  }

  updateMakeMKVVersion(payload: MakeMKVUpdateRequest): Observable<MakeMKVUpdateResponse> {
    return this.http.post<MakeMKVUpdateResponse>(`${this.apiUrl}/system/makemkv/update`, payload);
  }

  startMakeMKVUpdate(payload: MakeMKVUpdateRequest): Observable<MakeMKVUpdateJobResponse> {
    return this.http.post<MakeMKVUpdateJobResponse>(`${this.apiUrl}/system/makemkv/update/start`, payload);
  }

  getMakeMKVUpdateJob(jobId: string): Observable<MakeMKVUpdateJobStatus> {
    return this.http.get<MakeMKVUpdateJobStatus>(`${this.apiUrl}/system/makemkv/update/job/${jobId}`);
  }

  /** Get in-progress MakeMKV update job if any (for reattach after refresh or to avoid double install). */
  getMakeMKVUpdateActive(): Observable<MakeMKVUpdateActiveResponse> {
    return this.http.get<MakeMKVUpdateActiveResponse>(`${this.apiUrl}/system/makemkv/update/active`);
  }

  streamUpdate(jobId: string): EventSource {
    return new EventSource(`${this.apiUrl}/events/makemkv/${jobId}`);
  }

  getRegistrationStatus(): Observable<MakeMKVRegistrationStatus> {
    return this.http.get<MakeMKVRegistrationStatus>(`${this.apiUrl}/system/makemkv/registration`);
  }

  registerKey(key: string): Observable<MakeMKVRegistrationStatus> {
    return this.http.post<MakeMKVRegistrationStatus>(`${this.apiUrl}/system/makemkv/register`, { key });
  }

  getStorage(path?: string): Observable<StorageInfo> {
    const url = path ? `${this.apiUrl}/system/storage?path=${encodeURIComponent(path)}` : `${this.apiUrl}/system/storage`;
    return this.http.get<StorageInfo>(url);
  }
  getStorageSummary(): Observable<StorageSummary> {
    return this.http.get<StorageSummary>(`${this.apiUrl}/system/storage/summary`);
  }

  listDirectory(path?: string): Observable<StorageDirEntry[]> {
    const url = path ? `${this.apiUrl}/system/storage/listdir?path=${encodeURIComponent(path)}` : `${this.apiUrl}/system/storage/listdir`;
    return this.http.get<StorageDirEntry[]>(url);
  }

  makeDirectory(path: string, name: string): Observable<StorageDirEntry> {
    return this.http.post<StorageDirEntry>(`${this.apiUrl}/system/storage/mkdir`, { path, name });
  }

  getRsyncConfig(): Observable<RsyncConfigResponse> {
    return this.http.get<RsyncConfigResponse>(`${this.apiUrl}/system/transfer/rsync/config`);
  }

  saveRsyncConfig(cfg: RsyncConfig): Observable<RsyncConfigResponse> {
    return this.http.post<RsyncConfigResponse>(`${this.apiUrl}/system/transfer/rsync/config`, cfg);
  }

  uploadRsyncKey(file: File): Observable<RsyncConfigResponse> {
    const form = new FormData();
    form.append('file', file, file.name);
    return this.http.post<RsyncConfigResponse>(`${this.apiUrl}/system/transfer/rsync/key`, form);
  }

  deleteRsyncKey(): Observable<RsyncConfigResponse> {
    return this.http.delete<RsyncConfigResponse>(`${this.apiUrl}/system/transfer/rsync/key`);
  }

  validateRsync(cfg?: RsyncConfig): Observable<RsyncValidateResponse> {
    return this.http.post<RsyncValidateResponse>(`${this.apiUrl}/system/transfer/rsync/validate`, cfg || {});
  }


  getPreviewConfig(): Observable<PreviewConfig> {
    return this.http.get<PreviewConfig>(`${this.apiUrl}/system/preview/config`);
  }

  savePreviewConfig(cfg: PreviewConfig): Observable<PreviewConfig> {
    return this.http.post<PreviewConfig>(`${this.apiUrl}/system/preview/config`, cfg);
  }

  getDiscordConfig(): Observable<DiscordConfig> {
    return this.http.get<DiscordConfig>(`${this.apiUrl}/system/discord/config`);
  }

  saveDiscordConfig(cfg: DiscordConfig): Observable<DiscordConfig> {
    return this.http.post<DiscordConfig>(`${this.apiUrl}/system/discord/config`, cfg);
  }

  /** GET /system/tmdb/config — returns { api_key_set, api_key, backfill? }.
   *  #610: api_key is the persisted value, echoed so the Settings → TMDB
   *  input field can pre-populate (mirrors MakeMKV registration). */
  getTmdbConfig(): Observable<TmdbConfigResponse> {
    return this.http.get<TmdbConfigResponse>(`${this.apiUrl}/system/tmdb/config`);
  }

  /** POST /system/tmdb/config — sets or clears the TMDB v3 API key. Pass
   *  null or an empty string to clear. On the no-key → key transition the
   *  backend also runs a backfill over unlabeled discs and returns the
   *  counts under ``backfill`` so the UI can show "Found N suggestions". */
  saveTmdbConfig(apiKey: string | null): Observable<TmdbConfigResponse> {
    return this.http.post<TmdbConfigResponse>(
      `${this.apiUrl}/system/tmdb/config`,
      { api_key: apiKey },
    );
  }

  getMediaServerConfig(): Observable<MediaServerConfig> {
    return this.http.get<MediaServerConfig>(`${this.apiUrl}/system/media-server/config`);
  }

  saveMediaServerConfig(cfg: MediaServerConfig): Observable<MediaServerConfig> {
    return this.http.post<MediaServerConfig>(`${this.apiUrl}/system/media-server/config`, cfg);
  }

  getDiscdbLookupConfig(): Observable<DiscDbLookupConfig> {
    return this.http.get<DiscDbLookupConfig>(`${this.apiUrl}/system/discdb-lookup/config`);
  }

  saveDiscdbLookupConfig(cfg: DiscDbLookupConfig): Observable<DiscDbLookupConfig> {
    return this.http.post<DiscDbLookupConfig>(`${this.apiUrl}/system/discdb-lookup/config`, cfg);
  }

  getAutoRipConfig(): Observable<AutoRipConfig> {
    return this.http.get<AutoRipConfig>(`${this.apiUrl}/system/auto-rip/config`);
  }

  saveAutoRipConfig(cfg: AutoRipConfig): Observable<AutoRipConfig> {
    return this.http.post<AutoRipConfig>(`${this.apiUrl}/system/auto-rip/config`, cfg);
  }

  /** Ask the backend to send a single test message to Discord (used by setup step). */
  sendDiscordTest(): Observable<{ status: string; message: string }> {
    return this.http.post<{ status: string; message: string }>(`${this.apiUrl}/system/discord/test`, {});
  }

  /** #206: `/system/devmode` was firing 3-5× on cold Library load because
   * multiple services (shell, logger, workflow, frontend-version, setup
   * guard) each subscribed independently. shareReplay caches the response
   * for the lifetime of the SystemService singleton so any concurrent
   * subscribers share one HTTP call. Dev-mode changes require a page
   * reload anyway (see ENABLE_DEVMODE env), so a session-lifetime cache
   * is safe. */
  private _devMode$: Observable<DevModeStatus> | null = null;
  getDevMode(): Observable<DevModeStatus> {
    if (!this._devMode$) {
      this._devMode$ = this.http.get<DevModeStatus>(`${this.apiUrl}/system/devmode`).pipe(
        shareReplay({ bufferSize: 1, refCount: false }),
      );
    }
    return this._devMode$;
  }

  /** SHA-256 prefix of the served index.html. Changes on every frontend
   * build — the FrontendVersionService polls this to detect rebuilds and
   * either auto-reload (dev) or surface a "new version available" toast. */
  getFrontendVersion(): Observable<{ version: string }> {
    return this.http.get<{ version: string }>(`${this.apiUrl}/system/frontend-version`);
  }

  getQuickPostProcessTestsEnabled(): Observable<{ enabled: boolean }> {
    return this.http.get<{ enabled: boolean }>(`${this.apiUrl}/system/quick-postprocess-tests`);
  }

  setQuickPostProcessTestsEnabled(enabled: boolean): Observable<{ enabled: boolean }> {
    return this.http.post<{ enabled: boolean }>(`${this.apiUrl}/system/quick-postprocess-tests`, { enabled });
  }

  getFfmpegDetectionEnabled(): Observable<{ enabled: boolean }> {
    return this.http.get<{ enabled: boolean }>(`${this.apiUrl}/system/ffmpeg-detection`);
  }

  setFfmpegDetectionEnabled(enabled: boolean): Observable<{ enabled: boolean }> {
    return this.http.post<{ enabled: boolean }>(`${this.apiUrl}/system/ffmpeg-detection`, { enabled });
  }

  /**
   * Read the "Disable DiscDB" devmode toggle. OFF (default) = production
   * behaviour (real TheDiscDB lookups run). ON = simulate miss for every
   * disc + force the miss workflow in the UI.
   */
  getDiscdbDisabled(): Observable<{ disabled: boolean }> {
    return this.http.get<{ disabled: boolean }>(`${this.apiUrl}/system/discdb-disabled`);
  }

  setDiscdbDisabled(disabled: boolean): Observable<{ disabled: boolean }> {
    return this.http.post<{ disabled: boolean }>(`${this.apiUrl}/system/discdb-disabled`, { disabled });
  }

  /**
   * Re-run the TheDiscDB lookup against an existing disc's content hash.
   * Backend gates execution on `is_dev_mode()`; non-dev callers will see
   * a 403. On hit, the disc's titles auto-fill and `jobs.discdb_result`
   * flips to 'hit'. Response: `{ result: 'hit' | 'miss', disc_id }`.
   */
  relookupDiscdb(discId: string): Observable<{ result: 'hit' | 'miss'; disc_id: string }> {
    return this.http.post<{ result: 'hit' | 'miss'; disc_id: string }>(
      `${this.apiUrl}/discs/${encodeURIComponent(discId)}/relookup-discdb`,
      {},
    );
  }

  getSetupStatus(): Observable<{ first_time_setup_complete: boolean; setup_step: number }> {
    return this.http.get<{ first_time_setup_complete: boolean; setup_step: number }>(`${this.apiUrl}/system/setup/status`);
  }

  markSetupComplete(): Observable<{ first_time_setup_complete: boolean; setup_step: number }> {
    return this.http.post<{ first_time_setup_complete: boolean; setup_step: number }>(`${this.apiUrl}/system/setup/complete`, {});
  }

  saveSetupProgress(step: number): Observable<{ first_time_setup_complete: boolean; setup_step: number }> {
    return this.http.patch<{ first_time_setup_complete: boolean; setup_step: number }>(`${this.apiUrl}/system/setup/progress`, { setup_step: step });
  }

  // New transfer config methods
  getTransferConfigs(): Observable<TransferConfigSummary[]> {
    return this.http.get<TransferConfigSummary[]>(`${this.apiUrl}/system/transfer/configs`);
  }

  getTransferConfigById(configId: string): Observable<TransferConfigRecord> {
    return this.http.get<TransferConfigRecord>(`${this.apiUrl}/system/transfer/configs/${configId}`);
  }

  createTransferConfig(config: TransferConfigCreate): Observable<TransferConfigRecord> {
    return this.http.post<TransferConfigRecord>(`${this.apiUrl}/system/transfer/configs`, config);
  }

  updateTransferConfig(configId: string, config: TransferConfigUpdate): Observable<TransferConfigRecord> {
    return this.http.put<TransferConfigRecord>(`${this.apiUrl}/system/transfer/configs/${configId}`, config);
  }

  deleteTransferConfig(configId: string): Observable<void> {
    return this.http.delete<void>(`${this.apiUrl}/system/transfer/configs/${configId}`);
  }

  activateTransferConfig(configId: string): Observable<TransferConfigRecord> {
    return this.http.post<TransferConfigRecord>(`${this.apiUrl}/system/transfer/configs/${configId}/activate`, {});
  }

  probeTransferCapabilities(configId: string): Observable<{ success: boolean; config_id: string; queued: boolean }> {
    return this.http.post<{ success: boolean; config_id: string; queued: boolean }>(
      `${this.apiUrl}/system/transfer/configs/${configId}/probe-capabilities`,
      {},
    );
  }

  validateTransferConfig(configId: string): Observable<ValidationResult> {
    return this.http.post<ValidationResult>(`${this.apiUrl}/system/transfer/configs/${configId}/validate`, {});
  }

  triggerHealthCheck(configId: string): Observable<TransferHealthStatus> {
    return this.http.post<TransferHealthStatus>(`${this.apiUrl}/system/transfer/configs/${configId}/health-check`, {});
  }

  getTransferHealth(configId: string): Observable<TransferHealthStatus> {
    return this.http.get<TransferHealthStatus>(`${this.apiUrl}/system/transfer/configs/${configId}/health`);
  }

  testPathTemplate(configId: string, sampleData: Record<string, any>): Observable<{ resolved: string }> {
    return this.http.post<{ resolved: string }>(`${this.apiUrl}/system/transfer/configs/${configId}/test-template`, sampleData);
  }

  getTransferHistory(jobId?: string, configId?: string, limit?: number): Observable<TransferHistorySummary[]> {
    const params: string[] = [];
    if (jobId) params.push(`job_id=${encodeURIComponent(jobId)}`);
    if (configId) params.push(`config_id=${encodeURIComponent(configId)}`);
    if (limit) params.push(`limit=${limit}`);
    const query = params.length > 0 ? `?${params.join('&')}` : '';
    return this.http.get<TransferHistorySummary[]>(`${this.apiUrl}/system/transfer/history${query}`);
  }

  getTransferStatistics(configId?: string, days?: number): Observable<TransferStatistics> {
    const params: string[] = [];
    if (configId) params.push(`config_id=${encodeURIComponent(configId)}`);
    if (days) params.push(`days=${days}`);
    const query = params.length > 0 ? `?${params.join('&')}` : '';
    return this.http.get<TransferStatistics>(`${this.apiUrl}/system/transfer/statistics${query}`);
  }

  exportHistory(): Observable<Blob> {
    return this.http.post(`${this.apiUrl}/system/export`, {}, {
      responseType: 'blob'
    });
  }

  importHistory(file: File): Observable<ImportSummary> {
    const form = new FormData();
    form.append('file', file, file.name);
    return this.http.post<ImportSummary>(`${this.apiUrl}/system/import`, form);
  }

  getMakeMKVHealth(): Observable<MakeMKVHealth> {
    return this.http.get<MakeMKVHealth>(`${this.apiUrl}/system/makemkv/health`);
  }

  /**
   * Returns the browser URL for the extracted MakeMKV EULA text (#625).
   * Consumed as an `<a href>` in the Setup Assistant — the browser fetches
   * on new-tab click; this method does not issue an HTTP request itself.
   */
  getMakeMKVEulaUrl(): string {
    return `${this.apiUrl}/system/makemkv/eula`;
  }

  getSystemHealth(): Observable<SystemHealth> {
    return this.http.get<SystemHealth>(`${this.apiUrl}/system/health`);
  }
}
