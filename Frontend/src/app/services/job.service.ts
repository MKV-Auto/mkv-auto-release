// src/app/services/job.service.ts
import { Injectable }        from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { canonicalTrackTitle } from '../utils/canonical-track-title.util';
import { Observable, tap, timeout, TimeoutError } from 'rxjs';
import { environment }       from '../environments/environment';
import { LoggerService } from './logger.service';
import { ToastService } from './toast.service';

/** True when the error is a 400 indicating no active transfer config (user should create one via setup modal). */
export function isNoActiveTransferConfigError(err: any): boolean {
  if (err?.status !== 400) return false;
  const detail = err?.error?.detail;
  if (typeof detail !== 'string') return false;
  return detail.toLowerCase().includes('no active transfer configuration');
}

/** True when the error is a 400 indicating transfer config or path (path not set, path invalid, etc.). */
export function isTransferConfigOrPathError(err: any): boolean {
  if (err?.status !== 400) return false;
  const detail = err?.error?.detail;
  if (typeof detail !== 'string') return false;
  const s = detail.toLowerCase();
  return (
    s.includes('destination path') ||
    s.includes('transfer path') ||
    s.includes('transfer directory not configured') ||
    s.includes('cannot create destination path') ||
    s.includes('no write permission') ||
    s.includes('no active transfer configuration')
  );
}

export interface CurrentJobResponse {
  jobId:       string;
  createdAt:   string;
  disc:        any;
  job_status:  string;
  rip_progress:    number;
}

export interface JobStatus {
  jobId:           string;
  disc_id?: string | null;
  release_id?: string | null;
  movie_name?: string | null;
  boxset_id?: string | null;
  release_year?: number | null;  // Boxset/release year (e.g., 2017)
  production_year?: number | null;  // Movie production year (e.g., 2001, 2002, 2004, 2005)
  resolution?: string | null;
  job_status:          string;
  rip_progress:        number;
  /** "copy" | "verification" | null during rip */
  rip_phase?: string | null;
  post_progress:       number;
  logs:            string[];
  stage_profile?: 'hit' | 'miss' | null;
  discdb_result?: 'hit' | 'miss' | 'error' | 'unknown' | null;
  pipeline?: Record<string, string> | null;
  phase?: string | null;
  rip_state?: string | null;
  rip_started_at?: string | null;   // ISO UTC when rip began (#26/#344)
  rip_completed_at?: string | null;  // ISO UTC when rip completed (#26/#344)
  label_state?: string | null;
  finalize_state?: string | null;
  post_state?: string | null;
  transfer_state?: string | null;
  /** Stage-admission queue marker (#863): non-null while the job waits for a heavy-stage slot. */
  dispatch_queued_at?: string | null;
  finalize_release_state?: string | null;
  artifacts?: Record<string, any> | null;
  disc_hash?: string | null;
  disc_group?: string | null;
  group_type?: string | null;
  completedTitles?: string[] | null;
  perTitleStatus?: Record<string, string> | null;
  disc_payload?: any;
  label_draft?: any;
  job_dir?: string | null;
  ripped_files?: Record<string, string> | null;  // title_id -> relative_path (files in raw/ after rip)
  post_paths?: Record<string, string> | null;     // title_id -> relative_path (files in transient/ after post-processing)
  error_reason?: string | null;
  transfer_paths?: string[] | null;
  transfer_error?: string | null;
  transfer_progress?: number | null;
  transfer_verification_hash?: string | null;
  transfer_verification_status?: string | null;
  transfer_retry_count?: number | null;
  transfer_max_retries?: number | null;
  transfer_speed_mbps?: number | null;
  transfer_bytes_transferred?: number | null;
  transfer_total_bytes?: number | null;
  transfer_conflict_resolution?: string | null;
  transfer_source_cleaned?: boolean | null;
  transfer_validation_status?: string | null;
  transfer_validation_error?: string | null;
  transfer_deduplicated?: boolean | null;
  titlesCompleted?: number;
  totalTitles?: number;
  currentTitleProgress?: number;
  currentTitleId?: string | null;
  currentTitleNumber?: number | null;
  perTitleProgress?: Record<string, number>;
  label_required?: boolean | null;
  label_ready?: boolean | null;
  dev_mode?: boolean | null;
  dev_validation?: Record<string, any> | null;
  export_path?: string | null;
  /** Set on POST responses that advance step (rip, label/complete, postprocess, transfer, workflow/step/complete) */
  workflow_step?: string | null;
  /** POST /jobs/rip only: true when a new job was created and task dispatched, false when returning an existing job */
  job_created?: boolean | null;
  preview?: {
    status: 'queued' | 'running' | 'completed' | 'failed';
    titles: Record<string, { status: 'queued' | 'running' | 'completed' | 'failed'; manifest?: string; error?: string | null }>;
    queue_position?: number | null;
    updated_at?: string | null;
  } | null;
  /** Path A — segment-reorder state machine. Populated only on jobs running
   * the selective-rip workflow; null on every other job. */
  segment_reorder_state?: {
    stage?: 'exploratory_ripping' | 'awaiting_segment_order' | 'matching_playlists'
          | 'canonical_ripping_pending' | 'cancelled' | 'previews_failed' | string;
    exploratory_title_index?: number | null;
    group_member_indexes?: number[];
    sorted_segment_key?: string;
    submitted_order?: string[] | null;
    matched_playlist_index?: number | null;
    previews_manifest?: Array<{
      index: number;
      path: string;
      cum_start_s: number;
      mode: 'full' | 'stitch';
      src_dur_s: number;
      clip_name?: string;
      head_s?: number;
      tail_s?: number;
    }>;
    error?: string;
  } | null;
  /** Per-title rip set used by the selective-rip path. Null on default
   * all-mode rips. UI uses len(rip_set) to render "title K of N". */
  rip_set?: number[] | null;
}

export interface DiscJobState {
  disc: any;
  job?: JobStatus | null;
}

export interface TitleLabel {
  title_id: string;
  title?: string | null;
  description?: string | null;
  source_file?: string | null;
  season?: number | null;
  episode?: number | null;
  type?: 'episode' | 'movie' | 'extra' | 'trailer' | 'deleted' | null;
  note?: string | null;
  duration?: number | null;
  size?: number | null;
  streams?: Record<string, any> | any[] | null;
}

export interface LabelRequest {
  mode: 'movie' | 'series';
  movie_id?: string | null;
  tmdb_id?: string | null;  // Deprecated, use movie_id
  disc_format: 'Blu-Ray' | 'UHD' | 'DVD';
  disc_number?: number | null;
  release_slug?: string | null;
  release_name?: string | null;
  upc?: string | null;
  asin?: string | null;
  cover_front_url?: string | null;
  cover_back_url?: string | null;
  disc_name?: string | null;
  disc_slug?: string | null;
  titles: TitleLabel[];
}

export interface PostProcessFile {
  name: string;           // comment (MakeMKV export name)
  path: string;           // final_path (full path in transient/)
  sourceFile?: string;    // source_file (from titles, optional)
  status: 'pending' | 'processing' | 'completed';
  progress?: number | null;
  titleId?: string | null;
  titleName?: string | null;
  // New fields for folder structure
  relativePath?: string;  // Path relative to transient/ (e.g., "Movies/Movie Name (2023)/...")
  folderPath?: string;    // Folder path (e.g., "Movies/Movie Name (2023)")
  fileName?: string;      // Just the filename (e.g., "Movie Name (2023) - Disc 1.mkv")
  isIgnored?: boolean;    // Whether the matching title is ignored
}

export interface PostProcessStatus {
  pending: PostProcessFile[];
  inProgress: PostProcessFile[];
  completed: PostProcessFile[];
  isLoading: boolean;
}

// HistoryService interfaces (merged)
/** A subsequence-superset matcher candidate (Path B iteration). */
export interface SupersetCandidate {
  title_index: number;
  source_file: string | null;
  /** Clips in the mpls that are NOT in the user's submitted order. */
  extras_clips: string[];
  /** Absolute positions of `extras_clips` in the mpls's segment_map. */
  extras_positions: number[];
  /** Disc-titles.size (bytes). null when the disc cache didn't carry it. */
  mpls_total_size_b: number | null;
  /** Sorted-segment-set key — used by the picker to group candidates. */
  sorted_set_key: string;
}

export interface SubmitSegmentOrderResponse {
  matched: boolean;
  title_index?: number;
  rip_set_size?: number;
  sorted_set_match_count?: number;
  skipped_canonical_rip?: boolean;
  exact_count?: number;
  sorted_set_count?: number;
  candidates?: number[];
  subsequence_supersets?: SupersetCandidate[];
}

export interface ConfirmSegmentOrderResponse {
  confirmed: boolean;
  exact_count: number;
  sorted_set_count: number;
  subsequence_supersets: SupersetCandidate[];
}

export interface FlagDecoysResponse {
  eliminated_title_indexes: number[];
  newly_eliminated_count: number;
}

export interface SetSegmentFlagResponse {
  disc_id: string;
  flags: Record<string, 'potentially' | 'definitely'>;
}

export interface RemainingPlaylistSizeResponse {
  disc_id: string;
  remaining_size_b: number;
  total_size_b: number;
  ignored_count: number;
  total_count: number;
  free_disk_b: number | null;
  threshold_b: number;
  allows_rip_rest: boolean;
}

export interface RipSupersetResponse {
  dispatched: boolean;
  exploratory_title_index: number;
  rip_set_size: number;
  sibling_count: number;
}

export interface RipTheRestResponse {
  dispatched: boolean;
  rip_set_size: number;
  remaining_size_b: number;
  threshold_b: number;
}

export interface HistoryItem {
  jobId: string;
  disc_num: string;
  mount_point: string;
  disc_hash?: string | null;
  disc_group?: string | null;
  job_status: string;
  mode: string;
  rip_progress: number;
  post_progress: number;
  created_at: string;
  updated_at: string;
  job_dir?: string | null;
  movie_name?: string | null;
  error_reason?: string | null;
  transfer_state?: string | null;
  transfer_progress?: number | null;
  transfer_error?: string | null;
  dev_mode?: boolean | null;
  dev_validation?: Record<string, any> | null;
  export_path?: string | null;
}

@Injectable({ providedIn: 'root' })
export class JobService {
  private readonly apiUrl = environment.apiBase ?? 'http://localhost:8000';

  constructor(
    private http: HttpClient,
    private logger: LoggerService,
    private toast: ToastService
  ) {}
  private log(method: string, payload: any) {
    // Log summaries instead of full objects to reduce log size
    let summary: any;
    if (Array.isArray(payload)) {
      summary = { count: payload.length, firstItem: payload[0] ? { id: payload[0].jobId || payload[0].id } : null };
    } else if (payload && typeof payload === 'object') {
      // Log only key fields, not entire objects
      summary = {
        jobId: payload.jobId || payload.id,
        status: payload.job_status || payload.status,
        progress: payload.rip_progress,
        ...(payload.titles && { titlesCount: Array.isArray(payload.titles) ? payload.titles.length : 'N/A' })
      };
    } else {
      summary = payload;
    }
    this.logger.debug(`[JobService] ${method}`, summary);
  }

  /** Kick off a new rip job. Returns JobStatus with workflow_step for POST-driven state. */
  startRip(ripJob: any): Observable<JobStatus> {
    return this.http.post<JobStatus>(
      `${this.apiUrl}/jobs/rip`, ripJob
    ).pipe(
      tap(data => this.log('startRip', data)),
      tap({
        next: (data) => {
          // Only show "Rip job started" when backend actually created a new job; otherwise user might see success but no new job
          if (data.job_created === false) {
            this.toast.show('Rip already in progress for this disc', 'info');
          } else {
            this.toast.show('Rip job started', 'success');
          }
        },
        error: (err) => {
          // Log error but don't show generic toast - backend sends specific notification via WebSocket
          this.logger.error('[JobService] Failed to start rip job', err);
        }
      })
    );
  }

  /**
   * Path A — start a rip job that ripps one exploratory playlist via the
   * selective-rip path. Subsequent /jobs/{id}/segment-order resolves the
   * canonical playlist and updates rip_set for the final rip.
   */
  startRipWithSegmentReorder(payload: {
    mount_point: string;
    disc_id?: string;
    disc_num?: string;
    /** Optional. Backend auto-picks within the largest duplicate-segment-map
     * group (DiscDB > MakeMKV flag-clear > first member) when omitted. */
    exploratory_title_index?: number;
    output_dir?: string;
  }): Observable<JobStatus> {
    return this.http.post<JobStatus>(
      `${this.apiUrl}/jobs/rip-with-segment-reorder`, payload,
    ).pipe(tap(data => this.log('startRipWithSegmentReorder', data)));
  }

  /**
   * Path A — submit the user's segment ordering for matching. Returns a
   * shape the caller renders into the next workflow step:
   *   { matched: true, title_index, rip_set_size }
   *   { matched: false, candidates, subsequence_supersets }
   * subsequence_supersets is populated when no exact/sorted-set match is
   * found; the page gates display behind the confirmation modal so the
   * user re-affirms their order before seeing superset candidates.
   */
  submitSegmentOrder(jobId: string, order: string[]): Observable<SubmitSegmentOrderResponse> {
    return this.http.post<SubmitSegmentOrderResponse>(
      `${this.apiUrl}/jobs/${jobId}/segment-order`, { order },
    ).pipe(tap(data => this.log('submitSegmentOrder', data)));
  }

  /** Path B iteration — user re-affirms their order via the confirmation
   * gate. Backend marks `confirmed_segment_order`, records the iteration,
   * and returns matcher results including subsequence_supersets filtered
   * by the disc's per-clip flags. */
  confirmSegmentOrder(jobId: string, order: string[]): Observable<ConfirmSegmentOrderResponse> {
    return this.http.post<ConfirmSegmentOrderResponse>(
      `${this.apiUrl}/jobs/${jobId}/segment-order/confirm`, { order },
    ).pipe(tap(data => this.log('confirmSegmentOrder', data)));
  }

  /** Path B iteration — mark the exploratory mpls + every sibling sharing
   * its sorted-segment-set as `type='ignore'`. Used by the "previous order
   * had decoys" escape hatch and to terminate an unproductive iteration. */
  flagSegmentDecoys(jobId: string, exploratoryTitleIndex: number): Observable<FlagDecoysResponse> {
    return this.http.post<FlagDecoysResponse>(
      `${this.apiUrl}/jobs/${jobId}/segment-order/flag-decoys`,
      { exploratory_title_index: exploratoryTitleIndex },
    ).pipe(tap(data => this.log('flagSegmentDecoys', data)));
  }

  /** Read the per-disc clip-obfuscation-flag dictionary; used to hydrate
   * the segment-reorder page's per-tile flag UI on load. */
  getSegmentFlags(discId: string): Observable<SetSegmentFlagResponse> {
    return this.http.get<SetSegmentFlagResponse>(
      `${this.apiUrl}/discs/${discId}/segment-flags`,
    ).pipe(tap(data => this.log('getSegmentFlags', data)));
  }

  /** Disk-pressure snapshot driving the segment-reorder page's
   * eliminated-count + "Rip the rest" CTA. `allows_rip_rest=true`
   * means remaining-size fits under min(200 GB, free_disk * 0.9). */
  getRemainingPlaylistSize(discId: string): Observable<RemainingPlaylistSizeResponse> {
    return this.http.get<RemainingPlaylistSizeResponse>(
      `${this.apiUrl}/discs/${discId}/remaining-playlist-size`,
    ).pipe(tap(data => this.log('getRemainingPlaylistSize', data)));
  }

  /** Dispatch an exploratory rip on a subsequence-superset candidate
   * the user picked from the picker. Reuses the existing job — the
   * post-rip hook regenerates previews so the user re-enters the
   * ordering UI with the new candidate's segments. */
  ripSupersetCandidate(jobId: string, titleIndex: number): Observable<RipSupersetResponse> {
    return this.http.post<RipSupersetResponse>(
      `${this.apiUrl}/jobs/${jobId}/segment-order/rip-superset`,
      { title_index: titleIndex },
    ).pipe(tap(data => this.log('ripSupersetCandidate', data)));
  }

  /** Final escape hatch — rip every non-ignored, non-subsumed title.
   * Backend pre-flights remaining-size against the threshold and
   * returns 409 if it doesn't fit. */
  ripTheRest(jobId: string): Observable<RipTheRestResponse> {
    return this.http.post<RipTheRestResponse>(
      `${this.apiUrl}/jobs/${jobId}/rip-the-rest`, {},
    ).pipe(tap(data => this.log('ripTheRest', data)));
  }

  /** Set or clear a per-disc clip obfuscation flag. flag=null clears the
   * flag for that clip; `definitely` excludes mpls containing it from the
   * superset matcher; `potentially` rank-deprioritises. Persists across
   * jobs since the flag describes the physical disc. */
  setSegmentFlag(
    discId: string,
    clipId: string,
    flag: 'potentially' | 'definitely' | null,
  ): Observable<SetSegmentFlagResponse> {
    return this.http.patch<SetSegmentFlagResponse>(
      `${this.apiUrl}/discs/${discId}/segment-flags`,
      { clip_id: clipId, flag },
    ).pipe(tap(data => this.log('setSegmentFlag', data)));
  }

  /** Path A — bail out of the segment-reorder workflow. */
  cancelSegmentReorder(jobId: string): Observable<void> {
    return this.http.post<void>(
      `${this.apiUrl}/jobs/${jobId}/segment-reorder/cancel`, {},
    ).pipe(tap(() => this.log('cancelSegmentReorder', { jobId })));
  }

  /** If there's already a running job, resume it on page load */
  getCurrentJob(): Observable<CurrentJobResponse> {
    return this.http.get<CurrentJobResponse>(
      `${this.apiUrl}/jobs/current`
    ).pipe(tap(data => this.log('getCurrentJob', data)));
  }

  /** Combined disc + job snapshot for bootstrapping UI */
  getCurrentDiscState(): Observable<DiscJobState> {
    return this.http.get<DiscJobState>(`${this.apiUrl}/discs/current`).pipe(tap(data => this.log('getCurrentDiscState', data)));
  }

  // State management removed - active job state now comes from WorkflowService.activeContext$
  // titleJobProgress, updateJobStatus, clearJobState removed - use WorkflowService for active job state

  /** One-shot fetch of job status (used to verify a stored jobId) */
  getJobStatus(jobId: string): Observable<JobStatus> {
    return this.http.get<JobStatus>(`${this.apiUrl}/jobs/${jobId}/status`).pipe(tap(data => this.log('getJobStatus', data)));
  }

  /** Fetch status (stateless - no state management) */
  refreshJobStatus(jobId: string): Observable<JobStatus> {
    return this.getJobStatus(jobId);
  }

  /** Lookup the most recent job for a disc by id (preferred), hash, or drive_num (slot) */
  getJobByDisc(opts: { disc_id?: string | null; disc_hash?: string | null; drive_num?: string | null; disc_num?: string | null }): Observable<JobStatus> {
    const params = new URLSearchParams();
    if (opts.disc_id) params.set('disc_id', opts.disc_id);
    if (opts.disc_hash) params.set('disc_hash', opts.disc_hash);
    const drive = opts.drive_num || opts.disc_num;
    if (drive) params.set('drive_num', drive);
    return this.http.get<JobStatus>(`${this.apiUrl}/jobs/by-disc?${params.toString()}`).pipe(tap(data => this.log('getJobByDisc', data)));
  }

  prefillLabel(jobId: string): Observable<any> {
    return this.http
      .get(`${this.apiUrl}/jobs/${jobId}/label/prefill`)
      .pipe(tap(data => this.log('prefillLabel', { jobId, titles: (data as any)?.titles?.length })));
  }

  saveLabel(jobId: string, payload: LabelRequest): Observable<JobStatus> {
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/label`, payload);
  }

  updateLabel(jobId: string, payload: any): Observable<JobStatus> {
    return this.http.patch<JobStatus>(`${this.apiUrl}/jobs/${jobId}/label`, { data: payload });
  }

  /** Complete workflow step only (no stage changes). Returns JobStatus with workflow_step for POST-driven state. */
  completeWorkflowStep(jobId: string, toStep: 'boxset' | 'disc' | 'titles' | 'postprocess' | 'transfer'): Observable<JobStatus> {
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/workflow/step/complete`, { to_step: toStep }).pipe(
      timeout(30000),
      tap({
        error: (err) => {
          if (err instanceof TimeoutError) {
            this.logger.error('[JobService] completeWorkflowStep timed out');
            this.toast.show('Step advance timed out. Please try again.', 'error');
          } else {
            // WorkflowService.advanceStepTo may recover from 400 "Invalid step transition" (no user toast).
            this.logger.warn('[JobService] completeWorkflowStep HTTP error', err);
          }
        }
      })
    );
  }

  /** Complete label stage (titles -> postprocess). Optional labelForm to apply; else backend uses label_draft. Returns JobStatus with workflow_step. */
  completeLabel(jobId: string, labelForm?: Record<string, unknown>): Observable<JobStatus> {
    const body = labelForm != null ? { labelForm } : {};
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/label/complete`, body).pipe(
      tap({
        error: (err: any) => {
          this.logger.error('[JobService] Failed to complete label', err);
          const status = err?.status;
          const detail = err?.error?.detail;
          const jobIdFromDetail = typeof detail === 'object' && detail?.job_id;
          const detailStr =
            typeof detail === 'string'
              ? detail
              : detail != null && typeof detail === 'object' && 'message' in detail
                ? String((detail as { message?: unknown }).message ?? '')
                : '';
          const msg =
            status === 404 && jobIdFromDetail
              ? `Job not found (${String(jobIdFromDetail).slice(0, 8)}…). The job may have been removed or you may be connected to a different server.`
              : detailStr.trim() || 'Failed to complete label';
          this.toast.show(msg, 'error');
        }
      })
    );
  }

  finalizeLabel(jobId: string): Observable<JobStatus> {
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/label/finalize`, {}).pipe(
      tap({
        next: () => this.toast.show('Label finalized', 'success'),
        error: (err) => {
          this.logger.error('[JobService] Failed to finalize label', err);
          this.toast.show('Failed to finalize label', 'error');
        }
      })
    );
  }

  getPreviewStatus(jobId: string): Observable<any> {
    return this.http.get(`${this.apiUrl}/jobs/${jobId}/previews/status`);
  }

  getPreviewQueue(): Observable<{ items: any[] }> {
    return this.http.get<{ items: any[] }>(`${this.apiUrl}/jobs/previews/queue`);
  }

  listenPreviewQueue(): EventSource {
    return new EventSource(`${this.apiUrl}/events/previews/queue`);
  }

  /** Fetch artifacts (paths) for a job */
  getJobArtifacts(jobId: string): Observable<{ jobId: string; job_dir?: string | null; ripped_files?: Record<string, string> | null; post_paths?: Record<string, string> | null }> {
    return this.http.get<{ jobId: string; job_dir?: string | null; ripped_files?: Record<string, string> | null; post_paths?: Record<string, string> | null }>(
      `${this.apiUrl}/jobs/${jobId}/artifacts`
    );
  }

  /** Transfer job files */
  transferJob(jobId: string, req?: any): Observable<JobStatus> {
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/transfer`, req || {}).pipe(
      timeout(60000),
      tap({
        next: () => this.toast.show('Transfer started', 'success'),
        error: (err) => {
          if (err instanceof TimeoutError) {
            this.logger.error('[JobService] transferJob timed out');
            this.toast.show('Transfer request timed out. Please try again.', 'error');
          } else {
            this.logger.error('[JobService] Failed to start transfer', err);
            const message = isTransferConfigOrPathError(err)
              ? (err?.error?.detail || 'Set transfer destination in Settings > Transfer Configs.')
              : 'Failed to start transfer';
            this.toast.show(message, 'error');
          }
        }
      })
    );
  }

  /** Retry a failed transfer */
  retryTransfer(jobId: string): Observable<JobStatus> {
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/transfer/retry`, {});
  }

  /** Verify transfer hash */
  verifyTransfer(jobId: string): Observable<JobStatus> {
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/transfer/verify`, {});
  }

  /** Cleanup source files after transfer */
  cleanupSource(jobId: string): Observable<JobStatus> {
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/transfer/cleanup`, {});
  }

  /** Validate transfer preconditions */
  validateTransfer(jobId: string): Observable<{ success: boolean; message: string; errors?: string[] | null }> {
    return this.http.post<{ success: boolean; message: string; errors?: string[] | null }>(`${this.apiUrl}/jobs/${jobId}/transfer/validate`, {});
  }

  // clearJobState removed - no state to clear (stateless service)

  /** Fetch list of jobs (consolidated from listJobs and getHistory) */
  listJobs(limit: number = 50): Observable<HistoryItem[]> {
    return this.http.get<HistoryItem[]>(`${this.apiUrl}/jobs?limit=${limit}`).pipe(tap(data => this.log('listJobs', data)));
  }

  /** Start post-processing for a job. Returns JobStatus with workflow_step for POST-driven state. */
  startPostProcess(jobId: string): Observable<JobStatus> {
    return this.http.post<JobStatus>(`${this.apiUrl}/jobs/${jobId}/postprocess`, {}).pipe(
      timeout(30000),
      tap(data => this.log('startPostProcess', data)),
      tap({
        next: () => this.toast.show('Post-processing started', 'success'),
        error: (err) => {
          if (err instanceof TimeoutError) {
            this.logger.error('[JobService] startPostProcess timed out');
            this.toast.show('Start post-processing timed out. Please try again.', 'error');
          } else {
            this.logger.error('[JobService] Failed to start post-processing', err);
            this.toast.show('Failed to start post-processing', 'error');
          }
        }
      })
    );
  }

  /** Resume a job */
  resumeJob(jobId: string): Observable<HistoryItem> {
    return this.http.post<HistoryItem>(`${this.apiUrl}/jobs/${jobId}/resume`, {}).pipe(
      timeout(30000),
      tap(data => this.log('resumeJob', data)),
      tap({
        next: () => this.toast.show('Job resumed', 'success'),
        error: (err) => {
          if (err instanceof TimeoutError) {
            this.logger.error('[JobService] resumeJob timed out');
            this.toast.show('Resume timed out. Please try again.', 'error');
          } else {
            this.logger.error('[JobService] Failed to resume job', err);
            this.toast.show('Failed to resume job', 'error');
          }
        }
      })
    );
  }

  /** Revert transfer for a job */
  revertTransfer(jobId: string): Observable<HistoryItem> {
    return this.http.post<HistoryItem>(`${this.apiUrl}/jobs/${jobId}/revert-transfer`, {}).pipe(
      tap(data => this.log('revertTransfer', data)),
      tap({
        next: () => this.toast.show('Transfer reverted', 'success'),
        error: (err) => {
          this.logger.error('[JobService] Failed to revert transfer', err);
          this.toast.show('Failed to revert transfer', 'error');
        }
      })
    );
  }

  /** Reset post-processing for a job */
  resetPostprocess(jobId: string, clearFiles: boolean = false, backupFiles: boolean = true): Observable<HistoryItem> {
    return this.http.post<HistoryItem>(`${this.apiUrl}/jobs/${jobId}/reset-postprocess?clear_files=${clearFiles}&backup_files=${backupFiles}`, {}).pipe(
      tap(data => this.log('resetPostprocess', data)),
      tap({
        next: () => this.toast.show('Post-processing reset', 'success'),
        error: (err) => {
          this.logger.error('[JobService] Failed to reset post-processing', err);
          this.toast.show('Failed to reset post-processing', 'error');
        }
      })
    );
  }

  /** Restore post-processing for a job */
  restorePostprocess(jobId: string): Observable<HistoryItem> {
    return this.http.post<HistoryItem>(`${this.apiUrl}/jobs/${jobId}/restore-postprocess`, {}).pipe(
      tap(data => this.log('restorePostprocess', data)),
      tap({
        next: () => this.toast.show('Post-processing restored', 'success'),
        error: (err) => {
          this.logger.error('[JobService] Failed to restore post-processing', err);
          this.toast.show('Failed to restore post-processing', 'error');
        }
      })
    );
  }

  /** Regenerate previews for a job */
  regeneratePreviews(jobId: string): Observable<{ status: string }> {
    return this.http.post<{ status: string }>(`${this.apiUrl}/jobs/${jobId}/previews/regenerate`, {}).pipe(tap(data => this.log('regeneratePreviews', data)));
  }

  /** Retry preview for a specific track */
  retryPreviewTrack(jobId: string, trackKey: string): Observable<{ status: string; track_key: string }> {
    return this.http.post<{ status: string; track_key: string }>(`${this.apiUrl}/jobs/${jobId}/previews/regenerate/${encodeURIComponent(trackKey)}`, {}).pipe(tap(data => this.log('retryPreviewTrack', data)));
  }

  /** Batch regenerate all failed previews for a job */
  regenerateAllPreviews(jobId: string): Observable<{ regenerated: number; tracks: string[] }> {
    return this.http.post<{ regenerated: number; tracks: string[] }>(`${this.apiUrl}/jobs/${jobId}/previews/regenerate-failed`, {}).pipe(tap(data => this.log('regenerateAllPreviews', data)));
  }

  /**
   * Queue ffprobe metadata + padding/junk detection for raw titles (detect_raw_titles worker).
   * Use force=true for a full rescan of all titles that have a raw file; when force is false,
   * missing_only (default true) limits to failed scan or missing detection_flags.
   */
  regenerateJobDetection(
    jobId: string,
    options?: { missingOnly?: boolean; force?: boolean }
  ): Observable<{ status: string; titles?: string[]; count?: number }> {
    let params = new HttpParams();
    const missingOnly = options?.missingOnly !== false;
    params = params.set('missing_only', String(missingOnly));
    params = params.set('force', String(options?.force === true));
    return this.http
      .post<{ status: string; titles?: string[]; count?: number }>(
        `${this.apiUrl}/jobs/${jobId}/detection/regenerate`,
        {},
        { params }
      )
      .pipe(tap((data) => this.log('regenerateJobDetection', data)));
  }

  /** Delete previews for a job */
  deletePreviews(jobId: string): Observable<{ status: string }> {
    return this.http.delete<{ status: string }>(`${this.apiUrl}/jobs/${jobId}/previews`).pipe(tap(data => this.log('deletePreviews', data)));
  }

  /** Extract post-processing files from job status */
  getPostProcessFiles(jobStatus: JobStatus | null): PostProcessFile[] {
    if (!jobStatus) return [];
    
    // Use post_paths (title_id -> relative_path) instead of final_paths
    const postPaths = jobStatus.post_paths || {};
    const perTitleStatus = jobStatus.perTitleStatus || {};
    const artifacts = jobStatus.artifacts || {};
    
    return Object.entries(postPaths).map(([titleId, path]) => {
      let status: 'pending' | 'processing' | 'completed' = 'pending';
      const titleStatus = perTitleStatus[titleId];
      
      if (titleStatus === 'completed' || titleStatus === 'done') {
        status = 'completed';
      } else if (titleStatus === 'running' || titleStatus === 'processing' || titleStatus === 'active') {
        status = 'processing';
      } else if (postPaths[titleId]) {
        // If in post_paths but no status, assume completed
        status = 'completed';
      }
      
      // Extract folder structure from path
      const fullPath = path as string;
      // Remove transient/ prefix if present
      const relativePath = fullPath.replace(/^.*?transient[\/\\]/, '').replace(/^[\/\\]+/, '');
      // Extract folder path and filename
      const pathParts = relativePath.split(/[\/\\]/);
      const fileName = pathParts[pathParts.length - 1] || '';
      const folderPath = pathParts.length > 1 ? pathParts.slice(0, -1).join('/') : '';
      
      return {
        name: titleId,  // Use title_id as the key/name
        path: fullPath,
        relativePath,
        folderPath,
        fileName,
        status,
        progress: artifacts[titleId]?.progress || null,
      };
    });
  }

  /** Get categorized post-processing status with title matching */
  getPostProcessStatus(
    jobStatus: JobStatus | null, 
    titles: any[] = []
  ): PostProcessStatus {
    const files = this.getPostProcessFiles(jobStatus);
    
    // Match files to titles (titles can be TitleLabel, TitleInfo, or TitleEntry)
    // file.name is now title_id (UUID), so match by title_id first
    const filesWithTitles = files.map(file => {
      const matchingTitle = titles.find((t: any) => 
        t.title_id === file.name ||  // Primary match: title_id (file.name is now title_id)
        t.key === file.name ||
        t.track_id === file.name ||
        t.source_file === file.path ||
        t.file === file.path ||
        t.comment === file.name
      );
      
      // Get display name from title (handle various property names)
      let titleName: string | null = null;
      if (matchingTitle) {
        titleName =
          (matchingTitle as any).title_name ||
          canonicalTrackTitle(matchingTitle) ||
          null;
      }
      
      // Check if title is ignored (case-insensitive)
      const isIgnored = matchingTitle ? (matchingTitle.type || '').toString().toLowerCase() === 'ignore' : false;
      
      return {
        ...file,
        titleId: matchingTitle?.title_id || null,
        titleName,
        sourceFile: matchingTitle?.source_file || null,
        isIgnored,
      };
    });
    
    // Filter out ignored titles before categorizing
    const nonIgnoredFiles = filesWithTitles.filter(f => !f.isIgnored);
    
    // Categorize files by status
    const pending = nonIgnoredFiles.filter(f => f.status === 'pending');
    const inProgress = nonIgnoredFiles.filter(f => f.status === 'processing');
    const completed = nonIgnoredFiles.filter(f => f.status === 'completed');
    
    // Determine loading state (have a job but no files yet)
    const isLoading = jobStatus !== null && 
      files.length === 0 &&
      (jobStatus.post_state === 'running' || 
       jobStatus.post_state === 'processing' ||
       jobStatus.post_state === 'pending');
    
    return { pending, inProgress, completed, isLoading };
  }
}
