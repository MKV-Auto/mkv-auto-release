import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { NO_ERRORS_SCHEMA } from '@angular/core';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { WorkflowLabelingComponent } from './workflow-labeling.component';
import { WorkflowService } from '../../../../services/workflow.service';
import { WorkflowContext } from '../../../../services/workflow.service';
import { MetadataService } from '../../../../services/metadata.service';
import { MobileService } from '../../../../services/mobile.service';
import { JobService } from '../../../../services/job.service';
import { LoggerService } from '../../../../services/logger.service';
import { ToastService } from '../../../../services/toast.service';

describe('WorkflowLabelingComponent', () => {
  let component: WorkflowLabelingComponent;
  let fixture: ComponentFixture<WorkflowLabelingComponent>;
  let workflowSvc: jasmine.SpyObj<WorkflowService>;
  let metadataSvc: jasmine.SpyObj<MetadataService>;

  const minimalContext = {
    labelForm: { workflow_step: 'film' as const, group_type: 'movie', boxset_id: null },
    discdbHit: null,
  };

  beforeEach(async () => {
    const workflowSpy = jasmine.createSpyObj('WorkflowService', [
      'getActiveContext',
      'getCurrentContext',
      'determineWorkflowStep',
      'computeFurthestStep',
      'setWorkflowStep',
      'canNavigateToStep',
      'navigateToStep',
      'updateContext',
      'applyMetadataSelectionToActiveContext',
      'saveJobWorkflowContext',
      'saveDiscWorkflowContext',
      'getDiscInfoState',
      'patchDiscTitle',
      'finalizeLabel',
      'createAndLinkMovieToActiveContext',
      'createAndLinkReleaseToActiveContext',
      'createAndLinkBoxsetToActiveContext',
      'linkReleaseToContext',
      'linkBoxsetToContext',
      // #371 — episode catalog selectors consumed by workflow-breadcrumb.
      'getTvSeasonCount$',
      'getPrimarySeason$',
      'setPrimarySeason',
    ]);
    workflowSpy.getTvSeasonCount$.and.returnValue(of(null));
    workflowSpy.getPrimarySeason$.and.returnValue(of(1));
    workflowSpy.createAndLinkMovieToActiveContext = jasmine.createSpy('createAndLinkMovieToActiveContext')
      .and.returnValue(of({ movie: { id: 'mv-1' } as any, linked: true }));
    workflowSpy.getActiveContext.and.returnValue(of(minimalContext));
    workflowSpy.getCurrentContext.and.returnValue(minimalContext);
    workflowSpy.determineWorkflowStep.and.returnValue('film');
    workflowSpy.computeFurthestStep.and.returnValue('film');
    workflowSpy.setWorkflowStep.calls.reset();
    workflowSpy.canNavigateToStep.and.returnValue({ allowed: true, reason: null });
    workflowSpy.applyMetadataSelectionToActiveContext.and.returnValue(of(undefined));
    workflowSpy.saveJobWorkflowContext.and.returnValue(of(undefined));
    workflowSpy.saveDiscWorkflowContext.and.returnValue(of(undefined));
    workflowSpy.patchDiscTitle.and.returnValue(of(undefined));
    workflowSpy.finalizeLabel.and.returnValue(of(undefined));
    workflowSpy.createAndLinkReleaseToActiveContext.and.returnValue(of(undefined));
    workflowSpy.createAndLinkBoxsetToActiveContext.and.returnValue(of(undefined));
    workflowSpy.linkReleaseToContext.and.returnValue(of(undefined));
    workflowSpy.linkBoxsetToContext.and.returnValue(of(undefined));
    workflowSpy.getDiscInfoState.and.returnValue({ currentDiscId: null });
    Object.defineProperty(workflowSpy, 'isWorkflowReady$', { value: of(false), writable: true });

    const metadataSpy = jasmine.createSpyObj('MetadataService', [
      'getMovieOptions', 'getBoxsetOptions', 'getGroupOptions', 'listReleases', 'getMovies', 'filterMovies',
      'createMovieForDisc',
      'createReleaseForDisc',
      'findReleaseByMovieBoxset',
      'refreshMovieOptions',
      'refreshBoxsetOptions',
      'searchTmdb',
    ]);
    metadataSpy.searchTmdb.and.returnValue(of({ candidates: [], normalized_query: '', hints: {} }));
    metadataSpy.createMovieForDisc.and.returnValue(of({ movie: { id: 'mv-default' } as any }));
    metadataSpy.getMovieOptions.and.returnValue({ asObservable: () => of([]) });
    metadataSpy.getBoxsetOptions.and.returnValue({ asObservable: () => of([]) });
    metadataSpy.getGroupOptions.and.returnValue({ asObservable: () => of([]) });
    metadataSpy.listReleases.and.returnValue(of([]));
    metadataSpy.getMovies.and.returnValue(of([]));
    metadataSpy.filterMovies.and.returnValue([]);
    metadataSpy.createReleaseForDisc.and.returnValue(of({ release: {} as any, linked: true }));
    metadataSpy.findReleaseByMovieBoxset.and.returnValue(of(null));

    const mobileStub = { isMobile$: of(false) };
    const jobSpy = jasmine.createSpyObj('JobService', ['getPostProcessStatus']);
    jobSpy.getPostProcessStatus.and.returnValue({ pending: [], inProgress: [], completed: [] });
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error']);
    const toastSpy = jasmine.createSpyObj('ToastService', ['show']);

    await TestBed.configureTestingModule({
      imports: [WorkflowLabelingComponent],
      providers: [
        provideNoopAnimations(),
        { provide: WorkflowService, useValue: workflowSpy },
        { provide: MetadataService, useValue: metadataSpy },
        { provide: MobileService, useValue: mobileStub },
        { provide: JobService, useValue: jobSpy },
        { provide: LoggerService, useValue: loggerSpy },
        { provide: ToastService, useValue: toastSpy },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(WorkflowLabelingComponent);
    component = fixture.componentInstance;
    workflowSvc = TestBed.inject(WorkflowService) as jasmine.SpyObj<WorkflowService>;
    metadataSvc = TestBed.inject(MetadataService) as jasmine.SpyObj<MetadataService>;
  });

  it('should create', () => {
    fixture.detectChanges();
    expect(component).toBeTruthy();
  });

  it('sets currentStep from context workflowStep on init', () => {
    fixture.detectChanges();
    expect(workflowSvc.determineWorkflowStep).toHaveBeenCalled();
    expect(component.currentStep).toBe('film');
    expect(component.currentStepIndex).toBe(0);
  });

  it('shows film step when currentStep is film and context has labelForm', () => {
    fixture.detectChanges();
    const filmStep = fixture.nativeElement.querySelector('.workflow-step');
    expect(filmStep).toBeTruthy();
  });

  it('calls metadataService.createReleaseForDisc when creating release from combobox (desktop)', () => {
    const contextWithDisc = {
      ...minimalContext,
      id: 'job-abc',
      type: 'job' as const,
      discInfo: { disc_id: 'disc-123' },
      labelForm: { ...minimalContext.labelForm, movie_id: 'movie-1' },
    } as unknown as WorkflowContext;
    workflowSvc.getCurrentContext.and.returnValue(contextWithDisc);
    metadataSvc.createReleaseForDisc.and.returnValue(
      of({ release: { id: 'rel-new', slug: 'rel-new' } as any, linked: true })
    );
    fixture.detectChanges();

    component.onReleaseCreated({
      name: 'Test Release',
      release_year: 2020,
      upc: '123456789012',
      cover_front_url: 'https://example.com/cover.jpg',
    });

    expect(metadataSvc.createReleaseForDisc).toHaveBeenCalledWith(
      'disc-123',
      null,
      jasmine.objectContaining({
        release_name: 'Test Release',
        release_year: 2020,
        upc: '123456789012',
        cover_front_url: 'https://example.com/cover.jpg',
        movie_id: 'movie-1',
      })
    );
    expect(workflowSvc.linkReleaseToContext).toHaveBeenCalledWith(
      'rel-new',
      'rel-new',
      'job-abc',
      'job',
      null
    );
    expect(workflowSvc.createAndLinkReleaseToActiveContext).not.toHaveBeenCalled();
  });

  it('onBoxsetSelected toasts when movie_id is missing', () => {
    const toastSpy = TestBed.inject(ToastService) as jasmine.SpyObj<ToastService>;
    workflowSvc.getCurrentContext.and.returnValue({
      ...minimalContext,
      labelForm: { ...minimalContext.labelForm, movie_id: null },
    } as any);
    fixture.detectChanges();
    component.onBoxsetSelected({ id: 'b1', slug: 'bx' } as any);
    expect(toastSpy.show).toHaveBeenCalled();
    expect(metadataSvc.findReleaseByMovieBoxset).not.toHaveBeenCalled();
  });

  it('onBoxsetSelected applies metadata when a release exists for movie+boxset', () => {
    const ctx = {
      ...minimalContext,
      id: 'j1',
      type: 'job' as const,
      labelForm: { ...minimalContext.labelForm, movie_id: 'm1' },
    };
    workflowSvc.getCurrentContext.and.returnValue(ctx as any);
    metadataSvc.findReleaseByMovieBoxset.and.returnValue(
      of({ id: 'rel-x', slug: 'sx', boxset_id: 'b1' } as any)
    );
    fixture.detectChanges();
    component.onBoxsetSelected({ id: 'b1', slug: 'bs' } as any);
    expect(metadataSvc.findReleaseByMovieBoxset).toHaveBeenCalledWith('m1', 'b1');
    expect(workflowSvc.applyMetadataSelectionToActiveContext).toHaveBeenCalled();
    expect(metadataSvc.createReleaseForDisc).not.toHaveBeenCalled();
  });

  it('onBoxsetSelected creates release and linkBoxsetToContext when none exists', () => {
    const ctx = {
      ...minimalContext,
      id: 'j1',
      type: 'job' as const,
      discInfo: { disc_id: 'd1' },
      labelForm: { ...minimalContext.labelForm, movie_id: 'm1' },
    };
    workflowSvc.getCurrentContext.and.returnValue(ctx as any);
    metadataSvc.findReleaseByMovieBoxset.and.returnValue(of(null));
    metadataSvc.createReleaseForDisc.and.returnValue(
      of({ release: { id: 'new-r', slug: 'new-s' } as any, linked: true })
    );
    fixture.detectChanges();
    component.onBoxsetSelected({ id: 'b1', slug: 'bs' } as any);
    expect(metadataSvc.createReleaseForDisc).toHaveBeenCalledWith(
      'd1',
      null,
      jasmine.objectContaining({ movie_id: 'm1', boxset_id: 'b1' })
    );
    expect(workflowSvc.linkBoxsetToContext).toHaveBeenCalledWith('b1', 'bs', 'j1', 'job', {
      id: 'new-r',
      slug: 'new-s',
    });
  });

  it('shows workflow container when context has discdbHit true and labelForm null', () => {
    const discdbHitContext = {
      id: 'mount-1',
      type: 'drive' as const,
      labelForm: null,
      discdbHit: true,
      jobStatus: null,
      workflowStep: 'summary' as const,
    } as unknown as WorkflowContext;
    workflowSvc.getActiveContext.and.returnValue(of(discdbHitContext));
    workflowSvc.getCurrentContext.and.returnValue(discdbHitContext);
    workflowSvc.determineWorkflowStep.and.returnValue('summary');
    workflowSvc.computeFurthestStep.and.returnValue('summary');
    fixture.detectChanges();
    const container = fixture.nativeElement.querySelector('.workflow-labeling-container');
    expect(container).toBeTruthy();
    expect(component.currentStep).toBe('summary');
  });

  it('getMovieSummary returns synthetic summary when discdbHit and context has movieName/movieCover', () => {
    const ctx = {
      ...minimalContext,
      discdbHit: true,
      labelForm: null,
      movieName: 'Test Movie',
      productionYear: 2021,
      movieCover: 'https://example.com/cover.jpg',
      movieOptions: [],
    } as unknown as WorkflowContext;
    const result = component.getMovieSummary(ctx);
    expect(result).toBeTruthy();
    expect(result?.name).toBe('Test Movie');
    expect(result?.production_year).toBe(2021);
    expect(result?.cover_url).toBe('https://example.com/cover.jpg');
  });

  it('mergeTitlesWithBackend retains detection fields from backend', () => {
    fixture.detectChanges();
    const backendTitles = [
      {
        title_id: '1',
        title: 'Backend Title',
        type: '',
        detection_warning: true,
        detection_flags: { is_suspicious_bitrate: true },
        detection_confidence: 0.85,
      },
    ];
    const currentTitles = [{ title_id: '1', title: 'Backend Title', type: '' }];
    const merged = (component as any).mergeTitlesWithBackend(currentTitles, backendTitles);
    expect(merged.length).toBe(1);
    expect(merged[0].detection_warning).toBe(true);
    expect(merged[0].detection_flags).toEqual({ is_suspicious_bitrate: true });
    expect(merged[0].detection_confidence).toBe(0.85);
  });

  it('mergeTitlesWithBackend preserves detection fields from current when backend omits them', () => {
    fixture.detectChanges();
    const backendTitles = [
      { title_id: '1', title: 'Title', type: 'MainMovie' },
    ];
    const currentTitles = [
      {
        title_id: '1',
        title: 'Title',
        type: 'MainMovie',
        detection_warning: true,
        detection_flags: { black_frame_duration: 10 },
        detection_confidence: 0.9,
      },
    ];
    const merged = (component as any).mergeTitlesWithBackend(currentTitles, backendTitles);
    expect(merged.length).toBe(1);
    expect(merged[0].detection_warning).toBe(true);
    expect(merged[0].detection_flags).toEqual({ black_frame_duration: 10 });
    expect(merged[0].detection_confidence).toBe(0.9);
  });

  it('mergeLabelFormDiscFieldsWithBackend keeps backend slug when client slug is blank', () => {
    fixture.detectChanges();
    const merged = (component as any).mergeLabelFormDiscFieldsWithBackend(
      { disc_name: 'My Disc', disc_slug: '' },
      { disc_name: 'My Disc', disc_slug: 'my-disc' }
    );
    expect(merged.disc_slug).toBe('my-disc');
  });

  // ────────────────────────────────────────────────────────────────────────
  // TMDB suggestion + override search (#389)
  // ────────────────────────────────────────────────────────────────────────

  describe('TMDB suggestion handlers (#389)', () => {
    const suggestion = {
      tmdb_id: '119051',
      tmdb_type: 'tv' as const,
      title: 'Wednesday',
      year: 2022,
      cover_url: 'https://image.tmdb.org/t/p/w500/wed.jpg',
      score: 0.91,
      normalized_query: 'wednesday',
      hints: { season: 1, disc_num: 2 },
      candidates: [],
    };

    function contextWithSuggestion(overrides: any = {}) {
      return {
        ...minimalContext,
        id: 'job-tmdb',
        type: 'job' as const,
        discInfo: { tmdb_suggestion: suggestion, ...overrides.discInfo },
        labelForm: { ...minimalContext.labelForm, ...(overrides.labelForm || {}) },
      } as any;
    }

    it('getTmdbSuggestion reads tmdb_suggestion from context.discInfo', () => {
      fixture.detectChanges();
      const ctx = contextWithSuggestion();
      expect(component.getTmdbSuggestion(ctx)?.tmdb_id).toBe('119051');
      expect(component.getTmdbSuggestion(null)).toBeNull();
      expect(component.getTmdbSuggestion({ discInfo: null } as any)).toBeNull();
    });

    it('showTmdbSuggestionCard is true on DiscDB-miss with no chosen movie', () => {
      fixture.detectChanges();
      expect(component.showTmdbSuggestionCard(contextWithSuggestion())).toBe(true);
    });

    it('showTmdbSuggestionCard is false once a movie is picked', () => {
      fixture.detectChanges();
      const ctx = contextWithSuggestion({ labelForm: { movie_id: 'm1' } });
      expect(component.showTmdbSuggestionCard(ctx)).toBe(false);
    });

    it('showTmdbSuggestionCard is false in override mode', () => {
      fixture.detectChanges();
      component.tmdbSuggestionMode = 'override';
      expect(component.showTmdbSuggestionCard(contextWithSuggestion())).toBe(false);
    });

    it('onAcceptTmdbSuggestion creates the movie via createMovieForDisc (CLAUDE.md disc-scoped flow)', fakeAsync(() => {
      const ctxWithDisc = {
        ...contextWithSuggestion(),
        id: 'job-tmdb',
        type: 'job' as const,
        discInfo: { disc_id: 'disc-fallout', tmdb_suggestion: suggestion } as any,
      };
      workflowSvc.getCurrentContext.and.returnValue(ctxWithDisc);
      const createdMovie = { id: 'mv-1', name: 'Wednesday', tmdb_id: '119051', tmdb_type: 'tv' } as any;
      metadataSvc.createMovieForDisc.and.returnValue(of({ movie: createdMovie }));
      // After create, the service-level cache is checked via .value — return a
      // BehaviorSubject-like stub with the new movie present.
      metadataSvc.getMovieOptions.and.returnValue({ value: [createdMovie], asObservable: () => of([createdMovie]) } as any);
      fixture.detectChanges();

      component.onAcceptTmdbSuggestion(suggestion as any);
      tick(0);  // The shared helper waits one tick before applying the selection.

      expect(metadataSvc.createMovieForDisc).toHaveBeenCalledWith(
        'disc-fallout',
        null,
        jasmine.objectContaining({
          name: 'Wednesday',
          tmdb_id: '119051',
          tmdb_type: 'tv',
          production_year: 2022,
        })
      );
      expect(workflowSvc.applyMetadataSelectionToActiveContext).toHaveBeenCalledWith(
        jasmine.objectContaining({
          movieId: 'mv-1',
          tmdbId: '119051',
          groupType: 'series',
        })
      );
    }));

    it('onAcceptTmdbSuggestion seeds the new movie into the context movieOptions', fakeAsync(() => {
      // Without this, <app-movie-selector> can't resolve selectedMovieId to a
      // row and the selector card renders empty — the original bug.
      const ctxWithDisc = {
        ...contextWithSuggestion(),
        id: 'job-tmdb',
        type: 'job' as const,
        discInfo: { disc_id: 'disc-fallout', tmdb_suggestion: suggestion } as any,
        movieOptions: [{ id: 'existing-mv', name: 'Other' } as any],
      };
      workflowSvc.getCurrentContext.and.returnValue(ctxWithDisc);
      const createdMovie = { id: 'mv-new', name: 'Wednesday', tmdb_id: '119051', tmdb_type: 'tv' } as any;
      metadataSvc.createMovieForDisc.and.returnValue(of({ movie: createdMovie }));
      metadataSvc.getMovieOptions.and.returnValue({ value: [createdMovie], asObservable: () => of([createdMovie]) } as any);
      fixture.detectChanges();

      component.onAcceptTmdbSuggestion(suggestion as any);
      tick(0);

      expect(workflowSvc.updateContext).toHaveBeenCalledWith(
        jasmine.objectContaining({
          movieOptions: jasmine.arrayContaining([
            jasmine.objectContaining({ id: 'existing-mv' }),
            jasmine.objectContaining({ id: 'mv-new' }),
          ]),
        })
      );
    }));

    it('onTmdbSearchRequested calls metadataSvc.searchTmdb and stores candidates', () => {
      const results = [
        { tmdb_id: '1', tmdb_type: 'movie' as const, title: 'Dune', year: 2021, cover_url: null, score: 0.9 },
      ];
      metadataSvc.searchTmdb.and.returnValue(of({ candidates: results, normalized_query: 'dune', hints: {} }));
      fixture.detectChanges();
      component.onTmdbSearchRequested('Dune');
      expect(metadataSvc.searchTmdb).toHaveBeenCalledWith('Dune', jasmine.any(Object));
      expect(component.tmdbSearchResults).toEqual(results);
      expect(component.tmdbSearchLoading).toBe(false);
    });

    it('onTmdbSearchRequested no-ops on empty query', () => {
      fixture.detectChanges();
      component.onTmdbSearchRequested('   ');
      expect(metadataSvc.searchTmdb).not.toHaveBeenCalled();
    });

    it('enterTmdbOverrideMode / dismissTmdbOverride toggle the mode', () => {
      fixture.detectChanges();
      // localStorage mutations from enterTmdbOverrideMode would persist —
      // keep the test isolated by stubbing it. Defensive against prior
      // specs (e.g. drive-selector) that may already hold the spy and
      // hadn't released it — Jasmine throws on a second spyOn of the
      // same method otherwise. Mirrors the drive-selector spec's idiom.
      const existing = (localStorage.setItem as any);
      if (!existing?.and) spyOn(localStorage, 'setItem');
      workflowSvc.getCurrentContext.and.returnValue({ ...contextWithSuggestion(), discInfo: null } as any);
      expect(component.tmdbSuggestionMode).toBe('suggestion');
      component.enterTmdbOverrideMode();
      expect(component.tmdbSuggestionMode).toBe('override');
      component.dismissTmdbOverride();
      expect(component.tmdbSuggestionMode).toBe('suggestion');
      expect(component.tmdbSearchResults).toBeNull();
    });

    it('once Use This is clicked, showTmdbSuggestionCard stays false even after movie_id is cleared', () => {
      // Reproduces the live-testing complaint: after Use This → Change-from-
      // selected-chip → reload, the card used to come back. It shouldn't.
      fixture.detectChanges();
      const handledKey = 'mkv-auto.tmdb-suggestion-handled.disc-fallout';
      // Simulate that the disc has been "handled" via prior interaction.
      spyOn(localStorage, 'getItem').and.callFake((k) =>
        k === handledKey ? '1' : null
      );
      const ctx = {
        ...contextWithSuggestion(),
        discInfo: { disc_id: 'disc-fallout', tmdb_suggestion: suggestion } as any,
        labelForm: { movie_id: null } as any,
      } as any;
      expect(component.showTmdbSuggestionCard(ctx)).toBe(false);
    });

    it('showTmdbSuggestionCard returns true on a fresh disc with a suggestion', () => {
      // Baseline — without the handled flag, the card renders.
      fixture.detectChanges();
      spyOn(localStorage, 'getItem').and.returnValue(null);
      const ctx = {
        ...contextWithSuggestion(),
        discInfo: { disc_id: 'disc-fresh', tmdb_suggestion: suggestion } as any,
        labelForm: { movie_id: null } as any,
      } as any;
      expect(component.showTmdbSuggestionCard(ctx)).toBe(true);
    });
  });
});
