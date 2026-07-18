import { ChangeDetectionStrategy, Component, EventEmitter, Input, OnChanges, Output, SimpleChanges, ViewEncapsulation } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MetadataService, MovieCreate } from '../../services/metadata.service';
import { LoggerService } from '../../services/logger.service';

@Component({
  selector: 'app-film-label',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './film-label.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
})
export class FilmLabelComponent implements OnChanges {
  @Input() labelForm: any;
  @Input() labelSaving = false;
  @Input() lastAutosaveOk = true;
  @Input() hasLabelContent = false;
  @Input() lastFilmDetails: any = null;

  tmdbUrl = '';
  lookupLoading = false;
  lookupError: string | null = null;
  filmData: MovieCreate | null = null;

  @Output() labelChanged = new EventEmitter<void>();
  @Output() fieldBlur = new EventEmitter<void>();
  @Output() filmSelected = new EventEmitter<any>();

  private focusDepth = 0;
  isActive = false;

  get showSpinner(): boolean {
    return this.labelSaving || this.isActive || this.lookupLoading;
  }

  get missingFilmId(): boolean {
    return !this.labelForm?.film_id;
  }

  get filmName(): string {
    return this.filmData?.name || this.labelForm?.film_name || this.lastFilmDetails?.name || '';
  }

  get productionYear(): number | null {
    return this.filmData?.production_year || this.labelForm?.production_year || this.lastFilmDetails?.production_year || null;
  }

  displayFilmName(): string {
    const name = this.filmName;
    const year = this.productionYear;
    if (!name) return '—';
    if (year) {
      return `${name} · ${year}`;
    }
    return name;
  }

  get coverImageUrl(): string | null {
    return this.filmData?.cover_url || this.labelForm?.film_cover_url || this.lastFilmDetails?.cover_url || null;
  }

  get coverImagePath(): string | null {
    return this.labelForm?.film_cover_path || this.lastFilmDetails?.cover_path || null;
  }

  constructor(
    private filmService: MetadataService,
    private logger: LoggerService
  ) {}

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['labelForm'] && this.labelForm) {
      // If film_id is set, try to load film data
      if (this.labelForm.film_id && !this.filmData) {
        this.loadFilm(this.labelForm.film_id);
      }
      // If film data is already in labelForm, use it
      if (this.labelForm.film_name || this.labelForm.film_id) {
        this.filmData = {
          name: this.labelForm.film_name || '',
          production_year: this.labelForm.production_year || null,
          tmdb_id: this.labelForm.film_tmdb_id || null,
          tmdb_type: this.labelForm.film_tmdb_type || null,
          cover_url: this.labelForm.film_cover_url || null,
        };
      }
    }
    if (changes['lastFilmDetails'] && this.lastFilmDetails) {
      this.filmData = {
        name: this.lastFilmDetails.name || '',
        production_year: this.lastFilmDetails.production_year || null,
        tmdb_id: this.lastFilmDetails.tmdb_id || null,
        tmdb_type: this.lastFilmDetails.tmdb_type || null,
        cover_url: this.lastFilmDetails.cover_url || null,
      };
    }
  }

  onLookup(): void {
    if (!this.tmdbUrl.trim()) {
      this.lookupError = 'Please enter a TMDB URL';
      return;
    }

    this.lookupLoading = true;
    this.lookupError = null;

    this.filmService.lookupMovie(this.tmdbUrl.trim()).subscribe({
      next: (data) => {
        this.filmData = data;
        this.lookupLoading = false;
        
        // Create or update film in database
        if (data.tmdb_id) {
          // Check if film already exists by tmdb_id
          this.filmService.getMovies().subscribe({
            next: (films) => {
              const existing = films.find(f => f.tmdb_id === data.tmdb_id);
              if (existing && existing.id) {
                // Use existing film
                this.updateLabelFormWithFilm(existing.id, data);
              } else {
                // Create new film
                this.filmService.createMovie(data).subscribe({
                  next: (created) => {
                    this.updateLabelFormWithFilm(created.id, data);
                  },
                  error: (err) => {
                    this.logger.error('Failed to create film:', err);
                    this.lookupError = 'Failed to create film: ' + (err.error?.detail || err.message);
                  },
                });
              }
            },
            error: (err) => {
              this.logger.error('Failed to check existing films:', err);
              // Try to create anyway
              this.filmService.createMovie(data).subscribe({
                next: (created) => {
                  this.updateLabelFormWithFilm(created.id, data);
                },
                error: (createErr) => {
                  this.logger.error('Failed to create film:', createErr);
                  this.lookupError = 'Failed to create film: ' + (createErr.error?.detail || createErr.message);
                },
              });
            },
          });
        } else {
          // No tmdb_id, just update form with scraped data
          this.updateLabelFormWithFilm(null, data);
        }
      },
      error: (err) => {
        this.lookupLoading = false;
        this.lookupError = err.error?.detail || err.message || 'Failed to lookup film';
        this.logger.error('Film lookup error:', err);
      },
    });
  }

  private updateLabelFormWithFilm(filmId: string | null, filmData: MovieCreate): void {
    if (!this.labelForm) return;
    
    if (filmId) {
      this.labelForm.film_id = filmId;
    }
    this.labelForm.film_name = filmData.name;
    this.labelForm.production_year = filmData.production_year;
    this.labelForm.film_tmdb_id = filmData.tmdb_id;
    this.labelForm.film_tmdb_type = filmData.tmdb_type;
    this.labelForm.film_cover_url = filmData.cover_url;
    
    this.labelChanged.emit();
    this.filmSelected.emit({ film_id: filmId, ...filmData });
  }

  private loadFilm(filmId: string): void {
    this.filmService.getMovie(filmId).subscribe({
      next: (film) => {
        this.filmData = {
          name: film.name,
          production_year: film.production_year || null,
          tmdb_id: film.tmdb_id || null,
          tmdb_type: film.tmdb_type || null,
          cover_url: film.cover_url || null,
        };
      },
      error: (err) => {
        this.logger.error('Failed to load film:', err);
      },
    });
  }

  onFocusIn(): void {
    this.focusDepth += 1;
    this.isActive = true;
  }

  onFocusOut(): void {
    this.focusDepth = Math.max(0, this.focusDepth - 1);
    this.isActive = this.focusDepth > 0;
  }

  onImageError(event: Event): void {
    const target = event.target as HTMLImageElement;
    if (target) {
      target.style.display = 'none';
    }
  }
}

