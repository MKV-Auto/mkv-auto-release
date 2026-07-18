// src/app/components/movie-selector/movie-selector.component.ts
import {
  Component,
  Input,
  Output,
  EventEmitter,
  OnInit,
  OnDestroy,
  OnChanges,
  SimpleChanges,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subject, takeUntil, debounceTime, switchMap, of } from 'rxjs';
import { MovieRecord, MovieSummary, MetadataService, TmdbSearchCandidate } from '../../services/metadata.service';
import { MobileService } from '../../services/mobile.service';
import { MobileDrawerComponent } from '../mobile-drawer/mobile-drawer.component';
import {
  EditionMetadataFormComponent,
  EditionFormValue,
} from '../edition-metadata-form/edition-metadata-form.component';
import { ToastService, formatHttpErrorDetail } from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';

@Component({
  selector: 'app-movie-selector',
  standalone: true,
  imports: [CommonModule, FormsModule, MobileDrawerComponent, EditionMetadataFormComponent],
  templateUrl: './movie-selector.component.html',
  styleUrls: ['./movie-selector.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MovieSelectorComponent implements OnInit, OnDestroy, OnChanges {
  @Input() movieOptions: MovieSummary[] = [];
  @Input() selectedMovieId: string | null = null;
  @Input() contentType: 'movie' | 'series' = 'movie';
  @Input() tmdbUrl: string = '';
  @Input() loading: boolean = false;
  @Input() error: string | null = null;
  /** Live TMDB search results from the parent (POST /movies/tmdb-search).
   *  Rendered inline in the empty-results state below a "From TMDB" header
   *  so the user can pick a candidate without leaving the combobox.
   *  Null = no search has been run yet; empty = ran with no hits. */
  @Input() tmdbSearchResults: TmdbSearchCandidate[] | null = null;
  @Input() tmdbSearchLoading: boolean = false;
  @Input() tmdbSearchError: string | null = null;

  @Output() movieSelected = new EventEmitter<MovieSummary>();
  @Output() movieCleared = new EventEmitter<void>();
  @Output() tmdbUrlLookup = new EventEmitter<string>();
  @Output() movieMetadataPatched = new EventEmitter<MovieRecord>();
  /** Emitted when the user clicks the "Search TMDB for ‹q›" CTA in the
   *  empty-results state. The parent calls /movies/tmdb-search and writes
   *  the response back to ``tmdbSearchResults``. */
  @Output() tmdbSearchRequested = new EventEmitter<string>();
  /** Emitted when the user picks a row from the TMDB results sub-list.
   *  Same downstream handler as the suggestion-card "Use this" CTA. */
  @Output() tmdbCandidateSelected = new EventEmitter<TmdbSearchCandidate>();

  isOpen = false;
  /** Row pencil: edit movie metadata without selecting. */
  showRowEdit = false;
  rowEditMovie: MovieSummary | null = null;
  rowEditSaving = false;
  rowEditErrors: string[] = [];
  movieRowEditPrefill: Partial<EditionFormValue> = {};
  movieRowEditResetVersion = 0;
  showTmdbInput = false;
  internalTmdbUrl = '';
  isMobile = false;
  
  /** Search term for backend search-as-you-type */
  searchTerm = '';
  /** Results from backend search (shown instead of movieOptions when searching) */
  searchResults: MovieSummary[] | null = null;
  /** Whether a backend search is in progress */
  searching = false;
  
  private destroy$ = new Subject<void>();
  private _searchInput$ = new Subject<string>();

  constructor(
    private cdr: ChangeDetectorRef,
    private mobileService: MobileService,
    private metadataSvc: MetadataService,
    private toastSvc: ToastService,
    private logger: LoggerService
  ) {}

  get selectedMovie(): MovieSummary | undefined {
    if (!this.selectedMovieId) return undefined;
    return this.movieOptions.find((m) => m.id === this.selectedMovieId);
  }

  get filteredMovies(): MovieSummary[] {
    // If backend search returned results, use those (already filtered by type on server)
    if (this.searchResults !== null) {
      return this.searchResults.filter((m) => {
        const isSeries = (m.tmdb_type || '').toLowerCase() === 'tv';
        return this.contentType === 'series' ? isSeries : !isSeries;
      });
    }
    // Otherwise filter from pre-loaded options (short list or no search)
    return this.movieOptions.filter((m) => {
      const isSeries = (m.tmdb_type || '').toLowerCase() === 'tv';
      return this.contentType === 'series' ? isSeries : !isSeries;
    });
  }

  get selectPlaceholder(): string {
    return this.contentType === 'movie'
      ? 'Select a movie'
      : 'Select a series';
  }

  get drawerTitle(): string {
    return this.showTmdbInput
      ? 'Add from TMDB'
      : this.contentType === 'movie'
        ? 'Select Movie'
        : 'Select Series';
  }

  ngOnInit(): void {
    this.internalTmdbUrl = this.tmdbUrl || '';
    this.mobileService.isMobile$
      .pipe(takeUntil(this.destroy$))
      .subscribe((mobile) => {
        this.isMobile = mobile;
        this.cdr.markForCheck();
      });
    
    // Backend search-as-you-type: debounce 300ms, search after 3+ chars
    this._searchInput$.pipe(
      debounceTime(300),
      switchMap(term => {
        if (!term || term.length < 3) {
          // Clear search results, fall back to pre-loaded options
          return of(null as MovieSummary[] | null);
        }
        this.searching = true;
        this.cdr.markForCheck();
        return this.metadataSvc.searchMoviesBackend(term, 20);
      }),
      takeUntil(this.destroy$),
    ).subscribe(results => {
      this.searchResults = results;
      this.searching = false;
      this.cdr.markForCheck();
    });
  }

  /** Called when user types in the search input */
  onSearchInput(term: string): void {
    this.searchTerm = term;
    this._searchInput$.next(term);
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['tmdbUrl']) {
      this.internalTmdbUrl = this.tmdbUrl || '';
    }
    if (changes['selectedMovieId'] || changes['movieOptions']) {
      this.cdr.markForCheck();
    }
    if (
      changes['loading'] &&
      !this.loading &&
      changes['loading'].previousValue &&
      !this.error
    ) {
      this.cdr.markForCheck();
    }
    // TMDB live-search inputs change via parent's async HTTP callback —
    // without an explicit markForCheck the OnPush view stays stuck on the
    // "Searching TMDB…" indicator forever even after the request lands.
    if (
      changes['tmdbSearchResults']
      || changes['tmdbSearchLoading']
      || changes['tmdbSearchError']
    ) {
      this.cdr.markForCheck();
    }
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  openPanel(): void {
    this.isOpen = true;
    this.showTmdbInput = false;
    this.showRowEdit = false;
    this.rowEditMovie = null;
    this.rowEditErrors = [];
    this.cdr.markForCheck();
  }

  closePanel(): void {
    this.isOpen = false;
    this.showTmdbInput = false;
    this.showRowEdit = false;
    this.rowEditMovie = null;
    this.rowEditErrors = [];
    // Clear search when closing panel
    this.searchTerm = '';
    this.searchResults = null;
    this.cdr.markForCheck();
  }

  cancelMovieRowEdit(): void {
    this.showRowEdit = false;
    this.rowEditMovie = null;
    this.rowEditErrors = [];
    this.cdr.markForCheck();
  }

  openMovieRowEdit(movie: MovieSummary, event: Event): void {
    event.stopPropagation();
    this.rowEditMovie = movie;
    const y = movie.production_year;
    this.movieRowEditPrefill = {
      name: (movie.name || '').trim(),
      year: y != null && y >= 1000 && y <= 9999 ? y : null,
      upc: '',
      asin: '',
      cover_front_url: (movie.cover_url || '').trim(),
      cover_back_url: '',
    };
    this.movieRowEditResetVersion++;
    this.rowEditErrors = [];
    this.showRowEdit = true;
    this.showTmdbInput = false;
    this.cdr.markForCheck();
  }

  onMovieRowEditionSave(v: EditionFormValue): void {
    const m = this.rowEditMovie;
    if (!m?.id) return;
    this.rowEditSaving = true;
    this.rowEditErrors = [];
    this.cdr.markForCheck();
    const coverTrim = v.cover_front_url?.trim();
    const payload: Record<string, string | number | null> = {
      name: v.name.trim(),
      production_year: v.year ?? null,
    };
    if (coverTrim) payload['cover_url'] = coverTrim;
    this.metadataSvc
      .updateMovie(m.id, payload)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (rec) => {
          this.rowEditSaving = false;
          this.toastSvc.show('Movie updated', 'success', 2000);
          this.movieMetadataPatched.emit(rec);
          this.cancelMovieRowEdit();
        },
        error: (err) => {
          this.rowEditSaving = false;
          this.rowEditErrors = [formatHttpErrorDetail(err)];
          this.logger.error('[MovieSelector] update movie failed', err);
          this.cdr.markForCheck();
        },
      });
  }

  onSelectMovie(movie: MovieSummary): void {
    this.movieSelected.emit(movie);
    this.closePanel();
  }

  onClear(): void {
    this.movieCleared.emit();
  }

  onShowTmdbInput(): void {
    this.showTmdbInput = true;
    this.cdr.markForCheck();
  }

  onBackFromTmdb(): void {
    this.showTmdbInput = false;
    this.internalTmdbUrl = '';
    this.cdr.markForCheck();
  }

  onTmdbLookup(): void {
    const url = (this.internalTmdbUrl || '').trim();
    if (url) {
      this.tmdbUrlLookup.emit(url);
    }
  }

  isMovieType(m: MovieSummary): boolean {
    return (m.tmdb_type || '').toLowerCase() !== 'tv';
  }

  /** True when the parent should be invited to run a TMDB search — the
   *  user has typed something searchable but no DB matches came back. */
  get showTmdbSearchCta(): boolean {
    return !this.searching
      && this.filteredMovies.length === 0
      && this.searchTerm.length >= 3
      && this.tmdbSearchResults === null
      && !this.tmdbSearchLoading;
  }

  onTmdbSearchCtaClick(): void {
    const q = (this.searchTerm || '').trim();
    if (q.length >= 3) this.tmdbSearchRequested.emit(q);
  }

  onTmdbCandidateClick(c: TmdbSearchCandidate): void {
    this.tmdbCandidateSelected.emit(c);
    this.closePanel();
  }

  isCandidateMovieType(c: TmdbSearchCandidate): boolean {
    return c.tmdb_type !== 'tv';
  }
}
