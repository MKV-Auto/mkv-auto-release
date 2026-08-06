// src/app/services/workflow.service.ts
import { Injectable, OnDestroy, Inject, Optional, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject, EMPTY, Observable, timer, of, from, combineLatest, Subject, throwError, defer, firstValueFrom } from 'rxjs';
import { map, switchMap, startWith, catchError, shareReplay, distinctUntilChanged, takeUntil, tap, scan, filter, take, timeout, concatMap, last, finalize, throttleTime, debounceTime } from 'rxjs/operators';
import { environment } from '../environments/environment';
import { JobStatus, JobService } from './job.service';
import { DiscDetail, Drive, DriveService } from './drive.service';
import { MovieSummary, BoxsetSummary, ReleaseSummary, MetadataService } from './metadata.service';
import { LoggerService } from './logger.service';
import { SystemService } from './system.service';
import { ToastService, formatHttpErrorDetail } from './toast.service';
import { TitleStore } from './title-store.service';
import { LabelForm, isReleaseSufficientlyComplete } from '../pages/ripper/services/label-form.service';
import { sortTitlesForDisplay } from '../utils/title-display-sort.util';
import { areLabelTitlesComplete, isTitleIgnoredForStats } from '../utils/title-label-stats.util';
import { getStepOrderForContext } from './workflow-step-order.util';
import { buildTitleLabelEntities, getPrimaryTitleForEntity } from '../utils/title-label-entities.util';
import { normalizeTitleTypeForSelect } from '../constants/title-type-options';
import { canonicalTrackTitle } from '../utils/canonical-track-title.util';
import { mergeTitleFromSetPrimaryResponse } from '../utils/title-set-primary-merge.util';
import type { LibraryReattachReport } from './library-reattach.types';

function drivesEqual(a: Drive, b: Drive): boolean {
  return a.disc_num === b.disc_num && a.mount_point === b.mount_point && a.name === b.name;
}

// Merged interfaces from WorkflowCoordinatorService
export interface DiscMetadata {
  disc_id: string;
  disc_num?: string | null;
  mount_point?: string | null;
  disc_hash?: string | null;
  disc_state: 'in_drive' | 'unfinished';
  job_id?: string | null;
  scan_state?: 'pending' | 'scanning' | 'ready' | 'failed' | null;
  scan_error?: string | null;
  movie_name?: string | null;
  release_name?: string | null;
  info_title?: string | null;
  disc_number?: number | null;
  release_image?: string | null;
  disc_format?: string | null;
  resolution?: string | null;
  release_year?: number | null;
  production_year?: number | null;
  last_modified_at?: string | null;
  created_at?: string | null;
  /** TheDiscDB lookup outcome for carousel badge (independent of short-workflow / stage_profile). */
  discdb_result?: 'hit' | 'miss' | 'error' | 'unknown' | null;
  /** Coordinator/API may send discdb_hit on disc payloads; normalized onto cards when present. */
  discdb_hit?: boolean | null;
  /** True when a completed job exists for this in-drive disc (green check badge). */
  has_completed_job?: boolean | null;
  /** Job status for unfinished cards: 'running', 'validating', 'failed'. */
  job_status?: string | null;
  /** #603: disc.finalized matched against the inserted content_hash — when true,
   *  the carousel renders an "Already in Library" card with a Re-rip button. */
  finalized?: boolean | null;
  finalized_release_id?: string | null;
  finalized_release_name?: string | null;
  finalized_release_slug?: string | null;
}

export interface InsertedDisc {
  disc_id: string;
  disc_num: string;
  mount_point: string;
  disc_hash?: string;
}

export interface UnfinishedJob {
  job_id: string;
  disc_id: string | null;
  mount_point: string | null;
}

// Merged interfaces from WorkflowWebsocketService
export interface WorkflowContextMessage {
  type: 'workflow_context_updated';
  context: WorkflowContext;
}

export interface ProgressUpdateMessage {
  type: 'progress_update';
  job_id: string;
  disc_id?: string | null;  // Optional disc_id for routing progress updates
  rip_progress: number;
  /** "copy" | "verification" | null during rip; declarative stage for UI (e.g. "Ripping…" vs "Verifying…") */
  rip_phase?: string | null;
  /** #604: Stage state shipped on every rip/transfer-progress emit so a
   *  verifying → terminal transition advances the UI even when the
   *  backend's in-process rip-progress callback suppresses the
   *  authoritative context_changed event. */
  rip_state?: string | null;
  /** #605: post_state shipped so the transfer-step CTA button reflects
   *  prep progress without depending on context_changed refetches. */
  post_state?: string | null;
  /** #605: transfer_state shipped so the transfer-stage UI advances
   *  ready → running → completed under the auto-dispatch path even
   *  when the in-process transfer-progress emit suppresses
   *  context_changed. */
  transfer_state?: string | null;
  post_progress: number;
  transfer_progress?: number | null;
  per_title_progress?: Record<string, number> | null;
  current_title_progress?: number | null;
  current_title_id?: string | null;
  current_title_number?: number | null;
}

/** Backend-emitted notification (toast + optional action). Frontend displays via ToastService; no Discord from frontend. */
export interface BackendNotification {
  message: string;
  kind: 'info' | 'success' | 'warning' | 'error';
  level: string;
  /** Stable id for dedupe / push replace */
  id?: string;
  /** ISO timestamp (UTC) */
  timestamp?: string;
  /** Source, e.g. "backend" */
  source?: string;
  /** Optional short title (display instead of message when set) */
  title?: string;
  /** Optional disc/title name for context (e.g. MakeMKV info_title) */
  info_title?: string;
  /** Optional action buttons { label, url }[] */
  actions?: Array<{ label: string; url?: string }>;
  action_type?: string;
  action_payload?: Record<string, unknown>;
}

// Merged interfaces from WorkflowOrchestrationService
//
// `exploratory_rip` is the user-facing pill for the Path A flow (selective rip
// + segment-reorder). It only appears in the breadcrumb when the active job
// has segment_reorder_state — see workflow-step-order.util.ts. Sub-phases
// (exploratory_ripping, awaiting_segment_order, matching_playlists,
// canonical_ripping_pending) all live UNDER this single breadcrumb step;
// path-a-workspace switches its inner cards on segment_reorder_state.stage.
// #365 Phase 2 § 6.4 — 'postprocess' removed. The standalone postprocess
// step was collapsed into transfer's "preparing" sub-phase. Code paths
// that branched on step === 'postprocess' became unreachable once backend
// stopped emitting workflow_step="postprocess" and were cleaned up in the
// same PR. Pipeline-phase reads (jobStatus.post_state,
// jobStatus.pipeline?.['postprocess']) are unchanged — they belong to a
// separate concept (job lifecycle phase) that stays intact for the
// frontend's existing readers.
export type WorkflowStep = 'film' | 'exploratory_rip' | 'boxset' | 'disc' | 'titles' | 'summary' | 'transfer';

/** Readiness of the active workflow context: loading (fetching), ready (showing backend data), stale (refetching after context_changed), pending (step-advance POST in flight), error (fetch failed). */
export type WorkflowContextStatus = 'loading' | 'ready' | 'stale' | 'pending' | 'error';

export interface WorkflowValidationResult {
  valid: boolean;
  errors: string[];
}

// Stage progress interfaces
export interface StageProgressValues {
  rip: number | null;
  label: number | null;
  postprocess: number | null;
  transfer: number | null;
  upload?: number | null;
  /** Sub-phase labels (e.g. "Verifying…") when stage is at 100% but not yet complete. */
  ripPhaseLabel?: string | null;
  postPhaseLabel?: string | null;
  transferPhaseLabel?: string | null;
  /** ISO UTC when rip started (for elapsed time display). */
  ripStartedAt?: string | null;
}

export interface StageCompletionValues {
  rip: boolean;
  label: boolean;
  postprocess: boolean;
  transfer: boolean;
  upload?: boolean;
}

// UI Orchestration State (merged from RipperStateService)
export interface UIOrchestrationState {
  selectedCard: { type: 'drive' | 'job', id: string } | null;
  loadingInfo: boolean;
  unknownDisc: boolean;
  contextLoading: boolean;
  driveLoadingStates: Map<string, boolean>;
  backendError: string | null;
  driveError: string | null;
  driveScanState: 'idle' | 'scanning' | 'ready' | 'error';
}

export interface DiscInfoState {
  lastDiscInfo: DiscDetail | null;
  activeDiscKey: string | null;
  discDbState: 'unknown' | 'hit' | 'miss';
  currentDiscId: string | null;
  hydratedDiscHash: string | null;
  lookupAttemptedKey: string | null;
}

// Merged interfaces from WorkflowContextService
export interface FunctionBindings {
  titleStatusFn?: (id: string | null | undefined) => string;
  titleProgressValueFn?: (id: string | null | undefined) => number;
  titleActiveFn?: (id: string | null | undefined) => boolean;
  previewUrlFn?: (t: any) => string | null;
  previewStateFn?: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null;
  titlePathFn?: (t: any) => string | null;
  stageProgressFn?: (key: any) => number | null;
  isStageCompletedFn?: (key: any) => boolean;
  saveCallback?: (labelForm: any) => Observable<void>;
}

export interface WorkflowContext {
  // Identification
  id: string; // jobId for jobs, mount_point for drives
  type: 'job' | 'drive';
  discNum?: string; // disc_num from drive manager
  
  // Core workflow data
  labelForm: any | null;
  jobStatus: JobStatus | null;
  discInfo: DiscDetail | null; // Only for discs
  titles: any[];
  titleOrder: string[];
  titlesComplete: boolean; // Whether titles are fully loaded from API/WebSocket
  titlesVersion?: number;
  titlesVersionAck?: number;
  
  // Options and metadata
  movieOptions: MovieSummary[];
  boxsetOptions: BoxsetSummary[];
  releaseOptions: ReleaseSummary[];
  /** DiscDB candidate not yet linked (disc.release_id null); same shape as ReleaseSummary + link flags */
  pendingRelease?: ReleaseSummary | null;
  groupOptions: any[];
  
  // State flags
  labelDraftProcessed: boolean;
  discNameLocked: boolean;
  discSlugLocked: boolean;
  isSeries: boolean;
  discdbHit: boolean;
  /** Actual TheDiscDB match/miss for informational UI (badges); independent of discdbHit (workflow branch). */
  discdbResult?: 'hit' | 'miss' | 'error' | 'unknown' | null;
  discMode: 'copy' | 'rip';
  
  // Additional data
  lastReleaseDetails: any | null;
  releaseNameHint: string;
  releaseSlugHint: string;
  postProcessFiles: any[];
  transferDestination: any | null;
  releaseDiscs: any[];
  boxsetMovies: any[];
  movieCover: string | null;
  movieName: string | null;
  productionYear: number | null;
  
  // Function references (these need to be bound in parent)
  titleStatusFn?: (id: string | null | undefined) => string;
  titleProgressValueFn?: (id: string | null | undefined) => number;
  titleActiveFn?: (id: string | null | undefined) => boolean;
  previewUrlFn?: (t: any) => string | null;
  previewStateFn?: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null;
  titlePathFn?: (t: any) => string | null;
  
  // Stage progress functions
  stageProgressFn?: (key: 'rip' | 'label' | 'postprocess' | 'transfer' | 'upload' | 'done') => number | null;
  isStageCompletedFn?: (key: 'rip' | 'label' | 'postprocess' | 'transfer' | 'upload' | 'done') => boolean;
  stageTimeline?: Array<{ key: 'rip' | 'label' | 'postprocess' | 'transfer' | 'upload' | 'done', label: string }>;
  activeStage?: 'rip' | 'label' | 'postprocess' | 'transfer' | 'upload' | null;
  progressUpdateTrigger?: number;
  
  // Save callback
  saveCallback?: (labelForm: any) => Observable<void>;
  
  // Additional workflow state
  labelSaving: boolean;
  lastAutosaveOk: boolean;
  hasLabelContent: boolean;
  devMode: boolean;
  showTitleStatus: boolean;
  
  // Step progression tracking (Phase 1: Core Improvements)
  workflowStep?: WorkflowStep | null;  // Current step (moved from labelForm.workflow_step)
  stepNavigationSource?: 'user' | 'automatic' | 'initial';  // How step was determined
  stepCompletionState?: {
    film: boolean;
    boxset: boolean;
    disc: boolean;
    titles: boolean;
    postprocess: boolean;
    transfer: boolean;
  };

  /** Path B sorted-segment-set dedupe groups (empty when disc has none). */
  dedupeGroups?: DedupeGroup[];

  /** TMDB episode catalog for the linked TV series — populated on series confirm
   * (Path A: createAndLinkMovieToActiveContext) and on resume (Path B: workflow-context
   * hydration when labelForm.tmdb_id is set and group_type === 'series').
   * Always-absent for movies. See `_prefetchTmdbEpisodeCatalog` for trigger logic.
   * (#367 / #370)
   */
  tmdbEpisodeCatalog?: TmdbEpisodeCatalog | null;
}

export interface TmdbEpisodeSummary {
  season_number: number;
  episode_number: number;
  name: string;
  overview: string | null;
  air_date: string | null;
  runtime: number | null;
  still_url: string | null;
}

export interface TmdbSeasonEpisodes {
  tmdb_id: string;
  season_number: number;
  episodes: TmdbEpisodeSummary[];
  number_of_seasons: number;
  series_name: string | null;
}

export interface TmdbEpisodeCatalog {
  tmdb_id: string;
  numberOfSeasons: number;
  seriesName: string | null;
  seasons: Map<number, TmdbSeasonEpisodes>;
  loadingSeasons: Set<number>;
  errorSeasons: Set<number>;
}

/**
 * #325 — Rename response from `POST /releases/disc/{id}/rename`.
 * Mirrors `api.schemas.RenameResponse` / `RenamePreviewEntry`. Status
 * semantics: `preview` = dry-run only; `renamed` = moved on disk;
 * `collision` = dest exists; `missing` = source disappeared; `error` =
 * move call failed. `changed=false` rows (idempotent re-runs) are
 * excluded from the "files will change" summary count.
 */
export type RenameStatus = 'preview' | 'renamed' | 'collision' | 'missing' | 'error';

export interface RenamePreviewEntry {
  title_id: string;
  old_path: string;
  new_path: string;
  changed: boolean;
  status: RenameStatus;
  error: string | null;
}

export interface RenameResponse {
  disc_id: string;
  dry_run: boolean;
  results: RenamePreviewEntry[];
}

export interface RenameSummary {
  total: number;       // every row
  changed: number;     // rows where changed=true (the "files will change" denominator)
  succeeded: number;   // rows with status='renamed' (only meaningful after execute)
  failed: number;      // rows with status in ('collision','missing','error')
}

/**
 * Pure helper for the summary header rendered by both rename UIs
 * (history-page disc-details + workflow-labeling transfer step).
 * Lives here so the two consumers don't drift. Not a refactor — the
 * MetadataPatchBuilder extraction (#382) will own the larger DRY pass.
 */
export function renameSummary(results: RenamePreviewEntry[] | null | undefined): RenameSummary {
  const rows = results ?? [];
  const changed = rows.filter((r) => r.changed);
  return {
    total: rows.length,
    changed: changed.length,
    succeeded: changed.filter((r) => r.status === 'renamed').length,
    failed: changed.filter((r) => r.status === 'collision' || r.status === 'missing' || r.status === 'error').length,
  };
}

/**
 * Path B sorted-segment-set dedupe group, surfaced from the backend so the
 * label UI can collapse siblings behind a "Show N grouped duplicates"
 * disclosure on the representative title row, badge flagged siblings with
 * <app-obfuscation-badge>, and render <app-disagreement-compare> when
 * DiscDB and the obfuscation flag picked different siblings.
 *
 * See `Backend/core/path_b_dedupe.py` (`DedupeGroup.to_dict()`) for the
 * authoritative shape.
 */
export interface DedupeGroup {
  group_id: string;
  sorted_segment_key: string;
  duration_bucket_s: number;
  representative_title_id: string;
  representative_source: 'discdb' | 'makemkv_flag' | 'heuristic' | 'subsumption';
  sibling_title_ids: string[];
  discdb_pick_id: string | null;
  makemkv_flag_pick_id: string | null;
  disagreement?: {
    discdb_title_id?: string;
    makemkv_flag_title_id?: string;
    [k: string]: unknown;
  } | null;
}

export interface TitlePatchRequest {
  title_id: string;
  title?: string | null;
  edition?: string | null;
  description?: string | null;
  comment?: string | null;
  season?: number | null;
  episode?: number | null;
  type?: string | null;
  duration?: number | null;
  size?: number | null;
  streams?: any | null;
  order_index?: number | null;
  title_seq?: number | null;
  active?: boolean | null;
}

export interface TitlePatchResult {
  /** On a stale_seq conflict: the row as it now is, so the client can
   *  reconcile in place instead of refetching every title (#778 stage 2). */
  current_title?: any;
  title_id: string;
  success: boolean;
  error?: string | null;
  error_code?: string | null;
  updated_title?: any | null;
}

export interface TitlePatchResponse {
  titles_version: number;
  result: TitlePatchResult;
  /** Rows the server's duplicate-group sync modified as a side effect,
   *  each carrying its bumped title_seq (#775). */
  synced_titles?: any[];
}

export interface TitlePatchBatchResponse {
  titles_version: number;
  results: TitlePatchResult[];
  /** See TitlePatchResponse.synced_titles (#775). */
  synced_titles?: any[];
}

@Injectable({ providedIn: 'root' })
export class WorkflowService implements OnDestroy {
  /**
   * Get title identifier for deduplication (title_id only).
   */
  private getTitleKey(title: any, location: string): string {
    const key = title?.title_id;
    if (!key) {
      const errorMsg = `Title missing title_id at ${location}`;
      const errorData = {
        title: JSON.stringify(title),
        availableFields: Object.keys(title || {}),
        location
      };
      this.logger.error(errorMsg, errorData);
      throw new Error(errorMsg);
    }
    return key;
  }

  /**
   * Normalize duplicate_info (snake_case from API) to duplicateInfo (camelCase) for frontend.
   * See docs/DUPLICATE_DETECTION_UI.md.
   */
  private normalizeTitleDuplicateInfo(t: any): any {
    const raw = t?.duplicate_info ?? t?.duplicateInfo;
    if (!raw || typeof raw !== 'object') return null;
    const m = raw.metrics;
    const metrics =
      m && typeof m === 'object'
        ? {
            chaptersCount: typeof m.chapters_count === 'number' ? m.chapters_count : (m.chaptersCount ?? 0),
            subtitleTrackCount:
              typeof m.subtitle_track_count === 'number' ? m.subtitle_track_count : (m.subtitleTrackCount ?? 0),
            subtitleLanguageCount:
              typeof m.subtitle_language_count === 'number'
                ? m.subtitle_language_count
                : (m.subtitleLanguageCount ?? 0),
            audioScore: typeof m.audio_score === 'number' ? m.audio_score : (m.audioScore ?? 0),
            audioLanguageCount:
              typeof m.audio_language_count === 'number' ? m.audio_language_count : (m.audioLanguageCount ?? 0),
            videoBitrate:
              m.video_bitrate != null && m.video_bitrate !== ''
                ? Number(m.video_bitrate)
                : m.videoBitrate != null && m.videoBitrate !== ''
                  ? Number(m.videoBitrate)
                  : null,
            videoPixels: typeof m.video_pixels === 'number' ? m.video_pixels : (m.videoPixels ?? 0),
            scanUsable: !!(m.scan_usable ?? m.scanUsable),
          }
        : null;
    return {
      groupId: raw.group_id ?? raw.groupId ?? '',
      groupSize: typeof raw.group_size === 'number' ? raw.group_size : (raw.groupSize ?? 0),
      sameAs: Array.isArray(raw.same_as) ? raw.same_as : (raw.sameAs ?? []),
      tags: Array.isArray(raw.tags) ? raw.tags : [],
      diffTags: Array.isArray(raw.diff_tags) ? raw.diff_tags : (raw.diffTags ?? []),
      metrics,
      confidence: raw.confidence === 'high' || raw.confidence === 'medium' || raw.confidence === 'low' ? raw.confidence : 'high',
    };
  }

  private getTitleSeqFromContext(titleId: string): number {
    const current = this._activeContext$.value;
    const match = current?.titles?.find(t => t?.title_id === titleId);
    const seq = match?.title_seq;
    return typeof seq === 'number' ? seq : 0;
  }

  /** The version we last observed for this title. Read-only on purpose:
   *  the server owns version assignment (#778 stage 2). Area 5: the cache
   *  lives in the TitleStore. */
  private knownTitleSeq(titleId: string): number {
    return this.titleStore.knownSeq(titleId) || this.getTitleSeqFromContext(titleId);
  }

  private syncTitleSeqsFromTitles(titles: any[] | null | undefined): void {
    this.titleStore.learnRowSeqs(titles);
  }

  private getContextDiscKey(context: WorkflowContext | null): string | null {
    if (!context) return null;
    const discId = (context.discInfo as any)?.disc_id || (context as any)?.discId || null;
    if (discId) return discId;
    return null;
  }

  private updateTitlesVersionAck(context: WorkflowContext, versionOverride?: number): void {
    const discKey = this.getContextDiscKey(context);
    const version = versionOverride ?? context.titlesVersion;
    if (!discKey || typeof version !== 'number') return;
    context.titlesVersionAck = this.titleStore.ackVersion(discKey, version);
  }
  private readonly apiUrl = environment.apiBase ?? 'http://localhost:8000';
  private readonly wsBase = this.apiUrl.replace(/^http/, 'ws');
  
  // Context management (from WorkflowService + WorkflowContextService)
  private _activeContext$ = new BehaviorSubject<WorkflowContext | null>(null);
  private _workflowContextStatus$ = new BehaviorSubject<WorkflowContextStatus>('ready');
  private currentCard: { type: 'job' | 'drive', id: string } | null = null;
  private functionBindings: FunctionBindings = {};
  private cancelPreviousRequest$ = new Subject<void>();

  /** Monotonic save sequence per job for PATCH /jobs/{id}/workflow-context (drops stale HTTP completions). */
  private readonly _jobWorkflowContextSaveSeq = new Map<string, number>();
  
  /** Debounce subject for context_changed WebSocket events (prevents rapid-fire refetches) */
  private _contextChangedDebounce$ = new Subject<any>();
  
  // Post-process progress caching (prevents flashing to 0%)
  private lastPostProgressCache = new Map<string, number | null>();
  private lastPostprocessContextRefresh = new Map<string, number>();
  private readonly postprocessContextRefreshCooldownMs = 2000;
  private postprocessRefreshTimeouts = new Map<string, any>();
  private postprocessRefreshAttempts = new Map<string, number>();
  private readonly postprocessRefreshMaxAttempts = 4;
  private readonly postprocessRefreshDelayMs = 1500;
  
  // Coordinator observables (from WorkflowCoordinatorService)
  private _discs = new BehaviorSubject<DiscMetadata[]>([]);
  discs$ = this._discs.asObservable();
  private _insertedDiscs = new BehaviorSubject<InsertedDisc[]>([]);
  insertedDiscs$ = this._insertedDiscs.asObservable();
  private _unfinishedJobs = new BehaviorSubject<UnfinishedJob[]>([]);
  unfinishedJobs$ = this._unfinishedJobs.asObservable();
  /** Backend-emitted notifications (toast + optional action). Subscribe in ToastService and action handler. */
  private _notifications = new Subject<BackendNotification>();
  notifications$ = this._notifications.asObservable();
  /** MakeMKV update events */
  private _makemkvUpdateMessages = new Subject<any>();
  makemkvUpdateMessages$ = this._makemkvUpdateMessages.asObservable();
  /** #613: Fires when the backend's post-MakeMKV-install / cold-boot drive
   *  warmup completes. Consumers (carousel, setup makemkv step) refetch
   *  /drives/drives + sync the coordinator state when this fires — without
   *  it the carousel stays at "Insert Disc" even for discs already loaded
   *  at boot, until a udev event forces a refresh. Payload:
   *  `{ drives_count, source: 'lifespan' | 'post-install', job_id? }`. */
  private _makemkvDrivesReady = new Subject<{ drives_count: number; source: string; job_id?: string }>();
  makemkvDrivesReady$ = this._makemkvDrivesReady.asObservable();
  private _coordinatorConnected = new BehaviorSubject<boolean>(false);
  coordinatorConnected$ = this._coordinatorConnected.asObservable();
  private _coordinatorError = new BehaviorSubject<string | null>(null);
  coordinatorError$ = this._coordinatorError.asObservable();
  
  // Legacy observables for backward compatibility
  private drives$ = new BehaviorSubject<Drive[]>([]);
  private unfinishedJobsLegacy$ = new BehaviorSubject<JobStatus[]>([]);
  
  // Websocket management (from WorkflowWebsocketService)
  private readonly maxConnections = 5;
  private activeConnections = new Map<string, WebSocket>();
  private reconnectTimeouts = new Map<string, any>();
  private reconnectAttempts = new Map<string, number>();
  private isDestroyed = false;
  private websocketContexts = new Map<string, BehaviorSubject<WorkflowContext | null>>();
  private websocketProgress = new Map<string, BehaviorSubject<ProgressUpdateMessage | null>>();
  private connectionStates = new Map<string, BehaviorSubject<boolean>>();
  
  // Coordinator websocket (from WorkflowCoordinatorService)
  private coordinatorWebsocket: WebSocket | null = null;
  private coordinatorReconnectAttempts = 0;
  private maxCoordinatorReconnectAttempts = 10;
  private coordinatorReconnectTimeout: any = null;
  
  // Caching
  private driveDiscInfoCache = new Map<string, DiscDetail>();
  private jobDataCache = new Map<string, JobStatus>();
  private activeDiscContextRequests = new Map<string, Observable<WorkflowContext | null>>();
  private activeJobContextRequests = new Map<string, Observable<WorkflowContext | null>>();
  private failedJobIds = new Set<string>();
  // Area 5: the three title-state shadow caches (seq, pending text,
  // version acks) moved into TitleStore — one home, one spec.
  
  // Initial load suppression (prevent redundant refetch from WebSocket sync right after HTTP load)
  private _initialLoadSuppressUntil = new Map<string, number>();

  /** Throttle progress-driven context updates so UI updates at most every 150ms. */
  private _progressContextUpdate$ = new Subject<void>();
  private _pendingProgressUpdate: { jobId: string; jobStatus: JobStatus } | null = null;

  // ── Context Cache (LRU) ──────────────────────────────────────
  // Stores previously-viewed workflow contexts keyed by "job:{id}" or "drive:{mount}".
  // Enables instant card switching (optimistic display of cached context) and keeps
  // all cached contexts live via WebSocket updates.
  private contextCache = new Map<string, WorkflowContext>();
  private readonly CONTEXT_CACHE_MAX = 10;

  // UI Orchestration State (merged from RipperStateService)
  private uiOrchestrationState$ = new BehaviorSubject<UIOrchestrationState>({
    selectedCard: null,
    loadingInfo: false,
    unknownDisc: false,
    contextLoading: false,
    driveLoadingStates: new Map<string, boolean>(),
    backendError: null,
    driveError: null,
    driveScanState: 'idle',
  });
  
  private discInfoState$ = new BehaviorSubject<DiscInfoState>({
    lastDiscInfo: null,
    activeDiscKey: null,
    discDbState: 'unknown',
    currentDiscId: null,
    hydratedDiscHash: null,
    lookupAttemptedKey: null,
  });

  /** Dev-only override for workflow mode (DiscDB Hit vs Miss). When dev mode is on and this is non-null, context.discdbHit uses this instead of backend value. */
  private workflowModeOverride$ = new BehaviorSubject<boolean | null>(null);
  private devMode$ = new BehaviorSubject<boolean>(false);
  
  // Public observables for UI orchestration
  getUIOrchestrationState$(): Observable<UIOrchestrationState> {
    return this.uiOrchestrationState$.asObservable();
  }
  
  getDiscInfoState$(): Observable<DiscInfoState> {
    return this.discInfoState$.asObservable();
  }
  
  // Getters for current state
  getUIOrchestrationState(): UIOrchestrationState {
    return this.uiOrchestrationState$.value;
  }
  
  getDiscInfoState(): DiscInfoState {
    return this.discInfoState$.value;
  }
  
  // State updaters
  updateUIOrchestrationState(updates: Partial<UIOrchestrationState>): void {
    this.uiOrchestrationState$.next({
      ...this.uiOrchestrationState$.value,
      ...updates,
    });
  }
  
  updateDiscInfoState(updates: Partial<DiscInfoState>): void {
    this.discInfoState$.next({
      ...this.discInfoState$.value,
      ...updates,
    });
  }
  
  // Convenience methods for selectedCard
  getSelectedCard(): { type: 'drive' | 'job', id: string } | null {
    return this.uiOrchestrationState$.value.selectedCard;
  }
  
  getSelectedCard$(): Observable<{ type: 'drive' | 'job', id: string } | null> {
    return this.uiOrchestrationState$.pipe(
      map(state => state.selectedCard)
    );
  }
  
  /** SessionStorage key for last selected card (survives reload in same tab). */
  static readonly LAST_SELECTED_CARD_KEY = 'mkv-auto-last-selected-card';

  setSelectedCard(card: { type: 'drive' | 'job', id: string } | null): void {
    this.updateUIOrchestrationState({ selectedCard: card });
    try {
      if (card?.id) {
        sessionStorage.setItem(WorkflowService.LAST_SELECTED_CARD_KEY, JSON.stringify(card));
      } else {
        sessionStorage.removeItem(WorkflowService.LAST_SELECTED_CARD_KEY);
      }
    } catch {
      // Ignore sessionStorage errors (e.g. private mode)
    }
  }

  clearCardSelection(): void {
    this.updateUIOrchestrationState({ selectedCard: null });
    try {
      sessionStorage.removeItem(WorkflowService.LAST_SELECTED_CARD_KEY);
    } catch {
      // Ignore sessionStorage errors
    }
  }
  
  // ===== Computed Observables =====
  
  /**
   * Check if workflow is ready (has context and required data)
   * For drive cards: requires lastDiscInfo
   * For job cards: requires labelForm (context is ready when labelForm exists)
   */
  get isWorkflowReady$(): Observable<boolean> {
    return combineLatest([
      this.getDiscInfoState$(),
      this.getActiveContext(),
      this.getUIOrchestrationState$()
    ]).pipe(
      map(([discInfo, context, uiState]) => {
        // If context is loading, workflow is not ready
        if (uiState.contextLoading) {
          return false;
        }
        
        // For job cards: check if labelForm exists
        if (context?.type === 'job') {
          const isReady = !!context.labelForm;
          return isReady;
        }
        
        // For drive cards: require lastDiscInfo
        const isReady = !!discInfo.lastDiscInfo;
        return isReady;
      })
    );
  }
  
  /**
   * Determine if workflow should be rendered
   */
  get shouldRenderWorkflow$(): Observable<boolean> {
    return combineLatest([
      this.getUIOrchestrationState$(),
      this.getDiscInfoState$(),
    ]).pipe(
      map(([ui, discInfo]) => {
        return !ui.loadingInfo && !!discInfo.lastDiscInfo;
      })
    );
  }
  
  /**
   * Get active job ID observable
   */
  getActiveJobId$(): Observable<string | null> {
    return this._activeContext$.pipe(
      map(context => context?.jobStatus?.jobId || null)
    );
  }
  
  /**
   * Get active drive key observable
   */
  getActiveDriveKey$(): Observable<string | null> {
    return this.getDiscInfoState$().pipe(
      map(discInfo => discInfo.activeDiscKey)
    );
  }
  
  private useWebsockets = true; // Feature flag for websocket support
  private websocketFallbackActive = false;
  
  private metadataSvc = inject(MetadataService);
  private toastSvc = inject(ToastService);
  /** Area 5: single owner of title-state machinery (seq/pending caches,
   *  per-title write queue, merge rules). The service is the glue between
   *  the store and the active context — see the bridge in the ctor. */
  private titleStore = inject(TitleStore);

  constructor(
    private http: HttpClient,
    private driveSvc: DriveService,
    private jobSvc: JobService,
    private logger: LoggerService,
    private systemService: SystemService
  ) {
    // Give the TitleStore its one window into the active context. The
    // dependency stays one-directional (service → store); the store owns
    // title-state logic, the context stays the single rendering source.
    this.titleStore.attach({
      getActiveDiscKey: () => this.getContextDiscKey(this._activeContext$.value),
      getActiveTitles: () => this._activeContext$.value?.titles ?? null,
      applyTitles: (update) => this.updateContext(update as Partial<WorkflowContext>),
      titleKey: (row, context) => this.getTitleKey(row, context),
    });

    // Subscribe to drives from DriveService
    this.driveSvc.drives$.subscribe(drives => {
      this.drives$.next(drives || []);
    });

    this.systemService.getDevMode().subscribe(status => {
      this.devMode$.next(!!status?.enabled);
    });

    // Throttle progress-based context updates to reduce CD pressure during rip
    this._progressContextUpdate$.pipe(throttleTime(150)).subscribe(() => {
      const p = this._pendingProgressUpdate;
      if (p) {
        const ctx = this._activeContext$.value;
        if (ctx?.jobStatus?.jobId === p.jobId) {
          this.updateContext({ jobStatus: p.jobStatus });
        }
      }
    });

    // Debounce context_changed events: when multiple arrive within 300ms,
    // only the last one triggers an HTTP refetch. This prevents rapid-fire
    // refetches during state transitions that emit multiple context_changed events.
    this._contextChangedDebounce$.pipe(debounceTime(300)).subscribe(message => {
      this._handleDebouncedContextChanged(message);
    });

    // Fetch initial state via HTTP first (more reliable than WebSocket)
    this.fetchInitialState();

    // Delay WebSocket connection until Angular is ready
    // This prevents connection attempts before the page is fully rendered
    setTimeout(() => {
      // Connect to unified workflow websocket (for all real-time updates)
      this.connectUnified();

      // Use websockets if available, otherwise fall back to HTTP polling
      if (this.useWebsockets) {
        this.setupWebsocketSubscriptions();
      } else {
        this.setupHttpPolling();
      }
    }, 100); // Small delay to ensure Angular is ready

    // Tab-visibility resync: even when the browser keeps the WS open during
    // backgrounding, background-tab throttling delays timer-driven pipelines
    // (debounceTime, in particular) by many seconds, so context_changed
    // messages that landed while hidden may not have finished routing to the
    // UI by the time the user comes back. Force a fresh workflow-context
    // fetch on visible so the UI matches backend state without a reload.
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          this._resyncActiveWorkflowState('visibility-change');
        }
      });
    }
  }
  
  /**
   * When dev mode is on and workflow mode override is set, use override for context.discdbHit; otherwise use backend value.
   */
  private effectiveDiscdbHit(backendValue: boolean): boolean {
    if (this.devMode$.value && this.workflowModeOverride$.value !== null) {
      return this.workflowModeOverride$.value;
    }
    return backendValue;
  }

  setWorkflowModeOverride(value: boolean | null): void {
    this.workflowModeOverride$.next(value);
  }

  getWorkflowModeOverride(): Observable<boolean | null> {
    return this.workflowModeOverride$.asObservable();
  }

  ngOnDestroy(): void {
    this.isDestroyed = true;
    this.disconnectUnified();
    // Old per-disc/per-job connections removed - unified WebSocket handles all updates
  }
  
  /**
   * Sync UI orchestration and disc info state from workflow context
   */
  syncStateFromContext(context: WorkflowContext | null): void {    
    if (!context) {
      // Reset state when context is cleared
      this.updateUIOrchestrationState({
        selectedCard: null,
        loadingInfo: false,
        contextLoading: false,
      });
      this.updateDiscInfoState({
        lastDiscInfo: null,
        activeDiscKey: null,
        discDbState: 'unknown',
        currentDiscId: null,
        hydratedDiscHash: null,
      });
      return;
    }
    
    // Update disc info from context
    if (context.discInfo) {
      this.updateDiscInfoState({
        lastDiscInfo: context.discInfo,
        currentDiscId: (context.discInfo as any).disc_id || null,
        hydratedDiscHash: context.discInfo.disc_hash || null,
        discDbState: context.discdbHit ? 'hit' : 'miss',
      });
    } else {
      // When context has no discInfo (e.g. job-only context), set currentDiscId from labelForm or jobStatus when present
      const discIdFromForm = (context.labelForm as { disc_id?: string | null })?.disc_id ?? null;
      const discIdFromJob = (context.jobStatus as { disc_id?: string | null })?.disc_id ?? null;
      const currentDiscId = discIdFromForm ?? discIdFromJob ?? null;
      this.updateDiscInfoState({
        lastDiscInfo: null,
        activeDiscKey: null,
        discDbState: 'unknown',
        currentDiscId,
        hydratedDiscHash: null,
      });
    }
    
    // Update selected card from context (drive cards match carousel by mount_point, not disc UUID)
    const driveCardId =
      (context.discInfo as { mount_point?: string } | null)?.mount_point || context.id;
    this.updateUIOrchestrationState({
      selectedCard: context.type === 'job'
        ? { type: 'job', id: context.id }
        : { type: 'drive', id: driveCardId },
      loadingInfo: false,
      contextLoading: false,
    });
  }
  
  private setupWebsocketSubscriptions(): void {
    // Subscribe to inserted discs from coordinator (now internal)
    this.insertedDiscs$.subscribe(discs => {
      // Discs are managed by coordinator, we just track them for deduplication
      // The actual disc contexts will come from per-workflow websockets
    });
    
    // Subscribe to unfinished jobs from coordinator (now internal)
    this.unfinishedJobs$.subscribe(jobs => {
      // Convert UnfinishedJob[] to JobStatus[] for backward compatibility
      const jobStatuses: JobStatus[] = [];
      
      // Load job status for each unfinished job
      jobs.forEach(job => {
        // Try to get from cache first
        const cached = this.jobDataCache.get(job.job_id);
        if (cached) {
          jobStatuses.push(cached);
        } else {
          // Fetch from HTTP as fallback
          this.jobSvc.getJobStatus(job.job_id).subscribe({
            next: (jobStatus) => {
              this.jobDataCache.set(job.job_id, jobStatus);
              // Update observable
              const current = this.unfinishedJobsLegacy$.value;
              if (!current.find(j => j.jobId === job.job_id)) {
                this.unfinishedJobsLegacy$.next([...current, jobStatus]);
              }
            },
            error: () => {
              // Silently fail - job might not exist anymore
            }
          });
        }
      });
      
      // Update observable with cached jobs
      if (jobStatuses.length > 0) {
        this.unfinishedJobsLegacy$.next(jobStatuses);
      }
    });
    
    // Monitor coordinator connection state for fallback
    this.coordinatorConnected$.subscribe(connected => {
      if (!connected && !this.websocketFallbackActive) {
        // Websocket disconnected, fall back to HTTP polling
        this.websocketFallbackActive = true;
        this.setupHttpPolling();
      } else if (connected && this.websocketFallbackActive) {
        // Websocket reconnected, stop HTTP polling
        this.websocketFallbackActive = false;
      }
    });
  }
  
  private setupHttpPolling(): void {
    // Load unfinished jobs with workflow contexts from backend cache
    // This fetches all unfinished jobs with their complete workflow contexts in one call
    // Note: This is a fallback when websockets are unavailable. The coordinator should
    // provide this data via WebSocket in normal operation.
    this.loadUnfinishedJobsFromBackend().subscribe({
      next: () => {
        // Successfully loaded unfinished jobs
      },
      error: (err) => {
        // Silently handle errors - coordinator will provide data via WebSocket
        // Only log if it's not a 404 (endpoint doesn't exist) or if websocket fallback is active
        if (this.websocketFallbackActive && err.status !== 404) {
          this.logger.warn('[WorkflowService] Failed to load unfinished jobs from backend (fallback mode):', err.status || err.message);
        }
      }
    });

    // Rely on WebSocket for updates; no 30s HTTP polling.
    // if (this.websocketFallbackActive) {
    //   timer(30000, 30000)
    //     .pipe(
    //       switchMap(() => this.loadUnfinishedJobsFromBackend())
    //     )
    //     .subscribe({
    //       error: (err) => {
    //         if (err.status !== 404) {
    //           this.logger.warn('[WorkflowService] Failed to refresh unfinished jobs:', err.status || err.message);
    //         }
    //       }
    //     });
    // }
  }
  
  /**
   * Get observable of all drives.
   * @deprecated Use getDrives$() instead - derives from coordinator discs$
   */
  getDrives(): Observable<Drive[]> {
    return this.drives$.asObservable();
  }
  
  /**
   * Get observable of all drives (derived from coordinator discs$).
   * Filters discs$ for disc_state === 'in_drive' and converts to Drive[].
   */
  getDrives$(): Observable<Drive[]> {
    return this.discs$.pipe(
      map((discs: DiscMetadata[]) => {
        return discs
          .filter((d: DiscMetadata) => d.disc_state === 'in_drive')
          .map((d: DiscMetadata) => ({
            disc_num: d.disc_num || '',
            mount_point: d.mount_point || '',
            name: d.info_title || d.movie_name || undefined
          }));
      }),
      distinctUntilChanged((a, b) => a.length === b.length && a.every((d, i) => drivesEqual(d, b[i]))),
      shareReplay(1)
    );
  }
  
  /**
   * Get observable of selected drive (derived from selectedCard$ and discs$).
   * Returns the Drive object for the currently selected card if it's a drive card.
   */
  getSelectedDrive$(): Observable<Drive | null> {
    return combineLatest([
      this.getSelectedCard$(),
      this.discs$
    ]).pipe(
      map(([selectedCard, discs]) => {
        if (!selectedCard || selectedCard.type !== 'drive') {
          return null;
        }
        const disc = discs.find((d: DiscMetadata) => 
          d.mount_point === selectedCard.id && d.disc_state === 'in_drive'
        );
        if (!disc) return null;
        return {
          disc_num: disc.disc_num || '',
          mount_point: disc.mount_point || '',
          name: disc.info_title || disc.movie_name || undefined
        };
      }),
      distinctUntilChanged((a, b) => (a === null && b === null) || (a !== null && b !== null && drivesEqual(a, b))),
      shareReplay(1)
    );
  }
  
  /**
   * Select a drive by mount point.
   * Updates selectedCard to the drive card.
   */
  selectDrive(mountPoint: string): void {
    this.setSelectedCard({ type: 'drive', id: mountPoint });
    // Load context for the selected drive
    this.setContextByCard({ type: 'drive', id: mountPoint }).subscribe({
      error: (err) => this.logger.error('[WorkflowService] Failed to load context for drive', err)
    });
  }
  
  /**
   * Refresh disc info for a drive by calling backend API.
   * Returns an Observable that emits the updated DiscDetail.
   */
  refreshDiscInfo(mountPoint: string): Observable<DiscDetail> {
    return this.http.post<Drive[]>(`${this.apiUrl}/events/drive/rescan?stream=0`, {
      device: mountPoint
    }).pipe(
      switchMap(() => {
        // After rescan, the coordinator will send updated disc info via WebSocket
        // Return the current disc info from context or wait for update
        const context = this.getCurrentContext();
        if (context?.discInfo && context.discInfo.mount_point === mountPoint) {
          return of(context.discInfo);
        }
        // If no context, return a pending placeholder
        // The WebSocket will update it shortly
        return of({
          disc_num: '',
          mount_point: mountPoint,
          pending: true
        } as DiscDetail);
      }),
      catchError((err) => {
        this.logger.error('[WorkflowService] Failed to refresh disc info', err);
        return throwError(() => err);
      })
    );
  }
  
  /**
   * Get observable of unfinished jobs (legacy - returns JobStatus[]).
   */
  getUnfinishedJobs(): Observable<JobStatus[]> {
    return this.unfinishedJobsLegacy$.asObservable();
  }
  
  /**
   * Get observable of inserted discs (from coordinator).
   */
  getInsertedDiscs(): Observable<InsertedDisc[]> {
    return this.insertedDiscs$;
  }
  
  /**
   * Get observable of all discs (inserted + unfinished, from coordinator).
   */
  getDiscs(): Observable<DiscMetadata[]> {
    return this.discs$;
  }
  
  /**
   * Sort titles using stable fields only (no title name). Clusters by segment_map (see title-display-sort.util).
   */
  private sortTitles(titles: any[], titleActiveFn?: (id: string | null | undefined) => boolean): any[] {
    return sortTitlesForDisplay(titles, titleActiveFn);
  }
  
  /**
   * Update disc info cache for a drive.
   */
  updateDiscInfoCache(discNum: string, discInfo: DiscDetail | null): void {
    if (discInfo) {
      this.driveDiscInfoCache.set(discNum, discInfo);
      this.patchInDriveDiscMetadataDiscdb(discNum, discInfo);
    } else {
      this.driveDiscInfoCache.delete(discNum);
      this.patchInDriveDiscMetadataDiscdb(discNum, null);
    }
  }

  /** Sync TheDiscDB fields on in-drive carousel cards from enriched disc info (ripper page cache). */
  private patchInDriveDiscMetadataDiscdb(discNum: string, discInfo: DiscDetail | null): void {
    const current = this._discs.value;
    let changed = false;
    const next = current.map((d) => {
      if (d.disc_state !== 'in_drive' || d.disc_num !== discNum) return d;
      changed = true;
      if (!discInfo) {
        return { ...d, discdb_result: undefined, discdb_hit: undefined };
      }
      const dr = this.discdbResultFromDiscDetail(discInfo);
      const di = discInfo as unknown as Record<string, unknown>;
      const hitExplicit = di['discdb_hit'];
      return {
        ...d,
        ...(dr !== undefined ? { discdb_result: dr } : {}),
        ...(hitExplicit === true || hitExplicit === false ? { discdb_hit: hitExplicit as boolean } : {}),
      };
    });
    if (changed) {
      this._discs.next(next);
    }
  }
  
  /**
   * Get cached disc info for a drive.
   */
  getCachedDiscInfo(discNum: string): DiscDetail | null {
    return this.driveDiscInfoCache.get(discNum) || null;
  }
  
  /**
   * Get cached job data.
   */
  getCachedJobData(jobId: string): JobStatus | null {
    return this.jobDataCache.get(jobId) || null;
  }
  
  /**
   * Cache job data (exposed for components that need to update the cache).
   */
  cacheJobData(jobId: string, job: JobStatus): void {
    this.jobDataCache.set(jobId, job);
  }
  
  /**
   * Load unfinished jobs with workflow contexts from backend cache.
   * This replaces the old method of fetching jobs and then loading each one individually.
   */
  private loadUnfinishedJobsFromBackend(): Observable<void> {
    // Use lightweight summaries endpoint (~500 bytes/job vs ~960KB/job for full contexts).
    // Full workflow context is loaded on-demand when the user clicks a card (setContextByCard).
    return this.http.get<any[]>(`${this.apiUrl}/jobs/unfinished/summaries`).pipe(
      map((summaries) => {
        const jobs: UnfinishedJob[] = [];
        const currentDiscHashes = new Set<string>();
        
        // Collect disc hashes from currently inserted discs
        this.drives$.value.forEach(drive => {
          const cachedInfo = this.driveDiscInfoCache.get(drive.disc_num);
          if (cachedInfo) {
            const hash = cachedInfo.disc_hash || (cachedInfo as any)?.content_hash;
            if (hash) {
              currentDiscHashes.add(String(hash).toUpperCase());
            }
          }
        });
        // In-drive rows in _discs may have disc_hash before drive cache is updated
        for (const d of this._discs.value) {
          if (d.disc_state === 'in_drive' && d.disc_hash) {
            currentDiscHashes.add(String(d.disc_hash).toUpperCase());
          }
        }

        // Process each summary (lightweight — no full context to parse)
        summaries.forEach(summary => {
          // Filter out jobs for currently inserted discs
          const discHash = summary.disc_hash;
          if (discHash && currentDiscHashes.has(String(discHash).toUpperCase())) {
            return;
          }
          
          // Build DiscMetadata for the card carousel from the summary
          const discMetadata: DiscMetadata = {
            disc_id: summary.disc_id || summary.job_id,
            disc_num: undefined,
            mount_point: summary.mount_point || null,
            disc_hash: summary.disc_hash || null,
            disc_state: 'unfinished',
            job_id: summary.job_id,
            scan_state: 'ready',
            movie_name: summary.movie_name || null,
            release_name: summary.release_name || null,
            info_title: summary.movie_name || null,
            disc_number: summary.disc_number ?? null,
            release_image: summary.release_image || null,
            disc_format: summary.disc_format || null,
            resolution: summary.resolution || null,
            release_year: summary.release_year || null,
            production_year: summary.production_year || null,
            created_at: summary.created_at || null,
            discdb_result: summary.discdb_result ?? null,
            job_status: summary.job_status || null,
          };
          
          // Add to discs list (for card carousel)
          const currentDiscs = this._discs.value;
          const existingIdx = currentDiscs.findIndex(d => d.job_id === summary.job_id);
          if (existingIdx < 0) {
            // Only add if not already present from coordinator initial_state
            this._discs.next([...currentDiscs, discMetadata]);
          } else {
            const merged = { ...currentDiscs[existingIdx], ...discMetadata };
            const next = [...currentDiscs];
            next[existingIdx] = merged;
            this._discs.next(next);
          }
          
          jobs.push({
            job_id: summary.job_id,
            disc_id: summary.disc_id || null,
            mount_point: summary.mount_point || null,
          });
        });
        
        this._unfinishedJobs.next(jobs);
      }),
      catchError((err) => {
        if (err.status !== 404) {
          this.logger.warn('[WorkflowService] Failed to load unfinished job summaries:', err.status || err.message);
        }
        // Fallback to old workflow-contexts endpoint if summaries endpoint doesn't exist
        return this.http.get<any[]>(`${this.apiUrl}/jobs/unfinished/workflow-contexts`).pipe(
          map((contexts) => {
            const jobs: UnfinishedJob[] = [];
            contexts.forEach(context => {
              if (context.jobStatus) {
                jobs.push({
                  job_id: context.jobStatus.jobId || context.id,
                  disc_id: (context.jobStatus as any).disc_id || null,
                  mount_point: (context.jobStatus as any).mount_point || null
                });
              }
            });
            this._unfinishedJobs.next(jobs);
          }),
          catchError(() => of(undefined))
        );
      }),
      map(() => undefined)
    );
  }
  
  /**
   * Filter unfinished jobs, excluding those associated with currently inserted discs.
   * @deprecated Use loadUnfinishedJobsFromBackend() instead, which fetches from backend cache.
   */
  private filterUnfinishedJobs(jobs: JobStatus[]): JobStatus[] {
    // Get list of currently inserted disc hashes (normalized to uppercase for case-insensitive comparison)
    const currentDiscHashes = new Set<string>();
    
    // Collect disc hashes from cached disc info for each drive
    this.drives$.value.forEach(drive => {
      const cachedInfo = this.driveDiscInfoCache.get(drive.disc_num);
      if (cachedInfo) {
        const hash = cachedInfo.disc_hash || (cachedInfo as any)?.content_hash;
        if (hash) {
          currentDiscHashes.add(String(hash).toUpperCase());
        }
      }
    });
    
    // Filter jobs that are not completed or failed AND are past the rip/copy stage
    return jobs.filter(job => {
      const status = job.job_status?.toLowerCase();
      const ripState = job.pipeline?.['rip']?.toLowerCase() || job.rip_state?.toLowerCase() || '';
      
      // Exclude jobs that:
      // 1. Are completed or failed overall
      if (status === 'completed' || status === 'failed') {
        return false;
      }
      
      // 2. Haven't finished copying (rip state is not completed or skipped)
      if (ripState !== 'completed' && ripState !== 'skipped') {
        return false;
      }
      
      // 3. Are associated with discs currently inserted in drives
      // Extract job disc hash (check all possible locations)
      const jobDiscHash = job.disc_hash || 
                          (job as any)?.disc_payload?.disc_hash || 
                          (job as any)?.disc_payload?.content_hash ||
                          (job as any)?.disc?.disc_hash ||
                          (job as any)?.disc?.content_hash || 
                          null;
      
      // Only check by disc_hash (unique per disc - disc_num can be reused for different discs)
      // Normalize hash to uppercase for case-insensitive comparison
      if (jobDiscHash && currentDiscHashes.has(String(jobDiscHash).toUpperCase())) {
        return false; // Job is for currently inserted disc
      }
      
      // If job has no hash, don't filter it out (show as unfinished job)
      
      return true;
    });
  }
  
  getActiveContext(): Observable<WorkflowContext | null> {
    return this._activeContext$.asObservable();
  }

  /** Readiness of the active workflow context (loading | ready | stale | pending | error). */
  getWorkflowContextStatus$(): Observable<WorkflowContextStatus> {
    return this._workflowContextStatus$.asObservable();
  }

  /**
   * Get rip_progress directly from context - simple observable, no processing
   * Backend sends rip_progress in context updates, we just extract and expose it
   */
  getRipProgress$(): Observable<number | null> {
    return this._activeContext$.asObservable().pipe(
      map(context => context?.jobStatus?.rip_progress ?? null),
      distinctUntilChanged()
    );
  }
  
  setActiveContext(id: string, type: 'job' | 'drive'): void {
    // Since we no longer cache contexts, trigger a fetch via setContextByCard
    // This maintains backward compatibility for components that call this method
    this.setContextByCard({ type, id }).subscribe({
      next: () => {
        // Context loaded and set as active
      },
      error: (err) => {
        this.logger.warn('[WorkflowService] Failed to set active context:', err);
        this._activeContext$.next(null);
      }
    });
  }
  
  
  buildContextFromJob(
    job: JobStatus,
    movieOptions: MovieSummary[],
    boxsetOptions: BoxsetSummary[],
    releaseOptions: ReleaseSummary[],
    groupOptions: any[],
    titleStatusFn?: (id: string | null | undefined) => string,
    titleProgressValueFn?: (id: string | null | undefined) => number,
    titleActiveFn?: (id: string | null | undefined) => boolean,
    previewUrlFn?: (t: any) => string | null,
    previewStateFn?: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null,
    titlePathFn?: (t: any) => string | null,
    postProcessFiles: any[] = [],
    transferDestination: any = null,
    releaseDiscs: any[] = [],
    boxsetMovies: any[] = [],
    devMode: boolean = false,
    lastReleaseDetails: any | null = null,
    lastManualReleaseDetails: any | null = null
  ): WorkflowContext {
    const payload = (job as any)?.disc_payload?.label_payload || (job as any)?.label_payload || job.label_draft || null;
    const discPayload = (job as any)?.disc_payload || null;
    
    let labelForm: any = null;
    let titles: any[] = [];
    let titleOrder: string[] = [];
    
    if (payload) {
      labelForm = this.buildLabelForm(payload, false, lastReleaseDetails, lastManualReleaseDetails);

      // Auto-populate group_type from selected movie when job has movie_id (so Movie/Series toggle matches)
      if (labelForm?.movie_id && Array.isArray(movieOptions) && movieOptions.length > 0) {
        const movie = movieOptions.find((m: any) => m.id === labelForm.movie_id);
        if (movie && (movie.tmdb_type === 'tv' || movie.tmdb_type === 'movie')) {
          labelForm.group_type = movie.tmdb_type === 'tv' ? 'series' : 'movie';
          labelForm.mode = labelForm.group_type;
        }
      }

      // Merge boxset/release from job and disc_payload so step completion sees assigned boxset/release.
      // label_draft only has movie_id/group_type; backend puts boxset_id/release_id on JobStatus and in disc_payload.
      const j = job as any;
      if (labelForm && (j?.boxset_id ?? j?.release_id ?? discPayload?.release_name ?? discPayload?.release_slug)) {
        if (j.boxset_id != null && j.boxset_id !== '') {
          labelForm.boxset_id = j.boxset_id;
        }
        if (j.release_id != null && j.release_id !== '') {
          labelForm.release_id = j.release_id;
        }
        if (discPayload?.release_name != null && discPayload.release_name !== '') {
          labelForm.release_name = labelForm.release_name ?? discPayload.release_name;
        }
        if (discPayload?.release_slug != null && discPayload.release_slug !== '') {
          labelForm.release_slug = labelForm.release_slug ?? discPayload.release_slug;
        }
      }

      // Load titles from job payload
      if (discPayload?.titles) {
        titles = Object.entries(discPayload.titles)
          .map(([src, t]: [string, any]) => {
            const titleId = t?.title_id || null;
            if (!titleId) return null;
            const dup = this.normalizeTitleDuplicateInfo(t);
            return {
              ...(t || {}),
              src: titleId,
              title_id: titleId,
              source_file: t?.source_file || t?.file || null,
              file: t?.file || null,
              mode: 'copy' as const,
              ...(dup ? { duplicateInfo: dup } : {}),
            };
          })
          .filter((t: any) => !!t);
        // Deduplicate titles - only use title_id or source_file for identification
        // Keep first occurrence when duplicates are found
        // This prevents duplicates when backend has entries with different dictionary keys
        // that resolve to the same title_id (e.g., "13" and "00102.mpls" both -> same title_id)
        const seen = new Set<string>();
        titles = titles.filter(t => {
          try {
            const key = this.getTitleKey(t, 'buildContextFromJob:discPayload');
            if (seen.has(key)) {
              return false;
            }
            seen.add(key);
            return true;
          } catch (error) {
            // Error already logged in getTitleKey, skip this title
            return false;
          }
        });
        // Sort titles and update order_index based on sorted position
        titles = this.sortTitles(titles, titleActiveFn);
        titles.forEach((t, idx) => {
          if (t.order_index === undefined || t.order_index === null) {
            t.order_index = idx;
          }
        });
        titleOrder = titles.map(t => t.src);
      } else if (payload?.titles) {
        // Fallback to titles in label payload
        titles = Object.entries(payload.titles)
          .map(([src, t]: [string, any]) => {
            const titleId = t?.title_id || null;
            if (!titleId) return null;
            const dup = this.normalizeTitleDuplicateInfo(t);
            return {
              ...(t || {}),
              src: titleId,
              title_id: titleId,
              source_file: t?.source_file || t?.file || null,
              file: t?.file || null,
              mode: 'copy' as const,
              ...(dup ? { duplicateInfo: dup } : {}),
            };
          })
          .filter((t: any) => !!t);
        // Deduplicate titles - only use title_id or source_file for identification
        // Keep first occurrence when duplicates are found
        const seen = new Set<string>();
        titles = titles.filter(t => {
          try {
            const key = this.getTitleKey(t, 'buildContextFromJob:payload');
            if (seen.has(key)) {
              return false;
            }
            seen.add(key);
            return true;
          } catch (error) {
            // Error already logged in getTitleKey, skip this title
            return false;
          }
        });
        // Sort titles and update order_index based on sorted position
        titles = this.sortTitles(titles, titleActiveFn);
        titles.forEach((t, idx) => {
          if (t.order_index === undefined || t.order_index === null) {
            t.order_index = idx;
          }
        });
        titleOrder = titles.map(t => t.src);
      }
    }
    
    // Extract metadata
    const sp = job.stage_profile;
    let backendWorkflowHitPath: boolean;
    if (sp === 'hit' || sp === 'miss') {
      backendWorkflowHitPath = sp === 'hit';
    } else {
      backendWorkflowHitPath = !!(
        job.discdb_result === 'hit' ||
        (discPayload && !discPayload.label_required)
      );
    }
    const isDiscDbHit = this.effectiveDiscdbHit(backendWorkflowHitPath);
    const isSeries = (discPayload?.group_type || job.group_type || 'movie') === 'series';
    
    // Extract movie/release info
    const movieName = job.movie_name || discPayload?.movie_name || null;
    const productionYear = job.release_year || discPayload?.production_year || discPayload?.release_year || null;
    const movieCover = discPayload?.movie_cover_url || discPayload?.release_image || null;
    
    // Build discInfo from job's disc_payload if available.
    // Prefer explicit disc_payload keys (including null) over labelForm so backend null clears stale UI values.
    const pickDiscPayloadField = <K extends keyof typeof discPayload>(
      key: K
    ): (typeof discPayload)[K] | null | undefined => {
      if (!discPayload || !Object.prototype.hasOwnProperty.call(discPayload, key)) {
        return (labelForm as any)?.[key] ?? null;
      }
      return (discPayload as any)[key];
    };
    let discInfo: DiscDetail | null = null;
    if (discPayload) {
      discInfo = {
        disc_num: (job as any).disc_num || discPayload.disc_num || '',
        mount_point: (job as any).mount_point || discPayload.mount_point || '',
        disc_id: discPayload.disc_id || null,
        disc_hash: discPayload.disc_hash || discPayload.content_hash || null,
        disc_name: (pickDiscPayloadField('disc_name') as string | null | undefined) ?? null,
        disc_format: (pickDiscPayloadField('disc_format') as string | null | undefined) ?? null,
        disc_slug: (pickDiscPayloadField('disc_slug') as string | null | undefined) ?? null,
        disc_number: (pickDiscPayloadField('disc_number') as number | null | undefined) ?? null,
        release_image: discPayload.release_image || discPayload.movie_cover_url || movieCover || undefined,
        movie_name: discPayload.movie_name || movieName || null,
        movie_id: discPayload.movie_id || labelForm?.movie_id || null,
        production_year: discPayload.production_year || discPayload.release_year || productionYear || null,
      } as DiscDetail;
    }

    // Keep labelForm disc fields aligned with disc_payload (authoritative on job snapshots; null clears stale label_payload).
    if (labelForm && discPayload) {
      for (const key of ['disc_name', 'disc_slug', 'disc_number', 'disc_format'] as const) {
        if (Object.prototype.hasOwnProperty.call(discPayload, key)) {
          (labelForm as any)[key] = (discPayload as any)[key];
        }
      }
    }
    
    // Migrate workflow_step, highest_step_visited, and step_navigation_source from labelForm to top-level
    let workflowStep = labelForm?.workflow_step ? (labelForm.workflow_step as WorkflowStep) : null;
    const highestStepVisited = labelForm?.highest_step_visited ? (labelForm.highest_step_visited as WorkflowStep) : null;
    const stepNavigationSource = labelForm?.step_navigation_source ? (labelForm.step_navigation_source as 'user' | 'automatic' | 'initial') : null;
    if ((workflowStep || highestStepVisited || stepNavigationSource) && labelForm) {
      // Remove migrated fields from labelForm
      const { workflow_step, highest_step_visited, step_navigation_source, ...labelFormWithoutStep } = labelForm;
      labelForm = labelFormWithoutStep;
    }
    
    // Clear jobStatus only when job failed before/during rip. If rip completed but job later failed,
    // keep jobStatus so the action bar shows the correct stage (e.g. Label) instead of "0% Copy".
    let jobStatus: JobStatus | null = job;
    if (jobStatus) {
      const jobStatusValue = jobStatus.job_status;
      const ripState = jobStatus.rip_state || jobStatus.pipeline?.['rip'];
      const isFailed = jobStatusValue === 'failed' || ripState === 'failed';
      const ripCompleted = ripState === 'completed';
      if (isFailed && !ripCompleted) {
        jobStatus = null; // Clear when rip never completed - show workflow shell
        workflowStep = isDiscDbHit ? 'summary' : 'film';
      }
    }
    
    const context: WorkflowContext = {
      id: job.jobId,
      type: 'job',
      labelForm,
      jobStatus: jobStatus,
      discInfo,
      titles,
      titleOrder,
      titlesComplete: titles.length > 0, // Titles are complete if we have titles from job payload
      movieOptions,
      boxsetOptions,
      releaseOptions,
      groupOptions,
      labelDraftProcessed: !!payload,
      discNameLocked: !!payload?.disc_name,
      discSlugLocked: !!payload?.disc_slug,
      isSeries,
      discdbHit: isDiscDbHit,
      discdbResult: this.discdbResultFromJobSources(job, discPayload, discInfo),
      discMode: 'copy', // Default, can be overridden
      lastReleaseDetails: null, // Will be set by parent if needed
      releaseNameHint: '',
      releaseSlugHint: '',
      postProcessFiles,
      transferDestination,
      releaseDiscs,
      boxsetMovies,
      movieCover,
      movieName,
      productionYear,
      titleStatusFn,
      // Step progression tracking (Phase 1)
      workflowStep: workflowStep || null,
      stepNavigationSource: stepNavigationSource || (workflowStep ? 'initial' : 'initial'),
      stepCompletionState: undefined, // Will be computed in updateContext
      titleProgressValueFn,
      titleActiveFn,
      previewUrlFn,
      previewStateFn,
      titlePathFn,
      labelSaving: false,
      lastAutosaveOk: true,
      hasLabelContent: false,
      devMode,
      showTitleStatus: true,
    };
    
    return context;
  }
  
  buildContextFromDisc(
    discInfo: DiscDetail,
    mountPoint: string,
    movieOptions: MovieSummary[],
    boxsetOptions: BoxsetSummary[],
    releaseOptions: ReleaseSummary[],
    groupOptions: any[],
    lastReleaseDetails: any | null,
    lastManualReleaseDetails: any | null = null,
    releaseNameHint: string,
    releaseSlugHint: string,
    titleStatusFn?: (id: string | null | undefined) => string,
    titleProgressValueFn?: (id: string | null | undefined) => number,
    titleActiveFn?: (id: string | null | undefined) => boolean,
    previewUrlFn?: (t: any) => string | null,
    previewStateFn?: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null,
    titlePathFn?: (t: any) => string | null,
    postProcessFiles: any[] = [],
    transferDestination: any = null,
    releaseDiscs: any[] = [],
    boxsetMovies: any[] = [],
    devMode: boolean = false,
    discMode: 'copy' | 'rip' = 'copy',
    labelDraftProcessed: boolean = false,
    discNameLocked: boolean = false,
    discSlugLocked: boolean = false,
    labelSaving: boolean = false,
    lastAutosaveOk: boolean = true,
    hasLabelContent: boolean = false
  ): WorkflowContext {
    // Build labelForm from disc info if available
    let labelForm: any = null;
    let titles: any[] = [];
    let titleOrder: string[] = [];
    
    // Extract titles from discInfo
    if (discInfo.titles) {
      titles = Object.entries(discInfo.titles)
        .map(([src, t]: [string, any]) => {
          const titleId = t?.title_id || null;
          if (!titleId) return null;
          const dup = this.normalizeTitleDuplicateInfo(t);
          return {
            src: titleId,
            title_id: titleId,
            source_file: t?.source_file || t?.file || null,
            ...(t || {}),
            mode: discMode,
            ...(dup ? { duplicateInfo: dup } : {}),
          };
        })
        .filter((t: any) => !!t);
      // Deduplicate titles - only use title_id or source_file for identification
      // Keep first occurrence when duplicates are found
      const seen = new Set<string>();
      const duplicatesBeforeFilter: string[] = [];
      titles = titles.filter(t => {
          try {
            const key = this.getTitleKey(t, 'buildContextFromDisc');
            if (seen.has(key)) {
              return false;
            }
          seen.add(key);
          return true;
        } catch (error) {
          // Error already logged in getTitleKey, skip this title
          return false;
        }
      });
      // Sort titles and update order_index based on sorted position
      titles = this.sortTitles(titles);
      titles.forEach((t, idx) => {
        if (t.order_index === undefined || t.order_index === null) {
          t.order_index = idx;
        }
      });
      titleOrder = titles.map(t => t.src);
    }
    
    const di = discInfo as any;
    const backendWorkflowHitPath = di?.label_required === false && di?.discdb_hit === true;
    const isDiscDbHit = this.effectiveDiscdbHit(backendWorkflowHitPath);
    const isSeries = (discInfo.group_type || 'movie') === 'series';

    // Extract movie/release info
    const movieName = (discInfo as any)?.movie_name || null;
    const productionYear = (discInfo as any)?.production_year || (discInfo as any)?.release_year || null;
    const movieCover = (discInfo as any)?.movie_cover_url || (discInfo as any)?.release_image || null;
    
    // Migrate workflow_step, highest_step_visited, and step_navigation_source from labelForm to top-level
    const workflowStep = labelForm?.workflow_step ? (labelForm.workflow_step as WorkflowStep) : null;
    const highestStepVisited = labelForm?.highest_step_visited ? (labelForm.highest_step_visited as WorkflowStep) : null;
    const stepNavigationSource = labelForm?.step_navigation_source ? (labelForm.step_navigation_source as 'user' | 'automatic' | 'initial') : null;
    if ((workflowStep || highestStepVisited || stepNavigationSource) && labelForm) {
      // Remove migrated fields from labelForm
      const { workflow_step, highest_step_visited, step_navigation_source, ...labelFormWithoutStep } = labelForm;
      labelForm = labelFormWithoutStep;
    }
    
    const context: WorkflowContext = {
      id: mountPoint,
      type: 'drive',
      labelForm,
      jobStatus: null,
      discInfo: discInfo,
      titles,
      titleOrder,
      titlesComplete: titles.length > 0, // Titles are complete if we have titles from discInfo
      movieOptions,
      boxsetOptions,
      releaseOptions,
      groupOptions,
      labelDraftProcessed,
      discNameLocked,
      discSlugLocked,
      isSeries,
      discdbHit: isDiscDbHit,
      discdbResult: this.discdbResultFromDiscDetail(discInfo),
      discMode,
      lastReleaseDetails,
      releaseNameHint,
      releaseSlugHint,
      postProcessFiles,
      transferDestination,
      releaseDiscs,
      boxsetMovies,
      movieCover,
      movieName,
      productionYear,
      // Step progression tracking (Phase 1)
      workflowStep: workflowStep || null,
      stepNavigationSource: stepNavigationSource || (workflowStep ? 'initial' : 'initial'),
      stepCompletionState: undefined, // Will be computed in updateContext
      titleStatusFn,
      titleProgressValueFn,
      titleActiveFn,
      previewUrlFn,
      previewStateFn,
      titlePathFn,
      labelSaving,
      lastAutosaveOk,
      hasLabelContent,
      devMode,
      showTitleStatus: true,
    };

    return context;
  }

  preloadContexts(
    jobs: JobStatus[],
    movieOptions: MovieSummary[],
    boxsetOptions: BoxsetSummary[],
    releaseOptions: ReleaseSummary[],
    groupOptions: any[],
    titleStatusFn?: (id: string | null | undefined) => string,
    titleProgressValueFn?: (id: string | null | undefined) => number,
    titleActiveFn?: (id: string | null | undefined) => boolean,
    previewUrlFn?: (t: any) => string | null,
    previewStateFn?: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null,
    titlePathFn?: (t: any) => string | null,
    postProcessFiles: any[] = [],
    transferDestination: any = null,
    releaseDiscs: any[] = [],
    boxsetMovies: any[] = [],
    devMode: boolean = false,
    lastReleaseDetails: any | null = null,
    lastManualReleaseDetails: any | null = null
  ): void {
    if (jobs.length === 0) return;
    
    // Build context for first job immediately (for auto-selection)
    const firstJob = jobs[0];
    const firstContext = this.buildContextFromJob(
      firstJob,
      movieOptions,
      boxsetOptions,
      releaseOptions,
      groupOptions,
      titleStatusFn,
      titleProgressValueFn,
      titleActiveFn,
      previewUrlFn,
      previewStateFn,
      titlePathFn,
      postProcessFiles,
      transferDestination,
      releaseDiscs,
      boxsetMovies,
      devMode,
      lastReleaseDetails,
      lastManualReleaseDetails
    );
    // Contexts are no longer cached - they will be fetched on demand when cards are selected
    // Queue remaining jobs for async processing (no longer needed since we don't cache, but keep for compatibility)
    if (jobs.length > 1) {
      const remainingJobs = jobs.slice(1);
      let index = 0;
      
      const processNext = () => {
        if (index >= remainingJobs.length) return;
        
        const job = remainingJobs[index];
        // Contexts are no longer cached - they will be fetched on demand
        // This processing loop is kept for backward compatibility but doesn't cache contexts
        
        index++;
        // Process next job after a short delay to avoid blocking
        setTimeout(processNext, 10);
      };
      
      // Start processing after initial render
      setTimeout(processNext, 100);
    }
  }
  
  
  /**
   * Fetch workflow context from backend for a disc (by mount_point or disc_id).
   */
  fetchDiscWorkflowContext(identifier: string, useDiscId: boolean = false, mountPoint?: string, include?: string): Observable<WorkflowContext | null> {
    // Create a unique cache key for this request (used for active request tracking only)
    const cacheKey = useDiscId ? `disc_id:${identifier}` : `mount:${identifier}`;
    
    // Check if there's already an active request for this context
    const activeRequest = this.activeDiscContextRequests.get(cacheKey);
    if (activeRequest) {
      return activeRequest;
    }
    
    // Always fetch from HTTP
    return this.fetchDiscWorkflowContextHttp(identifier, useDiscId, mountPoint, { include });
  }
  
  private fetchDiscWorkflowContextHttp(
    identifier: string,
    useDiscId: boolean = false,
    mountPoint?: string,
    options: { suppressLoading?: boolean; include?: string } = {}
  ): Observable<WorkflowContext | null> {
    const cacheKey = useDiscId ? `disc_id:${identifier}` : `mount:${identifier}`;
    
    // Set loading state when starting request (similar to fetchJobWorkflowContextHttp)
    if (!options.suppressLoading) {
      this.updateUIOrchestrationState({ contextLoading: true });
    }
    
    const url = useDiscId
      ? `${this.apiUrl}/discs/${identifier}/workflow-context`
      : `${this.apiUrl}/discs/workflow-context`;
    // Build params: mount_point for non-disc-id requests, include for deferred loading
    const params: any = {};
    if (!useDiscId) {
      params.mount_point = identifier;
    }
    if (options.include) {
      params.include = options.include;
    }
    const httpOptions = { params };
    // Create the request and share it to prevent duplicates (params encoded by HttpClient)
    const request$ = this.http.get<any>(url, httpOptions).pipe(
      map(response => {
        const context = this._convertApiResponseToContext(response);
        // Remove from active requests
        this.activeDiscContextRequests.delete(cacheKey);
        // Clear loading state on success
        if (!options.suppressLoading) {
          this.updateUIOrchestrationState({ contextLoading: false });
        }
        return context;
      }),
      catchError(error => {
        // Remove from active requests on error
        this.activeDiscContextRequests.delete(cacheKey);
        // Clear loading state on error
        if (!options.suppressLoading) {
          this.updateUIOrchestrationState({ contextLoading: false });
        }
        // 404 is expected when workflow context doesn't exist yet - return null and let caller handle it
        if (error.status === 404) {
          return of(null);
        }
        throw error;
      }),
      shareReplay(1) // Share the result with multiple subscribers
    );
    
    // Store the active request
    this.activeDiscContextRequests.set(cacheKey, request$);
    
    return request$;
  }
  
  /**
   * Fetch workflow context from backend for a job.
   */
  fetchJobWorkflowContext(jobId: string): Observable<WorkflowContext | null> {
    const cacheKey = `job:${jobId}`;
    
    // Check if there's already an active request for this context
    const activeRequest = this.activeJobContextRequests.get(cacheKey);
    if (activeRequest) {
      return activeRequest;
    }
    
    // Always fetch from HTTP
    return this.fetchJobWorkflowContextHttp(jobId);
  }
  
  private fetchJobWorkflowContextHttp(
    jobId: string,
    options: { suppressLoading?: boolean } = {}
  ): Observable<WorkflowContext | null> {
    const cacheKey = `job:${jobId}`;
    const url = `${this.apiUrl}/jobs/${jobId}/workflow-context`;
    
    // Set loading state when starting request
    if (!options.suppressLoading) {
      this.updateUIOrchestrationState({ contextLoading: true });
    }
    
    // Create the request and share it to prevent duplicates
    const request$ = this.http.get<any>(url).pipe(
      map(response => {
        const context = this._convertApiResponseToContext(response);
        // Remove from active requests
        this.activeJobContextRequests.delete(cacheKey);
        // Clear loading state on success
        if (!options.suppressLoading) {
          this.updateUIOrchestrationState({ contextLoading: false });
        }
        return context;
      }),
      catchError(error => {
        // Remove from active requests on error
        this.activeJobContextRequests.delete(cacheKey);
        // Clear loading state on error
        if (!options.suppressLoading) {
          this.updateUIOrchestrationState({ contextLoading: false });
        }
        // 404 is expected when workflow context doesn't exist yet - return null and let caller handle it
        if (error.status === 404) {
          return of(null);
        }
        throw error;
      }),
      shareReplay(1) // Share the result with multiple subscribers
    );
    
    // Store the active request
    this.activeJobContextRequests.set(cacheKey, request$);
    
    return request$;
  }
  
  /**
   * Save workflow context to backend for a disc (by mount_point or disc_id).
   * Phase 1: Includes workflowStep in labelForm for backwards compatibility
   */
  saveDiscWorkflowContext(
    identifier: string,
    labelForm: any,
    isFullUpdate: boolean = false,
    useDiscId: boolean = false
  ): Observable<WorkflowContext> {
    // Skip redundant context_changed refetch after our own save (avoids "reload" when toggling Movie/Series)
    this._lastDiscContextSaveUntil = Date.now() + 500;
    this._setContextApplySuppressFor(WorkflowService.INTERACT_SUPPRESS_MS);

    const url = useDiscId
      ? `${this.apiUrl}/discs/${identifier}/workflow-context`
      : `${this.apiUrl}/discs/workflow-context`;
    const method = isFullUpdate ? 'put' : 'patch';
    
    // Include workflowStep and stepNavigationSource in labelForm for persistence
    const context = this._activeContext$.value;
    const workflowStep = context?.workflowStep;
    const stepNavigationSource = context?.stepNavigationSource;
    // #349: Strip tracks[] from the save blob — title edits go through dedicated PATCH endpoints.
    // This reduces payload from ~600KB to ~5KB for discs with 300+ titles.
    const { tracks: _stripTracks, ...labelFormNoTracks } = labelForm || {};
    const labelFormWithStep = {
      ...labelFormNoTracks,
      ...(workflowStep ? { workflow_step: workflowStep } : {}),
      ...(stepNavigationSource ? { step_navigation_source: stepNavigationSource } : {}),
    };
    this._stripStaleTmdbIdWhenMovieIdPresent(labelFormWithStep);

    const body = { labelForm: labelFormWithStep };
    const requestOptions: { body: { labelForm: any }; params?: { mount_point: string } } = { body };
    if (!useDiscId) {
      requestOptions.params = { mount_point: identifier };
    }

    return this.http.request<any>(method.toUpperCase(), url, requestOptions).pipe(
      map(response => this._convertApiResponseToContext(response)),
      catchError((err) => {
        return throwError(() => err);
      })
    );
  }
  
  /**
   * Save workflow context to backend for a job.
   * Phase 1: Includes workflowStep in labelForm for backwards compatibility
   */
  saveJobWorkflowContext(
    jobId: string,
    labelForm: any,
    isFullUpdate: boolean = false,
    options?: { skipStaleResponseFilter?: boolean }
  ): Observable<WorkflowContext> {
    // Skip redundant context_changed refetch after our own save (avoids "reload" when toggling Movie/Series)
    this._setPostTransitionIgnore(jobId);
    this._setContextApplySuppressFor(WorkflowService.INTERACT_SUPPRESS_MS);

    const url = `${this.apiUrl}/jobs/${jobId}/workflow-context`;
    const method = isFullUpdate ? 'put' : 'patch';

    // Include workflowStep and stepNavigationSource in labelForm for persistence.
    // Context is merged first; labelForm spread last so caller’s workflow_step/step_navigation_source win
    // (critical for titles->postprocess where we must send postprocess/user).
    const context = this._activeContext$.value;
    const workflowStep = context?.workflowStep;
    const stepNavigationSource = context?.stepNavigationSource;
    // #349: Strip tracks[] from the save blob — title edits go through dedicated PATCH endpoints.
    const { tracks: _stripJobTracks, ...labelFormNoJobTracks } = labelForm || {};
    const labelFormWithStep: Record<string, unknown> = {
      ...(workflowStep ? { workflow_step: workflowStep } : {}),
      ...(stepNavigationSource ? { step_navigation_source: stepNavigationSource } : {}),
      ...labelFormNoJobTracks,
    };
    this._stripRegressiveWorkflowStepFromLabelForm(labelFormWithStep, context);
    this._ensureDiscSlugKeyForJobWorkflowPayload(labelFormWithStep);
    this._stripStaleTmdbIdWhenMovieIdPresent(labelFormWithStep);

    const body = { labelForm: labelFormWithStep };

    const skipStaleFilter = options?.skipStaleResponseFilter === true;
    const mySeq = skipStaleFilter ? 0 : this._bumpJobWorkflowContextSaveSeq(jobId);

    return this.http.request<any>(method.toUpperCase(), url, { body }).pipe(
      map(response => {
        if (!skipStaleFilter && this._jobWorkflowContextSaveSeq.get(jobId) !== mySeq) {
          return undefined;
        }
        return this._convertApiResponseToContext(response);
      }),
      filter((ctx): ctx is WorkflowContext => ctx !== undefined)
    );
  }

  private _bumpJobWorkflowContextSaveSeq(jobId: string): number {
    const prev = this._jobWorkflowContextSaveSeq.get(jobId) ?? 0;
    const next = prev + 1;
    this._jobWorkflowContextSaveSeq.set(jobId, next);
    return next;
  }

  /**
   * PATCH merge keeps absent disc_slug; send explicit empty when disc fields are present so the server can slugify.
   */
  private _ensureDiscSlugKeyForJobWorkflowPayload(labelForm: Record<string, unknown>): void {
    if (!('disc_name' in labelForm) && !('disc_slug' in labelForm)) {
      return;
    }
    const raw = labelForm['disc_slug'];
    if (raw == null || !Object.prototype.hasOwnProperty.call(labelForm, 'disc_slug')) {
      labelForm['disc_slug'] = '';
    } else {
      labelForm['disc_slug'] = typeof raw === 'string' ? raw.trim() : String(raw).trim();
    }
  }

  /**
   * When movie_id is set, omit tmdb_id from PATCH payloads so a stale TMDB string cannot override
   * the chosen movie (server uses movie_id; GET repopulates tmdb_id from DB).
   */
  private _stripStaleTmdbIdWhenMovieIdPresent(labelForm: Record<string, unknown>): void {
    const mid = labelForm['movie_id'];
    if (mid != null && String(mid).trim() !== '') {
      delete labelForm['tmdb_id'];
    }
  }

  /** Do not PATCH a workflow_step behind the persisted job row (stale client after UI-only back). */
  private _stripRegressiveWorkflowStepFromLabelForm(
    labelForm: Record<string, unknown>,
    context: WorkflowContext | null
  ): void {
    const incoming = labelForm['workflow_step'] as string | undefined;
    const server = context?.jobStatus?.workflow_step;
    if (!incoming || !server) {
      return;
    }
    const order: WorkflowStep[] = getStepOrderForContext(context);
    const si = order.indexOf(server as WorkflowStep);
    const ii = order.indexOf(incoming as WorkflowStep);
    if (si >= 0 && ii >= 0 && ii < si) {
      delete labelForm['workflow_step'];
      delete labelForm['step_navigation_source'];
    }
  }

  /** Persist a single-title patch. Area 5: the TitleStore owns the write —
   *  per-title queue (later edits to an in-flight title coalesce and send
   *  with the acked version, so one title's writes can never race each
   *  other), If-Match stamping, the one-shot user-wins stale retry, and
   *  applying the ack to the context. */
  patchDiscTitle(discId: string, patch: TitlePatchRequest): Observable<TitlePatchResponse> {
    return this.titleStore.enqueuePatch(discId, patch).pipe(
      catchError(err => {
        this.toastPipelineLockIfAny(err);
        return throwError(() => err);
      })
    );
  }

  /** #363 H1 — surface backend 409 pipeline-guard rejections
   * (labels_locked / type_change_locked) as a toast so the user learns
   * why the edit didn't stick. Other errors pass through untouched. */
  private toastPipelineLockIfAny(err: any): void {
    const detail = err?.error?.detail;
    if (err?.status === 409 && detail && typeof detail === 'object' &&
        (detail.error_code === 'labels_locked' || detail.error_code === 'type_change_locked')) {
      this.toastSvc.show(detail.message || 'Title edits are locked at this pipeline stage', 'error', 5000);
    }
  }

  /** Persist a batch of title patches. Area 5: delegated to the TitleStore
   *  (cache stamping, per-row one-shot stale retry, ack application). */
  patchDiscTitlesBatch(discId: string, patches: TitlePatchRequest[]): Observable<TitlePatchBatchResponse> {
    return this.titleStore.patchBatch(discId, patches);
  }

  /**
   * Fetch paginated titles for a disc (#349 Phase 2).
   * Returns lightweight summaries by default; use detail='full' for metadata_scan etc.
   */
  fetchDiscTitles(
    discId: string,
    offset: number = 0,
    limit: number = 50,
    detail: 'summary' | 'full' = 'summary'
  ): Observable<{ items: any[]; total: number; offset: number; limit: number; has_more: boolean }> {
    const url = `${this.apiUrl}/discs/${discId}/titles`;
    const params: any = { offset: offset.toString(), limit: limit.toString(), detail };
    return this.http.get<{ items: any[]; total: number; offset: number; limit: number; has_more: boolean }>(url, { params });
  }

  /** Set a title as the primary within its duplicate group. Backend transfers metadata from old primary. */
  setPrimary(discId: string, titleId: string): Observable<{ titles: any[] }> {
    const url = `${this.apiUrl}/discs/${discId}/titles/${titleId}/set-primary`;
    return this.http.post<{ titles: any[] }>(url, {}).pipe(
      tap((res) => {
        if (res?.titles?.length) {
          this.mergeSetPrimaryTitlesResponse(discId, res.titles);
        }
      })
    );
  }

  /**
   * Apply set-primary response rows onto current context titles (by title_id).
   * Preserves duplicateInfo, metadata_scan, segment_map, etc. not present in API rows.
   */
  private mergeSetPrimaryTitlesResponse(discId: string, returnedTitles: any[]): void {
    const current = this._activeContext$.value;
    if (!current?.titles?.length || !returnedTitles?.length) return;
    const currentDiscKey = this.getContextDiscKey(current);
    if (currentDiscKey !== discId) return;

    const byId = new Map<string, any>();
    for (const row of returnedTitles) {
      const id = row?.title_id;
      if (id) byId.set(String(id), row);
    }
    if (byId.size === 0) return;

    this.titleStore.learnRowSeqs(returnedTitles);

    const nextTitles = current.titles.map((t) => {
      let key: string;
      try {
        key = this.getTitleKey(t, 'mergeSetPrimaryTitlesResponse');
      } catch {
        return t;
      }
      const server = byId.get(key);
      if (!server) return t;
      return mergeTitleFromSetPrimaryResponse(t, server);
    });

    this.updateContext({ titles: nextTitles });
  }

  /** Preview rename: dry-run showing old → new path mapping. */
  previewRename(discId: string): Observable<RenameResponse> {
    return this.http.post<RenameResponse>(`${this.apiUrl}/releases/disc/${discId}/rename?dry_run=true`, {});
  }

  /** Execute rename: move files and update file_path on titles. */
  executeRename(discId: string): Observable<RenameResponse> {
    return this.http.post<RenameResponse>(`${this.apiUrl}/releases/disc/${discId}/rename?dry_run=false`, {});
  }

  /** Self-healing library reattach (#449). Walks the active
   * TransferConfig's transfer_dir, identifies on-disk MKVs by Matroska
   * Segment UID (or filename basename for legacy rows), and reattaches
   * them to DiscTitle.file_path. ``dryRun=true`` previews without
   * writing; ``dryRun=false`` applies the matches.
   *
   * Used by the "Verify Library Links" modal on the History page. The
   * modal calls dryRun=true on open to render the preview, then
   * dryRun=false on the Apply click. */
  verifyLibraryLinks(dryRun: boolean): Observable<LibraryReattachReport> {
    return this.http.post<LibraryReattachReport>(
      `${this.apiUrl}/releases/library/reattach?dry_run=${dryRun}`,
      {},
    );
  }

  // Area 5: applyTitlePatchResults, applyServerTitleRows,
  // handleStaleSeqConflicts and refreshTitleSeqsAfterConflict moved into
  // TitleStore (title-store.service.ts) — write acks, delta folds and
  // conflict reconciliation are the store's three inputs and live with
  // their caches. The store reaches the context through the bridge
  // attached in this service's constructor.

  /**
   * Convert API response to WorkflowContext format.
   */
  private _convertApiResponseToContext(response: any): WorkflowContext {
    // Jobs: id is job_id. Disc API uses type "disc" with id = disc_id; carousel + selectedCard use mount_point.
    const isJob = response.type === 'job';
    const mountForDisc =
      response.mountPoint ??
      response.discInfo?.mount_point ??
      null;
    const contextId = isJob
      ? response.id
      : (mountForDisc || response.discId || response.id);
    const contextType: 'job' | 'drive' = isJob ? 'job' : 'drive';
    
    // Migrate workflow_step from labelForm to top-level workflowStep.
    // Prefer persisted job.workflow_step over labelForm.workflow_step so UI-only "back" does not stick a stale step.
    let labelForm = response.labelForm;
    const profileHit = this.effectiveDiscdbHit(response.discdbHit || false);
    const stepOrder: WorkflowStep[] = getStepOrderForContext({
      discdbHit: profileHit,
      jobStatus: response.jobStatus,
    });
    const rawJobStatus = response.jobStatus as JobStatus | null | undefined;
    const stepFromJob = rawJobStatus?.workflow_step;
    const stepFromLf = labelForm?.workflow_step as string | undefined;
    const stepFromResponse = response.workflowStep as string | undefined;
    let workflowStep: WorkflowStep | null = null;
    if (stepFromJob && stepOrder.includes(stepFromJob as WorkflowStep)) {
      workflowStep = stepFromJob as WorkflowStep;
    } else if (stepFromLf && stepOrder.includes(stepFromLf as WorkflowStep)) {
      workflowStep = stepFromLf as WorkflowStep;
    } else if (stepFromResponse && stepOrder.includes(stepFromResponse as WorkflowStep)) {
      workflowStep = stepFromResponse as WorkflowStep;
    }
    if (workflowStep && labelForm && labelForm.workflow_step) {
      const { workflow_step, ...labelFormWithoutStep } = labelForm;
      labelForm = labelFormWithoutStep;
    }
    // Clear jobStatus only when job failed before/during rip (no completed rip to show). If rip completed
    // but job later failed (e.g. disc ejected during label), keep jobStatus so the action bar shows the
    // correct stage (Label) and progress instead of "0% Copy" / "Start Copy".
    let jobStatus: JobStatus | null = response.jobStatus;
    if (jobStatus) {
      const jobStatusValue = jobStatus.job_status;
      const ripState = jobStatus.rip_state || jobStatus.pipeline?.['rip'];
      const isFailed = jobStatusValue === 'failed' || ripState === 'failed';
      const ripCompleted = ripState === 'completed';
      if (isFailed && !ripCompleted) {
        jobStatus = null; // Clear failed job status when rip never completed - show workflow shell
        const discdbHit = this.effectiveDiscdbHit(response.discdbHit || false);
        workflowStep = discdbHit ? 'summary' : 'film';
      } else if (response.type === 'job' && !(jobStatus as any).jobId) {
        // Ensure job context always has jobId so progress updates only apply when message.job_id matches (no mixed context)
        jobStatus = { ...jobStatus, jobId: (jobStatus as any).job_id ?? response.id } as JobStatus;
      }
    }

    const context: WorkflowContext = {
      id: contextId,
      type: contextType,
      discNum: response.discNum,
      labelForm: labelForm,
      jobStatus: jobStatus,
      discInfo: response.discInfo || null,
      titles: (response.titles || []).map((t: any) => {
        const dup = this.normalizeTitleDuplicateInfo(t);
        return { ...t, ...(dup ? { duplicateInfo: dup } : {}) };
      }),
      titleOrder: response.titleOrder || [],
      titlesComplete: response.titlesComplete !== undefined ? response.titlesComplete : (response.titles && response.titles.length > 0),
      titlesVersion: response.titlesVersion ?? null,
      // Options are now loaded separately via MetadataService.loadWorkflowOptions().
      // Workflow context responses return empty arrays for options; merge cached options here.
      movieOptions: response.movieOptions?.length ? response.movieOptions : this.metadataSvc.getCachedOptions().movieOptions,
      boxsetOptions: response.boxsetOptions?.length ? response.boxsetOptions : this.metadataSvc.getCachedOptions().boxsetOptions,
      releaseOptions: response.releaseOptions?.length ? response.releaseOptions : this.metadataSvc.getCachedOptions().releaseOptions,
      pendingRelease: response.pendingRelease ?? null,
      groupOptions: response.groupOptions?.length ? response.groupOptions : this.metadataSvc.getCachedOptions().groupOptions,
      labelDraftProcessed: response.labelDraftProcessed || false,
      discNameLocked: response.discNameLocked || false,
      discSlugLocked: response.discSlugLocked || false,
      isSeries: response.isSeries || false,
      discdbHit: this.effectiveDiscdbHit(response.discdbHit || false),
      discdbResult: this.coalesceDiscdbResult(response.discdb_result ?? response.discdbResult, {
        jobStatus,
        discInfo: response.discInfo || null,
      }),
      discMode: response.discMode || 'copy',
      lastReleaseDetails: response.lastReleaseDetails,
      releaseNameHint: response.releaseNameHint || '',
      releaseSlugHint: response.releaseSlugHint || '',
      postProcessFiles: response.postProcessFiles || [],
      transferDestination: response.transferDestination,
      releaseDiscs: response.releaseDiscs || [],
      boxsetMovies: response.boxsetMovies || [],
      movieCover: response.movieCover,
      movieName: response.movieName,
      productionYear: response.productionYear,
      labelSaving: false,
      lastAutosaveOk: true,
      hasLabelContent: false,
      devMode: false,
      showTitleStatus: true,
      // Step progression tracking (Phase 1)
      workflowStep: workflowStep || null,
      stepNavigationSource: response.stepNavigationSource || (workflowStep ? 'initial' : 'initial'),
      stepCompletionState: response.stepCompletionState || undefined,
      dedupeGroups: Array.isArray(response.dedupeGroups) ? response.dedupeGroups : [],
    };

    // Ensure labelForm has release_id and disc_id from jobStatus when present (single source of truth for UI).
    // Only copy jobStatus.release_id onto labelForm when the form has no movie selected yet; otherwise
    // jobStatus.release_id is the disc's linked release and may belong to a different movie than the user chose.
    if (context.jobStatus && context.labelForm) {
      const js = context.jobStatus as { release_id?: string | null; disc_id?: string | null };
      const lf = context.labelForm as Record<string, unknown>;
      const formMovieId = lf['movie_id'];
      if (js.release_id != null && lf['release_id'] == null && (formMovieId == null || formMovieId === '')) {
        lf['release_id'] = js.release_id;
      }
      if (js.disc_id != null && lf['disc_id'] == null) {
        lf['disc_id'] = js.disc_id;
      }
    }

    // Set initial load suppression window to prevent redundant refetch from WebSocket sync
    // Use disc_id for disc contexts, job_id for job contexts
    const suppressionKey = response.type === 'job' 
      ? response.id 
      : (response.discInfo?.disc_id || response.discId || response.id);
    if (suppressionKey) {
      const suppressUntil = Date.now() + 2000; // 2 second window
      this._initialLoadSuppressUntil.set(suppressionKey, suppressUntil);
      this.logger.debug(`Set initial load suppression for ${suppressionKey} until ${new Date(suppressUntil).toISOString()}`);
    }

    this.updateTitlesVersionAck(context);
    return context;
  }
  
  // ===== Coordinator WebSocket Methods (from WorkflowCoordinatorService) =====
  
  private connectUnified(): void {
    if (this.isDestroyed) {
      return;
    }
    
    const wsUrl = `${this.wsBase}/ws/workflow`;
    
    try {
      this.coordinatorWebsocket = new WebSocket(wsUrl);
      
      this.coordinatorWebsocket.onopen = () => {
        this.logger.debug('[WorkflowService] Unified workflow WebSocket connected');
        this._coordinatorConnected.next(true);
        this._coordinatorError.next(null);
        // Capture reconnect state BEFORE the reset, so we can distinguish
        // "first connect after service init" (constructor already called
        // fetchInitialState() at line 847) from "reconnect after drop".
        const wasReconnect = this.coordinatorReconnectAttempts > 0;
        this.coordinatorReconnectAttempts = 0;
        if (wasReconnect) {
          // Any context_changed / progress_update / disc_updated the backend
          // emitted while this socket was closed is lost — the backend does
          // not buffer per-client WS traffic. Without a resync here, a
          // background-tab close/reopen (or an OS sleep/wake) leaves the UI
          // stuck showing whatever state was cached before the drop, even
          // though the backend has since advanced (e.g. postprocess or
          // transfer completed while the tab was hidden). Manual page reload
          // was the only recovery. Trigger a coordinator refresh + active
          // workflow context refetch so the UI catches up automatically.
          this.logger.log('[WorkflowService] WS reconnected — resyncing state');
          this._resyncActiveWorkflowState('ws-reconnect');
        }
      };
      
      this.coordinatorWebsocket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          this.handleUnifiedMessage(message);
        } catch (err) {
          this.logger.error('[WorkflowService] Failed to parse unified message:', err);
        }
      };
      
      this.coordinatorWebsocket.onerror = (error) => {
        this.logger.error('[WorkflowService] Unified workflow WebSocket error:', error);
        this._coordinatorError.next('WebSocket connection error');
      };
      
      this.coordinatorWebsocket.onclose = (event) => {
        this.logger.debug('[WorkflowService] Unified workflow WebSocket closed:', event.code, event.reason);
        this._coordinatorConnected.next(false);
        this.coordinatorWebsocket = null;
        
        // Attempt reconnection if not destroyed and not a normal closure
        if (!this.isDestroyed && event.code !== 1000) {
          this.scheduleUnifiedReconnect();
        }
      };
    } catch (err) {
      this.logger.error('[WorkflowService] Failed to create unified workflow WebSocket:', err);
      this._coordinatorError.next('Failed to create WebSocket connection');
      this.scheduleUnifiedReconnect();
    }
  }
  
  private scheduleUnifiedReconnect(): void {
    if (this.isDestroyed || this.coordinatorReconnectAttempts >= this.maxCoordinatorReconnectAttempts) {
      if (this.coordinatorReconnectAttempts >= this.maxCoordinatorReconnectAttempts) {
        this.logger.error('[WorkflowService] Max unified workflow reconnection attempts reached');
        this._coordinatorError.next('Max reconnection attempts reached');
      }
      return;
    }
    
    this.coordinatorReconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(2, this.coordinatorReconnectAttempts), 30000);
    this.logger.debug(`[WorkflowService] Scheduling unified workflow reconnect in ${delay}ms (attempt ${this.coordinatorReconnectAttempts})`);
    
    this.coordinatorReconnectTimeout = setTimeout(() => {
      this.coordinatorReconnectTimeout = null;
      this.connectUnified();
    }, delay);
  }
  
  /**
   * Apply the same disc/jobs mutations the `job_finished` WS handler applies.
   * Used both from the WS handler (canonical path) and from `finishJob()` for an
   * optimistic local update so the carousel card disappears at click time
   * rather than after the WS round-trip. Idempotent — running twice on the same
   * already-finished job state is a no-op.
   */
  private applyJobFinishedLocally(
    jobId: string | null | undefined,
    discId: string | null | undefined,
    jobStatus: string | undefined
  ): void {
    const discsAfterFinish = this._discs.value
      .filter(d => !(d.disc_state === 'unfinished' && d.job_id === jobId))
      .map(d => {
        if (d.disc_state !== 'in_drive') return d;
        const matchesJob = jobId && d.job_id === jobId;
        const matchesDisc = discId && d.disc_id === discId;
        if (jobStatus === 'failed' && (matchesJob || matchesDisc)) {
          return {
            ...d,
            job_id: (jobId as string) ?? d.job_id,
            job_status: 'failed',
            has_completed_job: false,
          };
        }
        // Completed (or legacy messages without job_status): green check, clear job.
        // Also flip finalized=true so the "Already in Library" card renders on the
        // next click of this disc — mirrors the backend's coordinator-side
        // derivation at websockets.py:190 (finalized=true when the in-drive disc
        // has a linked release AND (disc.finalized OR has_completed_job)). Without
        // this the AlreadyInLibraryCardComponent (which gates on discs[].finalized)
        // stays hidden until a coordinator refetch — visible to the user as
        // "the card only appears after a page reload".
        //
        // We don't have finalized_release_id/slug on the local DiscMetadata
        // (backend only sends those inside the finalized_* fields), so the
        // "Open in Library" link stays hidden until finishJob's post-success
        // fetchInitialState refreshes discs$ with the authoritative fields.
        if (matchesJob && jobStatus !== 'failed') {
          const hasLinkedRelease = !!(d.movie_name || d.release_name);
          return {
            ...d,
            job_id: null,
            has_completed_job: true,
            job_status: null,
            ...(hasLinkedRelease ? {
              finalized: true,
              finalized_release_name:
                d.finalized_release_name || d.movie_name || d.release_name || null,
            } : {}),
          };
        }
        return d;
      });
    this._discs.next(discsAfterFinish);
    const jobsAfterFinish = this._unfinishedJobs.value.filter(
      j => j.job_id !== jobId
    );
    this._unfinishedJobs.next(jobsAfterFinish);

    // If the finished job is the currently active context, clear it so the right
    // pane unloads alongside the disappearing card. (Selection is cleared by the
    // caller via setSelectedCard(null), but the activeContext is not.)
    const active = this._activeContext$.value;
    if (active && active.type === 'job' && jobId && active.id === jobId) {
      this._activeContext$.next(null);
    }
  }

  private handleUnifiedMessage(message: any): void {
    const messageType = message.type;

    switch (messageType) {
      case 'ping':
        if (this.coordinatorWebsocket && this.coordinatorWebsocket.readyState === WebSocket.OPEN) {
          try {
            this.coordinatorWebsocket.send(JSON.stringify({ type: 'pong' }));
            this.logger.debug('[WorkflowService] Responded to ping with pong');
          } catch (err) {
            this.logger.error('[WorkflowService] Error sending pong:', err);
          }
        }
        break;
        
      case 'pong':
        this.logger.debug('[WorkflowService] Received pong from server');
        break;
        
      case 'initial_state':
        // Handle initial state (now comes from HTTP, but can also come from request_sync)
        // New unified discs array (preferred)
        if (message.discs) {
          this._discs.next(message.discs);
          // Derive inserted_discs and unfinished_jobs from unified discs for backward compatibility
          const inserted = message.discs
            .filter((d: DiscMetadata) => d.disc_state === 'in_drive')
            .map((d: DiscMetadata) => ({
              disc_id: d.disc_id,
              disc_num: d.disc_num || '',
              mount_point: d.mount_point || '',
              disc_hash: d.disc_hash || undefined,
            }));
          const unfinished = message.discs
            .filter((d: DiscMetadata) => d.disc_state === 'unfinished')
            .map((d: DiscMetadata) => ({
              job_id: d.job_id || '',
              disc_id: d.disc_id,
              mount_point: d.mount_point || null,
            }));
          this._insertedDiscs.next(inserted);
          this._unfinishedJobs.next(unfinished);
        } else {
          // Fallback to legacy format
          this._insertedDiscs.next(message.inserted_discs || []);
          this._unfinishedJobs.next(message.unfinished_jobs || []);
        }
        this.logger.debug('[WorkflowService] Received initial state:', {
          discs: message.discs?.length || message.inserted_discs?.length || 0,
          jobs: message.unfinished_jobs?.length || 0,
        });
        break;
        
      case 'titles_changed':
        // Area 4 of the title-state redesign: the event carries the changed
        // rows themselves (id, new title_seq, resolved fields, provenance).
        // Fold them per row — no debounce, no ~1MB context refetch, no
        // timing windows. Per-row seq gating makes this idempotent: the tab
        // that made the write already applied these values from its PATCH
        // response (same seq), so its own echo is a no-op; other tabs
        // converge immediately.
        this.titleStore.foldServerRows(message.disc_id, message.titles || [], message.titles_version);
        break;

      case 'context_changed':
        // Skip fetch during POST-driven transition ignore window (e.g. after /label/complete)
        if (message.job_id && this._postTransitionIgnore && this._postTransitionIgnore.jobId === message.job_id && Date.now() < this._postTransitionIgnore.until) {
          break;
        }
        // Skip refetch when we just saved disc context (e.g. Movie/Series toggle on film step)
        if (message.disc_id && Date.now() < this._lastDiscContextSaveUntil) {
          break;
        }
        // Skip applying context to active card while user recently interacted (only progress updates should update UI)
        if (Date.now() < this._contextApplySuppressUntil && this._contextChangedMatchesActiveCard(message)) {
          break;
        }
        // Do not refetch while a step-advance POST is in flight for this context (ignore context_changed until ready)
        if (this._workflowContextStatus$.value === 'pending' && this._contextChangedMatchesActiveCard(message)) {
          break;
        }
        
        // Check initial load suppression window (prevent redundant refetch right after HTTP load)
        const suppressionKey = message.disc_id || message.job_id;
        if (suppressionKey) {
          const suppressUntil = this._initialLoadSuppressUntil.get(suppressionKey);
          if (suppressUntil !== undefined) {
            const now = Date.now();
            if (now < suppressUntil) {
              this.logger.debug(`Suppressed context_changed refetch for ${suppressionKey} (initial load window, ${suppressUntil - now}ms remaining)`);
              break;
            } else {
              // Clean up expired suppression entry
              this._initialLoadSuppressUntil.delete(suppressionKey);
            }
          }
        }
        
        // Debounce: route to debounce subject (300ms) to coalesce rapid-fire events.
        // The actual HTTP fetch happens in _handleDebouncedContextChanged().
        this._contextChangedDebounce$.next(message);
        break;
        
      case 'progress_update':
        // Handle progress updates from unified WebSocket
        this.handleProgressUpdate(message);
        break;
        
      case 'disc_inserted':
        if (message.disc_state) {
          const currentDiscs = this._discs.value;
          const newDisc: DiscMetadata = {
            ...message,
            scan_state: message.scan_state || 'pending',
          };
          
          const existingById = currentDiscs.find(d => d.disc_id === newDisc.disc_id);
          if (existingById) {
            const updatedDiscs = currentDiscs.map(d => 
              d.disc_id === newDisc.disc_id ? { ...d, ...newDisc } : d
            );
            this._discs.next(updatedDiscs);
          } else {
            // Check for existing drive — prefer mount_point (stable physical identity)
            // disc_num matching only used as fallback when mount_point is absent
            const matchDriveSlot = (d: DiscMetadata) =>
              d.disc_state === 'in_drive' && (
                (newDisc.mount_point && d.mount_point === newDisc.mount_point) ||
                (!newDisc.mount_point && newDisc.disc_num && d.disc_num === newDisc.disc_num) ||
                (!newDisc.mount_point && newDisc.disc_num && d.disc_id === `empty-${newDisc.disc_num}`)
              );
            const existingByMount = currentDiscs.find(matchDriveSlot);
            
            if (existingByMount) {
              const updatedDiscs = currentDiscs.map(d => matchDriveSlot(d) ? { ...d, ...newDisc } : d);
              this._discs.next(updatedDiscs);
            } else {
              this._discs.next([...currentDiscs, newDisc]);
            }
          }
        } else {
          // Legacy format
          const currentDiscs = this._insertedDiscs.value;
          const newDisc: InsertedDisc = {
            disc_id: message.disc_id,
            disc_num: message.disc_num,
            mount_point: message.mount_point,
            disc_hash: message.disc_hash,
          };
          if (!currentDiscs.find(d => d.disc_id === newDisc.disc_id)) {
            this._insertedDiscs.next([...currentDiscs, newDisc]);
          }
        }
        break;
        
      case 'disc_scanning':
        const currentDiscsScanning = this._discs.value;
        const updatedDiscsScanning = currentDiscsScanning.map(d => {
          // Prefer mount_point matching (stable physical identity); disc_num only as fallback
          const matchesByMount = message.mount_point && d.mount_point === message.mount_point;
          const matchesById = !matchesByMount && d.disc_id === message.disc_id;
          const matchesByNum = !matchesByMount && !message.mount_point && message.disc_num && d.disc_num === message.disc_num;
          const matchesEmptyId = !matchesByMount && !message.mount_point && message.disc_num && d.disc_id === `empty-${message.disc_num}`;
          
          if (matchesByMount || matchesById || matchesByNum || matchesEmptyId) {
            // Same slot, new disc: if message has a different disc_id, this is a new physical disc (swap) — always update
            const newDiscId = message.disc_id || (message.disc_num ? `pending-${message.disc_num}` : null);
            const isNewDiscInSlot = newDiscId && d.disc_id && d.disc_id !== newDiscId && !d.disc_id.startsWith('empty-') && !d.disc_id.startsWith('pending-');
            if (isNewDiscInSlot) {
              this.updateDiscInfoCache(d.disc_num || '', null);
              return {
                ...d,
                disc_id: newDiscId,
                disc_hash: null,
                scan_state: 'scanning' as const,
                scan_error: null,
              };
            }
            // Don't downgrade to 'scanning' if disc is already 'ready' or has disc_hash (same disc)
            if (d.scan_state === 'ready' || d.disc_hash) {
              return d;
            }
            return {
              ...d,
              disc_id: message.disc_id || d.disc_id,  // Update ID from empty-* to actual disc_id
              scan_state: 'scanning' as const,
              scan_error: null,
            };
          }
          return d;
        });
        if (!updatedDiscsScanning.find(d => 
          d.disc_id === message.disc_id || 
          (message.mount_point && d.mount_point === message.mount_point) ||
          (!message.mount_point && message.disc_num && d.disc_num === message.disc_num))) {
          updatedDiscsScanning.push({
            disc_id: message.disc_id || `pending-${message.disc_num}`,
            disc_num: message.disc_num || null,
            mount_point: message.mount_point || null,
            disc_hash: null,
            disc_state: 'in_drive' as const,
            scan_state: 'scanning' as const,
            scan_error: null,
          } as DiscMetadata);
        }
        this._discs.next(updatedDiscsScanning);
        break;
        
      case 'disc_ready':
        const currentDiscsReady = this._discs.value;
        let foundMatch = false;
        let updatedDiscsReady = currentDiscsReady.map(d => {
          if (d.disc_id === message.disc_id) {
            foundMatch = true;
            return {
              ...d,
              disc_hash: message.disc_hash || d.disc_hash,
              scan_state: 'ready' as const,
              scan_error: null,
            };
          }
          // Prefer mount_point matching (stable physical identity); disc_num only as fallback
          if (d.disc_state === 'in_drive' && 
              ((message.mount_point && d.mount_point === message.mount_point) ||
               (!message.mount_point && message.disc_num && d.disc_num === message.disc_num) ||
               (!message.mount_point && message.disc_num && d.disc_id === `empty-${message.disc_num}`))) {
            foundMatch = true;
            return {
              ...d,
              disc_id: message.disc_id,  // Update from empty-* or pending-* to actual disc_id
              disc_hash: message.disc_hash || d.disc_hash,
              scan_state: 'ready' as const,
              scan_error: null,
            };
          }
          return d;
        });
        
        if (!foundMatch) {
          updatedDiscsReady.push({
            disc_id: message.disc_id,
            disc_num: message.disc_num || null,
            mount_point: message.mount_point || null,
            disc_hash: message.disc_hash || null,
            disc_state: 'in_drive' as const,
            scan_state: 'ready' as const,
            scan_error: null,
          } as DiscMetadata);
        }
        // Deduplicate: when the same disc is now physically present (in_drive), remove
        // any unfinished card for it — but carry the job_id onto the in_drive card so that
        // if the disc is later ejected, the unfinished entry is properly recreated.
        if (message.disc_id || message.disc_hash) {
          const removedUnfinished = updatedDiscsReady.find(d =>
            d.disc_state === 'unfinished' &&
            ((message.disc_id && d.disc_id === message.disc_id) ||
             (message.disc_hash && d.disc_hash === message.disc_hash))
          );
          if (removedUnfinished) {
            const carriedJobId = removedUnfinished.job_id;
            updatedDiscsReady = updatedDiscsReady
              .filter(d => d !== removedUnfinished)
              .map(d => {
                // Carry job_id to the matching in_drive card
                if (carriedJobId && d.disc_state === 'in_drive' &&
                    ((message.disc_id && d.disc_id === message.disc_id) ||
                     (message.mount_point && d.mount_point === message.mount_point))) {
                  return {
                    ...d,
                    job_id: d.job_id || carriedJobId,
                    job_status: removedUnfinished.job_status ?? d.job_status,
                    created_at: removedUnfinished.created_at ?? d.created_at,
                  };
                }
                return d;
              });
          }
        }
        this._discs.next(updatedDiscsReady);
        break;
        
      case 'disc_scan_failed':
        const currentDiscsFailed = this._discs.value;
        const updatedDiscsFailed = currentDiscsFailed.map(d => {
          // Prefer mount_point matching (stable physical identity); disc_num only as fallback
          const matchesByMount = message.mount_point && d.mount_point === message.mount_point;
          const matchesById = !matchesByMount && d.disc_id === message.disc_id;
          const matchesByNum = !matchesByMount && !message.mount_point && message.disc_num && d.disc_num === message.disc_num;
          const matchesEmptyId = !matchesByMount && !message.mount_point && message.disc_num && d.disc_id === `empty-${message.disc_num}`;
          
          if (matchesByMount || matchesById || matchesByNum || matchesEmptyId) {
            const failed = {
              ...d,
              disc_id: message.disc_id || d.disc_id,  // Update ID from empty-* if needed
              scan_state: 'failed' as const,
              scan_error: message.scan_error || message.error || 'Scan failed',
            };
            // #723: the drive stopped responding, so whatever identity this
            // row still carries belongs to the PREVIOUS disc. Spreading `...d`
            // alone would leave the card headlined with the wrong movie —
            // exactly the reported fault. Drop the metadata so the card falls
            // back to the drive error. Only set for drive-level faults; an
            // empty-scan failure keeps its volume-label title.
            if (message.clear_identity) {
              return {
                ...failed,
                disc_hash: null,
                movie_name: null,
                release_name: null,
                info_title: null,
                disc_number: null,
                release_image: null,
                disc_format: null,
                resolution: null,
                release_year: null,
                production_year: null,
              };
            }
            return failed;
          }
          return d;
        });
        if (!updatedDiscsFailed.find(d => 
          d.disc_id === message.disc_id || 
          (message.mount_point && d.mount_point === message.mount_point) ||
          (!message.mount_point && message.disc_num && d.disc_num === message.disc_num) ||
          (!message.mount_point && message.disc_num && d.disc_id === `empty-${message.disc_num}`))) {
          updatedDiscsFailed.push({
            disc_id: message.disc_id || `pending-${message.disc_num}`,
            disc_num: message.disc_num || null,
            mount_point: message.mount_point || null,
            disc_hash: null,
            disc_state: 'in_drive' as const,
            scan_state: 'failed' as const,
            scan_error: message.scan_error || message.error || 'Scan failed',
          } as DiscMetadata);
        }
        this._discs.next(updatedDiscsFailed);
        break;
        
      case 'disc_ejected':
        // Reset ejected disc to empty drive state (frontend tracks drives, not backend)
        // This keeps the drive card visible but clears all disc metadata.
        // If the ejected disc had an active job, add an unfinished-job card so the carousel shows it.
        //
        // IMPORTANT: Prioritize mount_point matching over disc_num matching.
        // disc_num from udev (srN suffix) can collide with another drive's MakeMKV index
        // (e.g., /dev/sr2 sends disc_num="2" but MakeMKV DRV:2 is actually /dev/sr1).
        // mount_point is the stable physical identity.
        const currentDiscsEject = this._discs.value;
        let ejectedEntry: DiscMetadata | null = null;
        const updatedDiscsEject = currentDiscsEject.map(d => {
          // Prefer mount_point matching (stable physical identity)
          const matchesByMount = message.mount_point && d.mount_point === message.mount_point;
          // Only use disc_id/disc_num matching when mount_point is NOT available
          const matchesById = !message.mount_point && message.disc_id && d.disc_id === message.disc_id;
          const matchesByNum = !message.mount_point && message.disc_num && d.disc_num === message.disc_num;
          const matchesPendingId = !message.mount_point && message.disc_num && d.disc_id === `pending-${message.disc_num}`;

          const matchesSlot =
            matchesByMount || matchesById || matchesByNum || matchesPendingId;
          if (!matchesSlot) {
            return d;
          }
          // Unfinished job cards often reuse mount_point from summaries; only reset the physical drive row.
          if (d.disc_state !== 'in_drive') {
            return d;
          }

          if (d.job_id && d.disc_id && !d.disc_id.startsWith('empty-') && !d.disc_id.startsWith('pending-')) {
            ejectedEntry = { ...d, disc_state: 'unfinished' as const };
          }
          return {
            disc_id: `empty-${d.disc_num || message.disc_num}`,
            disc_num: d.disc_num || message.disc_num,
            mount_point: d.mount_point || message.mount_point,
            disc_hash: null,
            disc_state: 'in_drive' as const,
            scan_state: null,
            scan_error: null,
            job_id: null,
            movie_name: null,
            info_title: null,
            release_image: null,
            disc_format: null,
            resolution: null,
            release_year: null,
            production_year: null,
            last_modified_at: null,
          } as DiscMetadata;
        });
        // Do not add an unfinished card for jobs the backend just failed (e.g. disc ejected);
        // backend sends failed_job_ids so we avoid re-adding the card after job_finished removed it.
        const failedJobIds = Array.isArray((message as any).failed_job_ids) ? (message as any).failed_job_ids as string[] : [];
        const ejectedJobId = ejectedEntry ? (ejectedEntry as DiscMetadata).job_id ?? null : null;
        const isFailedJob = ejectedJobId !== null && ejectedJobId !== undefined && failedJobIds.includes(ejectedJobId);
        if (ejectedEntry && !isFailedJob && !updatedDiscsEject.some(d => d.disc_state === 'unfinished' && d.job_id === ejectedJobId)) {
          updatedDiscsEject.push(ejectedEntry);
        }
        this._discs.next(updatedDiscsEject);

        // Invalidate cached context for the ejected drive
        if (message.mount_point) {
          this.contextCache.delete(`drive:${message.mount_point}`);
        }

        // Also update _insertedDiscs for backward compatibility (remove from this legacy array)
        const insertedAfterEject = this._insertedDiscs.value.filter(d => 
          !(d.disc_id === message.disc_id || 
            (message.mount_point && d.mount_point === message.mount_point))
        );
        this._insertedDiscs.next(insertedAfterEject);
        break;
        
      case 'disc_updated':
        const currentDiscs = this._discs.value;
        const updatedDisc: DiscMetadata = {
          ...message,
          info_title: message.info_title || null,
          scan_state: message.scan_state || (message.disc_hash ? 'ready' : null),
          scan_error: message.scan_error || null,
          job_id: message.job_id || null,
        };
        let matchingDisc = currentDiscs.find(d => d.disc_id === updatedDisc.disc_id);
        if (!matchingDisc && updatedDisc.mount_point) {
          // Prefer mount_point matching (stable physical identity); disc_num only as fallback
          matchingDisc = currentDiscs.find(d => 
            d.disc_state === 'in_drive' && d.mount_point === updatedDisc.mount_point
          );
        }
        if (!matchingDisc && !updatedDisc.mount_point && updatedDisc.disc_num) {
          // Fallback: match by disc_num only when mount_point is absent
          matchingDisc = currentDiscs.find(d => 
            d.disc_state === 'in_drive' && 
            (d.disc_num === updatedDisc.disc_num ||
             d.disc_id === `empty-${updatedDisc.disc_num}`)
          );
        }
        const updatedDiscs = currentDiscs.map(d => {
          if (d.disc_id === updatedDisc.disc_id) {
            // Merge and handle scan_state transitions properly:
            // - If either current or new state is 'ready', use 'ready' (don't downgrade)
            // - Otherwise use new state if provided, else keep current state
            const newScanState: DiscMetadata['scan_state'] = 
              updatedDisc.scan_state === 'ready' || d.scan_state === 'ready'
                ? 'ready'
                : (updatedDisc.scan_state ?? d.scan_state);
            return {
              ...d,
              ...updatedDisc,
              scan_state: newScanState,
            };
          }
          // Match by mount_point (stable physical identity) or disc_id of the found match
          if (d.disc_state === 'in_drive' && matchingDisc && 
              d.disc_id === matchingDisc.disc_id) {
            // Merge and handle scan_state transitions properly
            const newScanState: DiscMetadata['scan_state'] = 
              updatedDisc.scan_state === 'ready' || d.scan_state === 'ready'
                ? 'ready'
                : (updatedDisc.scan_state ?? d.scan_state);
            return {
              ...d,
              ...updatedDisc,
              scan_state: newScanState,
            };
          }
          return d;
        });
        // Deduplicate: when the same disc is now physically present (in_drive), remove
        // any unfinished card — but carry the job_id to the in_drive card so eject can
        // recreate the unfinished entry.
        if (updatedDisc.disc_id || updatedDisc.disc_hash) {
          const removedUnfinished = updatedDiscs.find(d =>
            d.disc_state === 'unfinished' &&
            ((updatedDisc.disc_id && d.disc_id === updatedDisc.disc_id) ||
             (updatedDisc.disc_hash && d.disc_hash === updatedDisc.disc_hash))
          );
          if (removedUnfinished) {
            const carriedJobId = removedUnfinished.job_id;
            const deduped = updatedDiscs
              .filter(d => d !== removedUnfinished)
              .map(d => {
                if (carriedJobId && d.disc_state === 'in_drive' &&
                    ((updatedDisc.disc_id && d.disc_id === updatedDisc.disc_id) ||
                     (updatedDisc.mount_point && d.mount_point === updatedDisc.mount_point))) {
                  return {
                    ...d,
                    job_id: d.job_id || carriedJobId,
                    job_status: removedUnfinished.job_status ?? d.job_status,
                    created_at: removedUnfinished.created_at ?? d.created_at,
                  };
                }
                return d;
              });
            this._discs.next(deduped);
          } else {
            this._discs.next(updatedDiscs);
          }
        } else {
          this._discs.next(updatedDiscs);
        }
        break;
        
      case 'job_unfinished':
        if (message.disc_state) {
          const currentDiscs = this._discs.value;
          const newDisc: DiscMetadata = message;
          if (!currentDiscs.find(d => d.disc_id === newDisc.disc_id)) {
            this._discs.next([...currentDiscs, newDisc]);
          }
        } else {
          // Legacy format
          const currentJobs = this._unfinishedJobs.value;
          const newJob: UnfinishedJob = {
            job_id: message.job_id,
            disc_id: message.disc_id,
            mount_point: message.mount_point,
          };
          if (!currentJobs.find(j => j.job_id === newJob.job_id)) {
            this._unfinishedJobs.next([...currentJobs, newJob]);
          }
        }
        break;
        
      case 'job_finished': {
        // Remove unfinished cards for the finished job, but keep in-drive cards.
        this.applyJobFinishedLocally(
          message.job_id,
          message.disc_id,
          message.job_status as string | undefined
        );
        break;
      }
        
      case 'makemkv_update_log':
      case 'makemkv_update_status':
        // Emit MakeMKV update events for subscribers
        this._makemkvUpdateMessages.next(message);
        break;

      case 'makemkv_drives_ready':
        // #613: backend warmup finished (cold-boot or post-install). Fan
        // out so the carousel and setup wizard refetch drives + the
        // coordinator's disc list. The backend cache is already populated
        // via handle_disc_insert per drive at this point.
        this._makemkvDrivesReady.next({
          drives_count: message.drives_count ?? 0,
          source: message.source ?? 'unknown',
          job_id: message.job_id,
        });
        // Sync the coordinator so any already-loaded discs surface in the
        // carousel without waiting for the next udev event.
        try {
          this.syncCoordinator();
        } catch (err) {
          this.logger.warn('[WorkflowService] makemkv_drives_ready coordinator sync failed', err);
        }
        break;
        
      case 'options_changed':
        // Backend invalidated the cached workflow options (movie/boxset/release created/updated/deleted).
        // Refresh the MetadataService cache so the next context view has fresh options.
        this.metadataSvc.refreshWorkflowOptions();
        this.logger.debug('[WorkflowService] Received options_changed, refreshing cached options');
        break;
        
      case 'notification':
        // Backend-emitted notification: emit to notifications$ observable
        // Shell component subscribes and shows as toast (envelope: id, timestamp, source, title, actions)
        const notification: BackendNotification = {
          message: message.message,
          kind: message.kind,
          level: message.level,
          id: message.id,
          timestamp: message.timestamp,
          source: message.source,
          title: message.title,
          info_title: message.info_title,
          actions: message.actions,
          action_type: message.action_type,
          action_payload: message.action_payload
        };
        this._notifications.next(notification);
        break;
        
      default:
        this.logger.warn('[WorkflowService] Unknown unified message type:', messageType);
    }
  }
  
  /**
   * Handle debounced context_changed events.
   * Called after 300ms debounce window closes, ensuring rapid-fire events
   * are coalesced into a single HTTP refetch.
   */
  private _handleDebouncedContextChanged(message: any): void {
    // Trigger HTTP fetch of updated context (suppress loading so movie step change/select/create stays smooth)
    if (message.disc_id) {
      this.fetchDiscWorkflowContextHttp(message.disc_id, true, undefined, { suppressLoading: true, include: 'label,job' }).subscribe({
        next: (context) => {
          if (context) {
            // Always update cache regardless of whether this is the active context
            const discCacheKey = context.discInfo?.mount_point
              ? `drive:${context.discInfo.mount_point}`
              : (context.id ? `drive:${context.id}` : null);
            if (discCacheKey) {
              this.cacheContext(discCacheKey, { ...context, ...this.functionBindings });
            }

            const activeContext = this._activeContext$.value;
            if (!activeContext) return;
            
            const contextDiscId = context.discInfo?.disc_id || context.id;
            const activeDiscId = activeContext.discInfo?.disc_id || (activeContext.type === 'drive' ? activeContext.id : null);
            const shouldUpdate = (
              (activeContext.type === 'drive' && activeContext.id === context.id) ||
              (activeContext.type === 'job' && activeContext.discInfo?.disc_id === contextDiscId) ||
              (activeDiscId && contextDiscId && activeDiscId === contextDiscId) ||
              (activeContext.jobStatus?.jobId && context.jobStatus?.jobId && activeContext.jobStatus.jobId === context.jobStatus.jobId)
            );
            
            if (shouldUpdate) {
              if (activeContext.type === 'job') {
                const patch = this._discContextPatchForActiveJob(context, message.changed_fields);
                if (Object.keys(patch).length > 0) {
                  this.updateContext(patch);
                }
                const merged = { ...activeContext, ...patch } as WorkflowContext;
                const determinedStep = this.determineWorkflowStep(merged, {
                  respectUserNavigation: true,
                  considerJobStates: true,
                  updateHighestStepVisited: true
                });
                const noAutoFilmToBoxset = determinedStep === 'boxset' && (activeContext.workflowStep === 'film' || merged.workflowStep === 'film');
                const noAutoBoxsetOrDiscToTitles = determinedStep === 'titles' && (activeContext.workflowStep === 'boxset' || merged.workflowStep === 'boxset' || activeContext.workflowStep === 'disc' || merged.workflowStep === 'disc');
                const willApply = determinedStep !== merged.workflowStep && !noAutoFilmToBoxset && !noAutoBoxsetOrDiscToTitles;
                if (willApply) {
                  this.updateContext({
                    workflowStep: determinedStep,
                    stepNavigationSource: 'automatic'
                  });
                }
              } else {
                this.applyFetchedContext(context);

                const determinedStep = this.determineWorkflowStep(context, {
                  respectUserNavigation: true,
                  considerJobStates: true,
                  updateHighestStepVisited: true
                });
                const noAutoFilmToBoxset = determinedStep === 'boxset' && (activeContext.workflowStep === 'film' || context.workflowStep === 'film');
                const noAutoBoxsetOrDiscToTitles = determinedStep === 'titles' && (activeContext.workflowStep === 'boxset' || context.workflowStep === 'boxset' || activeContext.workflowStep === 'disc' || context.workflowStep === 'disc');
                const willApply = determinedStep !== context.workflowStep && !noAutoFilmToBoxset && !noAutoBoxsetOrDiscToTitles;
                if (willApply) {
                  this.updateContext({
                    workflowStep: determinedStep,
                    stepNavigationSource: 'automatic'
                  });
                }
              }
            }
          }
        },
        error: (err) => {
          this.logger.warn('[WorkflowService] Failed to fetch context after context_changed notification:', err);
        }
      });
    } else if (message.job_id) {
      this.fetchJobWorkflowContextHttp(message.job_id, { suppressLoading: true }).subscribe({
        next: (context) => {
          if (context) {
            // Always update cache regardless of whether this is the active context
            const jobCacheKey = `job:${message.job_id}`;
            this.cacheContext(jobCacheKey, { ...context, ...this.functionBindings });

            const jobId = message.job_id || context.jobStatus?.jobId || context.id;
            if (jobId && context.jobStatus?.job_status === 'failed') {
              const discsAfterFail = this._discs.value.filter(d => d.job_id !== jobId);
              this._discs.next(discsAfterFail);
              const jobsAfterFail = this._unfinishedJobs.value.filter(j => j.job_id !== jobId);
              this._unfinishedJobs.next(jobsAfterFail);
            }
            const activeContext = this._activeContext$.value;
            if (!activeContext) return;
            
            const contextDiscId = context.discInfo?.disc_id;
            const activeDiscId = activeContext.discInfo?.disc_id || 
              (activeContext.type === 'drive' ? activeContext.id : null);
            
            const shouldUpdate = (
              (activeContext.type === 'job' && activeContext.id === context.id) ||
              (activeContext.jobStatus?.jobId === context.jobStatus?.jobId) ||
              (activeContext.discInfo?.disc_id && contextDiscId && activeContext.discInfo.disc_id === contextDiscId) ||
              (activeDiscId && contextDiscId && activeDiscId === contextDiscId) ||
              (message.disc_id && activeContext.type === 'drive' && 
               (activeContext.id === message.disc_id || activeContext.discInfo?.disc_id === message.disc_id))
            );
            
            if (shouldUpdate) {
              this.applyFetchedContext(context);
              
              const determinedStep = this.determineWorkflowStep(context, {
                respectUserNavigation: true,
                considerJobStates: true,
                updateHighestStepVisited: true
              });
              const noAutoFilmToBoxsetJob = determinedStep === 'boxset' && (activeContext.workflowStep === 'film' || context.workflowStep === 'film');
              const noAutoBoxsetOrDiscToTitlesJob = determinedStep === 'titles' && (activeContext.workflowStep === 'boxset' || context.workflowStep === 'boxset' || activeContext.workflowStep === 'disc' || context.workflowStep === 'disc');
              const willApplyJob = determinedStep !== context.workflowStep && !noAutoFilmToBoxsetJob && !noAutoBoxsetOrDiscToTitlesJob;
              if (willApplyJob) {
                this.updateContext({
                  workflowStep: determinedStep,
                  stepNavigationSource: 'automatic'
                });
              }
            }
          }
        },
        error: (err) => {
          this.logger.warn('[WorkflowService] Failed to fetch context after context_changed notification:', err);
        }
      });
    }
  }

  private handleProgressUpdate(progressMsg: ProgressUpdateMessage): void {
    const jobId = progressMsg.job_id;
    if (!this.websocketProgress.has(jobId)) {
      this.websocketProgress.set(jobId, new BehaviorSubject<ProgressUpdateMessage | null>(null));
    }
    this.websocketProgress.get(jobId)!.next(progressMsg);
    
    // Update active context with progress data
    // Match by jobId if available, or by discId if this is a disc context and the active context matches
    const activeContext = this._activeContext$.value;
    
    // Hard guard: when viewing a job card, only ever apply progress for that job (context.id === job id)
    // But still update the cache for non-active jobs so they're fresh when switched to.
    if (activeContext?.type === 'job' && jobId !== activeContext.id) {
      this.updateCachedJobStatus(jobId, {
        rip_progress: progressMsg.rip_progress,
        rip_phase: progressMsg.rip_phase ?? undefined,
        post_progress: progressMsg.post_progress,
        transfer_progress: typeof progressMsg.transfer_progress === 'number' ? progressMsg.transfer_progress : undefined,
        perTitleProgress: progressMsg.per_title_progress ?? undefined,
        currentTitleProgress: progressMsg.current_title_progress ?? undefined,
        currentTitleId: progressMsg.current_title_id ?? undefined,
        currentTitleNumber: progressMsg.current_title_number ?? undefined,
      } as any);
      return;
    }
    
    // Check if we should update: jobId matches OR (discId matches AND active context is a disc context for the same disc)
    // Also update if context has no jobId yet but we have progress data (handles disc context before job creation)
    let shouldUpdate = false;
    let updateBranch = '';
    if (activeContext) {
      const existingJobId = activeContext.jobStatus?.jobId;
      const existingJobStatus = activeContext.jobStatus?.job_status;
      const existingRipProgress = activeContext.jobStatus?.rip_progress;

      if (this.failedJobIds.has(jobId) && (!existingJobId || existingJobId === jobId)) {
        return;
      }
      
      // Match by jobId (most common case) - this is the primary matching logic
      if (existingJobId === jobId) {
        shouldUpdate = true;
        updateBranch = 'jobIdMatch';
      } else if (!existingJobId && progressMsg.disc_id && activeContext.type === 'drive') {
        // For disc WebSocket: only update if context has NO jobId yet (new job just created)
        // This handles the case where a new job was just created and progress updates arrive before context is updated
        const activeDiscId = (activeContext.discInfo as any)?.disc_id;
        if (activeDiscId === progressMsg.disc_id) {
          // Same disc AND no existing jobId - update progress and set jobId
          shouldUpdate = true;
          updateBranch = 'driveNoJobId';
        }
      } else if (!existingJobId && progressMsg.per_title_progress && activeContext.type === 'drive') {
        // Disc context only: context has no jobId yet but we have progress data (e.g. disc just started rip).
        // Never use this branch for job context: when viewing a job card, context.id is the job id and we must
        // only apply progress when progressMsg.job_id === context.id, so we don't merge the ripping job's progress
        // into a different job's context (mixed progress bar vs button state).
        shouldUpdate = true;
        updateBranch = 'noJobIdPerTitle';
      }
    }

    if (!shouldUpdate && activeContext) {
      this.logger.debug(`[WorkflowService] Skipping progress update for job ${jobId}`, {
        activeContextJobId: activeContext.jobStatus?.jobId,
        activeContextType: activeContext.type,
        messageDiscId: progressMsg.disc_id
      });
    }
    
    if (shouldUpdate && activeContext) {
      const existingJobStatus = activeContext.jobStatus;
      const existingPreviews = (existingJobStatus as any)?.disc_payload?.previews;
      const existingTrackCount = existingPreviews?.tracks ? Object.keys(existingPreviews.tracks).length : 0;
      // Don't apply progress updates for failed jobs - show workflow shell instead
      // Check if this progress update is for a failed job
      const existingJobStatusValue = existingJobStatus?.job_status;
      const existingRipState = existingJobStatus?.rip_state || existingJobStatus?.pipeline?.['rip'];
      const existingIsFailed = existingJobStatusValue === 'failed' || existingRipState === 'failed';
      
      // If existing job is failed, don't update with progress (keep jobStatus null to show shell)
      if (existingIsFailed) {
        return; // Don't update progress for failed jobs
      }
      
      // Update or create jobStatus with progress data
      // Only update transfer_progress if it's actually a number (not null/undefined)
      // This prevents clearing transfer progress when progress updates don't include it
      const transferProgress = typeof progressMsg.transfer_progress === 'number' 
        ? progressMsg.transfer_progress 
        : (existingJobStatus?.transfer_progress ?? null);
      
      const updatedJobStatus = existingJobStatus ? {
        ...existingJobStatus,
        jobId: jobId, // Ensure jobId is set (important for new jobs)
        rip_progress: progressMsg.rip_progress,
        rip_phase: progressMsg.rip_phase ?? existingJobStatus.rip_phase ?? null,
        post_progress: progressMsg.post_progress,
        transfer_progress: transferProgress,
        perTitleProgress: progressMsg.per_title_progress ?? existingJobStatus.perTitleProgress,
        currentTitleProgress: progressMsg.current_title_progress ?? existingJobStatus.currentTitleProgress,
        currentTitleId: progressMsg.current_title_id ?? existingJobStatus.currentTitleId,
        currentTitleNumber: progressMsg.current_title_number ?? existingJobStatus.currentTitleNumber
      } : {
        jobId: jobId,
        job_status: 'running',
        rip_progress: progressMsg.rip_progress,
        rip_phase: progressMsg.rip_phase ?? null,
        post_progress: progressMsg.post_progress,
        transfer_progress: typeof progressMsg.transfer_progress === 'number' ? progressMsg.transfer_progress : null,
        perTitleProgress: progressMsg.per_title_progress ?? null,
        currentTitleProgress: progressMsg.current_title_progress ?? null,
        currentTitleId: progressMsg.current_title_id ?? null,
        currentTitleNumber: progressMsg.current_title_number ?? null,
        logs: [],
        job_dir: null
      } as any;
      
      // Throttle progress-driven context updates so UI updates at most every 150ms
      this._pendingProgressUpdate = { jobId, jobStatus: updatedJobStatus };
      this._progressContextUpdate$.next();

      this.maybeRefreshPostprocessContext(progressMsg, updatedJobStatus);
    }
  }

  private maybeRefreshPostprocessContext(
    progressMsg: ProgressUpdateMessage,
    updatedJobStatus: JobStatus
  ): void {
    const jobId = progressMsg.job_id;
    if (!jobId) return;

    const postProgress = progressMsg.post_progress;
    if (typeof postProgress !== 'number') return;

    const activeContext = this._activeContext$.value;
    if (!activeContext) return;

    // Skip full context refetch while user recently interacted (only progress should update UI)
    if (Date.now() < this._contextApplySuppressUntil && activeContext.jobStatus?.jobId === jobId) {
      return;
    }

    const postState = updatedJobStatus?.post_state || updatedJobStatus?.pipeline?.['postprocess'];
    const hasPostProcessFiles = (activeContext.postProcessFiles || []).length > 0;
    const isCompleted = postProgress >= 100 || postState === 'completed';
    const shouldRefresh = (!hasPostProcessFiles && postProgress > 0) ||
      (isCompleted && (postState !== 'completed' || !hasPostProcessFiles));

    if (!shouldRefresh) return;

    const lastRefresh = this.lastPostprocessContextRefresh.get(jobId) ?? 0;
    if (Date.now() - lastRefresh < this.postprocessContextRefreshCooldownMs) return;

    this.lastPostprocessContextRefresh.set(jobId, Date.now());
    this.fetchJobWorkflowContextHttp(jobId, { suppressLoading: true }).subscribe({
      next: (context) => {
        if (!context) return;
        const currentActive = this._activeContext$.value;
        if (!currentActive) return;

        const contextDiscId = context.discInfo?.disc_id;
        const activeDiscId = currentActive.discInfo?.disc_id ||
          (currentActive.type === 'drive' ? currentActive.id : null);
        const shouldUpdate = (
          (currentActive.type === 'job' && currentActive.id === context.id) ||
          (currentActive.jobStatus?.jobId === context.jobStatus?.jobId) ||
          (currentActive.discInfo?.disc_id && contextDiscId && currentActive.discInfo.disc_id === contextDiscId) ||
          (activeDiscId && contextDiscId && activeDiscId === contextDiscId) ||
          (progressMsg.disc_id && currentActive.type === 'drive' &&
           (currentActive.id === progressMsg.disc_id || currentActive.discInfo?.disc_id === progressMsg.disc_id))
        );

        if (!shouldUpdate) return;

        this.applyFetchedContext(context);

        const determinedStep = this.determineWorkflowStep(context, {
          respectUserNavigation: true,
          considerJobStates: true,
          updateHighestStepVisited: true
        });
        const noAutoFilmToBoxsetProgress = determinedStep === 'boxset' && (currentActive.workflowStep === 'film' || context.workflowStep === 'film');
        const noAutoBoxsetOrDiscToTitlesProgress = determinedStep === 'titles' && (currentActive.workflowStep === 'boxset' || context.workflowStep === 'boxset' || currentActive.workflowStep === 'disc' || context.workflowStep === 'disc');
        if (determinedStep !== context.workflowStep && !noAutoFilmToBoxsetProgress && !noAutoBoxsetOrDiscToTitlesProgress) {
          this.updateContext({
            workflowStep: determinedStep,
            stepNavigationSource: 'automatic'
          });
        }

        const hasFilesAfterRefresh = (context.postProcessFiles || []).length > 0;
        const refreshedPostState = context.jobStatus?.post_state || context.jobStatus?.pipeline?.['postprocess'];
        if (!hasFilesAfterRefresh || refreshedPostState !== 'completed') {
          this.schedulePostprocessContextRefresh(jobId, progressMsg.disc_id ?? null);
        }
      },
      error: (err) => {
        this.logger.warn('[WorkflowService] Failed to refresh context after post-process progress:', err);
        this.schedulePostprocessContextRefresh(jobId, progressMsg.disc_id ?? null);
      }
    });
  }

  private schedulePostprocessContextRefresh(jobId: string, discId: string | null): void {
    const attempts = this.postprocessRefreshAttempts.get(jobId) ?? 0;
    if (attempts >= this.postprocessRefreshMaxAttempts) return;
    if (this.postprocessRefreshTimeouts.has(jobId)) return;

    const timeout = setTimeout(() => {
      this.postprocessRefreshTimeouts.delete(jobId);
      this.postprocessRefreshAttempts.set(jobId, attempts + 1);

      this.fetchJobWorkflowContextHttp(jobId, { suppressLoading: true }).subscribe({
        next: (context) => {
          if (!context) return;
          const currentActive = this._activeContext$.value;
          if (!currentActive) return;

          const contextDiscId = context.discInfo?.disc_id;
          const activeDiscId = currentActive.discInfo?.disc_id ||
            (currentActive.type === 'drive' ? currentActive.id : null);
          const shouldUpdate = (
            (currentActive.type === 'job' && currentActive.id === context.id) ||
            (currentActive.jobStatus?.jobId === context.jobStatus?.jobId) ||
            (currentActive.discInfo?.disc_id && contextDiscId && currentActive.discInfo.disc_id === contextDiscId) ||
            (activeDiscId && contextDiscId && activeDiscId === contextDiscId) ||
            (discId && currentActive.type === 'drive' &&
             (currentActive.id === discId || currentActive.discInfo?.disc_id === discId))
          );

          if (!shouldUpdate) return;

          this.applyFetchedContext(context);

          const determinedStep = this.determineWorkflowStep(context, {
            respectUserNavigation: true,
            considerJobStates: true,
            updateHighestStepVisited: true
          });
          const noAutoFilmToBoxsetRetry = determinedStep === 'boxset' && (currentActive.workflowStep === 'film' || context.workflowStep === 'film');
          const noAutoBoxsetOrDiscToTitlesRetry = determinedStep === 'titles' && (currentActive.workflowStep === 'boxset' || context.workflowStep === 'boxset' || currentActive.workflowStep === 'disc' || context.workflowStep === 'disc');
          if (determinedStep !== context.workflowStep && !noAutoFilmToBoxsetRetry && !noAutoBoxsetOrDiscToTitlesRetry) {
            this.updateContext({
              workflowStep: determinedStep,
              stepNavigationSource: 'automatic'
            });
          }

          const hasFilesAfterRefresh = (context.postProcessFiles || []).length > 0;
          const refreshedPostState = context.jobStatus?.post_state || context.jobStatus?.pipeline?.['postprocess'];
          if (!hasFilesAfterRefresh || refreshedPostState !== 'completed') {
            this.schedulePostprocessContextRefresh(jobId, discId);
          }
        },
        error: (err) => {
          this.logger.warn('[WorkflowService] Failed to refresh post-process context (retry):', err);
          this.schedulePostprocessContextRefresh(jobId, discId);
        }
      });
    }, this.postprocessRefreshDelayMs);

    this.postprocessRefreshTimeouts.set(jobId, timeout);
  }
  
  private disconnectUnified(): void {
    if (this.coordinatorReconnectTimeout) {
      clearTimeout(this.coordinatorReconnectTimeout);
      this.coordinatorReconnectTimeout = null;
    }
    
    if (this.coordinatorWebsocket) {
      this.coordinatorWebsocket.close(1000, 'Service destroyed');
      this.coordinatorWebsocket = null;
    }
  }
  
  private fetchInitialState(): void {
    this.http.get<any>(`${this.apiUrl}/coordinator/initial-state`).subscribe({
      next: (state) => {
        this.handleUnifiedMessage(state);
        this.logger.debug('[WorkflowService] Fetched initial state via HTTP');
      },
      error: (err) => {
        this.logger.error('[WorkflowService] Failed to fetch initial state:', err);
        this._coordinatorError.next('Failed to fetch initial state');
      }
    });
  }
  
  private async fetchCoordinatorInitialStateViaHttp(): Promise<void> {
    try {
      const response = await fetch(`${this.apiUrl}/coordinator/initial-state`);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      const message = await response.json();
      this.handleUnifiedMessage(message);
    } catch (err) {
      this.logger.error('[WorkflowService] Failed to fetch coordinator initial state via HTTP:', err);
    }
  }
  
  /**
   * Manually request a coordinator sync (useful for testing or manual refresh).
   */
  syncCoordinator(): void {
    this.requestCoordinatorSync();
  }

  /**
   * Refetch coordinator state + the active workflow context. Called when the
   * unified WS reconnects after a drop OR when the browser tab regains
   * visibility, since messages may have been lost (WS closed by browser/OS on
   * backgrounding) or heavily throttled (background-tab timer throttling can
   * delay debounced context_changed handling by many seconds). Without this
   * resync, job-scope state transitions that landed while the tab was hidden
   * — rip_state → completed, post_state → completed, transfer_state →
   * completed — never reach the UI and the user has to reload.
   *
   * The synthetic context_changed message routes through the existing debounce
   * → HTTP fetch → updateContext pipeline (with changed_fields=['jobStatus']
   * so _discContextPatchForActiveJob applies the fresh jobStatus).
   */
  private _resyncActiveWorkflowState(source: 'ws-reconnect' | 'visibility-change'): void {
    this.fetchInitialState();
    const activeContext = this._activeContext$.value;
    if (!activeContext) return;
    const jobId = activeContext.jobStatus?.jobId;
    const discId = activeContext.discInfo?.disc_id;
    if (!jobId && !discId) return;
    const syntheticMsg: any = {
      type: 'context_changed',
      changed_fields: ['jobStatus'],
    };
    if (discId) syntheticMsg.disc_id = discId;
    if (jobId) syntheticMsg.job_id = jobId;
    this.logger.debug(`[WorkflowService] Resyncing active workflow context (${source})`, syntheticMsg);
    this._contextChangedDebounce$.next(syntheticMsg);
  }
  
  private requestCoordinatorSync(): void {
    if (this.coordinatorWebsocket && this.coordinatorWebsocket.readyState === WebSocket.OPEN) {
      this.coordinatorWebsocket.send(JSON.stringify({ type: 'request_sync' }));
      this.logger.debug('[WorkflowService] Sent unified workflow request_sync');
    }
  }
  
  // ===== Workflow WebSocket Methods (from WorkflowWebsocketService) =====
  
  // connectDisc and connectJob methods removed - unified WebSocket handles all updates
  
  /**
   * Get progress updates for a job.
   */
  getJobProgress(jobId: string): Observable<ProgressUpdateMessage | null> {
    if (!this.websocketProgress.has(jobId)) {
      this.websocketProgress.set(jobId, new BehaviorSubject<ProgressUpdateMessage | null>(null));
    }
    return this.websocketProgress.get(jobId)!.asObservable();
  }
  
  /**
   * Get connection state for a workflow.
   */
  getConnectionState(key: string): Observable<boolean> {
    if (!this.connectionStates.has(key)) {
      this.connectionStates.set(key, new BehaviorSubject<boolean>(false));
    }
    return this.connectionStates.get(key)!.asObservable();
  }
  
  /**
   * Disconnect from a workflow websocket.
   */
  disconnectWorkflow(key: string): void {
    const fullKey = key.startsWith('disc:') || key.startsWith('job:') ? key : `disc:${key}`;
    
    if (this.reconnectTimeouts.has(fullKey)) {
      clearTimeout(this.reconnectTimeouts.get(fullKey)!);
      this.reconnectTimeouts.delete(fullKey);
    }
    
    if (this.activeConnections.has(fullKey)) {
      const ws = this.activeConnections.get(fullKey)!;
      ws.close(1000, 'Disconnected by client');
      this.activeConnections.delete(fullKey);
    }
    
    this.reconnectAttempts.delete(fullKey);
    this.connectionStates.get(fullKey)?.next(false);
  }
  
  private disconnectAllWorkflows(): void {
    for (const timeout of this.reconnectTimeouts.values()) {
      clearTimeout(timeout);
    }
    this.reconnectTimeouts.clear();
    
    for (const ws of this.activeConnections.values()) {
      ws.close(1000, 'Service destroyed');
    }
    this.activeConnections.clear();
    
    this.reconnectAttempts.clear();
  }
  
  private _connectWorkflow(key: string, url: string): void {
    if (this.isDestroyed) {
      return;
    }
    
    try {
      const ws = new WebSocket(url);
      this.activeConnections.set(key, ws);
      
      ws.onerror = (error) => {
        this.logger.error(`[WorkflowService] Workflow WebSocket error for ${key}:`, error);
      };
      
      ws.onopen = () => {
        this.logger.debug(`[WorkflowService] Connected to ${key}`);
        this.connectionStates.get(key)?.next(true);
        this.reconnectAttempts.set(key, 0);
        this.requestWorkflowContext(key);
      };
      
      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          this.handleWorkflowMessage(key, message);
        } catch (err) {
          this.logger.error(`[WorkflowService] Failed to parse workflow message for ${key}:`, err);
        }
      };
      
      ws.onerror = (error) => {
        this.logger.error(`[WorkflowService] Workflow WebSocket error for ${key}:`, error);
      };
      
      ws.onclose = (event) => {
        this.logger.debug(`[WorkflowService] Workflow WebSocket closed for ${key}:`, event.code, event.reason);
        this.activeConnections.delete(key);
        this.connectionStates.get(key)?.next(false);
        
        if (!this.isDestroyed && event.code !== 1000) {
          this.scheduleWorkflowReconnect(key, url);
        }
      };
    } catch (err) {
      this.logger.error(`[WorkflowService] Failed to create workflow WebSocket for ${key}:`, err);
      this.scheduleWorkflowReconnect(key, url);
    }
  }
  
  private scheduleWorkflowReconnect(key: string, url: string): void {
    if (this.isDestroyed) {
      return;
    }
    
    const attempts = this.reconnectAttempts.get(key) || 0;
    if (attempts >= 10) {
      this.logger.error(`[WorkflowService] Max reconnection attempts reached for ${key}`);
      return;
    }
    
    this.reconnectAttempts.set(key, attempts + 1);
    const delay = Math.min(1000 * Math.pow(2, attempts), 30000);
    this.logger.debug(`[WorkflowService] Scheduling workflow reconnect for ${key} in ${delay}ms (attempt ${attempts + 1})`);
    
    const timeout = setTimeout(() => {
      this.reconnectTimeouts.delete(key);
      this._connectWorkflow(key, url);
    }, delay);
    
    this.reconnectTimeouts.set(key, timeout);
  }
  
  private handleWorkflowMessage(key: string, message: any): void {
    const messageType = message.type;
    
    if (messageType === 'context_changed') {
      // Skip fetch during POST-driven transition ignore window (e.g. after /label/complete)
      if (message.job_id && this._postTransitionIgnore && this._postTransitionIgnore.jobId === message.job_id && Date.now() < this._postTransitionIgnore.until) {
        return;
      }
      // Skip refetch when we just saved disc context (e.g. Movie/Series toggle on film step)
      if (message.disc_id && Date.now() < this._lastDiscContextSaveUntil) {
        return;
      }
      // Trigger HTTP fetch of updated context
      if (message.disc_id) {
        this.fetchDiscWorkflowContextHttp(message.disc_id, true, undefined, { suppressLoading: true, include: 'label,job' }).subscribe({
          next: (context) => {
            if (context) {
              // Check if this context matches the active context
              const activeContext = this._activeContext$.value;
              if (!activeContext) return;
              
              // Match by disc_id from discInfo or by id (mount_point for drives)
              const contextDiscId = context.discInfo?.disc_id || context.id;
              const activeDiscId = activeContext.discInfo?.disc_id || (activeContext.type === 'drive' ? activeContext.id : null);
              const shouldUpdate = (
                (activeContext.type === 'drive' && activeContext.id === context.id) ||
                (activeContext.type === 'job' && activeContext.discInfo?.disc_id === contextDiscId) ||
                (activeDiscId && contextDiscId && activeDiscId === contextDiscId) ||
                (activeContext.jobStatus?.jobId && context.jobStatus?.jobId && activeContext.jobStatus.jobId === context.jobStatus.jobId)
              );
              
              if (shouldUpdate) {
                if (activeContext.type === 'job') {
                  const patch = this._discContextPatchForActiveJob(context, message.changed_fields);
                  if (Object.keys(patch).length > 0) {
                    this.updateContext(patch);
                  }
                } else {
                  // Preserve current step for the active user (no auto-advance; step changes only via explicit Continue)
                  const activeStep = activeContext.workflowStep;
                  const activeSource = activeContext.stepNavigationSource;
                  // Don't overwrite labelForm with a sparser one (e.g. after Start Copy, refetch can return before disc save)
                  context = this._mergeLabelFormFromActive(context, activeContext);
                  this.applyFetchedContext(context);
                  if (activeStep != null && activeStep !== undefined) {
                    this.updateContext({
                      workflowStep: activeStep,
                      stepNavigationSource: activeSource ?? 'user'
                    });
                  }
                }
              }
            }
          },
          error: (err) => {
            this.logger.warn('[WorkflowService] Failed to fetch context after context_changed notification:', err);
          }
        });
      } else if (message.job_id) {
        this.fetchJobWorkflowContextHttp(message.job_id, { suppressLoading: true }).subscribe({
          next: (context) => {
            if (context) {
              const activeContext = this._activeContext$.value;
              if (!activeContext) return;
              
              const shouldUpdate = (
                (activeContext.type === 'job' && activeContext.id === context.id) ||
                (activeContext.jobStatus?.jobId === context.jobStatus?.jobId) ||
                (activeContext.discInfo?.disc_id && context.discInfo?.disc_id && activeContext.discInfo.disc_id === context.discInfo.disc_id)
              );
              
              if (shouldUpdate) {
                const activeStep = activeContext.workflowStep;
                const activeSource = activeContext.stepNavigationSource;
                context = this._mergeLabelFormFromActive(context, activeContext);
                this.applyFetchedContext(context);
                if (activeStep != null && activeStep !== undefined) {
                  this.updateContext({
                    workflowStep: activeStep,
                    stepNavigationSource: activeSource ?? 'user'
                  });
                }
              }
            }
          },
          error: (err) => {
            this.logger.warn('[WorkflowService] Failed to fetch context after context_changed notification:', err);
          }
        });
      }
    } else if (messageType === 'progress_update') {
      const progressMsg = message as ProgressUpdateMessage;
      const jobId = progressMsg.job_id;
      if (!this.websocketProgress.has(jobId)) {
        this.websocketProgress.set(jobId, new BehaviorSubject<ProgressUpdateMessage | null>(null));
      }
      this.websocketProgress.get(jobId)!.next(progressMsg);
      // Skip applying progress to active context during post–Start Copy ignore window
      // so an initial "all zeros" progress_update doesn't cause the UI to revert to pre rip state
      if (this._postTransitionIgnore?.jobId === jobId && Date.now() < this._postTransitionIgnore.until) {
        return;
      }
      // Update active context with progress data
      // Since contexts are unified, we can update progress directly
      // Match by jobId if available, or by discId if this is a disc WebSocket and the active context matches
      const activeContext = this._activeContext$.value;
      
      // Check if we should update: jobId matches OR (disc WebSocket AND active context is a disc context for the same disc)
      // Also update if context has no jobId yet but we have progress data (handles disc context before job creation)
      // CRITICAL: Don't match by discId if there's already a different jobId - this prevents old failed job progress from being applied to new jobs
      let shouldUpdate = false;
      let updateReason = '';
      if (activeContext) {
        const existingJobId = activeContext.jobStatus?.jobId;
        const existingJobStatus = activeContext.jobStatus?.job_status;
        const existingRipProgress = activeContext.jobStatus?.rip_progress;

        if (this.failedJobIds.has(jobId) && (!existingJobId || existingJobId === jobId)) {
          return;
        }
        
        // Match by jobId (most common case) - this is the primary matching logic
        if (existingJobId === jobId) {
          shouldUpdate = true;
          updateReason = 'jobId matches';
        } else if (!existingJobId && key.startsWith('disc:') && activeContext.type === 'drive') {
          // For disc WebSocket: only update if context has NO jobId yet (new job just created)
          // This handles the case where a new job was just created and progress updates arrive before context is updated
          // But we don't want to match if there's already a different jobId (old failed job)
          const discIdFromKey = key.replace('disc:', '');
          const activeDiscId = (activeContext.discInfo as any)?.disc_id;
          if (activeDiscId === discIdFromKey) {
            // Same disc AND no existing jobId - update progress and set jobId
            shouldUpdate = true;
            updateReason = 'discId matches via disc WebSocket, no existing jobId';
          }
        } else if (!existingJobId && progressMsg.per_title_progress) {
          // Context has no jobId yet but we have progress data - likely a disc context that needs progress
          // Only update if there's no existing jobId (don't overwrite old failed job's jobId)
          shouldUpdate = true;
          updateReason = 'context has no jobId, applying progress to set it';
        }
        // If existingJobId exists but doesn't match jobId, don't update - this prevents old failed job progress from being applied
      }
      
      // Removed excessive DEBUG logs that fire on every WebSocket update
      // Only log when skipping updates (less frequent) for debugging mismatches
      if (!shouldUpdate && activeContext) {
        // Keep this log as it's less frequent and useful for debugging context mismatches
        // But reduce payload size
        this.logger.debug(`[WorkflowService] Skipping progress update for job ${jobId} from ${key}`, {
          activeContextJobId: activeContext.jobStatus?.jobId,
          activeContextType: activeContext.type
        });
      }
      
      if (shouldUpdate && activeContext) {
        const existingJobStatus = activeContext.jobStatus;
        const existingPreviews = (existingJobStatus as any)?.disc_payload?.previews;
        const existingTrackCount = existingPreviews?.tracks ? Object.keys(existingPreviews.tracks).length : 0;
        // Don't apply progress updates for failed jobs - show workflow shell instead
        // Check if this progress update is for a failed job
        const existingJobStatusValue = existingJobStatus?.job_status;
        const existingRipState = existingJobStatus?.rip_state || existingJobStatus?.pipeline?.['rip'];
        const existingIsFailed = existingJobStatusValue === 'failed' || existingRipState === 'failed';
        
        // If existing job is failed, don't update with progress (keep jobStatus null to show shell)
        if (existingIsFailed) {
          return; // Don't update progress for failed jobs
        }
        if (!existingJobStatus || existingJobStatus?.job_status === 'failed') {
        }
        
        // Update or create jobStatus with progress data
        // Only update transfer_progress if it's actually a number (not null/undefined)
        // This prevents clearing transfer progress when progress updates don't include it
        const transferProgress = typeof progressMsg.transfer_progress === 'number' 
          ? progressMsg.transfer_progress 
          : (existingJobStatus?.transfer_progress ?? null);
        
        // Removed excessive DEBUG log that fires on every progress update with large payloads
        // Progress updates happen multiple times per second during ripping
        
        const updatedJobStatus = existingJobStatus ? {
          ...existingJobStatus,
          jobId: jobId, // Ensure jobId is set (important for new jobs)
          rip_progress: progressMsg.rip_progress,
          rip_phase: progressMsg.rip_phase ?? existingJobStatus.rip_phase ?? null,
          // #604 / #605: copy stage states from the progress message so
          // a stage transition flips the UI even when the authoritative
          // context_changed event was skipped backend-side. The ?? chain
          // preserves existing state when an older backend (or a
          // non-terminal progress tick) omits the field.
          rip_state: progressMsg.rip_state ?? existingJobStatus.rip_state ?? null,
          post_state: progressMsg.post_state ?? existingJobStatus.post_state ?? null,
          transfer_state: progressMsg.transfer_state ?? existingJobStatus.transfer_state ?? null,
          post_progress: progressMsg.post_progress,
          transfer_progress: transferProgress,
          perTitleProgress: progressMsg.per_title_progress ?? existingJobStatus.perTitleProgress,
          currentTitleProgress: progressMsg.current_title_progress ?? existingJobStatus.currentTitleProgress,
          currentTitleId: progressMsg.current_title_id ?? existingJobStatus.currentTitleId,
          currentTitleNumber: progressMsg.current_title_number ?? existingJobStatus.currentTitleNumber
        } : {
          jobId: jobId,
          job_status: 'running',
          rip_progress: progressMsg.rip_progress,
          rip_phase: progressMsg.rip_phase ?? null,
          rip_state: progressMsg.rip_state ?? null,
          post_state: progressMsg.post_state ?? null,
          transfer_state: progressMsg.transfer_state ?? null,
          post_progress: progressMsg.post_progress,
          transfer_progress: typeof progressMsg.transfer_progress === 'number' ? progressMsg.transfer_progress : null,
          perTitleProgress: progressMsg.per_title_progress ?? null,
          currentTitleProgress: progressMsg.current_title_progress ?? null,
          currentTitleId: progressMsg.current_title_id ?? null,
          currentTitleNumber: progressMsg.current_title_number ?? null,
          logs: [],
          job_dir: null
        } as any;
        
        // Throttle progress-driven context updates so UI updates at most every 150ms
        this._pendingProgressUpdate = { jobId, jobStatus: updatedJobStatus };
        this._progressContextUpdate$.next();
      }
    } else {
      this.logger.warn(`[WorkflowService] Unknown workflow message type for ${key}:`, messageType);
    }
  }
  
  private requestWorkflowContext(key: string): void {
    const ws = this.activeConnections.get(key);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'request_context' }));
      this.logger.debug(`[WorkflowService] Sent request_context for ${key}`);
    }
  }
  
  /**
   * Manually request context refresh (useful for testing or manual refresh).
   */
  refreshWorkflowContext(key: string): void {
    this.requestWorkflowContext(key);
  }
  
  // ===== Context Management Methods (from WorkflowContextService) =====
  
  /**
   * Get observable of the current active context
   */
  getContext$(): Observable<WorkflowContext | null> {
    return this._activeContext$.asObservable().pipe(
      distinctUntilChanged((prev, curr) => 
        prev?.id === curr?.id && prev?.type === curr?.type
      ),
      shareReplay(1)
    );
  }
  
  /**
   * Public getter for activeContext$ observable (for template access)
   */
  get activeContext$(): Observable<WorkflowContext | null> {
    return this._activeContext$.asObservable();
  }
  
  /**
   * Get the current context synchronously (may be null)
   */
  getCurrentContext(): WorkflowContext | null {
    return this._activeContext$.value;
  }

  /**
   * Return whether the given context refers to the currently selected card.
   * Use before merging/updating from a save or refetch response to avoid applying another job/disc data.
   */
  contextMatchesSelection(context: WorkflowContext): boolean {
    const card = this.getSelectedCard();
    if (!card || !context) return false;
    const contextJobId = context.jobStatus?.jobId ?? context.id;
    const contextMount = context.discInfo?.mount_point ?? (context.type === 'drive' ? context.id : null);
    const contextDiscId = context.discInfo?.disc_id;
    const matchesJob = card.type === 'job' && (context.id === card.id || contextJobId === card.id);
    const matchesDrive =
      card.type === 'drive' &&
      (contextMount === card.id ||
        (context.type === 'drive' && context.id === card.id) ||
        !!(contextDiscId && this._activeContext$.value?.discInfo?.disc_id === contextDiscId));
    const result = !!(matchesJob || matchesDrive);
    return result;
  }

  /**
   * Apply a full context only if it refers to the currently selected card.
   * Prevents stale save/refetch responses from overwriting the active context with another job/disc data.
   * Returns true if applied, false if skipped (e.g. user switched card before response arrived).
   */
  applyContextIfMatchesSelection(context: WorkflowContext): boolean {
    if (!this.contextMatchesSelection(context)) return false;
    this.applyFetchedContext(context);
    return true;
  }

  /**
   * Get observable of stage progress values
   * Recalculates whenever context changes
   */
  /**
   * Get stage progress values directly from context - simple extraction
   * Backend sends progress values in context updates, we just extract and expose them
   * Emits whenever context updates - Angular change detection will handle re-rendering
   */
  getStageProgress$(): Observable<StageProgressValues> {
    return this._activeContext$.asObservable().pipe(
      map((context) => {
        const progress = this.calculateStageProgress(context);
        // Always create a new object to ensure Angular detects changes even when values are the same
        return { ...progress };
      }),
      shareReplay(1)
    );
  }
  
  /**
   * Get observable of stage completion values
   * Recalculates whenever context changes
   */
  getStageCompletion$(): Observable<StageCompletionValues> {
    return this._activeContext$.asObservable().pipe(
      map((context) => this.calculateStageCompletion(context)),
      distinctUntilChanged((prev, curr) => 
        prev.rip === curr.rip &&
        prev.label === curr.label &&
        prev.postprocess === curr.postprocess &&
        prev.transfer === curr.transfer
      ),
      shareReplay(1)
    );
  }
  
  /**
   * Get the current card
   */
  getCurrentCard(): { type: 'job' | 'drive', id: string } | null {
    return this.currentCard;
  }
  
  // ===== Workflow UI Observables =====
  // These provide everything needed for workflow labeling and actions UI components
  
  /**
   * Get label form observable for workflow labeling UI
   */
  getLabelForm$(): Observable<any | null> {
    return this._activeContext$.pipe(
      map(context => context?.labelForm || null),
      distinctUntilChanged(),
      shareReplay(1)
    );
  }
  
  /**
   * Get job status observable for workflow actions UI
   */
  getJobStatus$(): Observable<JobStatus | null> {
    return this._activeContext$.pipe(
      map(context => context?.jobStatus || null),
      distinctUntilChanged(),
      shareReplay(1)
    );
  }
  
  /**
   * Get disc info observable for workflow UI
   */
  getDiscInfo$(): Observable<DiscMetadata | null> {
    return this._activeContext$.pipe(
      map(context => {
        if (!context?.discInfo) return null;
        const discDetail = context.discInfo;
        // Convert DiscDetail to DiscMetadata
        return {
          disc_id: discDetail.disc_id || '',
          disc_num: discDetail.disc_num || null,
          mount_point: discDetail.mount_point || null,
          disc_hash: discDetail.disc_hash || null,
          disc_state: 'in_drive' as const, // DiscDetail is always for in-drive discs
          job_id: null,
          scan_state: null,
          scan_error: null,
          movie_name: discDetail.movie_name || null,
          info_title: discDetail.makemkv_disc_name || null,
          release_image: discDetail.release_image || null,
          disc_format: discDetail.disc_format || null,
          resolution: discDetail.resolution || null,
          release_year: typeof discDetail.release_year === 'number' ? discDetail.release_year : (typeof discDetail.release_year === 'string' ? parseInt(discDetail.release_year) : null),
          production_year: typeof discDetail.year === 'number' ? discDetail.year : (typeof discDetail.year === 'string' ? parseInt(discDetail.year) : null),
          last_modified_at: null,
          created_at: null,
        };
      }),
      distinctUntilChanged(),
      shareReplay(1)
    );
  }
  
  /**
   * Get workflow step observable
   */
  getWorkflowStep$(): Observable<WorkflowStep | null> {
    return this._activeContext$.pipe(
      map(context => {
        if (!context) return null;
        // Phase 1: Use new determineWorkflowStep signature with updateHighestStepVisited enabled
        return this.determineWorkflowStep(context, {
          respectUserNavigation: true,
          considerJobStates: true,
          updateHighestStepVisited: true
        });
      }),
      distinctUntilChanged(),
      shareReplay(1)
    );
  }
  
  /**
   * Get active stage observable
   */
  getActiveStage$(): Observable<'rip' | 'postprocess' | 'transfer' | 'done' | null> {
    return this._activeContext$.pipe(
      map(context => {
        if (!context?.jobStatus) return null;
        return this.getActiveStage(context.jobStatus);
      }),
      distinctUntilChanged(),
      shareReplay(1)
    );
  }
  
  /**
   * Get progress observable for workflow actions UI
   */
  getProgress$(): Observable<{ rip: number; post: number; transfer: number }> {
    return this._activeContext$.pipe(
      map(context => {
        const jobStatus = context?.jobStatus;
        if (!jobStatus) {
          return { rip: 0, post: 0, transfer: 0 };
        }
        return {
          rip: typeof jobStatus.rip_progress === 'number' ? jobStatus.rip_progress : 0,
          post: typeof (jobStatus as any).post_progress === 'number' ? (jobStatus as any).post_progress : 0,
          transfer: typeof (jobStatus as any).transfer_progress === 'number' ? (jobStatus as any).transfer_progress : 0,
        };
      }),
      distinctUntilChanged(),
      shareReplay(1)
    );
  }
  
  
  /**
   * Check if rip can be started
   */
  canStartRip$(): Observable<boolean> {
    return this._activeContext$.pipe(
      map(context => {
        if (!context?.labelForm || !context?.discInfo) return false;
        const labelForm = context.labelForm;
        const jobStatus = context.jobStatus;
        // Can start if we have required fields and job is not already running
        return !!(labelForm.movie_id && labelForm.release_slug && 
                 (!jobStatus || jobStatus.job_status !== 'running'));
      }),
      distinctUntilChanged(),
      shareReplay(1)
    );
  }
  
  /**
   * Set function bindings that will be included in all contexts
   */
  setFunctionBindings(bindings: FunctionBindings): void {
    this.functionBindings = { ...this.functionBindings, ...bindings };
    const current = this._activeContext$.value;
    if (current) {
      this.updateContext({ ...this.functionBindings });
    }
  }
  
  /**
   * Set the active context by card selection
   * Always fetches fresh context from HTTP (no caching), unless same card already has context
   */
  setContextByCard(card: { type: 'job' | 'drive', id: string }): Observable<WorkflowContext> {
    const currentContext = this._activeContext$.value;
    const selectedCard = this.getSelectedCard();

    // Same-card re-selection: already have context for this card, skip clear/fetch to avoid loading overlay
    if (selectedCard && card.type === selectedCard.type && card.id === selectedCard.id &&
        currentContext && currentContext.type === card.type && currentContext.id === card.id) {
      return of(currentContext);
    }

    // Save current context to cache before switching — keyed by the context's
    // OWN identity, never by getSelectedCard(): callers (card-carousel, seamless
    // switch) advance selectedCard to the NEW card before calling us, which used
    // to store the previous card's context under the new card's cache key. The
    // optimistic restore below then displayed the old card's state as the new
    // one and the preserve-step graft made it durable (#693).
    if (currentContext && currentContext.id) {
      const prevCacheKey = this.cacheKeyForCard({ type: currentContext.type, id: currentContext.id });
      this.cacheContext(prevCacheKey, currentContext);
      
      if (currentContext.workflowStep && currentContext.saveCallback) {
        // Use context's save callback to save before switching
        // #349: Strip tracks from labelForm — only save form fields, not title data
        const { tracks: _stripSwitchTracks, ...labelFormNoSwitchTracks } = currentContext.labelForm || {};
        const labelFormWithStep = currentContext.workflowStep 
          ? { ...labelFormNoSwitchTracks, workflow_step: currentContext.workflowStep }
          : labelFormNoSwitchTracks;
        currentContext.saveCallback(labelFormWithStep).subscribe({
          next: () => {
            this.logger.debug('[WorkflowService] Saved workflowStep before switching cards', {
              workflowStep: currentContext.workflowStep,
              cardId: currentContext.id
            });
          },
          error: (err) => {
            this.logger.warn('[WorkflowService] Failed to save workflowStep before switching cards', err);
          }
        });
      }
    }
    
    // Optimistic UI: if we have a cached context, restore it immediately
    // (activeContext + ready status + lastDiscInfo) so the workflow surface
    // renders instantly on re-selection. The HTTP fetch still runs in the
    // background to refresh data; when it returns it'll update the context
    // with the latest. Without this, even a same-card re-select (after
    // SPA nav clears the short-circuit-match conditions above) flashed the
    // workflow off → "loading" → on, taking >1s when the data was already
    // in memory. #617.
    //
    // Cache miss: fall back to the clear-and-fetch path so the workflow
    // hides until the response arrives (no stale data shown).
    const cacheKey = this.cacheKeyForCard(card);
    const cached = this.getCachedContext(cacheKey);
    if (cached) {
      this._activeContext$.next(cached);
      this._workflowContextStatus$.next('ready');
      // Also clear loadingInfo — the drive-select subscriber in the ripper
      // page sets loadingInfo:true on every first emission of a fresh
      // component instance and arms a 10s watchdog (timer(10_000) at
      // ripper-page.component.ts:972). On cold load the coincident HTTP
      // fetch eventually clears loadingInfo elsewhere; on nav-back the
      // cache-hit short-circuits the fetch entirely, so nothing else
      // clears it and the watchdog fires 10s later with an updateUIOrch
      // burst + metadata refetch (#617 follow-up).
      this.updateUIOrchestrationState({ contextLoading: false, loadingInfo: false });
      this.syncStateFromContext(cached);
    } else {
      this._activeContext$.next(null);
      this._workflowContextStatus$.next('loading');
      this.updateUIOrchestrationState({ contextLoading: true });
    }
    
    // Set selected card
    this.setSelectedCard(card);
    
    // For drive cards, check if disc is ready before fetching context
    if (card.type === 'drive') {
      const discs = this._discs.value;
      const drives = this.driveSvc.getDrives() || [];
      const drive = drives.find(d => d.mount_point === card.id);
      const discFromCoordinator = discs.find(d => 
        d.disc_state === 'in_drive' && 
        (d.mount_point === card.id || d.disc_num === drive?.disc_num)
      );
      
        // Only fetch context when scan_state === 'ready'
      if (discFromCoordinator && discFromCoordinator.scan_state !== 'ready') {
        // Disc is still scanning - complete without emitting or erroring; caller can retry when scan becomes ready
        this.logger.debug('[WorkflowService] Disc scan not ready, skipping fetch', {
          disc_id: discFromCoordinator.disc_id,
          scan_state: discFromCoordinator.scan_state,
          card_id: card.id,
        });
        this._workflowContextStatus$.next('ready');
        this.updateUIOrchestrationState({ contextLoading: false });
        return EMPTY;
      }
    }
    
    // Cancel any previous request
    this.cancelPreviousRequest$.next();
    this.cancelPreviousRequest$.complete();
    this.cancelPreviousRequest$ = new Subject<void>();
    
    this.currentCard = card;
    
    // Always fetch fresh from HTTP
    return this.fetchCompleteContext(card).pipe(
      takeUntil(this.cancelPreviousRequest$),
      map(context => {
        if (this.currentCard?.type !== card.type || this.currentCard?.id !== card.id) {
          throw new Error(`Card changed during fetch: expected ${card.type}:${card.id}, got ${this.currentCard?.type}:${this.currentCard?.id}`);
        }
        
        if (!context) {
          throw new Error(`Failed to fetch context for ${card.type}:${card.id}`);
        }
        
        let convertedContext = context;
        // Carousel and selectedCard always key drives by mount_point from the clicked card; API may use disc id only.
        if (card.type === 'drive') {
          convertedContext = {
            ...context,
            type: 'drive',
            id: card.id,
            discInfo: context.discInfo
              ? { ...context.discInfo, mount_point: card.id }
              : context.discInfo,
          };
        }
        
        // When upgrading from cached → fresh, preserve user's navigation state —
        // but only when the on-screen context IS this card (#693: a foreign
        // context here must never donate its workflowStep to this card).
        const currentActive = this._activeContext$.value;
        const preserveStep = cached && currentActive?.workflowStep &&
          currentActive.type === convertedContext.type && currentActive.id === convertedContext.id;
        
        const completeContext: WorkflowContext = {
          ...convertedContext,
          ...this.functionBindings,
          // Preserve user navigation state from cached display (if user navigated while cached data was showing)
          ...(preserveStep ? {
            workflowStep: currentActive!.workflowStep,
            stepNavigationSource: currentActive!.stepNavigationSource,
          } : {}),
        };
        
        // Phase 2: Compute stepCompletionState for the complete context
        completeContext.stepCompletionState = this.getStepCompletionState(completeContext);

        // Emit context and cache it
        this._activeContext$.next(completeContext);
        this._workflowContextStatus$.next('ready');
        this._setContextApplySuppressFor(WorkflowService.INTERACT_SUPPRESS_MS);
        this.cacheContext(cacheKey, completeContext);

        // Sync state from context (this updates lastDiscInfo)
        this.syncStateFromContext(completeContext);

        // Path B: resume from history / drive click — if the linked movie is a
        // TV series, kick off the episode-catalog prefetch. Idempotent and dedup'd;
        // safe to call on every context hydration. (#367 / #370)
        this._prefetchTmdbEpisodeCatalog(null);
        
        // If the determined step needs titles/release data, load deferred data
        // (the initial card click only loads label+job for fast rendering)
        const stepNeedingData: WorkflowStep[] = ['titles', 'summary', 'transfer'];
        if (completeContext.workflowStep && stepNeedingData.includes(completeContext.workflowStep) &&
            (!completeContext.titles || completeContext.titles.length === 0)) {
          this.loadDeferredContextData();
        }
        
        // Always set contextLoading to false after context is loaded
        // Also clear driveLoadingStates for this card to prevent card from showing as loading
        const currentLoadingStates = new Map(this.uiOrchestrationState$.value.driveLoadingStates);
        currentLoadingStates.delete(card.id);
        this.updateUIOrchestrationState({ 
          contextLoading: false,
          driveLoadingStates: currentLoadingStates
        });
        
        return completeContext;
      }),
      catchError(err => {
        // Check if this was a cancellation (not a real error)
        const wasCancelled = err.name === 'EmptyError' || 
          (err.message && (err.message.includes('takeUntil') || err.message.includes('Card changed during fetch')));
        
        if (wasCancelled) {
          // Clean up cancelled requests from active request maps
          const cacheKey = card.type === 'job' ? `job:${card.id}` : card.id;
          if (card.type === 'job') {
            this.activeJobContextRequests.delete(cacheKey);
          } else if (card.type === 'drive') {
            // Clean up mount: key for drive contexts
            const mountKey = `mount:${card.id}`;
            this.activeDiscContextRequests.delete(mountKey);
            // Note: We can't safely delete disc_id: keys without knowing the disc_id
            // They'll be cleaned up when the request naturally completes/errors
          }
          
          // Don't set contextLoading to false on cancellation - let the new request handle it
          this.logger.debug('[WorkflowService] Context fetch cancelled', { 
            cardType: card.type, 
            cardId: card.id 
          });
        } else {
          // Real error - clear loading state and clean up active requests
          const cacheKey = card.type === 'job' ? `job:${card.id}` : card.id;
          if (card.type === 'job') {
            this.activeJobContextRequests.delete(cacheKey);
          } else if (card.type === 'drive') {
            const mountKey = `mount:${card.id}`;
            this.activeDiscContextRequests.delete(mountKey);
          }
          this._workflowContextStatus$.next('error');
          this.updateUIOrchestrationState({ contextLoading: false });
        }
        throw err;
      }),
      shareReplay(1)
    );
  }

  /** Retry loading context for the currently selected card (e.g. after error). Sets status to loading and refetches. */
  retryContextLoad(): void {
    const card = this.getSelectedCard();
    if (!card) return;
    this._workflowContextStatus$.next('loading');
    this.setContextByCard(card).subscribe({
      error: () => { /* error already set in setContextByCard catchError */ }
    });
  }

  /**
   * Load titles and full discInfo for the current context (deferred loading).
   * Called when user navigates to a step that needs titles (e.g., titles step).
   * The initial card click only loads label+job for fast rendering.
   */
  loadDeferredContextData(): void {
    const context = this._activeContext$.value;
    if (!context) return;
    
    // If titles are already loaded, skip
    if (context.titles && context.titles.length > 0) return;
    
    const discId = context.discInfo?.disc_id || (context.type === 'drive' ? null : null);
    const mountPoint = context.type === 'drive' ? context.id : null;
    
    if (discId) {
      this.fetchDiscWorkflowContextHttp(discId, true, undefined, { suppressLoading: true, include: 'titles,discinfo,release' }).subscribe({
        next: (fullContext) => {
          if (!fullContext) return;
          // Merge deferred data into active context
          this.updateContext({
            titles: fullContext.titles,
            titleOrder: fullContext.titleOrder,
            titlesVersion: fullContext.titlesVersion,
            discInfo: fullContext.discInfo,
            releaseDiscs: fullContext.releaseDiscs,
            boxsetMovies: fullContext.boxsetMovies,
            lastReleaseDetails: fullContext.lastReleaseDetails,
          });
        },
        error: (err) => {
          this.logger.warn('[WorkflowService] Failed to load deferred context data:', err);
        }
      });
    } else if (mountPoint) {
      this.fetchDiscWorkflowContextHttp(mountPoint, false, mountPoint, { suppressLoading: true, include: 'titles,discinfo,release' }).subscribe({
        next: (fullContext) => {
          if (!fullContext) return;
          this.updateContext({
            titles: fullContext.titles,
            titleOrder: fullContext.titleOrder,
            titlesVersion: fullContext.titlesVersion,
            discInfo: fullContext.discInfo,
            releaseDiscs: fullContext.releaseDiscs,
            boxsetMovies: fullContext.boxsetMovies,
            lastReleaseDetails: fullContext.lastReleaseDetails,
          });
        },
        error: (err) => {
          this.logger.warn('[WorkflowService] Failed to load deferred context data:', err);
        }
      });
    }
  }
  
  /**
   * Update the current context with partial updates
   */
  updateContext(updates: Partial<WorkflowContext>): void {
    const current = this._activeContext$.value;
    if (current) {
      if (updates.workflowStep && updates.stepNavigationSource !== 'user') {
      }
      let nextUpdates = updates;
    if (updates.titlesVersion !== undefined) {
        const discKey = this.getContextDiscKey(current);
        if (discKey && typeof updates.titlesVersion === 'number') {
          nextUpdates = {
            ...nextUpdates,
            titlesVersionAck: this.titleStore.ackVersion(discKey, updates.titlesVersion),
          };
        }
      }

      if (nextUpdates.titles) {
        let resetCount = 0;
        let reducedCount = 0;
        let mismatchCount = 0;
        let sameLengthMismatchCount = 0;
        let sampleKey: string | null = null;
        let samplePrev: number | null = null;
        let sampleNext: number | null = null;
        let samplePrevTitle: string | null = null;
        let sampleNextTitle: string | null = null;
        for (const t of nextUpdates.titles) {
          try {
            const key = this.getTitleKey(t, 'updateContext:incoming');
            const existing = current.titles?.find(ct => {
              try {
                return this.getTitleKey(ct, 'updateContext:current') === key;
              } catch {
                return false;
              }
            });
            const prevLength = (existing?.title ?? '').toString().length;
            const nextLength = (t?.title ?? '').toString().length;
            const prevTitle = (existing?.title ?? '').toString();
            const nextTitle = (t?.title ?? '').toString();
            if (prevTitle && nextTitle && prevTitle !== nextTitle) {
              mismatchCount += 1;
              if (prevLength === nextLength) {
                sameLengthMismatchCount += 1;
              }
              if (!samplePrevTitle) {
                samplePrevTitle = prevTitle;
                sampleNextTitle = nextTitle;
              }
            }
            if (prevLength > 0 && nextLength === 0) {
              resetCount += 1;
            }
            if (nextLength < prevLength) {
              reducedCount += 1;
              if (!sampleKey) {
                sampleKey = key;
                samplePrev = prevLength;
                sampleNext = nextLength;
              }
            }
          } catch {
            continue;
          }
        }
        if (mismatchCount > 0) {
          const stack = new Error().stack;
        }
        if (resetCount > 0 || reducedCount > 0) {
          const stack = new Error().stack;
        }
      }
      // Deduplicate titles if they're being updated (backend may send duplicates via WebSocket)
      // Only use title_id or source_file for identification - throw error if neither exists
      let deduplicatedTitles = nextUpdates.titles;
      if (nextUpdates.titles && nextUpdates.titles.length > 0) {
        const seen = new Set<string>();
        deduplicatedTitles = nextUpdates.titles.filter((t: any) => {
          try {
            const key = this.getTitleKey(t, 'updateContext');
            if (seen.has(key)) {
              return false;
            }
            seen.add(key);
            return true;
          } catch (error) {
            // Error already logged in getTitleKey, skip this title
            return false;
          }
        });
      }
      
      const updated: WorkflowContext = {
        ...current,
        ...nextUpdates,
        ...(deduplicatedTitles !== undefined ? { titles: deduplicatedTitles } : {}),
      };
      if (deduplicatedTitles !== undefined) {
        this.syncTitleSeqsFromTitles(deduplicatedTitles);
      }

      // Disc/drive workflow: keep context.id = mount_point so carousel active state matches WebSocket/HTTP refetches.
      if (updated.type !== 'job') {
        const mount =
          (updated.discInfo as { mount_point?: string } | null)?.mount_point ??
          (current.type !== 'job'
            ? (current.discInfo as { mount_point?: string } | null)?.mount_point
            : undefined);
        if (mount) {
          updated.type = 'drive';
          updated.id = mount;
        }
      }

      // Dev workflow mode override: when dev mode is on and override is set, keep context.discdbHit in sync
      if (this.devMode$.value && this.workflowModeOverride$.value !== null) {
        updated.discdbHit = this.workflowModeOverride$.value;
      }

      if (nextUpdates.jobStatus !== undefined || nextUpdates.discInfo !== undefined) {
        const resolved = this.resolveDiscdbResultForContext(updated);
        updated.discdbResult = resolved !== undefined ? resolved : undefined;
      }

      // Phase 2: Compute stepCompletionState if context changed in a way that affects it
      if (nextUpdates.labelForm || nextUpdates.jobStatus || nextUpdates.titles || nextUpdates.workflowStep) {
        updated.stepCompletionState = this.getStepCompletionState(updated);
      }

      // Emit updated context - always create new object reference to ensure observables emit
      this._activeContext$.next(updated);

      // Keep cache in sync for the active card
      const activeCard = this.getSelectedCard();
      if (activeCard) {
        this.cacheContext(this.cacheKeyForCard(activeCard), updated);
      }

      // Phase 2: Sync step with stage after context updates (called from ripper-page subscription)
      // Don't call here to avoid circular updates - let ripper-page handle it

      // Removed - ripperStateSvc no longer exists, state is managed directly in WorkflowService
    }
  }
  
  /**
   * Clear the current context
   */
  clearActiveContext(): void {
    this.currentCard = null;
    this._activeContext$.next(null);
  }

  // ── Context Cache Helpers ──────────────────────────────────────

  /** Build cache key from a card descriptor. */
  private cacheKeyForCard(card: { type: 'job' | 'drive', id: string }): string {
    return `${card.type}:${card.id}`;
  }

  /** Store a context in the LRU cache. Evicts oldest entry when over limit. */
  private cacheContext(key: string, context: WorkflowContext): void {
    // Delete and re-insert to maintain LRU order (Map preserves insertion order)
    this.contextCache.delete(key);
    this.contextCache.set(key, context);
    // Evict oldest if over limit
    if (this.contextCache.size > this.CONTEXT_CACHE_MAX) {
      const oldestKey = this.contextCache.keys().next().value;
      if (oldestKey) this.contextCache.delete(oldestKey);
    }
  }

  /** Get a cached context (returns undefined if not cached). */
  private getCachedContext(key: string): WorkflowContext | undefined {
    return this.contextCache.get(key);
  }

  /** Update a cached context's jobStatus in-place (for WebSocket progress/status updates on non-active contexts). */
  private updateCachedJobStatus(jobId: string, statusUpdate: Partial<JobStatus>): void {
    const key = `job:${jobId}`;
    const cached = this.contextCache.get(key);
    if (!cached) return;
    this.contextCache.set(key, {
      ...cached,
      jobStatus: cached.jobStatus
        ? { ...cached.jobStatus, ...statusUpdate }
        : statusUpdate as JobStatus,
    });
  }

  /**
   * Fetch complete context for a card, combining all data sources
   */
  private fetchCompleteContext(card: { type: 'job' | 'drive', id: string }): Observable<WorkflowContext | null> {
    if (card.type === 'job') {
      return this.fetchJobContextForCard(card.id);
    } else {
      return this.fetchDriveContextForCard(card.id);
    }
  }
  
  /**
   * Fetch complete context for a job (with options)
   */
  private fetchJobContextForCard(jobId: string): Observable<WorkflowContext | null> {
    // For now, use existing fetchJobWorkflowContext - options will be added later
    return this.fetchJobWorkflowContext(jobId);
  }
  
  /**
   * Fetch complete context for a drive (with options)
   */
  private fetchDriveContextForCard(mountPoint: string): Observable<WorkflowContext | null> {
    // Lightweight initial load: only label + job status (skip titles, release discs, full discInfo).
    // Titles and release data are loaded on-demand when user navigates to those steps.
    return this.fetchDiscWorkflowContext(mountPoint, false, mountPoint, 'label,job');
  }
  
  // ===== Orchestration Methods (from WorkflowOrchestrationService) =====

  /**
   * Step used for Continue, advance POST, and furthest checks: prefer persisted job.workflow_step
   * over client-only context.workflowStep (avoids titles→titles after UI-only back).
   *
   * Exception: when the user *explicitly* navigated backwards (Back button →
   * stepNavigationSource='user'), honor context.workflowStep. Otherwise the
   * Continue button validates against the backend-persisted step (which is
   * ahead of where the user is looking), the validation fails for the
   * still-incomplete next-step requirements, and the button looks broken
   * until the page is refreshed.
   */
  getEffectiveWorkflowStep(context: WorkflowContext | null): WorkflowStep {
    if (!context) {
      return 'film';
    }
    const order: WorkflowStep[] = getStepOrderForContext(context);
    const fromContext = context.workflowStep;
    if (
      context.stepNavigationSource === 'user' &&
      fromContext &&
      order.includes(fromContext)
    ) {
      return fromContext;
    }
    const fromJob = context.jobStatus?.workflow_step;
    if (fromJob && order.includes(fromJob as WorkflowStep)) {
      return fromJob as WorkflowStep;
    }
    if (fromContext && order.includes(fromContext)) {
      return fromContext;
    }
    return this.determineWorkflowStep(context);
  }
  
  /**
   * Determine the current workflow step based on context
   * Enhanced to consider job states and respect user navigation (Phase 1)
   */
  determineWorkflowStep(
    context: WorkflowContext,
    options?: {
      respectUserNavigation?: boolean;  // Don't auto-reset if user navigated
      considerJobStates?: boolean;  // Use job states to determine step
      updateHighestStepVisited?: boolean;  // Update highestStepVisited if determined step is higher
    }
  ): WorkflowStep {
    const respectUserNavigation = options?.respectUserNavigation !== false;
    const considerJobStates = options?.considerJobStates !== false;
    const updateHighestStepVisited = options?.updateHighestStepVisited !== false;
    
    const labelForm = context.labelForm;
    const jobStatus = context.jobStatus;
    const discdbHit = context.discdbHit;
    const jobStatusValue = jobStatus?.job_status;
    const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'];
    const postState = jobStatus?.post_state || jobStatus?.pipeline?.['postprocess'];
    const transferState = jobStatus?.transfer_state ?? jobStatus?.pipeline?.['transfer'];
    const labelState = jobStatus?.label_state || jobStatus?.pipeline?.['label'];
    
    
    // If jobStatus is null (failed job cleared to show shell), return initial step
    if (!jobStatus) {
      return discdbHit ? 'summary' : 'film';
    }
    
    // Check if job is failed - if so, reset to initial step
    const isFailed = jobStatusValue === 'failed' || ripState === 'failed';
    
    if (isFailed) {
      // Reset to initial step when job fails
      return discdbHit ? 'summary' : 'film';
    }
    
    // If workflowStep exists and we should respect user navigation, validate it's accessible
    if (context.workflowStep && respectUserNavigation && context.stepNavigationSource === 'user') {
      const furthestStep = this.computeFurthestStep(context);
      const steps: WorkflowStep[] = getStepOrderForContext(context);
      const storedStepIndex = steps.indexOf(context.workflowStep);
      const furthestStepIndex = steps.indexOf(furthestStep);
      const isAccessible = storedStepIndex <= furthestStepIndex;
      // Validate that the stored step is accessible (not beyond furthest step)
      if (isAccessible) {
        return context.workflowStep;
      }
      // Stored step is not accessible - compute the correct step instead
    }
    
    const steps: WorkflowStep[] = getStepOrderForContext(context);
    let determinedStep: WorkflowStep;
    let determinedReason = '';
    
    // For DiscDB hits, use summary step
    if (discdbHit) {
      // Check job states if enabled
      if (considerJobStates && jobStatus) {
        const postState = jobStatus.post_state || jobStatus.pipeline?.['postprocess'];
        const transferState = jobStatus.transfer_state ?? jobStatus.pipeline?.['transfer'];
        const ripState = jobStatus.rip_state || jobStatus.pipeline?.['rip'] || jobStatus.job_status;
        
        // #365 Phase 2 § 6.4 — postprocess collapsed into transfer's
        // "preparing" sub-phase. post-state running/completed now routes
        // to 'transfer' (where the transferPhaseLabel renders "Preparing
        // files…").
        if (transferState === 'running' || transferState === 'completed') {
          determinedStep = 'transfer';
          determinedReason = 'discdbHit transferState';
        } else if (postState === 'running' || postState === 'completed') {
          determinedStep = 'transfer';
          determinedReason = 'discdbHit postState (collapsed)';
        } else if (ripState === 'completed' || ripState === 'running') {
          // Rip completed; DiscDB hit has no titles step (only summary → transfer)
          if (labelForm?.disc_id && this.areTitlesComplete(context)) {
            determinedStep = 'transfer';
            determinedReason = 'discdbHit rip complete + titlesComplete';
          } else if (labelForm?.disc_id) {
            // Stay on summary until transfer is ready (never show titles step for DiscDB hit)
            determinedStep = 'summary';
            determinedReason = 'discdbHit rip complete + disc_id';
          } else {
            determinedStep = 'summary';
            determinedReason = 'discdbHit rip complete + no disc';
          }
        } else {
          // No rip started or rip not completed - show summary step
          determinedStep = 'summary';
          determinedReason = 'discdbHit default summary';
        }
      } else {
        // Default for DiscDB hits: start at summary
        determinedStep = 'summary';
        determinedReason = 'discdbHit no jobStates';
      }
    } else {
      // Normal flow (DiscDB miss)
      if (!labelForm) {
        determinedStep = 'film';
        determinedReason = 'miss no labelForm';
      } else {
        // Check job states if enabled
        if (considerJobStates && jobStatus) {
          const ripState = jobStatus.rip_state || jobStatus.pipeline?.['rip'] || jobStatus.job_status;
          const postState = jobStatus.post_state || jobStatus.pipeline?.['postprocess'];
          const transferState = jobStatus.transfer_state ?? jobStatus.pipeline?.['transfer'];
          const labelState = jobStatus.label_state || jobStatus.pipeline?.['label'];
          
          // If transfer is running/completed, we're on transfer step
          if (transferState === 'running' || transferState === 'completed') {
            determinedStep = 'transfer';
            determinedReason = 'miss transferState';
          } else if (postState === 'ready' || postState === 'running' || postState === 'completed') {
            // #365 Phase 2 § 6.4 — post-process collapsed into transfer's
            // preparing sub-phase.
            determinedStep = 'transfer';
            determinedReason = 'miss postState (collapsed)';
          } else if (ripState === 'completed' && labelState === 'completed') {
            // Rip completed and label completed, but don't auto-advance without postprocess state
            determinedStep = 'titles';
            determinedReason = 'miss rip complete + labelState completed (stay titles)';
          } else if (ripState === 'completed' && labelState !== 'completed') {
            // If rip is completed but label not completed, we're on titles
            determinedStep = 'titles';
            determinedReason = 'miss rip complete + labelState not completed';
          } else {
            // Determine based on form completion
            if (!labelForm.movie_id) {
              determinedStep = 'film';
              determinedReason = 'miss labelForm movie';
            } else if (!labelForm.release_id && !labelForm.boxset_id && (!labelForm.release_name && !labelForm.release_slug)) {
              determinedStep = 'boxset';
              determinedReason = 'miss labelForm release/boxset';
            } else if (!labelForm.disc_id && (!labelForm.disc_name || !labelForm.disc_format)) {
              determinedStep = 'disc';
              determinedReason = 'miss labelForm disc';
            } else if (!this.areTitlesComplete(context)) {
              determinedStep = 'titles';
              determinedReason = 'miss titles incomplete';
            } else {
              // All labeling steps complete, default to titles (user can continue to postprocess)
              determinedStep = 'titles';
              determinedReason = 'miss titles complete default';
            }
          }
        } else {
          // Determine based on form completion
          if (!labelForm.movie_id) {
            determinedStep = 'film';
            determinedReason = 'miss no jobStates movie';
          } else if (!labelForm.release_id && !labelForm.boxset_id && (!labelForm.release_name && !labelForm.release_slug)) {
            determinedStep = 'boxset';
            determinedReason = 'miss no jobStates release/boxset';
          } else if (!labelForm.disc_id && (!labelForm.disc_name || !labelForm.disc_format)) {
            determinedStep = 'disc';
            determinedReason = 'miss no jobStates disc';
          } else if (!this.areTitlesComplete(context)) {
            determinedStep = 'titles';
            determinedReason = 'miss no jobStates titles incomplete';
          } else {
            // All labeling steps complete, default to titles (user can continue to postprocess)
            determinedStep = 'titles';
            determinedReason = 'miss no jobStates titles complete default';
          }
        }
      }
    }
    
    return determinedStep;
  }
  
  /**
   * Titles step complete when each logical entity passes (duplicate group = primary only).
   * Label edits sync metadata to all segment_map siblings so backend per-row validation stays satisfied.
   */
  private areTitlesComplete(context: WorkflowContext): boolean {
    return areLabelTitlesComplete(context.titles);
  }
  
  /**
   * Get the active stage based on job status
   */
  getActiveStage(jobStatus: JobStatus | null): 'rip' | 'postprocess' | 'transfer' | 'done' | null {
    if (!jobStatus) return null;

    const ripState = (jobStatus as any).rip_state || 'pending';
    const postState = (jobStatus as any).post_state || 'pending';
    const transferState = (jobStatus as any).transfer_state || 'pending';

    if (transferState === 'completed') {
      return 'done';
    }
    if (transferState === 'running') {
      return 'transfer';
    }
    if (transferState === 'failed') {
      return 'transfer';
    }
    if (postState === 'completed' || postState === 'running') {
      return 'postprocess';
    }
    if (postState === 'failed') {
      return 'postprocess';
    }
    if (ripState === 'completed' || ripState === 'running') {
      return 'rip';
    }
    if (ripState === 'pending') {
      return 'rip';
    }
    if (ripState === 'failed') {
      return 'rip';
    }

    return null;
  }
  
  /**
   * Check if a stage is completed
   */
  isStageCompleted(stage: 'rip' | 'postprocess' | 'transfer' | 'done', jobStatus: JobStatus | null): boolean {
    if (!jobStatus) return false;

    const ripState = (jobStatus as any).rip_state || 'pending';
    const postState = (jobStatus as any).post_state || 'pending';
    const transferState = (jobStatus as any).transfer_state || 'pending';

    switch (stage) {
      case 'rip':
        return ripState === 'completed';
      case 'postprocess':
        return postState === 'completed';
      case 'transfer':
        return transferState === 'completed';
      case 'done':
        return transferState === 'completed';
      default:
        return false;
    }
  }
  
  /**
   * Calculate stage completion percentage (legacy method - use calculateStageProgress(context) instead)
   * @deprecated Use calculateStageProgress(context: WorkflowContext) instead
   */
  calculateStageProgressLegacy(stage: 'rip' | 'postprocess' | 'transfer', jobStatus: JobStatus | null): number | null {
    if (!jobStatus) return null;

    switch (stage) {
      case 'rip':
        const ripProgress = jobStatus.rip_progress;
        return typeof ripProgress === 'number' ? ripProgress : null;
      case 'postprocess':
        const postProgress = (jobStatus as any).post_progress;
        return typeof postProgress === 'number' ? postProgress : null;
      case 'transfer':
        const transferProgress = (jobStatus as any).transfer_progress;
        return typeof transferProgress === 'number' ? transferProgress : null;
      default:
        return null;
    }
  }
  
  /**
   * Get step completion state (Phase 2: Step completion tracking)
   */
  getStepCompletionState(context: WorkflowContext): {
    film: boolean;
    boxset: boolean;
    disc: boolean;
    titles: boolean;
    postprocess: boolean;
    transfer: boolean;
  } {
    const labelForm = context.labelForm;
    const jobStatus = context.jobStatus;
    const completion = this.calculateStageCompletion(context);
    
    return {
      film: !!labelForm?.movie_id,
      // #580: require film step done AND a fully populated release (link +
      // name + slug + year). See ``isReleaseSufficientlyComplete`` in
      // label-form.service.ts for the canonical predicate.
      boxset: !!labelForm?.movie_id && isReleaseSufficientlyComplete(labelForm ?? null),
      disc: !!(labelForm?.disc_name && labelForm?.disc_format),
      titles: this.areTitlesComplete(context),
      postprocess: completion.postprocess,
      transfer: completion.transfer,
    };
  }
  
  /**
   * Calculate rip progress from context
   */
  /**
   * Get rip progress directly from backend - no processing needed
   * Backend already sends rip_progress as 0-100
   */
  calculateRipProgress(context: WorkflowContext | null): number | null {
    if (!context?.jobStatus) {
      return null;
    }
    
    const ripProgress = context.jobStatus.rip_progress;
    
    // Backend sends rip_progress as 0-100, just return it directly
    if (typeof ripProgress === 'number' && ripProgress >= 0 && ripProgress <= 100) {
      return ripProgress;
    }
    
    // If not a valid number, return null
    return null;
  }
  
  /**
   * Calculate label progress from context
   */
  calculateLabelProgress(context: WorkflowContext | null): number | null {
    if (!context) return null;
    
    // DiscDB hits don't have labeling stage
    if (context.discdbHit) return 0;
    
    if (!context.labelForm) return 0;
    
    const progress = this.computeLabelProgress(context);
    if (progress.total === 0) return 0;
    
    const clamp = (v: number) => Math.max(0, Math.min(100, v));
    const roundPct = (v: number) => Math.round((v + Number.EPSILON) * 100) / 100;
    const pct = (progress.filled / progress.total) * 100;
    return clamp(roundPct(pct));
  }
  
  /**
   * Calculate post-process progress from context
   */
  calculatePostProcessProgress(context: WorkflowContext | null, lastPostProgress?: number | null): number | null {
    if (!context?.jobStatus) return null;
    
    const jobStatus = context.jobStatus;
    const status = jobStatus;
    const clamp = (v: number) => Math.max(0, Math.min(100, v));
    const roundPct = (v: number) => Math.round((v + Number.EPSILON) * 100) / 100;
    
    // If labeling not completed and DiscDB miss, wait for labeling
    const labelState = jobStatus.label_state || jobStatus.pipeline?.['label'];
    if (labelState !== 'completed' && !context.discdbHit) {
      return 0;
    }
    
    const postState = jobStatus.post_state || jobStatus.pipeline?.['postprocess'];
    
    // Only show 100% when post-processing is completed
    if (postState === 'completed') {
      return 100;
    }
    
    // When ready (not started yet), show 0%
    if (postState === 'ready') {
      return 0;
    }
    
    // If failed, don't show progress
    if (postState === 'failed') {
      return null;
    }
    
    // Only show progress when post-processing is actually active
    const isPostProcessingActive = postState === 'running' || status?.job_status === 'validating' || status?.phase === 'postprocess';
    if (!isPostProcessingActive) {
      return null;
    }
    
    // Use post_progress from job status (primary source)
    if (typeof status?.post_progress === 'number' && status.post_progress >= 0) {
      // If post_progress is 0 and we have a cached value > 0, use the cache (prevent flashing)
      if (status.post_progress === 0 && lastPostProgress !== null && lastPostProgress !== undefined && lastPostProgress > 0) {
        return clamp(roundPct(lastPostProgress));
      }
      return clamp(roundPct(status.post_progress));
    }
    
    // Fallback to disc_payload for backward compatibility
    if (postState !== 'failed') {
      const postPayload: any = (status as any)?.disc_payload || {};
      if (typeof postPayload?.post_progress === 'number') {
        const pctFromPayload = clamp(roundPct(postPayload.post_progress));
        if (pctFromPayload >= 100) return 100;
        return pctFromPayload;
      }
      if (typeof postPayload?.post_done === 'number' && typeof postPayload?.post_total === 'number' && postPayload.post_total > 0) {
        const pctFromPayload = clamp(roundPct((postPayload.post_done * 100) / postPayload.post_total));
        if (pctFromPayload >= 100) return 100;
        return pctFromPayload;
      }
    }
    
    // If we're in post-processing, use cached progress if available
    if (isPostProcessingActive) {
      if (lastPostProgress !== null && lastPostProgress !== undefined) {
        return clamp(roundPct(lastPostProgress));
      }
      return 0;
    }
    
    return null;
  }
  
  /**
   * Calculate transfer progress from context
   */
  calculateTransferProgress(context: WorkflowContext | null): number | null {
    if (!context?.jobStatus) return null;
    
    const jobStatus = context.jobStatus;
    const transferState = jobStatus.transfer_state ?? jobStatus.pipeline?.['transfer'];
    const clamp = (v: number) => Math.max(0, Math.min(100, v));
    const roundPct = (v: number) => Math.round((v + Number.EPSILON) * 100) / 100;
    
    if (transferState === 'completed') return 100;
    if (transferState === 'failed') return null;
    
    if (transferState === 'running') {
      const transferProgress = (jobStatus as any)?.transfer_progress;
      if (typeof transferProgress === 'number' && transferProgress >= 0) {
        return clamp(roundPct(transferProgress));
      }
      return 0;
    }
    
    return null;
  }
  
  /**
   * Calculate all stage progress values
   */
  calculateStageProgress(context: WorkflowContext | null): StageProgressValues {
    if (!context) {
      return { rip: null, label: null, postprocess: null, transfer: null };
    }
    
    // Get cached post-process progress for this job
    const jobId = context.jobStatus?.jobId;
    const lastPostProgress = jobId ? this.lastPostProgressCache.get(jobId) ?? null : null;
    
    // Update cache if we have new post_progress value
    if (jobId && context.jobStatus?.post_progress !== undefined && context.jobStatus.post_progress !== null) {
      const newPostProgress = context.jobStatus.post_progress;
      if (newPostProgress > 0) {
        this.lastPostProgressCache.set(jobId, newPostProgress);
      }
    }
    
    const jobStatus = context.jobStatus;
    const ripProgress = this.calculateRipProgress(context);
    const postProgress = this.calculatePostProcessProgress(context, lastPostProgress);
    const transferProgress = this.calculateTransferProgress(context);

    // Sub-phase labels: when progress is at 100% but stage is not yet complete
    let ripPhaseLabel: string | null = null;
    let postPhaseLabel: string | null = null;
    let transferPhaseLabel: string | null = null;

    if (jobStatus) {
      const ripState = (jobStatus as any).rip_state || jobStatus.pipeline?.['rip'];
      const postState = (jobStatus as any).post_state || jobStatus.pipeline?.['postprocess'];
      const transferState = (jobStatus as any).transfer_state || jobStatus.pipeline?.['transfer'];
      const ripPhase = (jobStatus as any).rip_phase;
      // Sub-phase emitted by the unified start_transfer worker
      // (Phase 2 collapse, #365): "preparing" | "transferring" | "verifying".
      // NULL on legacy / pre-Phase-2 jobs that never went through
      // start_transfer; the existing transferState-based labelling below
      // handles those.
      const transferPhase = (jobStatus as any).transfer_phase;
      const jobStatusVal = jobStatus.job_status;

      // Rip: at 100% but not complete → verifying or validating
      if (ripProgress != null && ripProgress >= 100 && ripState !== 'completed') {
        if (ripPhase === 'verification') {
          ripPhaseLabel = 'Verifying…';
        } else if (jobStatusVal === 'validating') {
          ripPhaseLabel = 'Validating…';
        } else if (ripState === 'running') {
          ripPhaseLabel = 'Finalizing…';
        }
      }
      // Postprocess: at 100% but not complete → validating
      if (postProgress != null && postProgress >= 100 && postState !== 'completed') {
        if (jobStatusVal === 'validating' && postState === 'running') {
          postPhaseLabel = 'Validating…';
        } else if (postState === 'running') {
          postPhaseLabel = 'Finalizing…';
        }
      }
      // Transfer sub-phase under the Phase 2 collapse. When transfer_phase
      // is set, prefer it over the legacy 100%-but-not-complete inference
      // — this is the canonical sub-phase signal from the unified
      // start_transfer worker.
      if (transferPhase === 'preparing') {
        transferPhaseLabel = 'Preparing files…';
      } else if (transferPhase === 'verifying') {
        transferPhaseLabel = 'Verifying integrity…';
      } else if (transferProgress != null && transferProgress >= 100 && transferState !== 'completed') {
        // Legacy fallback for jobs that predate the collapse: transfer at
        // 100% but not yet flipped to completed means the verification
        // step is in progress.
        if (transferState === 'running') {
          transferPhaseLabel = 'Verifying integrity…';
        }
      }
    }

    return {
      rip: ripProgress,
      label: this.calculateLabelProgress(context),
      postprocess: postProgress,
      transfer: transferProgress,
      ripPhaseLabel,
      postPhaseLabel,
      transferPhaseLabel,
      ripStartedAt: (jobStatus as any)?.rip_started_at ?? null,
    };
  }
  
  /**
   * Calculate stage completion values
   */
  calculateStageCompletion(context: WorkflowContext | null): StageCompletionValues {
    if (!context?.jobStatus) {
      return { rip: false, label: false, postprocess: false, transfer: false };
    }
    
    const jobStatus = context.jobStatus;
    const ripState = jobStatus.rip_state || jobStatus.pipeline?.['rip'] || jobStatus.job_status;
    const labelState = jobStatus.label_state || jobStatus.pipeline?.['label'];
    const postState = jobStatus.post_state || jobStatus.pipeline?.['postprocess'];
    const transferState = jobStatus.transfer_state ?? jobStatus.pipeline?.['transfer'];
    
    return {
      rip: ripState === 'completed',
      label: labelState === 'completed' || context.discdbHit, // DiscDB hits skip labeling
      postprocess: postState === 'completed',
      transfer: transferState === 'completed',
    };
  }
  
  /**
   * Check if we can proceed to a specific step
   */
  canProceedToStep(currentStep: WorkflowStep, targetStep: WorkflowStep, labelForm: LabelForm | null): boolean {
    if (!labelForm) return false;

    // #365 Phase 2 § 6.4 — 'postprocess' removed from the step order
    // (collapsed into transfer's preparing sub-phase).
    const stepOrder: WorkflowStep[] = ['film', 'boxset', 'disc', 'titles', 'transfer'];
    const currentIndex = stepOrder.indexOf(currentStep);
    const targetIndex = stepOrder.indexOf(targetStep);

    if (targetIndex < currentIndex) {
      return false;
    }

    switch (targetStep) {
      case 'film':
        return true;
      case 'boxset':
        return !!labelForm.movie_id;
      case 'disc':
        return !!labelForm.movie_id;
      case 'titles':
        return !!(labelForm.release_name || labelForm.release_slug);
      case 'transfer':
        // Transfer subsumes the old postprocess step. Require titles complete
        // (was the postprocess gate); transfer-stage gating is jobStatus-driven
        // in canNavigateToStep.
        return this.validateStepCompletion('titles', labelForm).valid;
      default:
        return false;
    }
  }
  
  /**
   * Validate that a step is completed
   */
  validateStepCompletion(step: WorkflowStep, labelForm: LabelForm | null): WorkflowValidationResult {
    const errors: string[] = [];

    if (!labelForm) {
      return { valid: false, errors: ['Label form is required'] };
    }

    switch (step) {
      case 'summary':
        // Summary step is always valid (it's just a display step)
        break;
        
      case 'film':
        if (!labelForm.movie_id) {
          errors.push('Movie selection is required');
        }
        break;

      case 'boxset': {
        // #580: gate Continue on the canonical predicate. The boxset step
        // historically only required ANY release identifier; the user
        // could advance with empty release_name/slug/year fields, leading
        // to malformed library output. Single source of truth is in
        // label-form.service.ts so all three gates stay in lockstep.
        if (!isReleaseSufficientlyComplete(labelForm)) {
          const hasReleaseLink =
            !!labelForm.release_id ||
            !!(labelForm.boxset_id && labelForm.boxset_id !== '__pending__');
          if (!hasReleaseLink) {
            errors.push('Release or boxset must be selected');
          } else {
            // Linked but incomplete — surface which fields the user is missing.
            const missing: string[] = [];
            if (!(labelForm.release_name || '').trim()) missing.push('release name');
            if (!(labelForm.release_slug || '').trim()) missing.push('release slug');
            const year = labelForm.release_year;
            if (typeof year !== 'number' || !Number.isInteger(year) || year <= 0) {
              missing.push('release year');
            }
            errors.push(`Release is missing required field(s): ${missing.join(', ')}`);
          }
        }
        break;
      }

      case 'disc':
        if (!labelForm.release_name && !labelForm.release_slug) {
          errors.push('Release name is required');
        }
        if (!labelForm.disc_name) {
          errors.push('Disc name is required');
        }
        break;

      case 'titles':
        if (!labelForm.tracks || labelForm.tracks.length === 0) {
          errors.push('At least one title is required');
        } else if (!areLabelTitlesComplete(labelForm.tracks)) {
          errors.push('All titles must be labeled or ignored');
        }
        break;

      case 'transfer':
        // Transfer step doesn't require labelForm validation; it depends
        // on job status (checked in canNavigateToStep). #365 Phase 2 § 6.4
        // — the old 'postprocess' case folded into this one when the
        // standalone step was removed.
        break;

      default:
        errors.push(`Unknown step: ${step}`);
    }

    return {
      valid: errors.length === 0,
      errors,
    };
  }
  
  /**
   * Get the next step in the workflow
   */
  getNextStep(currentStep: WorkflowStep, labelForm: LabelForm | null, jobStatus: JobStatus | null): WorkflowStep | null {
    // #365 Phase 2 § 6.4 — 'postprocess' removed from the step order.
    const stepOrder: WorkflowStep[] = ['film', 'boxset', 'disc', 'titles', 'transfer'];
    const currentIndex = stepOrder.indexOf(currentStep);

    if (currentIndex === -1) {
      return null;
    }

    const validation = this.validateStepCompletion(currentStep, labelForm);
    if (!validation.valid) {
      return currentStep;
    }

    for (let i = currentIndex + 1; i < stepOrder.length; i++) {
      const nextStep = stepOrder[i];
      if (this.canProceedToStep(currentStep, nextStep, labelForm)) {
        const nextValidation = this.validateStepCompletion(nextStep, labelForm);
        if (!nextValidation.valid) {
          return nextStep;
        }
      }
    }

    return null;
  }
  
  // ===== Create+Link Orchestration Methods =====
  
  /**
   * Create a movie and link it to the current workflow context.
   * Uses POST /discs/.../movies when disc_id or mount_point is known so the disc workflow stays consistent.
   */
  createAndLinkMovie(movieData: any, contextId: string, contextType: 'job' | 'drive'): Observable<{ movie: any; linked: boolean }> {
    const ctx = this._activeContext$.value;
    const contextMatches = !!(ctx && ctx.id === contextId && ctx.type === contextType);
    const discId = contextMatches
      ? ((ctx.discInfo as any)?.disc_id ??
          (ctx.labelForm as any)?.disc_id ??
          (ctx.jobStatus as any)?.disc_id ??
          null)
      : null;
    const mountPoint = contextMatches
      ? contextType === 'drive'
        ? ctx.id
        : ((ctx.discInfo as any)?.mount_point ?? null)
      : null;
    const useDiscCreate = !!(discId || mountPoint);

    const linkAfterCreate = (createdMovie: any) => {
      const groupType = (createdMovie as any)?.tmdb_type === 'tv' ? 'series' : 'movie';
      return this.applyMetadataSelection(
        { movieId: createdMovie.id, groupType },
        contextId,
        contextType
      ).pipe(map(() => ({ movie: createdMovie, linked: true })));
    };

    if (movieData.tmdb_url) {
      return this.metadataSvc.lookupMovie(movieData.tmdb_url).pipe(
        switchMap((lookupResult) => {
          const create$ = useDiscCreate
            ? this.metadataSvc.createMovieForDisc(discId, mountPoint, lookupResult)
            : this.metadataSvc.createMovie(lookupResult).pipe(map((m) => ({ movie: m })));
          return create$.pipe(switchMap(({ movie }) => linkAfterCreate(movie)));
        }),
        catchError((err) => {
          this.logger.error('[WorkflowService] Failed to create and link movie from TMDB URL', err);
          return throwError(() => err);
        })
      );
    }

    const create$ = useDiscCreate
      ? this.metadataSvc.createMovieForDisc(discId, mountPoint, movieData)
      : this.metadataSvc.createMovie(movieData).pipe(map((m) => ({ movie: m })));
    return create$.pipe(
      switchMap(({ movie }) => linkAfterCreate(movie)),
      catchError((err) => {
        this.logger.error('[WorkflowService] Failed to create and link movie', err);
        return throwError(() => err);
      })
    );
  }
  
  /**
   * Link an existing movie to the current workflow context
   */
  linkMovieToContext(movieId: string, contextId: string, contextType: 'job' | 'drive'): Observable<void> {
    const context = this._activeContext$.value;
    // Only update if the requested context matches the active context
    if (context && context.id === contextId && context.type === contextType) {
      const nextLabelForm: Record<string, unknown> = {
        ...(context.labelForm as Record<string, unknown>),
        movie_id: movieId,
      };
      this._stripStaleTmdbIdWhenMovieIdPresent(nextLabelForm);
      const updatedContext = {
        ...context,
        labelForm: nextLabelForm,
      };
      this.updateContext({ labelForm: updatedContext.labelForm });
      // Save to backend
      if (contextType === 'job') {
        return this.saveJobWorkflowContext(contextId, updatedContext.labelForm, false).pipe(map(() => undefined));
      } else {
        return this.saveDiscWorkflowContext(contextId, updatedContext.labelForm, false, false).pipe(map(() => undefined));
      }
    }
    return of(undefined);
  }
  
  /**
   * Create a release and link it to the current workflow context
   */
  createAndLinkRelease(releaseData: any, contextId: string, contextType: 'job' | 'drive'): Observable<{ release: any; linked: boolean }> {
    const context = this._activeContext$.value;
    if (!context || context.id !== contextId || context.type !== contextType) {
      return of({ release: null, linked: false });
    }
    const discId = (context.discInfo as any)?.disc_id ?? (context.labelForm as any)?.disc_id ?? (context.jobStatus as any)?.disc_id ?? null;
    const mountPoint = contextType === 'drive' ? context.id : (context.discInfo as any)?.mount_point ?? null;
    return this.withLabelContextSaveProgress(
      this.metadataSvc.createReleaseForDisc(discId, mountPoint, releaseData).pipe(
        switchMap(result => {
          const releaseId = result.release?.id;
          const releaseSlug = result.release?.slug;
          if (releaseId == null || releaseSlug == null) {
            return of({ release: result.release, linked: result.linked });
          }
          const displayName =
            (result.release as any)?.name ??
            (result.release as any)?.title ??
            (result.release as any)?.release_name ??
            null;
          return this.linkReleaseToContext(releaseId, releaseSlug, contextId, contextType, displayName).pipe(
            map(() => ({ release: result.release, linked: result.linked }))
          );
        })
      )
    );
  }
  
  /**
   * Link an existing release to the current workflow context
   */
  linkReleaseToContext(
    releaseId: string,
    releaseSlug: string,
    contextId: string,
    contextType: 'job' | 'drive',
    /** Human edition title from API; never use releaseSlug here — saves would write slug into releases.name */
    releaseDisplayName?: string | null
  ): Observable<void> {
    const context = this._activeContext$.value;
    // Only update if the requested context matches the active context
    if (context && context.id === contextId && context.type === contextType) {
      const lf = (context.labelForm || {}) as Record<string, unknown>;
      const nextLabelForm: Record<string, unknown> = {
        ...lf,
        release_id: releaseId,
        release_slug: releaseSlug,
      };
      const nm = (releaseDisplayName ?? '').toString().trim();
      if (nm) {
        nextLabelForm['release_name'] = nm;
      } else {
        const prev = (lf['release_name'] ?? '').toString().trim();
        if (prev && prev === releaseSlug) {
          nextLabelForm['release_name'] = null;
        }
      }
      const updatedContext = {
        ...context,
        labelForm: nextLabelForm,
      };
      this.updateContext({ labelForm: updatedContext.labelForm });
      // Save to backend and update context with response (which includes calculated disc_number)
      if (contextType === 'job') {
        return this.withLabelContextSaveProgress(
          this.saveJobWorkflowContext(contextId, updatedContext.labelForm, false).pipe(
            tap((responseContext) => {
              // Update context with response from backend (includes calculated disc_number)
              this.updateContext(responseContext);
            }),
            map(() => undefined),
            catchError((err) => {
              this.toastSvc.show(formatHttpErrorDetail(err), 'error', 5000);
              return throwError(() => err);
            })
          )
        );
      } else {
        return this.withLabelContextSaveProgress(
          this.saveDiscWorkflowContext(contextId, updatedContext.labelForm, false, false).pipe(
            tap((responseContext) => {
              // Update context with response from backend (includes calculated disc_number)
              this.updateContext(responseContext);
            }),
            map(() => undefined),
            catchError((err) => {
              this.toastSvc.show(formatHttpErrorDetail(err), 'error', 5000);
              return throwError(() => err);
            })
          )
        );
      }
    }
    return of(undefined);
  }
  
  /**
   * Create a boxset and link it to the current workflow context
   */
  createAndLinkBoxset(boxsetData: any, contextId: string, contextType: 'job' | 'drive'): Observable<{ boxset: any; linked: boolean }> {
    const context = this._activeContext$.value;
    if (!context || context.id !== contextId || context.type !== contextType) {
      return of({ boxset: null, linked: false });
    }
    const movieId = context.labelForm?.movie_id ?? null;
    if (!movieId) {
      return of({ boxset: null, linked: false });
    }
    const discId = (context.discInfo as any)?.disc_id ?? (context.labelForm as any)?.disc_id ?? (context.jobStatus as any)?.disc_id ?? null;
    const mountPoint = contextType === 'drive' ? context.id : (context.discInfo as any)?.mount_point ?? null;
    return this.withLabelContextSaveProgress(
      this.metadataSvc.createBoxsetForDisc(discId, mountPoint, boxsetData, movieId).pipe(
        switchMap(result => {
          const boxsetId = result.boxset?.id;
          const boxsetSlug = result.boxset?.slug;
          if (boxsetId == null || boxsetSlug == null) {
            return of({ boxset: result.boxset, linked: result.linked });
          }
          const rel = result.release;
          const linkedRelease =
            rel?.id != null && rel.slug != null
              ? { id: String(rel.id), slug: String(rel.slug) }
              : undefined;
          return this.linkBoxsetToContext(boxsetId, boxsetSlug, contextId, contextType, linkedRelease).pipe(
            map(() => ({ boxset: result.boxset, linked: result.linked }))
          );
        })
      )
    );
  }
  
  /**
   * Link an existing boxset to the current workflow context
   */
  linkBoxsetToContext(
    boxsetId: string,
    boxsetSlug: string,
    contextId: string,
    contextType: 'job' | 'drive',
    /** When set (e.g. after POST /discs/.../boxsets), overwrites stale release_id: null on save. */
    linkedRelease?: { id: string; slug: string }
  ): Observable<void> {
    const context = this._activeContext$.value;
    // Only update if the requested context matches the active context
    if (context && context.id === contextId && context.type === contextType) {
      const updatedContext = {
        ...context,
        labelForm: {
          ...context.labelForm,
          boxset_id: boxsetId,
          boxset_slug: boxsetSlug,
          ...(linkedRelease
            ? { release_id: linkedRelease.id, release_slug: linkedRelease.slug }
            : {}),
        },
      };
      this.updateContext({ labelForm: updatedContext.labelForm });
      // Save to backend and update context with response (which includes calculated disc_number)
      if (contextType === 'job') {
        return this.withLabelContextSaveProgress(
          this.saveJobWorkflowContext(contextId, updatedContext.labelForm, false).pipe(
            tap((responseContext) => {
              // Update context with response from backend (includes calculated disc_number)
              this.updateContext(responseContext);
            }),
            map(() => undefined),
            catchError((err) => {
              this.toastSvc.show(formatHttpErrorDetail(err), 'error', 5000);
              return throwError(() => err);
            })
          )
        );
      } else {
        return this.withLabelContextSaveProgress(
          this.saveDiscWorkflowContext(contextId, updatedContext.labelForm, false, false).pipe(
            tap((responseContext) => {
              // Update context with response from backend (includes calculated disc_number)
              this.updateContext(responseContext);
            }),
            map(() => undefined),
            catchError((err) => {
              this.toastSvc.show(formatHttpErrorDetail(err), 'error', 5000);
              return throwError(() => err);
            })
          )
        );
      }
    }
    return of(undefined);
  }
  
  /**
   * Link a disc to the current workflow context
   */
  linkDiscToContext(discId: string, contextId: string, contextType: 'job' | 'drive'): Observable<void> {
    const context = this._activeContext$.value;
    // Only update if the requested context matches the active context
    if (context && context.id === contextId && context.type === contextType) {
      const updatedContext = {
        ...context,
        labelForm: {
          ...context.labelForm,
          disc_id: discId,
        },
      };
      this.updateContext({ labelForm: updatedContext.labelForm });
      // Save to backend and update context with response
      if (contextType === 'job') {
        return this.saveJobWorkflowContext(contextId, updatedContext.labelForm, false).pipe(
          tap((responseContext) => {
            // Update context with response from backend
            this.updateContext(responseContext);
          }),
          map(() => undefined)
        );
      } else {
        return this.saveDiscWorkflowContext(contextId, updatedContext.labelForm, false, false).pipe(
          tap((responseContext) => {
            // Update context with response from backend
            this.updateContext(responseContext);
          }),
          map(() => undefined)
        );
      }
    }
    return of(undefined);
  }
  
  /**
   * Apply metadata selection to workflow context (immutable).
   * groupType (movie | series) is set when selecting a movie so the Movie/Series toggle reflects the selection.
   */
  applyMetadataSelection(selection: { movieId?: string | null; tmdbId?: string | null; groupType?: 'movie' | 'series' | null; releaseId?: string | null; releaseSlug?: string | null; releaseName?: string | null; releaseYear?: number | null; coverFrontUrl?: string | null; boxsetId?: string | null; boxsetSlug?: string | null }, contextId: string, contextType: 'job' | 'drive'): Observable<void> {
    // Use active context only - no caching
    const context = this._activeContext$.value;
    // Verify the context matches the requested context
    if (!context || context.id !== contextId || context.type !== contextType) {
      this.logger.warn('[WorkflowService] Context mismatch in applyMetadataSelection', {
        activeContextId: context?.id,
        activeContextType: context?.type,
        requestedContextId: contextId,
        requestedContextType: contextType
      });
      return of(undefined);
    }
    
    if (context && context.labelForm) {
      const hasSelectionProp = (key: keyof typeof selection): boolean =>
        Object.prototype.hasOwnProperty.call(selection, key);
      /** Film-step "Change": clear movie must drop release/boxset and release-derived fields or PATCH re-links the disc. */
      const clearingMovie = hasSelectionProp('movieId') && selection.movieId == null;
      const updatedLabelForm = {
        ...context.labelForm,
        ...(hasSelectionProp('movieId') && { movie_id: selection.movieId }),
        ...(hasSelectionProp('tmdbId') && { tmdb_id: selection.tmdbId }),
        ...(hasSelectionProp('groupType') && selection.groupType && { group_type: selection.groupType, mode: selection.groupType }),
        ...(hasSelectionProp('releaseId') && { release_id: selection.releaseId }),
        ...(hasSelectionProp('releaseSlug') && { release_slug: selection.releaseSlug }),
        ...(hasSelectionProp('releaseName') && { release_name: selection.releaseName }),
        ...(hasSelectionProp('releaseYear') && { release_year: selection.releaseYear }),
        ...(hasSelectionProp('coverFrontUrl') && { cover_front_url: selection.coverFrontUrl }),
        ...(hasSelectionProp('boxsetId') && { boxset_id: selection.boxsetId }),
        ...(hasSelectionProp('boxsetSlug') && { boxset_slug: selection.boxsetSlug }),
        ...(clearingMovie && {
          release_id: null,
          release_slug: null,
          release_name: null,
          release_year: null,
          boxset_id: null,
          boxset_slug: null,
          tmdb_id: null,
          movie_name: null,
          movie_production_year: null,
          movie_cover_url: null,
          movie_cover_path: null,
          cover_front_url: null,
          cover_back_url: null,
          upc: null,
          asin: null,
        }),
      } as Record<string, unknown>;
      if (!hasSelectionProp('tmdbId')) {
        this._stripStaleTmdbIdWhenMovieIdPresent(updatedLabelForm);
      }
      this.updateContext({ labelForm: updatedLabelForm });
      // Save to backend and update context with response (which includes calculated disc_number)
      // Ref-counted label save progress: primary CTA disabled + spinner on film/boxset steps until save completes
      if (contextType === 'job') {
        return defer(() => {
          this.beginLabelContextSave();
          return this.saveJobWorkflowContext(contextId, updatedLabelForm, false).pipe(
            tap((updatedContext) => {
              // If backend returned labelForm without movie_id but we sent one, preserve sent selection so UI does not revert
              let contextToApply = updatedContext;
              const returnedMovieId = updatedContext?.labelForm?.movie_id;
              if (selection.movieId != null && updatedContext?.labelForm != null && (returnedMovieId == null || returnedMovieId === '')) {
                contextToApply = {
                  ...updatedContext,
                  labelForm: {
                    ...updatedContext.labelForm,
                    movie_id: selection.movieId,
                    ...(selection.tmdbId != null && { tmdb_id: selection.tmdbId }),
                    ...(selection.groupType && { group_type: selection.groupType, mode: selection.groupType }),
                  },
                };
              }
              this.applyContextIfMatchesSelection(contextToApply);
            }),
            map(() => undefined),
            catchError((err) => {
              this.toastSvc.show(formatHttpErrorDetail(err), 'error', 5000);
              return throwError(() => err);
            }),
            finalize(() => this.endLabelContextSave())
          );
        });
      } else {
        // Determine if we should use disc_id or mount_point
        // If we have a disc_id in discInfo, use disc_id endpoint
        // Also check if contextId looks like a UUID (disc_id) vs mount_point path
        const discIdDrive = (context.discInfo as any)?.disc_id ?? (context.labelForm as any)?.disc_id ?? null;
        const mountPointDrive = contextType === 'drive' ? context.id : (context.discInfo as any)?.mount_point ?? null;
        const isUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(contextId);
        const useDiscId = !!discIdDrive || isUuid;
        const identifier = useDiscId ? (discIdDrive || contextId) : contextId;

        return defer(() => {
          this.beginLabelContextSave();
          return this.saveDiscWorkflowContext(identifier, updatedLabelForm, false, useDiscId).pipe(
            tap((updatedContext) => {
              let contextToApply = updatedContext;
              const returnedMovieId = updatedContext?.labelForm?.movie_id;
              if (selection.movieId != null && updatedContext?.labelForm != null && (returnedMovieId == null || returnedMovieId === '')) {
                contextToApply = {
                  ...updatedContext,
                  labelForm: {
                    ...updatedContext.labelForm,
                    movie_id: selection.movieId,
                    ...(selection.tmdbId != null && { tmdb_id: selection.tmdbId }),
                    ...(selection.groupType && { group_type: selection.groupType, mode: selection.groupType }),
                  },
                };
              }
              this.applyContextIfMatchesSelection(contextToApply);
            }),
            map(() => undefined),
            catchError((err) => {
              this.toastSvc.show(formatHttpErrorDetail(err), 'error', 5000);
              return throwError(() => err);
            }),
            finalize(() => this.endLabelContextSave())
          );
        });
      }
    }
    return of(undefined);
  }
  
  /**
   * Save label form to workflow context (private helper)
   */
  saveLabelFormToContext(labelForm: any, contextId: string, contextType: 'job' | 'drive'): Observable<void> {
    if (contextType === 'job') {
      return this.saveJobWorkflowContext(contextId, labelForm, false).pipe(map(() => undefined));
    } else {
      return this.saveDiscWorkflowContext(contextId, labelForm, false, false).pipe(map(() => undefined));
    }
  }
  
  // ===== Convenience Methods (Use Active Context Automatically) =====
  
  /**
   * Create a movie and link it to the active workflow context
   * Convenience method that automatically uses getCurrentContext()
   */
  createAndLinkMovieToActiveContext(movieData: any): Observable<{ movie: any; linked: boolean }> {
    const context = this.getCurrentContext();
    if (!context) {
      return throwError(() => new Error('No active workflow context'));
    }
    return this.createAndLinkMovie(movieData, context.id, context.type).pipe(
      tap((res) => {
        // Path A: fresh series confirm — prefetch the catalog without blocking.
        if (res?.movie) this._prefetchTmdbEpisodeCatalog(res.movie);
      }),
    );
  }

  // ---- TMDB episode catalog (#367 / #370) -----------------------------
  // Process-scoped cache shared across all activeContext$ instances.
  // Successful responses cached for session lifetime; 4xx/5xx not cached.
  private _episodeCatalogCache = new Map<string, TmdbSeasonEpisodes>();
  private _episodeCatalogInFlight = new Map<string, Observable<TmdbSeasonEpisodes>>();

  /** Fetch one season's episodes; dedupes concurrent + repeat calls. */
  fetchTmdbSeasonEpisodes(tmdb_id: string, season_number: number): Observable<TmdbSeasonEpisodes> {
    const key = `${tmdb_id}:${season_number}`;
    const cached = this._episodeCatalogCache.get(key);
    if (cached) return of(cached);
    const inflight = this._episodeCatalogInFlight.get(key);
    if (inflight) return inflight;
    const req$ = this.http
      .get<TmdbSeasonEpisodes>(`${this.apiUrl}/movies/${encodeURIComponent(tmdb_id)}/seasons/${season_number}/episodes`)
      .pipe(
        tap((res) => {
          this._episodeCatalogCache.set(key, res);
          this._episodeCatalogInFlight.delete(key);
        }),
        catchError((err) => {
          this._episodeCatalogInFlight.delete(key);
          return throwError(() => err);
        }),
        shareReplay(1),
      );
    this._episodeCatalogInFlight.set(key, req$);
    return req$;
  }

  /** Two trigger paths (Path A: fresh selection, Path B: resume). Both feed here.
   * Reads tmdb_id from explicit arg (Path A) or labelForm (Path B); reads
   * primary-season hint from ctx.discInfo.tmdb_suggestion.hints.season
   * (path verified live on Fallout S2 D1 2026-06-06). */
  private _prefetchTmdbEpisodeCatalog(arg: { tmdb_id?: string; tmdb_type?: string } | null): void {
    const ctx = this._activeContext$.value;
    if (!ctx) return;
    const tmdb_id = (arg?.tmdb_id || ctx.labelForm?.tmdb_id || '').toString().trim();
    if (!tmdb_id) return;
    const isSeries = arg?.tmdb_type === 'tv'
      || ctx.labelForm?.group_type === 'series'
      || ctx.isSeries === true;
    if (!isSeries) return;
    // Idempotency: don't refire if catalog already has anything for this tmdb_id.
    const existing = ctx.tmdbEpisodeCatalog;
    if (existing && existing.tmdb_id === tmdb_id && existing.seasons.size > 0) return;

    const hinted = (ctx.discInfo as any)?.tmdb_suggestion?.hints?.season;
    // Precedence: persisted user pick (round-tripped via discs.label_draft,
    // #536) > TMDB title-pattern heuristic > 1.
    const existingPrimary = ctx.labelForm?.primary_season;
    const primary: number = (typeof existingPrimary === 'number' && existingPrimary > 0)
      ? existingPrimary
      : (Number.isInteger(hinted) && hinted > 0 ? hinted : 1);

    // Seed labelForm.primary_season so the disc-card selector + per-row
    // dropdown (both shipped in #371) inherit it before tracks render.
    if (ctx.labelForm && ctx.labelForm.primary_season !== primary) {
      ctx.labelForm.primary_season = primary;
    }
    this._fetchSeasonIntoActiveContext(tmdb_id, primary);
  }

  private _fetchSeasonIntoActiveContext(tmdb_id: string, season_number: number): void {
    const cur = this._activeContext$.value;
    if (!cur) return;
    const catalog: TmdbEpisodeCatalog =
      cur.tmdbEpisodeCatalog?.tmdb_id === tmdb_id
        ? cur.tmdbEpisodeCatalog
        : { tmdb_id, numberOfSeasons: 1, seriesName: null,
            seasons: new Map(), loadingSeasons: new Set(), errorSeasons: new Set() };
    catalog.loadingSeasons.add(season_number);
    catalog.errorSeasons.delete(season_number);
    this._activeContext$.next({ ...cur, tmdbEpisodeCatalog: catalog });

    this.fetchTmdbSeasonEpisodes(tmdb_id, season_number).subscribe({
      next: (res) => {
        const c = this._activeContext$.value;
        if (!c?.tmdbEpisodeCatalog || c.tmdbEpisodeCatalog.tmdb_id !== tmdb_id) return;
        c.tmdbEpisodeCatalog.seasons.set(season_number, res);
        c.tmdbEpisodeCatalog.loadingSeasons.delete(season_number);
        c.tmdbEpisodeCatalog.numberOfSeasons = res.number_of_seasons;
        c.tmdbEpisodeCatalog.seriesName = res.series_name;
        this._activeContext$.next({ ...c, tmdbEpisodeCatalog: { ...c.tmdbEpisodeCatalog } });
      },
      error: (err) => {
        this.logger.warn('[WorkflowService] TMDB episode fetch failed', { tmdb_id, season_number, err });
        const c = this._activeContext$.value;
        if (!c?.tmdbEpisodeCatalog || c.tmdbEpisodeCatalog.tmdb_id !== tmdb_id) return;
        c.tmdbEpisodeCatalog.errorSeasons.add(season_number);
        c.tmdbEpisodeCatalog.loadingSeasons.delete(season_number);
        this._activeContext$.next({ ...c, tmdbEpisodeCatalog: { ...c.tmdbEpisodeCatalog } });
      },
    });
  }

  /** Selectors used by #371 ----------------------------------------------- */

  getActiveTmdbTvId(): string | null {
    return this._activeContext$.value?.tmdbEpisodeCatalog?.tmdb_id ?? null;
  }

  getTvSeasonCount$(): Observable<number | null> {
    return this._activeContext$.pipe(
      map((ctx) => ctx?.tmdbEpisodeCatalog?.numberOfSeasons ?? null),
      distinctUntilChanged(),
    );
  }

  getPrimarySeason$(): Observable<number> {
    return this._activeContext$.pipe(
      map((ctx) => (ctx?.labelForm?.primary_season as number | undefined) ?? 1),
      distinctUntilChanged(),
    );
  }

  setPrimarySeason(season: number): void {
    const cur = this._activeContext$.value;
    if (!cur?.labelForm) return;
    const oldPrimary = (cur.labelForm.primary_season as number | undefined) ?? 1;
    if (oldPrimary === season) return;
    const tracks = Array.isArray(cur.labelForm.tracks) ? cur.labelForm.tracks : [];
    const updatedTracks = tracks.map((t: any) =>
      (t.season == null || t.season === oldPrimary) ? { ...t, season } : t,
    );
    this._activeContext$.next({
      ...cur,
      labelForm: { ...cur.labelForm, primary_season: season, tracks: updatedTracks },
    });
    const tmdb_id = cur.tmdbEpisodeCatalog?.tmdb_id;
    if (tmdb_id) this._fetchSeasonIntoActiveContext(tmdb_id, season);

    // Persist to discs.label_draft (#536). saveDiscWorkflowContext already
    // strips tracks[] from the payload, so this is a small PATCH carrying
    // just the changed labelForm field. Fire-and-forget: optimistic state
    // is already applied; a transient PATCH failure is acceptable (next
    // setPrimarySeason call retries; cache invalidation re-syncs on the
    // next workflow-context fetch).
    const discId = cur.discInfo?.disc_id;
    if (discId) {
      this.saveDiscWorkflowContext(discId, { primary_season: season }, false, true)
        .subscribe({
          error: (err) => this.logger.warn('[WorkflowService] persist primary_season failed', err),
        });
    }
  }

  /** Episode dropdown options for one season — 'loading'/'error'/'unavailable'
   * are sentinel strings the template ngSwitches on. */
  getEpisodesForSeason$(season_number: number):
    Observable<TmdbEpisodeSummary[] | 'loading' | 'error' | 'unavailable'> {
    return this._activeContext$.pipe(
      map((ctx) => {
        const c = ctx?.tmdbEpisodeCatalog;
        if (!c) return 'unavailable' as const;
        if (c.errorSeasons.has(season_number)) return 'error' as const;
        if (c.loadingSeasons.has(season_number)) return 'loading' as const;
        const hit = c.seasons.get(season_number);
        return hit ? hit.episodes : 'unavailable' as const;
      }),
      distinctUntilChanged(),
    );
  }
  
  /**
   * Create a release and link it to the active workflow context
   * Convenience method that automatically uses getCurrentContext()
   */
  createAndLinkReleaseToActiveContext(releaseData: any): Observable<{ release: any; linked: boolean }> {
    const context = this.getCurrentContext();
    if (!context) {
      return throwError(() => new Error('No active workflow context'));
    }
    return this.createAndLinkRelease(releaseData, context.id, context.type);
  }
  
  /**
   * Create a boxset and link it to the active workflow context
   * Convenience method that automatically uses getCurrentContext()
   */
  createAndLinkBoxsetToActiveContext(boxsetData: any): Observable<{ boxset: any; linked: boolean }> {
    const context = this.getCurrentContext();
    if (!context) {
      return throwError(() => new Error('No active workflow context'));
    }
    return this.createAndLinkBoxset(boxsetData, context.id, context.type);
  }
  
  /**
   * Apply metadata selection to the active workflow context
   * Convenience method that automatically uses getCurrentContext()
   */
  applyMetadataSelectionToActiveContext(selection: { movieId?: string | null; tmdbId?: string | null; groupType?: 'movie' | 'series' | null; releaseId?: string | null; releaseSlug?: string | null; releaseName?: string | null; releaseYear?: number | null; coverFrontUrl?: string | null; boxsetId?: string | null; boxsetSlug?: string | null }): Observable<void> {
    const context = this.getCurrentContext();
    if (!context) {
      return throwError(() => new Error('No active workflow context'));
    }
    // Use the current context's ID and type directly (don't normalize)
    return this.applyMetadataSelection(selection, context.id, context.type).pipe(
      switchMap(() => of(undefined)),
      catchError((err) => {
        this.logger.error('[WorkflowService] Failed to apply metadata selection to active context', err);
        return throwError(() => err);
      })
    );
  }
  
  /**
   * Link an existing movie to the active workflow context
   * Convenience method that automatically uses getCurrentContext()
   */
  linkMovieToActiveContext(movieId: string): Observable<void> {
    const context = this.getCurrentContext();
    if (!context) {
      return throwError(() => new Error('No active workflow context'));
    }
    return this.linkMovieToContext(movieId, context.id, context.type);
  }
  
  /**
   * Link an existing release to the active workflow context
   * Convenience method that automatically uses getCurrentContext()
   */
  linkReleaseToActiveContext(releaseId: string, releaseSlug: string): Observable<void> {
    const context = this.getCurrentContext();
    if (!context) {
      return throwError(() => new Error('No active workflow context'));
    }
    return this.linkReleaseToContext(releaseId, releaseSlug, context.id, context.type);
  }
  
  /**
   * Link an existing boxset to the active workflow context
   * Convenience method that automatically uses getCurrentContext()
   */
  linkBoxsetToActiveContext(boxsetId: string, boxsetSlug: string): Observable<void> {
    const context = this.getCurrentContext();
    if (!context) {
      return throwError(() => new Error('No active workflow context'));
    }
    return this.linkBoxsetToContext(boxsetId, boxsetSlug, context.id, context.type);
  }
  
  // ===== Utility Methods =====

  /** TheDiscDB lookup outcome for badges (not workflow branch). */
  private discdbResultFromDiscDetail(discInfo: DiscDetail | null | undefined): JobStatus['discdb_result'] | undefined {
    if (!discInfo) return undefined;
    const di = discInfo as unknown as Record<string, unknown>;
    const dr = di['discdb_result'];
    if (dr === 'hit' || dr === 'miss' || dr === 'error' || dr === 'unknown') return dr as JobStatus['discdb_result'];
    if (di['discdb_hit'] === true) return 'hit';
    if (di['discdb_hit'] === false || di['discdb_miss'] === true) return 'miss';
    return undefined;
  }

  private discdbResultFromJobSources(
    job: JobStatus,
    discPayload: any,
    discInfo: DiscDetail | null
  ): JobStatus['discdb_result'] | undefined {
    const r = job.discdb_result;
    if (r === 'hit' || r === 'miss' || r === 'error' || r === 'unknown') return r;
    if (discPayload?.discdb_hit === true) return 'hit';
    if (discPayload?.discdb_hit === false) return 'miss';
    return this.discdbResultFromDiscDetail(discInfo);
  }

  private resolveDiscdbResultForContext(ctx: Pick<WorkflowContext, 'jobStatus' | 'discInfo'>): JobStatus['discdb_result'] | undefined {
    if (ctx.jobStatus) {
      return this.discdbResultFromJobSources(
        ctx.jobStatus,
        (ctx.jobStatus as any).disc_payload,
        ctx.discInfo
      );
    }
    return this.discdbResultFromDiscDetail(ctx.discInfo);
  }

  private coalesceDiscdbResult(
    responseOverride: unknown,
    ctx: Pick<WorkflowContext, 'jobStatus' | 'discInfo'>
  ): JobStatus['discdb_result'] | undefined {
    if (
      responseOverride === 'hit' ||
      responseOverride === 'miss' ||
      responseOverride === 'error' ||
      responseOverride === 'unknown'
    ) {
      return responseOverride;
    }
    return this.resolveDiscdbResultForContext(ctx);
  }
  
  /**
   * Compute DiscDB state from disc info and job status
   */
  computeDiscDbState(info: DiscDetail | null, status: JobStatus | null): 'hit' | 'miss' | 'unknown' {
    if (!info) return 'unknown';
    
    // Check discdb_hit first - this is the primary indicator
    const discdbHit = (info as any)?.discdb_hit;
    if (discdbHit === true) return 'hit';
    if (discdbHit === false) return 'miss';
    
    // If discdb_hit is null/undefined, check job status
    if (status) {
      const jobDiscdbResult = status.discdb_result;
      if (jobDiscdbResult === 'hit') return 'hit';
      if (jobDiscdbResult === 'miss') return 'miss';
      
      // Check disc_payload for discdb_hit
      const payloadDiscdbHit = (status as any)?.disc_payload?.discdb_hit;
      if (payloadDiscdbHit === true) return 'hit';
      if (payloadDiscdbHit === false) return 'miss';
    }
    
    return 'unknown';
  }
  
  /**
   * Generate a unique key for a disc
   */
  discKey(info: DiscDetail): string {
    const hash =
      (info as any)?.disc_hash ||
      (info as any)?.content_hash ||
      (info as any)?.hash ||
      null;
    const titleSig = (info as any)?.movie_name || (info as any)?.title || '';
    const ident = hash ? `hash:${hash}` : `sig:${titleSig}`;
    return `${ident}:${info.disc_num}:${info.mount_point || 'nomount'}`;
  }
  
  /**
   * Check if a job matches a disc
   */
  jobMatchesDisc(info: DiscDetail, status: JobStatus | null): boolean {
    if (!status) return false;
    
    // Check disc_id first (most reliable if available)
    const infoDiscId = (info as any)?.disc_id || null;
    const statusDiscId = status.disc_id || (status as any)?.disc_payload?.disc_id || null;
    if (infoDiscId && statusDiscId) {
      return infoDiscId === statusDiscId;
    }
    
    // Fallback to hash comparison (reliable for unique discs)
    const infoHash = info.disc_hash || (info as any)?.content_hash || null;
    const statusHash =
      status.disc_hash ||
      (status as any)?.disc_payload?.disc_hash ||
      (status as any)?.disc_payload?.content_hash ||
      null;
    if (infoHash && statusHash) {
      return infoHash === statusHash;
    }
    
    // Fallback to disc_num and mount_point (less reliable but better than nothing)
    const infoNum = info.disc_num || null;
    const statusNum = (status as any)?.disc_payload?.disc_num || (status as any)?.disc_num || null;
    const infoMount = info.mount_point || null;
    const statusMount = (status as any)?.disc_payload?.mount_point || (status as any)?.mount_point || null;
    
    // If we have both disc_num and mount_point, both must match
    if (infoNum && statusNum && infoMount && statusMount) {
      return infoNum === statusNum && infoMount === statusMount;
    }
    
    // If we only have disc_num, it must match
    if (infoNum && statusNum) {
      return infoNum === statusNum;
    }
    
    // If no identifiers match, assume it doesn't match
    return false;
  }
  
  /**
   * Save label form (public method for components)
   */
  saveLabelForm(labelForm: any, contextId: string, contextType: 'job' | 'drive'): Observable<void> {
    return this.saveLabelFormToContext(labelForm, contextId, contextType);
  }
  
  // ===== Action Methods =====
  
  /**
   * Start a rip job from current workflow context
   * Uses the active context automatically - no need to pass contextId
   * Phase 1: Auto-progresses step based on DiscDB hit/miss
   */
  private _startRipInProgress = false;
  private _startRipInProgress$ = new BehaviorSubject<boolean>(false);

  /**
   * True while saving label workflow context (PATCH): film (movie/series), boxset (release/boxset), and
   * related link/create+link paths. Exposed as getFilmStepSaveInProgress$ for the primary CTA (film + boxset steps).
   */
  private _filmStepSaveInProgress$ = new BehaviorSubject<boolean>(false);
  private _labelContextSaveDepth = 0;

  private beginLabelContextSave(): void {
    if (this._labelContextSaveDepth++ === 0) {
      this._filmStepSaveInProgress$.next(true);
    }
  }

  private endLabelContextSave(): void {
    this._labelContextSaveDepth = Math.max(0, this._labelContextSaveDepth - 1);
    if (this._labelContextSaveDepth === 0) {
      this._filmStepSaveInProgress$.next(false);
    }
  }

  /** Ref-counted wrapper so nested create+link does not clear the in-progress flag early. */
  private withLabelContextSaveProgress<T>(source: Observable<T>): Observable<T> {
    return defer(() => {
      this.beginLabelContextSave();
      return source.pipe(finalize(() => this.endLabelContextSave()));
    });
  }

  /** True while saving disc step labelForm then advancing (job). Primary CTA spinner on disc step. */
  private _discStepContinueInProgress$ = new BehaviorSubject<boolean>(false);

  /** When set, workflow_context_updated for this jobId will not overwrite jobStatus/workflow_step/labelForm.workflow_step for 500ms. */
  private _postTransitionIgnore: { jobId: string; until: number } | null = null;

  /** When set, context_changed refetch for disc_id is skipped for 500ms after we saved disc workflow context (avoids reload on Movie/Series toggle). */
  private _lastDiscContextSaveUntil = 0;

  /** Suppress applying context_changed to the active card until this timestamp (avoids UI jump while user is interacting). */
  private _contextApplySuppressUntil = 0;
  private static readonly POST_SUPPRESS_MS = 500;
  private static readonly INTERACT_SUPPRESS_MS = 5000;

  private _setContextApplySuppressFor(durationMs: number): void {
    this._contextApplySuppressUntil = Date.now() + durationMs;
  }

  /**
   * When applying a fetched context after context_changed, preserve critical labelForm fields from the
   * active context if the fetched form is missing them (e.g. after Start Copy, refetch can return before
   * disc workflow context was saved, so boxset/release appear unset and Continue is disabled).
   */
  private _mergeLabelFormFromActive(context: WorkflowContext, activeContext: WorkflowContext): WorkflowContext {
    const fetched = context.labelForm || {};
    const active = activeContext.labelForm || {};
    const criticalKeys = ['movie_id', 'release_id', 'boxset_id', 'release_name', 'release_slug'] as const;
    let merged: Record<string, unknown> | null = null;
    for (const k of criticalKeys) {
      const activeVal = active[k];
      const fetchedVal = fetched[k];
      const activeHas = activeVal != null && activeVal !== '';
      const fetchedMissing = fetchedVal == null || fetchedVal === '';
      if (activeHas && fetchedMissing) {
        if (!merged) merged = { ...fetched };
        (merged as any)[k] = activeVal;
      }
    }
    if (!merged) return context;
    return { ...context, labelForm: merged };
  }

  /**
   * Disc workflow-context refetch after re-insert can disagree with the active job on job-scoped fields
   * (discdbHit, jobStatus, titles). Merge discInfo plus **disc_number** only on labelForm — do not push
   * disc_name/disc_slug/disc_format from WS (same as title labels: those stay client-while-editing; save/Continue persist).
   *
   * jobStatus is backend-authoritative (rip_state / label_state / phase transitions come through as
   * `changed_fields: ['jobStatus']` from `_emit_to_job_workflow` after `apply_job_state`). When the
   * backend explicitly hints jobStatus changed, apply it here — otherwise stage-transition events
   * (e.g. rip_state 'running' → 'completed' at rip completion) never reach the UI while the user is
   * on a job-type active context, and the workflow surface stays stuck showing the previous state
   * (visible as "Finalizing…" persisting after the rip actually finished) until page reload.
   */
  /**
   * Apply a context that came from the SERVER — a refetch, a websocket-driven
   * fetch, or a save response — reconciling titles per row instead of
   * replacing the array.
   *
   * #778. A fetched context is a snapshot of server state at the moment the
   * request was *served*, but it is applied whenever the response happens to
   * *arrive*. Those are different instants. If the user edited a title in
   * between, wholesale replacement silently reverts that edit: no error, no
   * toast, the typing just undoes itself. Reconciling per row by version
   * makes the outcome independent of arrival order, which is the only way
   * this class of race stops needing timing windows to paper over it.
   */
  private applyFetchedContext(context: WorkflowContext): void {
    this.updateContext(this.withReconciledTitles(context));
  }

  /**
   * Merge fetched titles into the local array per row. Never used for local
   * edits — those are authoritative by definition and go through
   * updateContext directly.
   */
  private withReconciledTitles(context: WorkflowContext): WorkflowContext {
    const incoming = context?.titles;
    if (!Array.isArray(incoming)) return context;

    const current = this._activeContext$.value;
    const local = current?.titles;
    if (!Array.isArray(local) || local.length === 0) return context;

    // A fetch that returns no titles is not evidence that the titles were
    // deleted — lightweight fetches (include: 'label,job') legitimately omit
    // them. Treating "absent" as "empty" would wipe the table.
    if (incoming.length === 0) {
      return { ...context, titles: local };
    }

    const localByKey = new Map<string, any>();
    for (const t of local) {
      try {
        localByKey.set(this.getTitleKey(t, 'withReconciledTitles:local'), t);
      } catch {
        continue;
      }
    }

    // Server membership wins (a row genuinely removed upstream must vanish),
    // but per-row CONTENT is decided by version.
    const merged = incoming.map((row: any) => {
      let key: string;
      try {
        key = this.getTitleKey(row, 'withReconciledTitles:incoming');
      } catch {
        return row;
      }
      const mine = localByKey.get(key);
      if (!mine) return row;

      // The version we know about is the highest of what we last read and
      // what we have written since — latestTitleSeqById tracks in-flight
      // writes the fetch cannot have seen.
      const knownSeq = Math.max(
        this.titleStore.cachedSeq(key),
        typeof mine.title_seq === 'number' ? mine.title_seq : 0,
      );
      const incomingSeq = typeof row.title_seq === 'number' ? row.title_seq : 0;

      // Older-or-equal snapshot: keep what we have. Equal counts as older
      // because a fetch served before our write commits carries the same
      // version as the row we read.
      const base = incomingSeq > knownSeq ? row : mine;

      // Text the user is still typing is never overwritten, at any version:
      // its write has not resolved yet, so no server snapshot can contain it.
      const pending = this.titleStore.pendingTextFor(key);
      if (typeof pending === 'string' && base.title !== pending) {
        return { ...base, title: mine.title };
      }
      return base;
    });

    return { ...context, titles: merged };
  }

  private _discContextPatchForActiveJob(
    fetched: WorkflowContext,
    changedFields?: string[],
  ): Partial<WorkflowContext> {
    if (!fetched.discInfo) return {};
    const patch: Partial<WorkflowContext> = { discInfo: fetched.discInfo };
    if (changedFields?.includes('jobStatus') && fetched.jobStatus) {
      patch.jobStatus = fetched.jobStatus;
    }
    const active = this._activeContext$.value;
    const flf = fetched.labelForm;
    if (!active?.labelForm || !flf) return patch;
    if (!Object.prototype.hasOwnProperty.call(flf, 'disc_number')) {
      return patch;
    }
    const nextLabel = { ...active.labelForm, disc_number: (flf as any).disc_number };
    patch.labelForm = nextLabel;
    return patch;
  }

  /** True if the context_changed message refers to the currently active card (same job or disc). */
  private _contextChangedMatchesActiveCard(message: { job_id?: string; disc_id?: string }): boolean {
    const active = this._activeContext$.value;
    if (!active) return false;
    if (message.job_id && active.jobStatus?.jobId === message.job_id) return true;
    if (message.disc_id) {
      if (active.type === 'drive' && active.id === message.disc_id) return true;
      if ((active.discInfo as any)?.disc_id === message.disc_id) return true;
    }
    return false;
  }

  private _setPostTransitionIgnore(jobId: string, durationMs: number = 500): void {
    this._postTransitionIgnore = { jobId, until: Date.now() + durationMs };
    this._setContextApplySuppressFor(WorkflowService.POST_SUPPRESS_MS);
  }

  /** Apply POST response from workflow/step/complete or label/complete: updateContext + 500ms ignore. */
  private _applyStepResponse(res: JobStatus, step: string, jobId?: string): void {
    const form = this.getCurrentContext()?.labelForm || {};
    const up: Partial<WorkflowContext> = {
      jobStatus: res,
      workflowStep: (res.workflow_step ?? step) as WorkflowStep,
      labelForm: { ...form, workflow_step: res.workflow_step ?? step },
      stepNavigationSource: 'user',
    };
    if (step === 'postprocess') up.activeStage = 'postprocess';
    if (step === 'transfer') up.activeStage = 'transfer';
    this.updateContext(up);
    const jid = res.jobId ?? jobId ?? this.getCurrentContext()?.jobStatus?.jobId ?? this.getCurrentContext()?.id ?? '';
    if (jid) this._setPostTransitionIgnore(jid);
    this._workflowContextStatus$.next('ready');
  }

  /**
   * Observable for startRip in-progress state
   */
  getStartRipInProgress$(): Observable<boolean> {
    return this._startRipInProgress$.asObservable();
  }

  /**
   * Observable for label workflow-context save in progress (PATCH), including movie/series and release/boxset.
   * When true, primary action should be disabled and show spinner on the film and boxset steps.
   */
  getFilmStepSaveInProgress$(): Observable<boolean> {
    return this._filmStepSaveInProgress$.asObservable();
  }

  getDiscStepContinueInProgress$(): Observable<boolean> {
    return this._discStepContinueInProgress$.asObservable();
  }

  /** True while saveDiscStepAndContinueToNext is in flight (suppress overlapping autosave PATCHes). */
  isDiscStepContinueInProgress(): boolean {
    return this._discStepContinueInProgress$.value;
  }

  /**
   * Job disc step: persist current labelForm, apply response, then POST workflow/step/complete to titles.
   * Ensures slug/name are saved before advancing; pairs with getDiscStepContinueInProgress$ for CTA spinner.
   */
  saveDiscStepAndContinueToNext(): Observable<JobStatus | undefined> {
    const context = this.getCurrentContext();
    if (!context?.labelForm) {
      return throwError(() => new Error('Cannot continue disc step: missing label form'));
    }
    if (context.type !== 'job' || !context.jobStatus?.jobId) {
      return throwError(() => new Error('Cannot continue disc step: job context required'));
    }
    const jobId = context.jobStatus.jobId;
    if (this._discStepContinueInProgress$.value) {
      return throwError(() => new Error('Disc step advance already in progress'));
    }
    // #349: Don't include tracks in the save payload — they go through dedicated PATCH endpoints
    const { tracks: _stripDiscStepTracks, ...discStepPayload } = context.labelForm || {};
    this._discStepContinueInProgress$.next(true);
    return this.saveJobWorkflowContext(jobId, discStepPayload, false, { skipStaleResponseFilter: true }).pipe(
      tap((updated) => {
        if (updated) {
          this.applyContextIfMatchesSelection(updated);
        }
      }),
      switchMap(() => {
        const obs = this.continueToNextStep() as void | Observable<JobStatus>;
        if (obs != null && typeof (obs as Observable<JobStatus>).subscribe === 'function') {
          return obs as Observable<JobStatus>;
        }
        return of(undefined);
      }),
      finalize(() => this._discStepContinueInProgress$.next(false))
    );
  }

  /**
   * Apply POST /jobs/rip (or refetched) JobStatus to the active context when the current
   * selection matches. Used on successful HTTP and after ambiguous transport-error recovery.
   */
  private _applyStartRipHttpResult(result: JobStatus, contextAtRipStart: WorkflowContext): boolean {
    const activeContext = this._activeContext$.value;
    const resultJobId = result?.jobId;
    const sameDrive = activeContext && activeContext.id === contextAtRipStart.id;
    const switchedToJob = activeContext && resultJobId && activeContext.id === resultJobId;
    const contextMatches = Boolean((sameDrive || switchedToJob) && resultJobId);
    const nextStep = (result.workflow_step != null
      ? result.workflow_step
      : (contextAtRipStart.workflowStep === 'film' ? 'boxset' : undefined)) as WorkflowStep | undefined;
    if (!contextMatches || !resultJobId) {
      return false;
    }
    const up: Partial<WorkflowContext> = {
      jobStatus: result,
      stepNavigationSource: 'user',
    };
    if (nextStep != null) {
      up.workflowStep = nextStep;
      up.labelForm = { ...(contextAtRipStart.labelForm || {}), workflow_step: nextStep };
    }
    this.updateContext(up);
    this._setPostTransitionIgnore(result.jobId, 2000);
    this._workflowContextStatus$.next('ready');
    return true;
  }

  /**
   * After POST /jobs/rip fails with an ambiguous transport error, refetch job by disc and
   * merge into context if applicable. Returns whether recovery updated the UI.
   */
  tryRecoverStartRipAfterAmbiguousError(): Observable<boolean> {
    const context = this.getCurrentContext();
    if (!context?.labelForm) {
      return of(false);
    }
    const di = context.discInfo;
    const opts: {
      disc_id?: string | null;
      disc_hash?: string | null;
      disc_num?: string | null;
    } = {};
    if (di?.disc_id && !String(di.disc_id).startsWith('pending-')) {
      opts.disc_id = di.disc_id;
    }
    if (di?.disc_hash) {
      opts.disc_hash = di.disc_hash;
    }
    const discNum = di?.disc_num || context.discNum;
    if (discNum) {
      opts.disc_num = String(discNum);
    }
    if (!opts.disc_id && !opts.disc_hash && !opts.disc_num) {
      return of(false);
    }
    return this.jobSvc.getJobByDisc(opts).pipe(
      take(1),
      map((job) => {
        if (!job?.jobId) {
          return false;
        }
        return this._applyStartRipHttpResult(job, context);
      }),
      catchError(() => of(false))
    );
  }

  /**
   * @param options optional flags forwarded to ``POST /jobs/rip``. Currently:
   *   - ``forceConcurrentOnSaturatedBus`` (#578): retry after the user
   *     acknowledged the USB-bus-saturation modal. Translated to the
   *     backend's ``force_concurrent_on_saturated_bus: true`` field.
   */
  startRip(options?: { forceConcurrentOnSaturatedBus?: boolean }): Observable<JobStatus> {
    // Prevent duplicate requests
    if (this._startRipInProgress) {
      this.logger.warn('[WorkflowService] startRip called while already in progress, ignoring duplicate request');
      return throwError(() => new Error('Rip start already in progress'));
    }

    const context = this.getCurrentContext();

    if (!context || !context.labelForm) {
      throw new Error('Cannot start rip: missing context or label form');
    }

    // Build payload according to backend JobCreate schema (no label_payload, includes disc_num)
    const mountPoint = context.discInfo?.mount_point || context.id;
    const payload: any = {
      mount_point: mountPoint,
    };

    if (options?.forceConcurrentOnSaturatedBus) {
      payload.force_concurrent_on_saturated_bus = true;
    }

    // Add disc_id if available
    if (context.discInfo?.disc_id) {
      payload.disc_id = context.discInfo.disc_id;
    }
    
    // Add disc_num if available (required by backend or must be derivable)
    // Try multiple sources: discInfo.disc_num, context.discNum, or derive from drive list
    let discNum = context.discInfo?.disc_num || context.discNum;
    
    // If still not available, try to get from drive list by mount_point
    if (!discNum && mountPoint) {
      try {
        // getDrives() returns Drive[] synchronously
        const drives = this.driveSvc.getDrives();
        const drive = drives?.find(d => d.mount_point === mountPoint);
        if (drive?.disc_num) {
          discNum = drive.disc_num;
        }
      } catch (err) {
        // Silently fail - backend will try to derive it
      }
    }
    
    if (discNum) {
      payload.disc_num = discNum;
    }
    
    this.logger.debug('[WorkflowService] startRip: Calling jobSvc.startRip', { payload });
    this._startRipInProgress = true;
    this._startRipInProgress$.next(true);
    this._workflowContextStatus$.next('pending');
    return this.jobSvc.startRip(payload).pipe(
      tap((result) => {
        this._applyStartRipHttpResult(result, context);
        this._startRipInProgress = false;
        this._startRipInProgress$.next(false);
      }),
      catchError((err) => {
        this._workflowContextStatus$.next('ready');
        this._startRipInProgress = false;
        this._startRipInProgress$.next(false);
        return throwError(() => err);
      })
    );
  }
  
  /**
   * Start post-processing for the current job in context
   * Uses the active context automatically
   * Phase 1: Auto-progresses to postprocess step
   */
  startPostProcess(): Observable<any> {
    const context = this.getCurrentContext();
    if (!context?.jobStatus?.jobId) {
      throw new Error('Cannot start post-process: missing job in context');
    }
    const jobId = context.jobStatus.jobId;
    this._workflowContextStatus$.next('pending');
    return this.jobSvc.startPostProcess(jobId).pipe(
      tap((res) => {
        const form = this.getCurrentContext()?.labelForm || {};
        this.updateContext({
          jobStatus: res,
          workflowStep: (res.workflow_step ?? 'postprocess') as WorkflowStep,
          labelForm: { ...form, workflow_step: res.workflow_step ?? 'postprocess' },
          stepNavigationSource: 'automatic',
        });
        if (res?.jobId) this._setPostTransitionIgnore(res.jobId);
        this._workflowContextStatus$.next('ready');
      }),
      catchError((err) => {
        this._workflowContextStatus$.next('ready');
        return throwError(() => err);
      })
    );
  }
  
  /**
   * Start transfer for the current job in context
   * Uses the active context automatically
   * Phase 1: Auto-progresses to transfer step
   */
  startTransfer(transferConfig?: any): Observable<any> {
    const context = this.getCurrentContext();
    if (!context?.jobStatus?.jobId) {
      throw new Error('Cannot start transfer: missing job in context');
    }
    const jobId = context.jobStatus.jobId;
    this._workflowContextStatus$.next('pending');
    return this.jobSvc.transferJob(jobId, transferConfig).pipe(
      tap((res) => {
        const form = this.getCurrentContext()?.labelForm || {};
        this.updateContext({
          jobStatus: res,
          workflowStep: (res.workflow_step ?? 'transfer') as WorkflowStep,
          labelForm: { ...form, workflow_step: res.workflow_step ?? 'transfer' },
          activeStage: 'transfer',
          stepNavigationSource: 'automatic',
        });
        if (res?.jobId) this._setPostTransitionIgnore(res.jobId);
        this._workflowContextStatus$.next('ready');
      }),
      catchError((err) => {
        this._workflowContextStatus$.next('ready');
        return throwError(() => err);
      })
    );
  }
  
  /**
   * Resume the current job in context
   * Uses the active context automatically
   */
  resumeJob(): Observable<any> {
    const context = this.getCurrentContext();
    if (!context?.jobStatus?.jobId) {
      throw new Error('Cannot resume job: missing job in context');
    }
    const jobId = context.jobStatus.jobId;
    
    return this.jobSvc.resumeJob(jobId).pipe(
      tap(() => {
        // Progress updates will automatically flow through unified WebSocket connection
        // No need to explicitly connect per job
      })
    );
  }

  /**
   * Mark the job as completed (dismiss from carousel).
   * Calls POST /jobs/{jobId}/finish. Backend emits job_finished so the job is removed from discs$.
   * Uses responseType: 'text' so 204 No Content does not trigger a JSON parse error.
   *
   * On HTTP success we optimistically apply the same mutation the job_finished WS
   * handler will apply, so the carousel card disappears immediately rather than
   * after the WS round-trip (was a visible 100–500ms lag, looked like a bug).
   * The WS replay is idempotent — running the mutation twice on already-finished
   * state is a no-op.
   */
  finishJob(jobId: string): Observable<void> {
    const ctx = this._activeContext$.value;
    const discId = ctx && ctx.type === 'job' ? (ctx.jobStatus?.disc_id ?? null) : null;
    return this.http.post(
      `${this.apiUrl}/jobs/${jobId}/finish`,
      null,
      { responseType: 'text' }
    ).pipe(
      tap(() => {
        this.applyJobFinishedLocally(jobId, discId, 'completed');
        // Refresh coordinator state so the disc's finalized_release_id/slug
        // (populated only in the backend's finalized_* fields) land in discs$.
        // The optimistic mutation above already flips finalized=true so the
        // "Already in Library" card renders instantly on next click; this
        // fetch fills in the "Open in Library" link data shortly after.
        this.fetchInitialState();
      }),
      map(() => undefined)
    );
  }

  // dismissJob removed — cards are managed by lifecycle:
  // Unfinished: stays until Finish clicked; Failed: stays until disc re-ripped successfully
  
  /**
   * Navigate to next workflow step (Phase 1: Enhanced with progression tracking).
   * Returns Observable when it performs an async POST (titles->postprocess or step/complete); void when returning early. Caller should subscribe to run next/error (e.g. clear _continueInProgress).
   */
  continueToNextStep(): void | Observable<JobStatus> {
    const context = this.getCurrentContext();
    if (!context) return;
    
    const steps: WorkflowStep[] = getStepOrderForContext(context);

    const currentStep = this.getEffectiveWorkflowStep(context);
    const currentIndex = steps.indexOf(currentStep);


    if (currentIndex < 0 || currentIndex >= steps.length - 1) {
      return; // Already at last step or invalid step
    }
    
    // Validate current step is complete before proceeding.
    // titles: same source of truth as canContinue$ / onContinue — labelForm.tracks can be stale vs context.titles;
    // do not use validateStepCompletion('titles', labelForm) here.
    let validation: { valid: boolean; errors: string[] };
    if (currentStep === 'titles') {
      const completionState = context.stepCompletionState || this.getStepCompletionState(context);
      if (!completionState.titles) {
        validation = { valid: false, errors: ['All titles must be labeled or ignored'] };
      } else {
        validation = { valid: true, errors: [] };
      }
    } else {
      validation = this.validateStepCompletion(currentStep, context.labelForm);
    }
    if (!validation.valid) {
      this.logger.warn(`Cannot continue: current step '${currentStep}' is not complete:`, validation.errors);
      return; // Stay on current step
    }
    
    let nextStep = steps[currentIndex + 1];

    // titles->transfer: POST /label/complete; apply response (jobStatus,
    // workflow_step) and 500ms ignore. #365 Phase 2 § 6.4 — the previous
    // titles->postprocess hop collapsed into titles->transfer when the
    // standalone postprocess step was removed.
    if (currentStep === 'titles' && nextStep === 'transfer' && (context.jobStatus?.jobId || context.id)) {
      const jid = context?.jobStatus?.jobId ?? context?.id ?? undefined;
      return this.completeLabel().pipe(
        tap((res) => this._applyStepResponse(res, 'transfer', res?.jobId ?? jid))
      );
    }

    // Validate we can navigate to the next step (for breadcrumb navigation and other steps)
    const nextStepValidation = this.canNavigateToStep(context, nextStep);
    if (!nextStepValidation.allowed) {
      this.logger.warn(`Cannot navigate to next step '${nextStep}':`, nextStepValidation.reason);
      return;
    }

    // Step-only advance: boxset->disc, disc->titles, summary->transfer.
    // POST /workflow/step/complete and apply response.
    return this.advanceStepTo(nextStep as 'boxset' | 'disc' | 'titles' | 'transfer');
  }
  
  /**
   * Navigate to previous workflow step (Phase 1: Enhanced with progression tracking)
   */
  navigateToPreviousStep(): void {
    const context = this.getCurrentContext();
    if (!context) return;
    const steps: WorkflowStep[] = getStepOrderForContext(context);
    const currentStep = context.workflowStep || this.determineWorkflowStep(context);
    const currentIndex = steps.indexOf(currentStep);
    if (currentIndex > 0) {
      const prevStep = steps[currentIndex - 1];
      // #363 H1 — same gate as explicit navigateToStep: no backward entry
      // into labeling steps once labels are locked.
      const validation = this.canNavigateToStep(context, prevStep);
      if (!validation.allowed) {
        this.logger.debug('[WorkflowService] back navigation blocked', validation.reason);
        return;
      }
      this.updateContext({
        workflowStep: prevStep,
        stepNavigationSource: 'user'
      });
    }
  }

  /**
   * #363 H1 — labels are locked once the active job's postprocess or
   * transfer has actually consumed them. Mirrors the backend guard in
   * `assert_title_patch_allowed` (PATCH /discs/{id}/titles → 409
   * labels_locked).
   *
   * Note: label_state === 'completed' alone is NOT enough to lock. The
   * postprocess step doesn't auto-dispatch off complete_label — it waits
   * for the user's "Start Transfer" click on the Transfer page. Between
   * complete_label and that click, disc_titles is quiescent: no filenames
   * have been computed yet, nothing has been renamed or moved. Users
   * legitimately need to Back-navigate to Titles in that window to fix
   * mistakes discovered while eyeballing the Transfer preview. The old
   * behavior (lock immediately on complete_label) disabled Back at a
   * moment when relaxing it is safe.
   */
  areLabelsLocked(context: WorkflowContext | null): boolean {
    const jobStatus = context?.jobStatus;
    if (!jobStatus) return false;
    const postState = jobStatus.post_state || jobStatus.pipeline?.['postprocess'] || '';
    const transferState = jobStatus.transfer_state || jobStatus.pipeline?.['transfer'] || '';
    return (
      postState === 'running' ||
      postState === 'completed' ||
      transferState === 'running' ||
      transferState === 'completed'
    );
  }
  
  /**
   * Navigate to a specific step (Phase 1: Enhanced with validation)
   */
  navigateToStep(targetStep: WorkflowStep): { allowed: boolean; reason?: string } {
    const context = this.getCurrentContext();
    if (!context) {
      return { allowed: false, reason: 'No active context' };
    }

    const validation = this.canNavigateToStep(context, targetStep);
    if (!validation.allowed) return validation;
    
    // Load deferred data when navigating to steps that need titles/release data
    const stepsNeedingTitles: WorkflowStep[] = ['titles', 'summary', 'transfer'];
    if (stepsNeedingTitles.includes(targetStep)) {
      this.loadDeferredContextData();
    }

    const steps: WorkflowStep[] = getStepOrderForContext(context);
    const currentStep = this.getEffectiveWorkflowStep(context);
    const currentIndex = steps.indexOf(currentStep);
    const targetIndex = steps.indexOf(targetStep);
    const forwardSteps = ['boxset', 'disc', 'titles', 'transfer'] as const;

    // Backward: UI-only update, do not call advanceStepTo or completeWorkflowStep
    if (targetIndex < currentIndex) {
      this.updateContext({ workflowStep: targetStep, stepNavigationSource: 'user' });
      this._setContextApplySuppressFor(WorkflowService.INTERACT_SUPPRESS_MS);
      return validation;
    }

    // Same step: no-op
    if (targetIndex === currentIndex) return validation;

    // Forward: titles->transfer must use completeLabel, not advanceStepTo.
    // (#365 Phase 2 § 6.4 — was titles->postprocess before the standalone
    // postprocess step was collapsed into transfer's preparing sub-phase.)
    if (currentStep === 'titles' && targetStep === 'transfer' && (context.jobStatus?.jobId || context.id)) {
      const obs = this.continueToNextStep();
      if (obs != null && typeof (obs as any)?.subscribe === 'function') {
        (obs as Observable<unknown>).subscribe({
          error: (e) => this.logger.warn('[WorkflowService] continueToNextStep from navigateToStep (titles->transfer) failed', e),
        });
      }
      return validation;
    }

    if (forwardSteps.includes(targetStep as typeof forwardSteps[number])) {
      this._setContextApplySuppressFor(WorkflowService.INTERACT_SUPPRESS_MS);
      this.advanceStepTo(targetStep as typeof forwardSteps[number]).subscribe({
        error: (e) => this.logger.warn('[WorkflowService] advanceStepTo from navigateToStep failed', e),
      });
    }
    return validation;
  }

  /**
   * Derive furthest step from job state and form. Used as upper bound for forward navigation.
   * Copy started = rip_state in (running|completed) only — not pending.
   */
  computeFurthestStep(context: WorkflowContext): WorkflowStep {
    const discdbHit = context.discdbHit;
    const jobStatus = context.jobStatus;
    const labelForm = context.labelForm || {};

    const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status || '';
    const labelState = jobStatus?.label_state || jobStatus?.pipeline?.['label'] || '';
    const postState = jobStatus?.post_state || jobStatus?.pipeline?.['postprocess'] || '';
    const transferState = (jobStatus?.transfer_state ?? jobStatus?.pipeline?.['transfer']) || '';

    if (discdbHit) {
      if (transferState === 'ready' || transferState === 'pending' || transferState === 'running' || transferState === 'completed' || transferState === 'failed') {
        return 'transfer';
      }
      // #365 Phase 2 § 6.4 — postprocess step collapsed into transfer's
      // "preparing" sub-phase; post-state="completed" maps to transfer
      // (where the destination cards live).
      if (postState === 'completed') return 'transfer';
      return 'summary';
    }

    // Miss: film, boxset, disc, titles, transfer
    // When rip has not started (pending) or has failed, cap at film so page load shows film step
    if (ripState !== 'running' && ripState !== 'completed') {
      return 'film';
    }

    if (transferState === 'ready' || transferState === 'pending' || transferState === 'running' || transferState === 'completed' || transferState === 'failed') {
      return 'transfer';
    }
    // #365 Phase 2 § 6.4 — postprocess collapsed into transfer.
    if (postState === 'completed') return 'transfer';
    if (labelState === 'completed') return 'transfer';
    const discNumber = labelForm?.disc_number ?? (jobStatus as any)?.disc_payload?.disc_number;
    const discName = labelForm?.disc_name ?? (jobStatus as any)?.disc_name ?? (jobStatus as any)?.disc_payload?.disc_name;
    const discSlug = labelForm?.disc_slug ?? (jobStatus as any)?.disc_payload?.disc_slug;
    const discFieldsFilled = [discNumber, discName, discSlug].every(v => v != null && String(v).trim() !== '');
    if (discFieldsFilled) return 'titles';
    const hasRelease = !!(labelForm?.release_id || labelForm?.release_slug || labelForm?.release_name || (jobStatus as any)?.release_id);
    if (hasRelease) return 'disc';
    if (ripState === 'running' || ripState === 'completed') return 'boxset';
    return 'film';
  }

  /**
   * Check if navigation to a step is allowed (Phase 1)
   */
  canNavigateToStep(
    context: WorkflowContext,
    targetStep: WorkflowStep
  ): { allowed: boolean; reason?: string } {
    const discdbHit = context.discdbHit;
    const steps: WorkflowStep[] = getStepOrderForContext(context);

    const currentStep = this.getEffectiveWorkflowStep(context);
    const currentIndex = steps.indexOf(currentStep);
    const targetIndex = steps.indexOf(targetStep);
    const furthestStep = this.computeFurthestStep(context);
    const furthestIndex = steps.indexOf(furthestStep);

    if (targetIndex === -1) {
      return { allowed: false, reason: 'Invalid step' };
    }

    // Can always navigate to current step
    if (targetIndex === currentIndex) {
      return { allowed: true };
    }

    // Backward: allow any prior step (no furthest check) — except into
    // labeling steps once the pipeline has consumed the labels (#363 H1).
    if (targetIndex < currentIndex) {
      const labelingSteps: WorkflowStep[] = ['film', 'boxset', 'disc', 'titles'];
      if (labelingSteps.includes(targetStep) && this.areLabelsLocked(context)) {
        return {
          allowed: false,
          reason: 'Labels were already consumed by post-processing and can no longer be edited',
        };
      }
      return { allowed: true };
    }

    // Forward: allow only if targetStep <= furthestStep
    if (targetIndex > furthestIndex) {
      return { allowed: false, reason: 'Step is not available yet' };
    }

    // Can navigate forward only if prerequisites are met
    const labelForm = context.labelForm;
    const jobStatus = context.jobStatus;
    const completionState = this.getStepCompletionState(context);

    const copyDependentSteps: WorkflowStep[] = ['boxset', 'disc', 'titles'];
    const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status;
    const jobExists = !!jobStatus?.jobId;
    if (targetIndex > currentIndex && copyDependentSteps.includes(targetStep)) {
      if (ripState !== 'running' && ripState !== 'completed' && !jobExists) {
        return { allowed: false, reason: 'Copy must be started before labeling steps' };
      }
    }

    // Path A gate: forward navigation past exploratory_rip is blocked until
    // the canonical rip finishes. Backward navigation already returned above.
    const srStage = (jobStatus as any)?.segment_reorder_state?.stage;
    if (
      currentStep === 'exploratory_rip' &&
      targetIndex > currentIndex &&
      srStage !== 'canonical_complete'
    ) {
      return {
        allowed: false,
        reason: 'Exploratory rip must finish before continuing',
      };
    }

    switch (targetStep) {
      case 'exploratory_rip':
        // Reachable iff the job has a segment_reorder_state (Path A active).
        // Backward navigation to it already returned true above.
        if (!srStage) {
          return { allowed: false, reason: 'Exploratory rip is not active' };
        }
        break;

      case 'boxset':
        if (!completionState.film) {
          return { allowed: false, reason: 'Movie must be selected first' };
        }
        if (targetIndex > currentIndex) {
          if (ripState !== 'running' && ripState !== 'completed' && !jobExists) {
            return { allowed: false, reason: 'Copy must be started before boxset step' };
          }
        }
        break;
        
      case 'disc':
        if (!discdbHit) {
          // For DiscDB miss, require film and boxset
          if (!completionState.film) {
            return { allowed: false, reason: 'Movie must be selected first' };
          }
          if (!completionState.boxset) {
            return { allowed: false, reason: 'Release or boxset must be selected first' };
          }
        }
        break;
        
      case 'titles':
        if (!discdbHit) {
          // For DiscDB miss, require film, boxset, and disc
          if (!completionState.film) {
            return { allowed: false, reason: 'Movie must be selected first' };
          }
          if (!completionState.boxset) {
            return { allowed: false, reason: 'Release or boxset must be selected first' };
          }
        }
        if (!completionState.disc) {
          return { allowed: false, reason: 'Disc information must be completed first' };
        }
        break;
        
      case 'transfer':
        // #365 Phase 2 § 6.4 — the standalone 'postprocess' case was
        // folded into this one when the step was collapsed. Same
        // prerequisites apply: all labeling complete + rip done +
        // post-state past 'pending'. The transfer stage UI shows the
        // prep work as "Preparing files…" via transferPhaseLabel.
        if (!discdbHit) {
          // For DiscDB miss, require all 4 labeling steps
          if (!completionState.film) {
            return { allowed: false, reason: 'Movie selection must be completed first' };
          }
          if (!completionState.boxset) {
            return { allowed: false, reason: 'Release or boxset selection must be completed first' };
          }
          if (!completionState.disc) {
            return { allowed: false, reason: 'Disc information must be completed first' };
          }
          if (!completionState.titles) {
            return { allowed: false, reason: 'Titles must be completed first' };
          }
        } else {
          // For DiscDB hit, only require disc and titles
          if (!completionState.disc) {
            return { allowed: false, reason: 'Disc information must be completed first' };
          }
          if (!completionState.titles) {
            return { allowed: false, reason: 'Titles must be completed first' };
          }
        }

        if (jobStatus) {
          const ripState = jobStatus.rip_state || jobStatus.pipeline?.['rip'] || jobStatus.job_status;
          if (ripState !== 'completed' && ripState !== 'running') {
            return { allowed: false, reason: 'Rip must be completed before transfer' };
          }
          // post_state can be: pending, ready, running, completed, failed, skipped.
          // Breadcrumb should only be active once prep is ready or later.
          const postState = jobStatus.post_state || jobStatus.pipeline?.['postprocess'];
          if (postState === 'pending') {
            return { allowed: false, reason: 'Preparation is not ready yet' };
          }
        } else {
          return { allowed: false, reason: 'Job must be started before transfer' };
        }
        break;
    }
    
    return { allowed: true };
  }
  
  /**
   * Sync workflow step with active stage (Phase 2: Automatic coordination)
   * Only advances to activeStage when it's reachable (at or before computeFurthestStep)
   * to prevent a fight with updateCurrentStep which resets steps beyond furthest.
   */
  syncStepWithStage(): void {
    const context = this.getCurrentContext();
    if (!context) return;
    
    const activeStage = context.activeStage;
    const workflowStep = this.getEffectiveWorkflowStep(context);
    
    // Don't override user navigation
    if (context.stepNavigationSource === 'user') return;
    
    // Only sync for postprocess/transfer stages
    if (activeStage !== 'postprocess' && activeStage !== 'transfer') return;
    if (workflowStep === activeStage) return; // Already on the right step
    
    // Guard: only advance if the target stage is reachable (at or before furthestStep).
    // Without this guard, syncStepWithStage and updateCurrentStep fight each other in an
    // infinite loop: sync advances to activeStage → updateCurrentStep resets to furthestStep → repeat.
    const furthestStep = this.computeFurthestStep(context);
    const steps: WorkflowStep[] = getStepOrderForContext(context);
    const activeStageIndex = steps.indexOf(activeStage as WorkflowStep);
    const furthestStepIndex = steps.indexOf(furthestStep);
    
    if (activeStageIndex < 0 || activeStageIndex > furthestStepIndex) return;
    
    this.updateContext({ 
      workflowStep: activeStage as WorkflowStep,
      stepNavigationSource: 'automatic'
    });
  }
  
  /**
   * Set the workflow step programmatically
   * Used when the component needs to correct an invalid stored step
   */
  setWorkflowStep(step: WorkflowStep): void {
    this.updateContext({
      workflowStep: step,
      stepNavigationSource: 'automatic'
    });
  }
  
  /**
   * Save context and close workflow (for "finish later")
   * Phase 1: Includes workflowStep when saving
   */
  saveAndClose(): void {
    const context = this.getCurrentContext();
    if (!context) return;
    
    // Save current context to backend (workflowStep will be included via save methods)
    if (context.labelForm) {
      if (context.type === 'job') {
        this.saveJobWorkflowContext(context.id, context.labelForm, false).subscribe();
      } else {
        this.saveDiscWorkflowContext(context.id, context.labelForm, false, false).subscribe();
      }
    }
    
    // Clear selected card (closes workflow view)
    // Note: This should be done via RipperStateService, but we can trigger it here
    // The parent component should handle clearing the selection
  }
  
  /**
   * Finalize label for the current job in context
   * Uses the active context automatically
   */

  /** Complete label stage (titles -> postprocess). POST /jobs/{id}/label/complete. Applies response (jobStatus, workflow_step); call subscribe. */
  completeLabel(): Observable<JobStatus> {
    const context = this.getCurrentContext();
    const jobId = context?.jobStatus?.jobId ?? context?.id;
    if (!jobId) {
      throw new Error('Cannot complete label: missing context id');
    }
    this._workflowContextStatus$.next('pending');
    // Strip `tracks` from labelForm before sending. Individual title edits
    // are persisted via PATCH /api/discs/{id}/titles as the user works —
    // disc_titles is the source of truth for per-title fields. But
    // context.labelForm.tracks is a snapshot that never gets synced with
    // those PATCH responses (applyTitlePatchResults only updates
    // context.titles, not labelForm.tracks). Backend's
    // _apply_label_to_records at jobs.py:725 iterates `body.labelForm.tracks`
    // and OVERWRITES disc_titles.type/title/description/etc. with the
    // snapshot values, so sending stale tracks reverts the fresh types the
    // user just PATCHed — the subsequent _validate_all_titles_labeled then
    // finds the reverted nulls and returns 400. Reload appears to help
    // because workflow-context returns fresh labelForm.tracks synced from
    // disc_titles at fetch time, but any further edits made in-session
    // desync it again. Omitting `tracks` here makes backend skip the
    // overwrite block and trust the disc_titles rows the PATCHes wrote.
    const labelForm = context?.labelForm ? { ...context.labelForm } : undefined;
    if (labelForm && 'tracks' in labelForm) {
      delete (labelForm as any).tracks;
    }
    return this.jobSvc.completeLabel(jobId, labelForm).pipe(
      catchError((err) => {
        this._workflowContextStatus$.next('ready');
        return throwError(() => err);
      })
    );
  }

  /** Advance workflow_step via POST /jobs/{id}/workflow/step/complete. Applies response and sets 500ms ignore. Returns Observable for caller to subscribe. */
  advanceStepTo(toStep: 'boxset' | 'disc' | 'titles' | 'transfer', optionalJobId?: string): Observable<JobStatus> {
    const context = this.getCurrentContext();
    const jobId = optionalJobId ?? context?.jobStatus?.jobId ?? (context?.type === 'job' ? context?.id : null) ?? context?.id ?? null;
    if (!jobId) {
      this._workflowContextStatus$.next('ready');
      return of({} as JobStatus);
    }
    this._workflowContextStatus$.next('pending');
    // titles->transfer is only allowed via POST /label/complete, not
    // /workflow/step/complete. #365 Phase 2 § 6.4 — was titles->postprocess
    // before the standalone postprocess step was collapsed into transfer.
    if (toStep === 'transfer' && context) {
      const cur = this.getEffectiveWorkflowStep(context);
      if (cur === 'titles') {
        return this.completeLabel().pipe(
          tap((res) => this._applyStepResponse(res, 'transfer', jobId))
        );
      }
    }
    const applyStepResponse = (res: JobStatus, step: string) => this._applyStepResponse(res, step, jobId);
    return this.jobSvc.completeWorkflowStep(jobId, toStep).pipe(
      tap((res) => applyStepResponse(res, toStep)),
      catchError((err: unknown) => {
        const e = err as { status?: number; error?: unknown };
        const status = e?.status;
        const body = (e?.error ?? e) as Record<string, unknown> | null | undefined;
        const detail = body?.['detail'];
        const d = (typeof detail === 'object' && detail != null && 'current_step' in (detail as object))
          ? (detail as { current_step?: string; message?: string })
          : (body?.['current_step'] ? (body as { current_step?: string; message?: string }) : null);
        const currentStep = (d?.current_step ?? (body?.['current_step'] as string | null) ?? null) as string | null;
        const msg = String(d?.message ?? body?.['message'] ?? (typeof detail === 'string' ? detail : '') ?? '');
        // No-op: backend already at to_step (e.g. after navigating back, we tried to "advance" to current)
        if (Number(status) === 400 && currentStep === toStep && String(msg).includes('Invalid step transition')) {
          const noop = { ...(context?.jobStatus || {}), jobId, workflow_step: toStep } as JobStatus;
          return of(noop).pipe(tap((res) => applyStepResponse(res, toStep)));
        }
        const willRepair = Number(status) === 400 && !!currentStep && String(msg).includes('Invalid step transition');
        if (!willRepair) {
          this._workflowContextStatus$.next('ready');
          return throwError(() => err);
        }
        const steps: WorkflowStep[] = getStepOrderForContext(context);
        const fromIdx = steps.indexOf(currentStep as WorkflowStep);
        const toIdx = steps.indexOf(toStep);
        if (fromIdx < 0 || toIdx <= fromIdx) {
          this._workflowContextStatus$.next('ready');
          return throwError(() => err);
        }
        const intermediates = steps.slice(fromIdx + 1, toIdx + 1) as ('boxset' | 'disc' | 'titles' | 'transfer')[];
        return from(intermediates).pipe(
          concatMap((s) =>
            this.jobSvc.completeWorkflowStep(jobId, s).pipe(
              tap((res) => applyStepResponse(res, s))
            )
          ),
          last()
        );
      })
    );
  }

  finalizeLabel(): Observable<any> {
    const context = this.getCurrentContext();
    if (!context?.jobStatus?.jobId) {
      throw new Error('Cannot finalize label: missing job in context');
    }
    const jobId = context.jobStatus.jobId;
    
    return this.jobSvc.finalizeLabel(jobId).pipe(
      tap(() => {
        // Progress updates will automatically flow through unified WebSocket connection
        // No need to explicitly connect per job
      })
    );
  }
  
  /**
   * Finalize release (moved from RipperFacadeService)
   */
  finalizeRelease(releaseIdOrSlug: string): Observable<any> {
    // Call MetadataService if available, otherwise use HTTP directly
    if (this.metadataSvc) {
      return this.metadataSvc.finalizeRelease(releaseIdOrSlug);
    }
    return this.http.post(`${this.apiUrl}/releases/${encodeURIComponent(releaseIdOrSlug)}/finalize`, {});
  }
  
  /**
   * Revert transfer for the current job in context
   * Uses the active context automatically
   */
  revertTransfer(): Observable<any> {
    const context = this.getCurrentContext();
    if (!context?.jobStatus?.jobId) {
      throw new Error('Cannot revert transfer: missing job in context');
    }
    return this.jobSvc.revertTransfer(context.jobStatus.jobId);
  }
  
  /**
   * Reset post-processing for the current job in context
   * Uses the active context automatically
   */
  resetPostprocess(clearFiles: boolean = false, backupFiles: boolean = true): Observable<any> {
    const context = this.getCurrentContext();
    if (!context?.jobStatus?.jobId) {
      throw new Error('Cannot reset post-process: missing job in context');
    }
    return this.jobSvc.resetPostprocess(context.jobStatus.jobId, clearFiles, backupFiles);
  }
  
  /**
   * Restore post-processing for the current job in context
   * Uses the active context automatically
   */
  restorePostprocess(): Observable<any> {
    const context = this.getCurrentContext();
    if (!context?.jobStatus?.jobId) {
      throw new Error('Cannot restore post-process: missing job in context');
    }
    return this.jobSvc.restorePostprocess(context.jobStatus.jobId);
  }

  // ===== Labeling Helper Methods =====
  // Moved from LabelingHelperService to centralize labeling logic

  /**
   * Build a label form from a draft payload
   */
  private buildLabelForm(
    draft: any,
    applyDefaults: boolean = true,
    lastReleaseDetails: any | null = null,
    lastManualReleaseDetails: any | null = null
  ): any {
    const normalizeUiType = (val: any): string => normalizeTitleTypeForSelect(val);

    const normalizeFormat = (fmt: any): string | null => {
      const raw = (fmt || '').toString().toLowerCase();
      if (raw.includes('uhd') || raw.includes('4k')) return 'UHD';
      if (raw.includes('blu') || raw.includes('bd')) return 'Blu-Ray';
      if (raw.includes('dvd')) return 'DVD';
      return raw ? fmt : null;
    };

    const infoLabel = draft?.info_title || draft?.info_label || null;
    const baseReleaseName = draft?.release_name || '';
    const suggestedDiscFormat = normalizeFormat(draft?.disc_format);
    const releaseYear = draft?.release_year ?? draft?.year ?? null;
    const productionYear = draft?.production_year ?? draft?.year ?? null;
    const releaseSlug = draft?.release_slug || draft?.disc_group || '';

    const fromDraft = `${draft?.disc_name ?? draft?.disc_label ?? ''}`.trim();
    let defaultDiscName = fromDraft;
    if (!defaultDiscName && infoLabel && suggestedDiscFormat) {
      defaultDiscName = `${String(infoLabel).trim()} - ${suggestedDiscFormat}`;
    } else if (!defaultDiscName && suggestedDiscFormat) {
      defaultDiscName = suggestedDiscFormat;
    } else if (!defaultDiscName && infoLabel) {
      defaultDiscName = String(infoLabel).trim();
    }
    const defaultDiscSlug = draft?.disc_slug || '';
    const tracksSource = (draft?.titles && Array.isArray(draft.titles) ? draft.titles : draft?.tracks) || [];
    const tracks = tracksSource.map((t: any, idx: number) => ({
      source_file: t.source_file ?? t.output_file ?? null,
      track_id: t.title_id ?? t.id ?? null,
      title_id: t.title_id ?? t.id ?? null,
      disc_track_id: t.id ?? null,
      title: canonicalTrackTitle(t),
      description: t.description ?? t.note ?? '',
      note: t.description ?? t.note ?? '',
      comment: t.comment ?? null,
      season: t.season ?? null,
      episode: t.episode ?? null,
      type: normalizeUiType(t.type) || (t.content === false ? 'ignore' : ''),
      output_file: t.output_file || null,
      preview_url: t.preview_url || t.output_file || null,
      duration: t.duration ?? null,
      size: t.size ?? null,
      streams: t.streams ?? t.probe?.streams ?? null,
      chapters: t.chapters ?? null,
    }));

    const rawGroupType = draft?.group_type || draft?.title_type || 'movie';
    const normalizedGroupType = (rawGroupType === 'boxset' ? 'movie' : rawGroupType) as 'movie' | 'series';
    const rawMode = draft?.mode || 'movie';
    const normalizedMode = (rawMode === 'boxset' ? 'movie' : rawMode) as 'movie' | 'series';
    
    const form: any = {
      mode: normalizedMode,
      group_type: normalizedGroupType,
      disc_group: draft?.disc_group || draft?.release_slug || '',
      disc_number: draft?.disc_number ?? null,
      tmdb_id: '',
      disc_format: suggestedDiscFormat || null,
      release_name: baseReleaseName,
      release_slug: releaseSlug,
      info_title: infoLabel,
      upc: null,
      asin: null,
      cover_front_url: null,
      cover_back_url: null,
      release_year: releaseYear,
      production_year: productionYear,
      disc_name: defaultDiscName,
      disc_slug: defaultDiscSlug,
      movie_id: draft?.movie_id || null,
      boxset_id: draft?.boxset_id || null,
      // Disc-card primary season (#371, persisted in #536). Whitelisted here
      // so the job-context path (which routes through `buildLabelForm`)
      // doesn't strip it. Disc-context path passes labelForm through as-is.
      primary_season: (typeof draft?.primary_season === 'number' && draft.primary_season > 0)
        ? draft.primary_season : null,
      workflow_step: draft?.workflow_step || null,
      tracks,
    };
    
    const baseForm = applyDefaults 
      ? this.applyLastReleaseDefaults(form, lastReleaseDetails, lastManualReleaseDetails, false, false)
      : form;
    return baseForm;
  }

  /**
   * Merge user edits from old form into new form, preserving locked fields
   */
  private mergeUserEdits(
    newForm: any,
    oldForm: any,
    locks: { discNameLocked: boolean; discSlugLocked: boolean }
  ): any {
    if (!oldForm) return newForm;
    const merged = { ...newForm };
    
    // Preserve user edits if new payload left them blank
    if (oldForm.disc_name && !merged.disc_name) merged.disc_name = oldForm.disc_name;
    if (oldForm.disc_slug && !merged.disc_slug) merged.disc_slug = oldForm.disc_slug;
    if (oldForm.release_name && !merged.release_name) merged.release_name = oldForm.release_name;
    if (oldForm.release_slug && !merged.release_slug) merged.release_slug = oldForm.release_slug;
    if (oldForm.disc_group && !merged.disc_group) merged.disc_group = oldForm.disc_group;
    if (oldForm.disc_format && !merged.disc_format) {
      merged.disc_format = oldForm.disc_format;
    }
    
    // Merge tracks to preserve user edits (especially type selections)
    if (oldForm.tracks && Array.isArray(oldForm.tracks)) {
      const newTracks = merged.tracks || [];
      merged.tracks = this.mergeTracksPreservingLocalEdits(oldForm.tracks, newTracks);
    }
    
    return merged;
  }

  /**
   * Merge tracks preserving local edits
   */
  private mergeTracksPreservingLocalEdits(localTracks: any[], contextTracks: any[]): any[] {
    if (!Array.isArray(localTracks) || localTracks.length === 0) {
      return Array.isArray(contextTracks) ? [...contextTracks] : [];
    }
    if (!Array.isArray(contextTracks) || contextTracks.length === 0) {
      return [...localTracks];
    }

    const getTrackKey = (track: any): string => {
      return track.title_id || '';
    };

    const localTracksMap = new Map<string, any>();
    for (const track of localTracks) {
      const key = getTrackKey(track);
      if (key) {
        localTracksMap.set(key, track);
      }
    }

    const contextTracksMap = new Map<string, any>();
    for (const track of contextTracks) {
      const key = getTrackKey(track);
      if (key) {
        contextTracksMap.set(key, track);
      }
    }

    const mergedTracks: any[] = [];
    const processedKeys = new Set<string>();

    // First, process all context tracks (to maintain order from context)
    for (const contextTrack of contextTracks) {
      const key = getTrackKey(contextTrack);
      if (!key) continue;

      processedKeys.add(key);
      const localTrack = localTracksMap.get(key);

      if (localTrack) {
        // Track exists in both - merge preserving local edits
        const merged = { ...contextTrack };
        
        // Preserve local edits for editable fields
        if (localTrack.type !== undefined && localTrack.type !== null) merged.type = localTrack.type;
        if (localTrack.title !== undefined && localTrack.title !== null) merged.title = localTrack.title;
        if (localTrack.description !== undefined && localTrack.description !== null) merged.description = localTrack.description;
        if (localTrack.note !== undefined && localTrack.note !== null) merged.note = localTrack.note;
        if (localTrack.comment !== undefined && localTrack.comment !== null) merged.comment = localTrack.comment;
        if (localTrack.season !== undefined && localTrack.season !== null) merged.season = localTrack.season;
        if (localTrack.episode !== undefined && localTrack.episode !== null) merged.episode = localTrack.episode;
        if (localTrack.episode_name !== undefined && localTrack.episode_name !== null) {
          merged.episode_name = localTrack.episode_name;
        }
        merged.title = canonicalTrackTitle(merged);

        mergedTracks.push(merged);
      } else {
        // Track only in context - add it
        const onlyCtx = { ...contextTrack };
        onlyCtx.title = canonicalTrackTitle(onlyCtx);
        mergedTracks.push(onlyCtx);
      }
    }

    // Add any local tracks that weren't in context (shouldn't happen often, but handle it)
    for (const localTrack of localTracks) {
      const key = getTrackKey(localTrack);
      if (key && !processedKeys.has(key)) {
        const orphan = { ...localTrack };
        orphan.title = canonicalTrackTitle(orphan);
        mergedTracks.push(orphan);
      }
    }

    return mergedTracks;
  }

  /**
   * Apply last release defaults to a form
   */
  private applyLastReleaseDefaults(
    form: any,
    lastReleaseDetails: any | null,
    lastManualReleaseDetails: any | null,
    keepNamesEmpty: boolean = false,
    preferManual: boolean = false
  ): any {
    const ref = preferManual
      ? (lastManualReleaseDetails || null)
      : (lastReleaseDetails || lastManualReleaseDetails || null);

    const releaseRefSafe =
      !!ref &&
      !!form?.release_id &&
      !!ref.release_id &&
      String(ref.release_id) === String(form.release_id);
    const safeRef = releaseRefSafe ? ref : null;

    form.group_type = form.group_type || safeRef?.group_type || 'movie';
    form.mode = form.mode || (form.group_type === 'series' ? 'series' : 'movie');

    if (!keepNamesEmpty) {
      const isDefaultSlug = (val: string | null | undefined) => !!val && /^disc-\d+$/i.test(val);
      const releaseNameCandidate = form.release_name;
      const releaseSlugCandidate = form.release_slug;
      form.disc_group = form.disc_group || '';
      form.release_name = releaseNameCandidate && !isDefaultSlug(releaseNameCandidate)
        ? releaseNameCandidate
        : (form.release_name || safeRef?.release_name || '');
      form.release_slug = releaseSlugCandidate && !isDefaultSlug(releaseSlugCandidate)
        ? releaseSlugCandidate
        : '';
    }

    form.tmdb_id = form.tmdb_id || safeRef?.tmdb_id || '';
    form.upc = form.upc || safeRef?.upc || null;
    form.asin = form.asin || safeRef?.asin || null;
    form.cover_front_url = form.cover_front_url || safeRef?.cover_front_url || null;
    form.cover_back_url = form.cover_back_url || safeRef?.cover_back_url || null;
    form.release_year = form.release_year || safeRef?.release_year || null;
    form.production_year = form.production_year || safeRef?.production_year || null;
    form.disc_number = form.disc_number ?? null;
    form.release_id = form.release_id || safeRef?.release_id || null;

    return form;
  }

  /**
   * Compute label completion progress from WorkflowContext
   */
  computeLabelProgress(context: WorkflowContext | null, tmdbUrl: string = ''): { filled: number; total: number; releaseFilled: number; releaseTotal: number; discFilled: number; discTotal: number; titleFilled: number; titleTotal: number } {
    const labelForm = context?.labelForm || null;
    if (!labelForm) {
      return { filled: 0, total: 0, releaseFilled: 0, releaseTotal: 0, discFilled: 0, discTotal: 0, titleFilled: 0, titleTotal: 0 };
    }
    
    const f = labelForm;
    const isFilled = (v: any) => v !== null && v !== undefined && `${v}`.trim().length > 0;
    
    // Movie field (required) - movie_id or tmdb_id/tmdbUrl
    const movieFilled = !!(f.movie_id || (tmdbUrl || f.tmdb_id));
    const movieTotal = 1;
    
    // Release/Boxset field (required) - release_id or boxset_id
    const releaseBoxsetFilled = !!(f.release_id || f.boxset_id);
    const releaseBoxsetTotal = 1;
    
    // Disc fields (required: disc_name and disc_format)
    const discFields = [
      f.disc_name,
      f.disc_format,
    ];
    
    const discTotal = discFields.length;
    const discFilled = discFields.filter(isFilled).length;
    
    // Titles - count non-ignored titles
    const titlesRaw = Array.isArray(context?.titles) && context.titles.length > 0 
      ? context.titles 
      : (Array.isArray(f.tracks) ? f.tracks : []);
    
    const entities = buildTitleLabelEntities(titlesRaw);

    let titleFilled = 0;
    let titleTotal = 0;
    for (const entity of entities) {
      const t =
        entity.kind === 'group' ? getPrimaryTitleForEntity(entity.titles) : entity.title;
      if (!t) continue;
      const rawType = (t?.type ?? '').toString().toLowerCase();
      const ignored = rawType === 'ignore' || (!rawType && t?.content === false);
      if (ignored || isTitleIgnoredForStats(t)) continue;

      const type = (t?.type ?? '').toString().toLowerCase();
      const titleLike = canonicalTrackTitle(t) || (t?.description ?? t?.note ?? null);

      const hasValidType = type && type !== 'ignore';

      titleTotal += 1;
      if (hasValidType && isFilled(titleLike)) titleFilled += 1;

      if (type === 'episode') {
        titleTotal += 1;
        if (isFilled(t.season)) titleFilled += 1;
        titleTotal += 1;
        if (isFilled(t.episode)) titleFilled += 1;
      }
    }
    
    // Total required fields: movie (1) + release/boxset (1) + disc (2) + titles
    const releaseTotal = releaseBoxsetTotal; // Just release_id or boxset_id
    const releaseFilled = releaseBoxsetFilled ? 1 : 0;
    const total = movieTotal + releaseTotal + discTotal + titleTotal;
    const filled = (movieFilled ? 1 : 0) + releaseFilled + discFilled + titleFilled;
    
    return { filled, total, releaseFilled, releaseTotal, discFilled, discTotal, titleFilled, titleTotal };
  }
}

