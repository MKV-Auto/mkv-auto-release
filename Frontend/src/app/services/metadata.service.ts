import { Injectable } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { BehaviorSubject, Observable, of, map, shareReplay, tap, filter, take, catchError } from 'rxjs';
import { environment } from '../environments/environment';
import { LabelForm } from '../pages/ripper/services/label-form.service';
import { LoggerService } from './logger.service';

export interface MovieSummary {
  id: string;
  name: string;
  production_year?: number | null;
  tmdb_id?: string | null;
  tmdb_type?: string | null;
  cover_url?: string | null;
  cover_path?: string | null;
}

export interface BoxsetSummary {
  id: string;
  slug: string;
  name: string;
  title?: string | null;
  year?: number | null;
  release_count?: number | null;
  disc_count?: number | null;
  cover_front_url?: string | null;
  cover_back_url?: string | null;
  asin?: string | null;
  upc?: string | null;
  boxset_link_ready?: boolean | null;
  boxset_missing_required_fields?: string[] | null;
  modified?: boolean | null;
}

// Re-export interfaces from consolidated services
export interface MovieRecord {
  id: string;
  name: string;
  production_year?: number | null;
  tmdb_id?: string | null;
  tmdb_type?: string | null;
  cover_url?: string | null;
  cover_path?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface MovieCreate {
  name: string;
  production_year?: number | null;
  tmdb_id?: string | null;
  tmdb_type?: string | null;
  cover_url?: string | null;
}

/** Single result from POST /movies/tmdb-search (#387). */
export interface TmdbSearchCandidate {
  tmdb_id: string;
  tmdb_type: 'movie' | 'tv';
  title: string;
  year: number | null;
  cover_url: string | null;
  score: number;
}

/** Response shape from POST /movies/tmdb-search (#387). */
export interface TmdbSearchResponse {
  candidates: TmdbSearchCandidate[];
  normalized_query: string;
  hints: Record<string, unknown>;
}

/** Persisted TMDB auto-suggestion stored on disc.disc_info.tmdb_suggestion (#388).
 *  Surfaces on the film step (#389) so the user can confirm or override
 *  the best-guess match without manually pasting a TMDB URL. */
export interface TmdbSuggestion {
  tmdb_id: string;
  tmdb_type: 'movie' | 'tv';
  title: string;
  year: number | null;
  cover_url: string | null;
  score: number;
  normalized_query: string;
  hints: Record<string, unknown>;
  candidates: TmdbSearchCandidate[];
}

export interface ReleaseSummary {
  id?: string | null;
  slug: string;
  type?: string | null;
  name?: string | null;
  title?: string | null;
  release_name?: string | null;
  movie_id?: string | null;
  movie?: MovieSummary | null;
  tmdb_id?: string | null;
  upc?: string | null;
  asin?: string | null;
  cover_front_url?: string | null;
  cover_back_url?: string | null;
  finalize_state?: string | null;
  total_discs: number;
  completed_discs: number;
  finalized_discs: number;
  resolution?: string | null;
  release_year?: number | null;
  production_year?: number | null;
  discdb_hit?: boolean | null;
  boxset_id?: string | null;
  boxset_slug?: string | null;
  release_link_ready?: boolean | null;
  release_missing_required_fields?: string[] | null;
  modified?: boolean | null;
}

export interface ReleaseRecord {
  id: string;
  slug: string;
  type?: string | null;
  name?: string | null;
  movie_id?: string | null;
  movie?: MovieRecord | null;
  tmdb_id?: string | null;
  upc?: string | null;
  asin?: string | null;
  cover_front_url?: string | null;
  cover_back_url?: string | null;
  title_cover_url?: string | null;
  info_title?: string | null;
  release_year?: number | null;
  production_year?: number | null;
  finalized: boolean;
  finalized_at?: string | null;
  discs: DiscRecord[];
  boxset_id?: string | null;
  boxset_slug?: string | null;
}

/**
 * Per-title row carried on `DiscSummary.titles` after #380 shipped.
 * Mirrors `api.schemas.TitleSummary` on the backend. Includes
 * `file_path` + `file_path_stage` so the Library drawer (#500) can
 * answer "where did the bytes land?" without a per-disc round-trip.
 */
export interface TitleSummary {
  title_id: string;
  title?: string | null;
  type?: string | null;
  season?: number | null;
  episode?: number | null;
  edition?: string | null;
  description?: string | null;
  duration?: number | null;
  size?: number | null;
  mkv_size?: number | null;
  file_path?: string | null;
  file_path_stage?: 'rip' | 'postprocess' | 'transfer' | null;
  title_seq: number;
  active?: boolean | null;
  /**
   * #500 Phase 5 — reserved slot for v2 DiscDB auto-stream. When the
   * user is opted in, each label that hits the title row also streams
   * upstream to TheDiscDB; this flag turns true when the upstream
   * acknowledged. Library shows a passive "Contributed to DiscDB" chip
   * when truthy. Always undefined / false in v1; backend doesn't yet
   * project a column for it. Do not surface verify or export UI here —
   * superseded by the auto-stream plan.
   */
  contributed_to_discdb?: boolean | null;
}

export interface DiscSummary {
  id?: string | null;
  content_hash: string;
  release_id?: string | null;
  release_slug?: string | null;
  disc_number?: number | null;
  disc_slug?: string | null;
  disc_name?: string | null;
  format?: string | null;
  label_present: boolean;
  finalized: boolean;
  finalized_at?: string | null;
  latest_job_id?: string | null;
  latest_job_status?: string | null;
  latest_job_updated_at?: string | null;
  latest_job_progress?: number | null;
  transfer_state?: string | null;
  discdb_hit?: boolean | null;
  titles_completed?: number | null;
  total_titles?: number | null;
  per_title_progress?: Record<string, number> | null;
  /** Typed after #380; was `any[]`. Mirrors api.schemas.TitleSummary.
   * #530: absent on the Library page response — use `title_count`. */
  titles?: TitleSummary[] | null;
  /** #530: persisted-title count (Library page ships this instead of `titles`). */
  title_count?: number | null;
  /** Job / scan payload title list (library), not DB title_streams */
  tracks?: any[] | null;
  /** Persisted rows from title_streams (e.g. getDiscById / by-hash) */
  title_streams?: any[] | null;
}

export interface DiscRecord {
  id: string;
  content_hash: string;
  release_id?: string | null;
  disc_number?: number | null;
  disc_slug?: string | null;
  disc_name?: string | null;
  format?: string | null;
  info_title?: string | null;
  finalized: boolean;
  finalized_at?: string | null;
  artifacts?: Record<string, any> | null;
  finalize_result?: Record<string, any> | null;
  titles?: any[] | null;
  title_streams?: any[] | null;
}

export interface BoxsetRecord extends BoxsetSummary {
  finalize_result?: any;
  releases?: any[];
  created_at?: string | null;
  updated_at?: string | null;
}

/** Pre-structured payload for the Library (History) page from GET /releases/library */
export interface LibraryResponse {
  releases: ReleaseSummary[];
  release_discs: Record<string, DiscSummary[]>;
  boxsets: BoxsetSummary[];
  boxset_details: BoxsetRecord[];
}

/** Paginated library response for infinite scroll */
export interface LibraryPageResponse {
  items: ReleaseSummary[];
  release_discs: Record<string, DiscSummary[]>;
  boxsets: BoxsetSummary[];
  boxset_details: BoxsetRecord[];
  next_cursor: string | null;
  has_more: boolean;
  total_count?: number | null;
}

export interface BoxsetCreate {
  name: string;
  title?: string;
  sort_title?: string;
  year: number;
  upc?: string;
  asin?: string;
  locale?: string;
  region_code?: string;
  cover_front_url?: string;
  cover_back_url?: string;
  release_date?: string;
}

export interface BoxsetUpdate {
  name?: string;
  title?: string;
  sort_title?: string;
  year?: number;
  upc?: string;
  asin?: string;
  locale?: string;
  region_code?: string;
  cover_front_url?: string;
  cover_back_url?: string;
  release_date?: string;
}

/** Disc with full JobStatus for workflow postprocess/transfer (one-call release/boxset data). */
export interface DiscWithJobStatus {
  disc_id: string;
  disc_number?: number | null;
  disc_name?: string | null;
  disc_format?: string | null;
  job_status?: Record<string, unknown> | null;
}

/** Release metadata plus all discs with full JobStatus (one-call workflow data). */
export interface ReleaseFullResponse {
  id: string;
  slug: string;
  name?: string | null;
  movie_name?: string | null;
  production_year?: number | null;
  release_name?: string | null;
  release_slug?: string | null;
  cover_url?: string | null;
  discs: DiscWithJobStatus[];
}

/** Boxset metadata plus all discs with full JobStatus (one-call workflow data). */
export interface BoxsetFullResponse {
  id: string;
  slug: string;
  name?: string | null;
  year?: number | null;
  cover_url?: string | null;
  discs: DiscWithJobStatus[];
}

export interface GroupOption {
  release_id?: string | null;
  disc_group: string;
  group_type: string;
  release_name: string | null;
  release_slug: string;
  movie_id?: string | null;
  movie?: any;
  resolution?: string | null;
  tmdb_id?: string | null;
  upc?: string | null;
  asin?: string | null;
  cover_front_url?: string | null;
  cover_back_url?: string | null;
  release_year?: number | null;
  production_year?: number | null;
}

/** Bundled workflow options returned by GET /discs/options */
export interface WorkflowOptions {
  movieOptions: MovieSummary[];
  boxsetOptions: BoxsetSummary[];
  releaseOptions: ReleaseSummary[];
  groupOptions: GroupOption[];
}

@Injectable({ providedIn: 'root' })
export class MetadataService {
  private readonly apiUrl = environment.apiBase ?? 'http://localhost:8000';

  private _movieOptions$ = new BehaviorSubject<MovieSummary[]>([]);
  private _boxsetOptions$ = new BehaviorSubject<BoxsetSummary[]>([]);
  private _releaseOptions$ = new BehaviorSubject<ReleaseSummary[]>([]);
  private _groupOptions$ = new BehaviorSubject<GroupOption[]>([]);

  /** Whether workflow options have been loaded at least once */
  private _workflowOptionsLoaded = false;
  /** In-flight options request (shared to avoid duplicate requests) */
  private _workflowOptionsRequest$: Observable<WorkflowOptions> | null = null;

  constructor(
    private http: HttpClient,
    private logger: LoggerService
  ) {
    // Load workflow options from the dedicated cached endpoint (replaces
    // separate loadMovieOptions + loadBoxsetOptions on startup)
    this.loadWorkflowOptions().subscribe();
  }

  // ===== Workflow Options (cached, loaded from GET /discs/options) =====

  /**
   * Load all workflow options from the dedicated cached endpoint.
   * Returns cached data immediately if already loaded, otherwise fetches.
   * Deduplicates concurrent requests.
   */
  loadWorkflowOptions(): Observable<WorkflowOptions> {
    if (this._workflowOptionsLoaded) {
      return of(this.getCachedOptions());
    }
    // Deduplicate: if a request is already in flight, return it
    if (this._workflowOptionsRequest$) {
      return this._workflowOptionsRequest$;
    }
    this._workflowOptionsRequest$ = this.http.get<WorkflowOptions>(`${this.apiUrl}/discs/options`).pipe(
      tap(options => {
        this._applyWorkflowOptions(options);
        this._workflowOptionsLoaded = true;
        this._workflowOptionsRequest$ = null;
      }),
      catchError(err => {
        this.logger.error('[MetadataService] Failed to load workflow options:', err);
        this._workflowOptionsRequest$ = null;
        // Fall back to loading individual endpoints
        this.loadMovieOptions();
        this.loadBoxsetOptions();
        return of(this.getCachedOptions());
      }),
      shareReplay(1)
    );
    return this._workflowOptionsRequest$;
  }

  /**
   * Force refresh workflow options (e.g. after creating a movie/boxset/release,
   * or when receiving an options_changed WebSocket event).
   */
  refreshWorkflowOptions(): void {
    this._workflowOptionsLoaded = false;
    this._workflowOptionsRequest$ = null;
    this.loadWorkflowOptions().subscribe();
  }

  /**
   * Get the currently cached workflow options synchronously.
   * Returns whatever is in the BehaviorSubjects right now.
   */
  getCachedOptions(): WorkflowOptions {
    return {
      movieOptions: this._movieOptions$.value,
      boxsetOptions: this._boxsetOptions$.value,
      releaseOptions: this._releaseOptions$.value,
      groupOptions: this._groupOptions$.value,
    };
  }

  /**
   * Check if workflow options have been loaded at least once.
   */
  get workflowOptionsLoaded(): boolean {
    return this._workflowOptionsLoaded;
  }

  private _applyWorkflowOptions(options: WorkflowOptions): void {
    if (options.movieOptions) {
      this._movieOptions$.next(options.movieOptions);
    }
    if (options.boxsetOptions) {
      this._boxsetOptions$.next(options.boxsetOptions);
    }
    if (options.releaseOptions) {
      this._releaseOptions$.next(options.releaseOptions);
    }
    if (options.groupOptions) {
      this._groupOptions$.next(options.groupOptions);
    }
  }

  // Movie options
  getMovieOptions(): BehaviorSubject<MovieSummary[]> {
    return this._movieOptions$;
  }

  loadMovieOptions(): void {
    this.http.get<MovieSummary[]>(`${this.apiUrl}/movies`)
      .subscribe({
        next: (movies) => {
          this._movieOptions$.next(movies || []);
        },
        error: (err) => {
          this.logger.error('Failed to load movie options:', err);
          this._movieOptions$.next([]);
        }
      });
  }

  // Boxset options
  getBoxsetOptions(): BehaviorSubject<BoxsetSummary[]> {
    return this._boxsetOptions$;
  }

  loadBoxsetOptions(): void {
    this.http.get<BoxsetSummary[]>(`${this.apiUrl}/releases/boxsets`)
      .subscribe({
        next: (boxsets) => {
          this._boxsetOptions$.next(boxsets || []);
        },
        error: (err) => {
          this.logger.error('Failed to load boxset options:', err);
          this._boxsetOptions$.next([]);
        }
      });
  }

  // Release options (for a specific movie)
  getReleaseOptions(movieId: string): Observable<ReleaseSummary[]> {
    return this.http.get<ReleaseSummary[]>(`${this.apiUrl}/releases?movie_id=${encodeURIComponent(movieId)}`);
  }

  // Group options
  getGroupOptions(): BehaviorSubject<GroupOption[]> {
    return this._groupOptions$;
  }

  loadGroupOptions(movieId?: string): Observable<GroupOption[]> {
    return new Observable(observer => {
      this.listReleases(movieId ? { movie_id: movieId } : undefined).subscribe({
        next: (rels: ReleaseSummary[]) => {
          const mapped = (rels || [])
            .filter(r => r.discdb_hit !== true) // only show manually created releases
            .map(r => ({
              release_id: (r as any)?.id,
              disc_group: r.slug,
              group_type: r.type || 'movie',
              release_name: r.name || r.title || null,
              release_slug: r.slug,
              movie_id: r.movie_id,
              movie: r.movie,
              resolution: (r as any)?.resolution || null,
              tmdb_id: r.tmdb_id,
              upc: r.upc,
              asin: r.asin,
              cover_front_url: r.cover_front_url,
              cover_back_url: r.cover_back_url,
              release_year: r.release_year,
              production_year: r.production_year,
            }));

          this._groupOptions$.next(mapped);
          observer.next(mapped);
          observer.complete();
        },
        error: (err) => {
          this.logger.error('[MetadataService] Failed to load group options:', err);
          observer.error(err);
        },
      });
    });
  }

  /**
   * Filter group options by search term (synchronous, uses current options)
   */
  filterGroupOptions(searchTerm: string): GroupOption[] {
    const options = this._groupOptions$.value;

    if (!searchTerm) {
      return options;
    }

    const search = searchTerm.toLowerCase();
    return options.filter(g =>
      (g.release_name || '').toLowerCase().includes(search) ||
      (g.disc_group || '').toLowerCase().includes(search) ||
      (g.release_slug || '').toLowerCase().includes(search)
    );
  }

  // Search/filter methods
  /**
   * Search movies by name using the backend search endpoint.
   * Returns ≤20 matches for search-as-you-type combobox (≥3 chars).
   * Falls back to in-memory filtering for short queries.
   */
  searchMoviesBackend(query: string, limit: number = 20): Observable<MovieSummary[]> {
    if (!query || query.length < 3) {
      return of([]);
    }
    return this.http.get<MovieSummary[]>(
      `${this.apiUrl}/movies/search`,
      { params: { q: query, limit: limit.toString() } }
    );
  }

  /**
   * Search boxsets by name using the backend search endpoint.
   * Returns ≤20 matches for search-as-you-type combobox (≥3 chars).
   */
  searchBoxsetsBackend(query: string, limit: number = 20): Observable<BoxsetSummary[]> {
    if (!query || query.length < 3) {
      return of([]);
    }
    return this.http.get<BoxsetSummary[]>(
      `${this.apiUrl}/releases/boxsets/search`,
      { params: { q: query, limit: limit.toString() } }
    );
  }

  /**
   * Search releases by name using the backend search endpoint.
   * Returns ≤20 matches for search-as-you-type combobox.
   * Can filter by movie_id without requiring a search term.
   */
  searchReleasesBackend(query: string, movieId?: string, limit: number = 20): Observable<ReleaseSummary[]> {
    if ((!query || query.length < 3) && !movieId) {
      return of([]);
    }
    const params: any = { limit: limit.toString() };
    if (query && query.length >= 3) params.q = query;
    if (movieId) params.movie_id = movieId;
    return this.http.get<ReleaseSummary[]>(
      `${this.apiUrl}/releases/search`,
      { params }
    );
  }

  searchMovies(query: string): Observable<MovieSummary[]> {
    return this._movieOptions$.pipe(
      map(movies => {
        if (!query) return movies;
        const lowerQuery = query.toLowerCase();
        return movies.filter(m => 
          m.name.toLowerCase().includes(lowerQuery) ||
          (m.production_year && m.production_year.toString().includes(lowerQuery))
        );
      }),
      shareReplay(1)
    );
  }

  searchBoxsets(query: string): Observable<BoxsetSummary[]> {
    return this._boxsetOptions$.pipe(
      map(boxsets => {
        if (!query) return boxsets;
        const lowerQuery = query.toLowerCase();
        return boxsets.filter(b => 
          (b.name?.toLowerCase().includes(lowerQuery)) ||
          (b.title?.toLowerCase().includes(lowerQuery)) ||
          (b.year && b.year.toString().includes(lowerQuery))
        );
      }),
      shareReplay(1)
    );
  }

  // Refresh methods
  refreshMovieOptions(): void {
    this.loadMovieOptions();
  }

  refreshBoxsetOptions(): void {
    this.loadBoxsetOptions();
  }
  
  // ===== Movie CRUD Methods (from MovieService) =====
  
  getMovies(): Observable<MovieSummary[]> {
    return this.http.get<MovieSummary[]>(`${this.apiUrl}/movies`);
  }

  /**
   * Filter movies by search term (synchronous, uses current options)
   */
  filterMovies(searchTerm: string, limit: number = 50): MovieSummary[] {
    const options = this._movieOptions$.value;
    
    if (!searchTerm) {
      return options.slice(0, limit);
    }

    const search = searchTerm.toLowerCase();
    return options
      .filter(f =>
        f.name.toLowerCase().includes(search) ||
        (f.production_year && String(f.production_year).includes(search)) ||
        (f.tmdb_id && f.tmdb_id.includes(search))
      )
      .slice(0, limit);
  }

  getMovie(movieId: string): Observable<MovieRecord> {
    return this.http.get<MovieRecord>(`${this.apiUrl}/movies/${movieId}`);
  }

  createMovie(movieData: MovieCreate): Observable<MovieRecord> {
    return this.http.post<MovieRecord>(`${this.apiUrl}/movies`, movieData);
  }

  updateMovie(movieId: string, movieData: Partial<MovieCreate>): Observable<MovieRecord> {
    return this.http.patch<MovieRecord>(`${this.apiUrl}/movies/${movieId}`, movieData);
  }

  lookupMovie(tmdbUrl: string): Observable<MovieCreate> {
    return this.http.post<MovieCreate>(`${this.apiUrl}/movies/lookup`, { tmdb_url: tmdbUrl });
  }

  /** Fuzzy search TMDB by free-text query (#387). Used by the film-step
   *  override path when the auto-suggestion (#388) is wrong. Returns a 503
   *  with detail.code = "tmdb_unavailable" if the key is missing or the
   *  devmode toggle is on — UI should hide the search affordance in that
   *  case rather than offering a feature that will always fail. */
  searchTmdb(
    query: string,
    options: { year_hint?: number | null; media_type?: 'movie' | 'tv' | null; limit?: number } = {}
  ): Observable<TmdbSearchResponse> {
    const body: Record<string, unknown> = { query };
    if (options.year_hint != null) body['year_hint'] = options.year_hint;
    if (options.media_type) body['media_type'] = options.media_type;
    if (options.limit != null) body['limit'] = options.limit;
    return this.http.post<TmdbSearchResponse>(`${this.apiUrl}/movies/tmdb-search`, body);
  }

  downloadCover(movieId: string, jobId?: string): Observable<{ cover_path: string; cover_url: string }> {
    const options: { params?: HttpParams } = {};
    if (jobId) {
      options.params = new HttpParams().set('job_id', jobId);
    }
    return this.http.post<{ cover_path: string; cover_url: string }>(
      `${this.apiUrl}/movies/${movieId}/download-cover`,
      null,
      options
    );
  }
  
  // ===== Boxset CRUD Methods (from BoxsetService) =====
  
  listBoxsets(finalized?: boolean): Observable<BoxsetSummary[]> {
    const params: any = {};
    if (finalized !== undefined) {
      params.finalized = finalized;
    }
    return this.http.get<BoxsetSummary[]>(`${this.apiUrl}/releases/boxsets`, { params });
  }

  getBoxset(boxsetId: string): Observable<BoxsetRecord> {
    return this.http.get<BoxsetRecord>(`${this.apiUrl}/releases/boxsets/${encodeURIComponent(boxsetId)}`);
  }

  createBoxset(payload: BoxsetCreate): Observable<BoxsetSummary> {
    return this.http.post<BoxsetSummary>(`${this.apiUrl}/releases/boxsets`, payload);
  }

  updateBoxset(boxsetId: string, payload: BoxsetUpdate): Observable<BoxsetSummary> {
    return this.http.patch<BoxsetSummary>(`${this.apiUrl}/releases/boxsets/${encodeURIComponent(boxsetId)}`, payload);
  }

  /**
   * Filter boxsets by search term (synchronous, uses current options)
   */
  filterBoxsets(searchTerm: string): BoxsetSummary[] {
    const options = this._boxsetOptions$.value;

    if (!searchTerm) {
      return options;
    }

    const search = searchTerm.toLowerCase();
    return options.filter(b =>
      (b.name || '').toLowerCase().includes(search) ||
      (b.slug || '').toLowerCase().includes(search) ||
      (b.title || '').toLowerCase().includes(search)
    );
  }

  /**
   * Filter releases by search term (synchronous, uses provided releases or current options)
   */
  filterReleases(searchTerm: string, params?: { movie_id?: string }): ReleaseSummary[] {
    // For now, we'll need to load releases if not already loaded
    // In the future, we might want to cache releases similar to movies/boxsets
    // For now, return empty array and let the component handle filtering
    // This method signature allows for future caching
    return [];
  }

  /**
   * Get boxset by ID from current options
   */
  getBoxsetById(id: string): BoxsetSummary | null {
    const options = this._boxsetOptions$.value;
    return options.find(b => b.id === id) || null;
  }

  addReleaseToBoxset(boxsetId: string, releaseId: string): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/releases/boxsets/${encodeURIComponent(boxsetId)}/releases/${encodeURIComponent(releaseId)}`,
      {}
    );
  }

  removeReleaseFromBoxset(boxsetId: string, releaseId: string): Observable<any> {
    return this.http.delete<any>(
      `${this.apiUrl}/releases/boxsets/${encodeURIComponent(boxsetId)}/releases/${encodeURIComponent(releaseId)}`
    );
  }

  finalizeBoxset(boxsetId: string): Observable<any> {
    return this.http.post<any>(`${this.apiUrl}/releases/boxsets/${encodeURIComponent(boxsetId)}/finalize`, {});
  }

  exportBoxset(boxsetId: string, format: string = 'zip'): Observable<Blob> {
    return this.http.get(`${this.apiUrl}/releases/boxsets/${encodeURIComponent(boxsetId)}/export`, {
      params: { format },
      responseType: 'blob'
    });
  }

  deleteBoxset(boxsetId: string): Observable<any> {
    return this.http.delete<any>(`${this.apiUrl}/releases/boxsets/${encodeURIComponent(boxsetId)}`);
  }
  
  // ===== Release CRUD Methods (from ReleaseService) =====
  
  listReleases(params?: { movie_id?: string }): Observable<ReleaseSummary[]> {
    let url = `${this.apiUrl}/releases`;
    if (params?.movie_id) {
      url += `?movie_id=${encodeURIComponent(params.movie_id)}`;
    }
    // Removed excessive logging: listReleases is called frequently and doesn't need to be logged
    return this.http.get<ReleaseSummary[]>(url);
  }

  /**
   * One-shot load for the Library (History) page: releases with discs, boxsets with details.
   * Use this instead of listReleases + N×listDiscs + listBoxsets + M×getBoxset for faster load.
   */
  getLibrary(): Observable<LibraryResponse> {
    return this.http.get<LibraryResponse>(`${this.apiUrl}/releases/library`);
  }

  /**
   * Paginated library for infinite scroll.
   * Returns releases with discs for the current page. Use cursor for next page.
   */
  getLibraryPage(cursor: string | null, limit: number = 20, search?: string, tab?: string): Observable<LibraryPageResponse> {
    const params: any = { limit: limit.toString() };
    if (cursor) params.cursor = cursor;
    if (search) params.search = search;
    if (tab && tab !== 'all') params.tab = tab;
    return this.http.get<LibraryPageResponse>(`${this.apiUrl}/releases/library/page`, { params });
  }

  getRelease(id: string): Observable<ReleaseSummary> {
    return this.http.get<ReleaseSummary>(`${this.apiUrl}/releases/${encodeURIComponent(id)}`).pipe(tap((data: ReleaseSummary) => this.log('getRelease', data)));
  }

  getReleaseRecord(idOrSlug: string): Observable<ReleaseRecord> {
    return this.http.get<ReleaseRecord>(`${this.apiUrl}/releases/${encodeURIComponent(idOrSlug)}/record`);
  }

  /** Get full release data (metadata + all discs with full JobStatus) in one call for workflow steps. */
  getReleaseFull(releaseId: string): Observable<ReleaseFullResponse> {
    return this.http.get<ReleaseFullResponse>(`${this.apiUrl}/releases/${encodeURIComponent(releaseId)}/full`).pipe(
      tap((data: ReleaseFullResponse) => this.log('getReleaseFull', data))
    );
  }

  /** Get full boxset data (metadata + all discs with full JobStatus) in one call for workflow steps. */
  getBoxsetFull(boxsetId: string): Observable<BoxsetFullResponse> {
    return this.http.get<BoxsetFullResponse>(`${this.apiUrl}/releases/boxsets/${encodeURIComponent(boxsetId)}/full`).pipe(
      tap((data: BoxsetFullResponse) => this.log('getBoxsetFull', data))
    );
  }

  createRelease(payload: any): Observable<any> {
    return this.http.post(`${this.apiUrl}/releases`, payload);
  }

  updateRelease(idOrSlug: string, payload: any): Observable<ReleaseSummary> {
    return this.http.patch<ReleaseSummary>(`${this.apiUrl}/releases/${encodeURIComponent(idOrSlug)}`, payload);
  }

  finalizeRelease(idOrSlug: string): Observable<any> {
    return this.http.post(`${this.apiUrl}/releases/${encodeURIComponent(idOrSlug)}/finalize`, {});
  }

  deleteRelease(idOrSlug: string): Observable<any> {
    return this.http.delete(`${this.apiUrl}/releases/${encodeURIComponent(idOrSlug)}`);
  }

  exportRelease(slug: string, format: string = 'zip'): Observable<Blob> {
    return this.http.get(`${this.apiUrl}/releases/${encodeURIComponent(slug)}/export`, {
      params: { format },
      responseType: 'blob'
    });
  }

  findReleaseByMovieBoxset(movieId: string, boxsetId: string): Observable<ReleaseSummary | null> {
    return this.http.get<ReleaseSummary | null>(`${this.apiUrl}/releases/find-by-movie-boxset`, {
      params: { movie_id: movieId, boxset_id: boxsetId }
    });
  }
  
  // ===== Disc CRUD Methods (from DiscService + ReleaseService) =====
  
  listDiscs(releaseId: string): Observable<DiscSummary[]> {
    return this.http.get<DiscSummary[]>(`${this.apiUrl}/releases/${encodeURIComponent(releaseId)}/discs`);
  }

  getDiscById(discId: string): Observable<{ disc: DiscSummary; release: ReleaseSummary | null }> {
    const url = `${this.apiUrl}/releases/disc/by-hash?disc_id=${encodeURIComponent(discId)}`;
    return this.http.get<any>(url).pipe(tap(data => this.log('getDiscById', data)));
  }

  getDiscRecord(discId: string): Observable<DiscRecord> {
    return this.http.get<DiscRecord>(`${this.apiUrl}/releases/disc/${encodeURIComponent(discId)}`).pipe(tap(data => this.log('getDiscRecord', data)));
  }

  patchDiscRecord(discId: string, payload: Partial<DiscRecord>): Observable<DiscRecord> {
    return this.http.patch<DiscRecord>(`${this.apiUrl}/releases/disc/${encodeURIComponent(discId)}`, payload);
  }

  saveDiscLabel(discId: string, payload: any): Observable<any> {
    return this.http.post(`${this.apiUrl}/releases/disc/${encodeURIComponent(discId)}/label`, payload);
  }

  updateDiscMetadata(discId: string, payload: any): Observable<any> {
    return this.http.patch(`${this.apiUrl}/releases/disc/${encodeURIComponent(discId)}/metadata`, payload);
  }

  patchDiscOps(discId: string, ops: any[]): Observable<any> {
    return this.http.patch(`${this.apiUrl}/releases/disc/${encodeURIComponent(discId)}/ops`, { ops });
  }

  finalizeDisc(discId: string): Observable<any> {
    return this.http.post(`${this.apiUrl}/releases/disc/${encodeURIComponent(discId)}/finalize`, {});
  }

  revertDiscFinalization(discId: string): Observable<any> {
    return this.http.post<any>(`${this.apiUrl}/releases/disc/${discId}/revert-finalization`, {});
  }

  deleteDisc(discId: string): Observable<any> {
    return this.http.delete(`${this.apiUrl}/releases/disc/${encodeURIComponent(discId)}`);
  }
  
  // ===== Private List Update Methods =====
  
  /**
   * Upsert a movie into the cached options (add if new, update if existing).
   * Called from WebSocket metadata_updated events and after creation.
   */
  upsertMovie(movie: MovieSummary): void {
    if (!movie?.id) return;
    const current = this._movieOptions$.value;
    const idx = current.findIndex(m => m.id === movie.id);
    if (idx >= 0) {
      const updated = [...current];
      updated[idx] = { ...updated[idx], ...movie };
      this._movieOptions$.next(updated);
    } else {
      this._movieOptions$.next([...current, movie]);
    }
  }

  /**
   * Upsert a boxset into the cached options.
   */
  upsertBoxset(boxset: BoxsetSummary): void {
    if (!boxset?.id) return;
    const current = this._boxsetOptions$.value;
    const idx = current.findIndex(b => b.id === boxset.id);
    if (idx >= 0) {
      const updated = [...current];
      updated[idx] = { ...updated[idx], ...boxset };
      this._boxsetOptions$.next(updated);
    } else {
      this._boxsetOptions$.next([...current, boxset]);
    }
  }

  /**
   * Add a movie to the internal list (called automatically after creation)
   * @deprecated Use upsertMovie() instead
   */
  private addMovieToList(movie: MovieSummary): void {
    this.upsertMovie(movie);
  }

  /**
   * Add a boxset to the internal list (called automatically after creation)
   * @deprecated Use upsertBoxset() instead
   */
  private addBoxsetToList(boxset: BoxsetSummary): void {
    this.upsertBoxset(boxset);
  }

  /**
   * Add a release to the internal list (called automatically after creation)
   */
  private addReleaseToList(release: ReleaseSummary): void {
    if (!release?.id) return;
    const current = this._groupOptions$.value;
    if (current.some(g => g.release_id === release.id)) return;
    const groupOption = {
      release_id: (release as any)?.id,
      disc_group: release.slug,
      group_type: release.type || 'movie',
      release_name: release.name || release.title || null,
      release_slug: release.slug,
      movie_id: release.movie_id,
      movie: release.movie,
      resolution: (release as any)?.resolution || null,
      tmdb_id: release.tmdb_id,
      upc: release.upc,
      asin: release.asin,
      cover_front_url: release.cover_front_url,
      cover_back_url: release.cover_back_url,
      release_year: release.release_year,
      production_year: release.production_year,
    };
    this._groupOptions$.next([...current, groupOption]);
  }

  // ===== Disc-Based Creation Methods (WebSocket-Driven) =====
  
  /**
   * Create movie for disc and automatically link release to disc.
   * Updates movie list automatically. Backend emits workflow_context_updated via WebSocket.
   */
  createMovieForDisc(discId: string | null, mountPoint: string | null, movieData: MovieCreate): Observable<{ movie: MovieSummary }> {
    const url = discId 
      ? `${this.apiUrl}/discs/${discId}/movies`
      : `${this.apiUrl}/discs/workflow-context/movies?mount_point=${encodeURIComponent(mountPoint!)}`;
    
    return this.http.post<{ movie: MovieSummary }>(url, movieData).pipe(
      tap(result => {
        // Automatically update lists - no refresh needed
        this.addMovieToList(result.movie);
        // Note: Backend does not create a release when creating a movie for a disc
        // Release creation is postponed until user selects/creates a release
      })
    );
  }

  /**
   * Create release for disc and automatically link to disc.
   * Updates release list automatically. Backend emits workflow_context_updated via WebSocket.
   */
  createReleaseForDisc(discId: string | null, mountPoint: string | null, releaseData: any): Observable<{ release: ReleaseSummary; linked: boolean }> {
    const url = discId
      ? `${this.apiUrl}/discs/${discId}/releases`
      : `${this.apiUrl}/discs/workflow-context/releases?mount_point=${encodeURIComponent(mountPoint!)}`;
    
    return this.http.post<{ release: ReleaseSummary; linked: boolean }>(url, releaseData).pipe(
      tap(result => {
        this.addReleaseToList(result.release);
      })
    );
  }

  /**
   * Create boxset for disc, automatically create release for movie, and link both to disc.
   * Updates boxset and release lists automatically. Backend emits workflow_context_updated via WebSocket.
   */
  createBoxsetForDisc(discId: string | null, mountPoint: string | null, boxsetData: BoxsetCreate, movieId: string): Observable<{ boxset: BoxsetSummary; release: ReleaseSummary; linked: boolean }> {
    const url = discId
      ? `${this.apiUrl}/discs/${discId}/boxsets?movie_id=${encodeURIComponent(movieId)}`
      : `${this.apiUrl}/discs/workflow-context/boxsets?mount_point=${encodeURIComponent(mountPoint!)}&movie_id=${encodeURIComponent(movieId)}`;
    
    return this.http.post<{ boxset: BoxsetSummary; release: ReleaseSummary; linked: boolean }>(url, boxsetData).pipe(
      tap(result => {
        // Automatically update lists - no refresh needed
        this.addBoxsetToList(result.boxset);
        this.addReleaseToList(result.release);
      })
    );
  }

  // ===== Create+Link Methods =====
  
  /**
   * Create a movie and link it to workflow context
   */
  createAndLinkMovie(movieData: MovieCreate, contextId: string, contextType: 'job' | 'drive'): Observable<{ movie: MovieRecord; linked: boolean }> {
    return this.createMovie(movieData).pipe(
      map(movie => ({ movie, linked: true }))
    );
  }
  
  /**
   * Create a release and link it to workflow context
   */
  createAndLinkRelease(releaseData: any, contextId: string, contextType: 'job' | 'drive'): Observable<{ release: ReleaseSummary; linked: boolean }> {
    return this.createRelease(releaseData).pipe(
      map(release => ({ release, linked: true }))
    );
  }
  
  /**
   * Create a boxset and link it to workflow context
   */
  createAndLinkBoxset(boxsetData: BoxsetCreate, contextId: string, contextType: 'job' | 'drive'): Observable<{ boxset: BoxsetSummary; linked: boolean }> {
    return this.createBoxset(boxsetData).pipe(
      map(boxset => ({ boxset, linked: true }))
    );
  }
  
  // ===== Selection Helpers (Immutable) =====
  
  /**
   * Select a movie (returns new state, doesn't mutate)
   */
  selectMovie(movieId: string | null, currentState: any): any {
    return {
      ...currentState,
      movie_id: movieId,
      movie_name: movieId ? this._movieOptions$.value.find(m => m.id === movieId)?.name || null : null,
    };
  }
  
  /**
   * Select a release (returns new state, doesn't mutate)
   */
  selectRelease(releaseId: string | null, releaseSlug: string | null, currentState: any): any {
    return {
      ...currentState,
      release_id: releaseId,
      release_slug: releaseSlug,
      release_name: releaseSlug || releaseId || null,
    };
  }
  
  /**
   * Select a boxset (returns new state, doesn't mutate)
   */
  selectBoxset(boxsetId: string | null, boxsetSlug: string | null, currentState: any): any {
    return {
      ...currentState,
      boxset_id: boxsetId,
      boxset_slug: boxsetSlug,
    };
  }
  
  // ===== Label Form Helpers =====
  
  /**
   * Apply group selection to label form (immutable version)
   */
  applyGroupSelection(labelForm: LabelForm, group: GroupOption): LabelForm {
    if (!group || !labelForm) return labelForm;

    return {
      ...labelForm,
      group_type: (group.group_type as 'movie' | 'series') || labelForm.group_type,
      disc_group: group.disc_group || labelForm.disc_group,
      // Copy release_name from selected release if form field is empty (preserve user edits if they exist)
      release_name: labelForm.release_name || group.release_name || labelForm.release_name,
      release_slug: group.release_slug || labelForm.release_slug,
      release_id: group.release_id || labelForm.release_id,
      tmdb_id: group.tmdb_id || labelForm.tmdb_id,
      upc: group.upc || labelForm.upc,
      asin: group.asin || labelForm.asin,
      cover_front_url: group.cover_front_url || labelForm.cover_front_url,
      cover_back_url: group.cover_back_url || labelForm.cover_back_url,
      release_year: group.release_year || labelForm.release_year,
      production_year: group.production_year || labelForm.production_year,
    };
  }

  /**
   * Update current group option in the list
   */
  updateCurrentGroupOption(labelForm: LabelForm | null): void {
    if (!labelForm) return;

    const slug = labelForm.release_slug || labelForm.disc_group;
    const id = labelForm.release_id;

    // Only add/update once we have a stable identifier (id or slug) AND the required release fields
    if ((!slug && !id) || !this.hasRequiredReleaseFields(labelForm)) return;

    const currentOptions = this._groupOptions$.value;
    const idx = currentOptions.findIndex(
      g => (id && g.release_id === id) || (slug && g.disc_group === slug)
    );

    const entry: GroupOption = {
      release_id: id || null,
      disc_group: slug || '',
      group_type: labelForm.group_type || labelForm.mode || 'movie',
      release_name: labelForm.release_name || slug || '',
      release_slug: slug || '',
      tmdb_id: labelForm.tmdb_id || null,
      upc: labelForm.upc || null,
      asin: labelForm.asin || null,
      cover_front_url: labelForm.cover_front_url || null,
      cover_back_url: labelForm.cover_back_url || null,
      release_year: labelForm.release_year || null,
      production_year: labelForm.production_year || null,
    };

    if (idx >= 0) {
      currentOptions[idx] = entry;
    } else {
      currentOptions.unshift(entry);
    }

    this._groupOptions$.next([...currentOptions]);
  }

  /**
   * Check if label form has required release fields
   */
  private hasRequiredReleaseFields(labelForm: LabelForm): boolean {
    return !!(labelForm.release_name || labelForm.release_slug || labelForm.disc_group);
  }
  
  /**
   * Populate label form fields from boxset
   */
  populateFieldsFromBoxset(labelForm: LabelForm, boxset: BoxsetSummary): LabelForm {
    return {
      ...labelForm,
      boxset_id: boxset.id || null,
      boxset_slug: boxset.slug || null,
    };
  }
  
  private log(method: string, payload: any) {
    // Log summaries instead of full objects to reduce log size
    let summary: any;
    if (Array.isArray(payload)) {
      summary = { count: payload.length, firstItem: payload[0] ? { id: payload[0].id, slug: payload[0].slug } : null };
    } else if (payload && typeof payload === 'object') {
      // Log only key fields, not entire objects
      summary = {
        id: payload.id || payload.disc_id,
        slug: payload.slug,
        name: payload.name,
        ...(payload.disc && { discId: payload.disc.id }),
        ...(payload.release && { releaseId: payload.release.id })
      };
    } else {
      summary = payload;
    }
    this.logger.debug(`[MetadataService] ${method}`, summary);
  }
}