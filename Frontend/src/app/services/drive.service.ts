// src/app/services/drive.service.ts
import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject, Observable, firstValueFrom } from 'rxjs';
import { environment } from '../environments/environment';
import { LoggerService } from './logger.service';

export interface Drive {
  disc_num: string;
  mount_point: string;
  /** Combined label for UI (hardware — path — friendly) */
  name?: string;
  makemkv_disc_index?: string;
  drive_hardware_name?: string;
  friendly_label?: string;
}
export interface TitleInfo {
  type?: string;
  season?: string;
  episode?: string;
  format?: string | null;
  source_file?: string | null;
  track_id?: string | null;
  title_id?: string | null;
  file?: string | null;
  /** Canonical display name (same as legacy episode_name from API). */
  title?: string;
  episode_name?: string;
  duration?: number | null;
  size?: number | null;
  display_size?: string | null;
  meta_size?: number | null;
  meta?: { size?: number | string } | null;
  /** File size in bytes after ripping */
  mkv_size?: number | null;
  /** 0–1, higher = more likely padding/junk */
  detection_confidence?: number | null;
  /** True if flagged as suspicious */
  detection_warning?: boolean | null;
  /** Human-readable detection messages */
  detection_warnings?: string[] | null;
  /** FFmpeg padding detection raw flags (for icon/metrics display) */
  detection_flags?: {
    bitrate_mbps?: number;
    is_suspicious_bitrate?: boolean;
    black_frame_duration?: number | null;
    silence_duration?: number | null;
    freeze_detected?: boolean;
    freeze_duration?: number | null;
    signal_entropy?: number | null;
  } | null;
  /** FFprobe metadata summary for "what's inside?" hover (quality/subtitle/audio tiers + hints) */
  metadata_summary?: {
    quality_tier?: string;
    quality_hints?: string[];
    subtitle_tier?: string;
    subtitle_hints?: string[];
    audio_tier?: string;
    audio_hints?: string[];
  } | null;
}
/** TMDB candidate as returned by POST /movies/tmdb-search (#387) and embedded
 *  in disc.disc_info.tmdb_suggestion.candidates (#388). */
export interface TmdbCandidateInfo {
  tmdb_id: string;
  tmdb_type: 'movie' | 'tv';
  title: string;
  year: number | null;
  cover_url: string | null;
  score: number;
}

/** Auto-identification result persisted on disc.disc_info.tmdb_suggestion (#388),
 *  surfaced on the film step (#389) so the user starts with a pre-filled best
 *  guess instead of a blank form. */
export interface TmdbSuggestionInfo extends TmdbCandidateInfo {
  normalized_query: string;
  hints: Record<string, unknown>;
  candidates: TmdbCandidateInfo[];
}

export interface DiscDetail {
  disc_num: string;  // MakeMKV disc number (for drive selection)
  mount_point: string;  // Device mount point (for drive selection)
  makemkv_disc_name?: string;  // MakeMKV disc name from DRV line (e.g., "HARRY_POTTER_SORCERER") - for drive selection, read-only
  disc_name?: string;  // Release metadata disc name (e.g., "Disc 01") - for labeling, user-editable
  disc_number?: number;  // Release metadata disc number (1, 2, 3...) - for labeling
  disc_slug?: string;  // Release metadata disc slug - for labeling, user-editable
  disc_format?: string | null;  // Release metadata disc format (Blu-Ray, UHD, DVD) - for labeling
  movie_name?: string;  // Movie name (from movie.name), replaces legacy show_title
  release_image?: string;  // Release cover image (from DiscDB release.imageUrl), replaces legacy show_image
  discdb_hit?: boolean;
  discdb_miss?: boolean;
  label_required?: boolean;
  label_ready?: boolean;
  resolution?: string | null;
  release_year?: string | number | null;
  release_date?: string | null;
  year?: string | number | null;
  disc_group?: string | null;
  group_type?: string | null;
  title_type?: string | null;
  titles?: Record<string, TitleInfo>;
  disc_hash?: string | null;
  disc_id?: string | null;
  release_id?: string | null;
  pending?: boolean;
  /** TMDB auto-suggestion derived from disc.info_title at scan time (#388).
   *  Null when no key is configured, devmode disabled it, or TMDB returned
   *  no results. The film step (#389) uses this to render the suggestion
   *  card with "Use this" / "Change" affordances. */
  tmdb_suggestion?: TmdbSuggestionInfo | null;
}
export type DriveScanState = 'idle' | 'scanning' | 'ready' | 'error';

const PREF_KEY = 'preferred-drive';

@Injectable({ providedIn: 'root' })
export class DriveService {
  private readonly apiBase = environment.apiBase ?? 'http://localhost:8000';
  // REMOVED: SSE connection - disc metadata now comes from Workflow Coordinator WebSocket
  // DriveService is now primarily for drive management (selection, rescan) rather than real-time updates

  private _drives = new BehaviorSubject<Drive[] | null>(null);
  drives$ = this._drives.asObservable();

  private _selected = new BehaviorSubject<Drive | null>(this.loadPreference());
  selected$ = this._selected.asObservable();

  private _discInfo = new BehaviorSubject<DiscDetail | null>(null);
  discInfo$ = this._discInfo.asObservable();

  private _error = new BehaviorSubject<string | null>(null);
  error$ = this._error.asObservable();

  private _driveScanState = new BehaviorSubject<DriveScanState>('idle');
  driveScanState$ = this._driveScanState.asObservable();
  private discInfoCache = new Map<string, DiscDetail>();
  private discErrorCache = new Map<string, string>();
  // REMOVED: eventSource - SSE connection removed

  constructor(private http: HttpClient, private logger: LoggerService) {
    // REMOVED: listenEvents() call - disc metadata now comes from Workflow Coordinator
    // Drive list and disc info should be populated from coordinator's discs$ observable
  }

  private enterScanningState(): void {
    this._driveScanState.next('scanning');
    this._discInfo.next(null);
    this._error.next(null);
    this.discInfoCache.clear();
    this.discErrorCache.clear();
  }

  /** Compare two drive arrays to determine if they're equal */
  private areDrivesEqual(a: Drive[] | null, b: Drive[] | null): boolean {
    if (a === b) return true;
    if (!a || !b) return false;
    if (a.length !== b.length) return false;
    
    // Compare each drive by disc_num, mount_point, and name
    return a.every((driveA, index) => {
      const driveB = b[index];
      return driveA.disc_num === driveB.disc_num &&
             driveA.mount_point === driveB.mount_point &&
             driveA.name === driveB.name &&
             driveA.drive_hardware_name === driveB.drive_hardware_name &&
             driveA.friendly_label === driveB.friendly_label;
    });
  }

  /** Check if discinfo has enriched fields (release_resolution, movie_id, etc.) */
  private isDiscInfoEnriched(info: DiscDetail | null): boolean {
    if (!info) return false;
    // Wait until we have disc_hash (indicates backend processing completed)
    const hasDiscHash = !!(info.disc_hash || (info as any)?.content_hash);
    if (!hasDiscHash) return false;
    
    // Check if it's a DiscDB hit
    const isDiscDbHit = !!(info as any)?.discdb_hit;
    const hasReleaseId = !!(info as any)?.release_id;
    
    // For DiscDB hits, wait for enriched fields (release_resolution or movie_id)
    if (isDiscDbHit) {
      const hasReleaseResolution = !!(info as any)?.release_resolution;
      const hasMovieId = !!(info as any)?.movie_id;
      return hasReleaseResolution || hasMovieId;
    }
    
    // For non-DiscDB hits with a release_id, wait for enriched fields too
    if (hasReleaseId) {
      const hasReleaseResolution = !!(info as any)?.release_resolution;
      const hasMovieId = !!(info as any)?.movie_id;
      const hasResolution = !!(info as any)?.resolution;
      return hasReleaseResolution || hasMovieId || hasResolution;
    }
    
    // For non-DiscDB hits without release_id, having disc_hash is enough
    return true;
  }

  /** Allow seeding disc info for resume */
  seedDiscInfo(info: DiscDetail) {
    if (!info) return;
    this.discInfoCache.set(info.disc_num, info);
    this._discInfo.next(info);
    this._driveScanState.next('ready');
    this._error.next(null);
  }

  /** Current disc info snapshot */
  currentDiscInfo(): DiscDetail | null {
    return this._discInfo.value;
  }

  currentSelected(): Drive | null {
    return this._selected.value;
  }

  getDrives(): Drive[] | null {
    return this._drives.value;
  }

  /** Ensure a drive entry exists (used when reattaching to an active job). */
  upsertDrive(drive: Drive): void {
    const list = this._drives.value || [];
    const existingIdx = list.findIndex(d => d.disc_num === drive.disc_num);
    let next = list;
    if (existingIdx === -1) {
      next = [...list, drive];
    } else if (list[existingIdx].mount_point !== drive.mount_point || list[existingIdx].name !== drive.name) {
      next = [...list];
      next[existingIdx] = drive;
    }
    this._drives.next(next);
    this._driveScanState.next('ready');
    this._error.next(null);
  }

  selectDrive(drive: Drive): void {
    localStorage.setItem(PREF_KEY, JSON.stringify(drive));
    this._selected.next(drive);
    const cachedInfo = this.discInfoCache.get(drive.disc_num) || null;
    // Only emit cached discinfo if it's enriched to prevent showing card with incomplete data
    // If not enriched, wait for SSE event to provide enriched version
    if (cachedInfo && this.isDiscInfoEnriched(cachedInfo)) {
      this._discInfo.next(cachedInfo);
    } else {
      // Clear discinfo if cached version isn't enriched - SSE will provide enriched version
      this._discInfo.next(null);
    }
    const cachedError = this.discErrorCache.get(drive.disc_num) || null;
    // Suppress TheDiscDB errors - they're expected and handled with fallback
    if (cachedError && cachedError.includes('Unable to find disc data from TheDiscDB.')) {
      this._error.next(null);
    } else {
    this._error.next(cachedError);
    }
  }

  refreshDiscInfo(disc_num: string, mount_point: string, timeout: number = 60000): Promise<DiscDetail> {
    // Emit a pending placeholder; SSE discinfo event will replace it after rescan.
    const pending: DiscDetail = {
      disc_num,
      mount_point,
      movie_name: 'Loading…',
      release_image: '',
      resolution: null,
      title_type: 'Unknown',
      titles: {},
      disc_hash: null,
      pending: true,
    };
    this._discInfo.next(pending);
    this._driveScanState.next('scanning');
    this._error.next(null);
    
    // Create a timeout promise
    const timeoutPromise = new Promise<never>((_, reject) => {
      setTimeout(() => {
        reject(new Error(`Disc scan timeout after ${timeout}ms`));
      }, timeout);
    });
    
    return Promise.race([
      firstValueFrom(
        this.http.post<Drive[]>(`${this.apiBase}/events/drive/rescan?stream=0`, {
          device: mount_point,
        })
      ),
      timeoutPromise
    ])
      .then(list => {
        // Optionally reselect cached info after rescan; actual disc data will arrive via SSE shortly.
        this._drives.next(list);
        const cached = this.discInfoCache.get(disc_num);
        if (cached && this._selected.value?.disc_num === disc_num) {
          this._discInfo.next(cached);
        }
        this._driveScanState.next('ready');
        return cached || pending;
      })
      .catch(err => {
        this.logger.error('[DriveService] refresh disc info via rescan failed', err);
        const msg = this.normalizeDiscError(err?.message || 'Drive rescan failed');
        this._error.next(msg);
        this._driveScanState.next('error');
        throw err;
      });
  }

  // normalizeDiscError already defined above

  private loadPreference(): Drive | null {
    try {
      return JSON.parse(localStorage.getItem(PREF_KEY) ?? 'null');
    } catch {
      return null;
    }
  }

  // REMOVED: listenEvents() method - SSE connection removed
  // Disc metadata now comes from Workflow Coordinator WebSocket (WorkflowCoordinatorService)
  // DriveService is now primarily for drive management (selection, rescan) and caching

  private normalizeDiscError(msg: string, type?: string): string {
    const text = (msg || '').toLowerCase();
    if (type === 'discdb_not_found' || text.includes('discdb') || text.includes('no entry found')) {
      return 'Unable to find disc data from TheDiscDB.';
    }
    return msg || 'Failed to load disc info';
  }

  private buildFallbackDiscInfo(disc_num: string, mount_point: string): DiscDetail {
    return {
      disc_num,
      mount_point,
      movie_name: 'Unknown Disc',
      release_image: '',
      resolution: null,
      title_type: 'Unknown',
      titles: {},
      disc_hash: null,
      pending: false,
      discdb_miss: true,
    };
  }

  async rescanDrives(): Promise<Drive[]> {
    this.enterScanningState();
    try {
      const list = await firstValueFrom(
        this.http.post<Drive[]>(`${this.apiBase}/events/drive/rescan`, {})
      );
      this._drives.next(list);
      // auto-select single drive
      if (list.length === 1 && !this._selected.value) {
        this.selectDrive(list[0]);
      }
      // If the currently selected drive still exists, refresh its disc info after rescan.
      const sel = this._selected.value;
      if (sel) {
        this.discInfoCache.delete(sel.disc_num);
        this.discErrorCache.delete(sel.disc_num);
        const stillThere = list.find(d => d.disc_num === sel.disc_num);
        if (stillThere) {
          await this.refreshDiscInfo(stillThere.disc_num, stillThere.mount_point);
        } else {
          this._selected.next(null);
          this._discInfo.next(null);
        }
      }
      this._driveScanState.next('ready');
      this._error.next(null);
      return list;
    } catch (err: any) {
      this._driveScanState.next('error');
      this._error.next('Drive rescan failed');
      throw err;
    }
  }
}
