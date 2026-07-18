// src/app/components/movie-selector/movie-selector.component.spec.ts
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MovieSelectorComponent } from './movie-selector.component';
import { MobileService } from '../../services/mobile.service';
import { of } from 'rxjs';
import { MovieSummary, MetadataService } from '../../services/metadata.service';
import { ToastService } from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';

describe('MovieSelectorComponent', () => {
  let component: MovieSelectorComponent;
  let fixture: ComponentFixture<MovieSelectorComponent>;

  beforeEach(async () => {
    const mobileStub = { isMobile$: of(false) };
    const metadataStub = { searchMoviesBackend: () => of([]) };
    const toastStub = { show: () => {} };
    const loggerStub = { error: () => {} };

    await TestBed.configureTestingModule({
      imports: [MovieSelectorComponent],
      providers: [
        { provide: MobileService, useValue: mobileStub },
        { provide: MetadataService, useValue: metadataStub },
        { provide: ToastService, useValue: toastStub },
        { provide: LoggerService, useValue: loggerStub },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(MovieSelectorComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should expose selectedMovie when selectedMovieId matches an option', () => {
    const movie: MovieSummary = { id: '1', name: 'Test Movie', production_year: 2020 };
    component.movieOptions = [movie];
    component.selectedMovieId = '1';
    fixture.detectChanges();
    expect(component.selectedMovie).toEqual(movie);
  });

  it('should expose undefined selectedMovie when selectedMovieId is null', () => {
    component.movieOptions = [{ id: '1', name: 'A', production_year: 2020 }];
    component.selectedMovieId = null;
    fixture.detectChanges();
    expect(component.selectedMovie).toBeUndefined();
  });

  it('should filter movies by contentType (movie vs series via tmdb_type)', () => {
    const movies: MovieSummary[] = [
      { id: '1', name: 'Movie A', production_year: 2020, tmdb_type: 'movie' },
      { id: '2', name: 'Series B', production_year: 2021, tmdb_type: 'tv' },
    ];
    component.movieOptions = movies;
    component.contentType = 'movie';
    fixture.detectChanges();
    expect(component.filteredMovies.length).toBe(1);
    expect(component.filteredMovies[0].id).toBe('1');

    component.contentType = 'series';
    fixture.detectChanges();
    expect(component.filteredMovies.length).toBe(1);
    expect(component.filteredMovies[0].id).toBe('2');
  });

  it('should set selectPlaceholder from contentType', () => {
    component.contentType = 'movie';
    expect(component.selectPlaceholder).toBe('Select a movie');
    component.contentType = 'series';
    expect(component.selectPlaceholder).toBe('Select a series');
  });

  it('should emit movieSelected when onSelectMovie is called', () => {
    const movie: MovieSummary = { id: '1', name: 'Test Movie', production_year: 2020 };
    spyOn(component.movieSelected, 'emit');
    component.movieOptions = [movie];

    component.onSelectMovie(movie);

    expect(component.movieSelected.emit).toHaveBeenCalledWith(movie);
  });

  it('should emit movieCleared when onClear is called', () => {
    spyOn(component.movieCleared, 'emit');

    component.onClear();

    expect(component.movieCleared.emit).toHaveBeenCalled();
  });

  it('should emit tmdbUrlLookup when onTmdbLookup is called with non-empty url', () => {
    spyOn(component.tmdbUrlLookup, 'emit');
    component.internalTmdbUrl = 'https://www.themoviedb.org/movie/123';

    component.onTmdbLookup();

    expect(component.tmdbUrlLookup.emit).toHaveBeenCalledWith('https://www.themoviedb.org/movie/123');
  });

  it('should open and close panel', () => {
    expect(component.isOpen).toBe(false);
    component.openPanel();
    expect(component.isOpen).toBe(true);
    expect(component.showTmdbInput).toBe(false);
    component.closePanel();
    expect(component.isOpen).toBe(false);
    expect(component.showTmdbInput).toBe(false);
  });

  it('should set showTmdbInput and reset on back', () => {
    component.onShowTmdbInput();
    expect(component.showTmdbInput).toBe(true);
    component.internalTmdbUrl = 'https://example.com';
    component.onBackFromTmdb();
    expect(component.showTmdbInput).toBe(false);
    expect(component.internalTmdbUrl).toBe('');
  });

  it('should identify movie type via tmdb_type', () => {
    expect(component.isMovieType({ id: '1', name: 'A', tmdb_type: 'movie' })).toBe(true);
    expect(component.isMovieType({ id: '2', name: 'B', tmdb_type: 'tv' })).toBe(false);
    expect(component.isMovieType({ id: '3', name: 'C' })).toBe(true);
  });

  it('should sync internalTmdbUrl from tmdbUrl input in ngOnChanges', () => {
    component.tmdbUrl = 'https://example.com';
    component.internalTmdbUrl = '';
    component.ngOnChanges({
      tmdbUrl: { currentValue: 'https://example.com', previousValue: '', firstChange: false, isFirstChange: () => false },
    });
    expect(component.internalTmdbUrl).toBe('https://example.com');
  });

  it('should clean up on destroy', () => {
    spyOn(component['destroy$'], 'next');
    spyOn(component['destroy$'], 'complete');

    component.ngOnDestroy();

    expect(component['destroy$'].next).toHaveBeenCalled();
    expect(component['destroy$'].complete).toHaveBeenCalled();
  });
});
