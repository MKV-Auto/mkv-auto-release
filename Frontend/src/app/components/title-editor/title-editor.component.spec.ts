import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { BehaviorSubject } from 'rxjs';
import { TitleEditorComponent } from './title-editor.component';
import { WorkflowService } from '../../services/workflow.service';
import { LoggerService } from '../../services/logger.service';
import { JobService } from '../../services/job.service';
import { DriveService } from '../../services/drive.service';
import { MetadataService } from '../../services/metadata.service';

function makeTitle(over: Record<string, any> = {}): any {
  return {
    title_id: 't1',
    title: 'Inception',
    type: 'movie',
    duration: 8400,
    size: 1024 * 1024 * 1024,
    chapters: 12,
    ...over,
  };
}

describe('TitleEditorComponent', () => {
  let fixture: ComponentFixture<TitleEditorComponent>;
  let component: TitleEditorComponent;

  beforeEach(async () => {
    // TitleEditorComponent injects WorkflowService for the TMDB episode picker (#371).
    // Provide the minimum collaborator graph so it can be instantiated in unit tests.
    const jobSpy = jasmine.createSpyObj('JobService', ['getJobStatus', 'titleJobProgress']);
    const driveSpy = jasmine.createSpyObj('DriveService',
      ['currentSelected', 'getDrives'],
      { drives$: new BehaviorSubject<any[]>([]) });
    driveSpy.currentSelected.and.returnValue(null);
    driveSpy.getDrives.and.returnValue([]);
    const metadataSpy = jasmine.createSpyObj('MetadataService', [
      'getCachedOptions', 'loadWorkflowOptions', 'refreshWorkflowOptions',
    ]);
    metadataSpy.getCachedOptions.and.returnValue({
      movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
    });
    metadataSpy.loadWorkflowOptions.and.returnValue(new BehaviorSubject({
      movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
    }).asObservable());
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);

    await TestBed.configureTestingModule({
      imports: [TitleEditorComponent, HttpClientTestingModule],
      providers: [
        WorkflowService,
        { provide: JobService, useValue: jobSpy },
        { provide: DriveService, useValue: driveSpy },
        { provide: MetadataService, useValue: metadataSpy },
        { provide: LoggerService, useValue: loggerSpy },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(TitleEditorComponent);
    component = fixture.componentInstance;
  });

  it('renders nothing when title is null', () => {
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-editor')).toBeNull();
  });

  it('renders the title heading and form fields when a title is provided', () => {
    fixture.componentRef.setInput('title', makeTitle());
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.title-editor__heading')?.textContent?.trim()).toBe('Inception');
    expect(root.querySelector('input.title-editor__input')).toBeTruthy();
    expect(root.querySelector('select.title-editor__select')).toBeTruthy();
  });

  it('shows the close button only when showCloseButton is true', () => {
    fixture.componentRef.setInput('title', makeTitle());
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-editor__close')).toBeNull();

    fixture.componentRef.setInput('showCloseButton', true);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-editor__close')).toBeTruthy();
  });

  it('emits close when the close button is clicked', () => {
    fixture.componentRef.setInput('title', makeTitle());
    fixture.componentRef.setInput('showCloseButton', true);
    fixture.detectChanges();
    let fired = false;
    component.close.subscribe(() => (fired = true));
    (fixture.nativeElement as HTMLElement).querySelector('.title-editor__close')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(fired).toBeTrue();
  });

  it('shows season + episode inputs when isSeries is true and not ignored', () => {
    fixture.componentRef.setInput('title', makeTitle({ type: 'episode', season: 1, episode: 3 }));
    fixture.componentRef.setInput('isSeries', true);
    fixture.detectChanges();
    const inputs = (fixture.nativeElement as HTMLElement).querySelectorAll('input[type="number"]');
    expect(inputs.length).toBe(2);
  });

  it('hides description and edition when the title is ignored', () => {
    fixture.componentRef.setInput('title', makeTitle({ type: 'ignore' }));
    fixture.detectChanges();
    const labels = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('label.title-editor__label')).map(l => l.textContent?.trim());
    expect(labels).not.toContain('Edition (Optional)');
    expect(labels).not.toContain('Description (Optional)');
  });

  it('toggles the type to "ignore" via markAsIgnore and emits titleChanged', () => {
    const t = makeTitle();
    fixture.componentRef.setInput('title', t);
    fixture.detectChanges();
    let fired = false;
    component.titleChanged.subscribe(() => (fired = true));
    component.markAsIgnore();
    expect(t.type).toBe('ignore');
    expect(fired).toBeTrue();
  });

  it('clears editable fields when type flips to ignore', () => {
    const t = makeTitle({ description: 'Cool', season: 2, episode: 5 });
    fixture.componentRef.setInput('title', t);
    fixture.detectChanges();
    component.markAsIgnore();
    expect(t.title).toBe('');
    expect(t.description).toBe('');
    expect(t.season).toBeNull();
    expect(t.episode).toBeNull();
  });

  it('#642: button label reads "Ignore" when not ignored and "Un-ignore" when ignored', () => {
    fixture.componentRef.setInput('title', makeTitle({ type: 'movie' }));
    fixture.detectChanges();
    const findIgnoreBtn = () =>
      Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('ui-btn button'))
        .find((el) => /^(Ignore|Un-ignore)$/.test((el.textContent || '').trim())) as HTMLElement | undefined;
    expect(findIgnoreBtn()?.textContent?.trim()).toBe('Ignore');

    fixture.componentRef.setInput('title', makeTitle({ type: 'ignore' }));
    fixture.detectChanges();
    expect(findIgnoreBtn()?.textContent?.trim()).toBe('Un-ignore');
  });

  it('formats duration in h/m/s — sub-60s falls back to seconds', () => {
    expect(component.formatDuration(0)).toBe('');
    expect(component.formatDuration(45)).toBe('45s');
    expect(component.formatDuration(10)).toBe('10s');
    expect(component.formatDuration(59)).toBe('59s');
    expect(component.formatDuration(60)).toBe('1m');
    expect(component.formatDuration(3600)).toBe('1h 0m');
    expect(component.formatDuration(8400)).toBe('2h 20m');
  });

  it('formats size in MB', () => {
    expect(component.formatSize(0)).toBe('');
    expect(component.formatSize(1024 * 1024 * 4)).toBe('4 MB');
  });

  it('opens and closes the preview overlay', () => {
    fixture.componentRef.setInput('title', makeTitle());
    fixture.componentRef.setInput('previewUrlFn', () => 'https://example.com/preview.mp4');
    fixture.detectChanges();
    component.openPreview();
    expect(component.previewUrl).toBe('https://example.com/preview.mp4');
    component.closePreview();
    expect(component.previewUrl).toBeNull();
  });

  it('maps actionable statuses to descriptive labels and tones', () => {
    fixture.componentRef.setInput('title', makeTitle());
    fixture.componentRef.setInput('titleStatusFn', () => 'failed');
    fixture.detectChanges();
    expect(component.statusPillTone()).toBe('red');
    expect(component.statusPillLabel()).toBe('Rip failed');
    expect(component.showStatusPill()).toBeTrue();
  });

  it('hides the status pill for non-actionable states (pending / completed)', () => {
    fixture.componentRef.setInput('title', makeTitle());
    fixture.componentRef.setInput('titleStatusFn', () => 'completed');
    fixture.detectChanges();
    // Pending and completed are silent — the global stage breadcrumb and
    // the row's labeling-complete check cover them without per-title noise.
    expect(component.showStatusPill()).toBeFalse();

    fixture.componentRef.setInput('titleStatusFn', () => 'pending');
    fixture.detectChanges();
    expect(component.showStatusPill()).toBeFalse();
  });

  it('shows running progress percentage in the status pill label', () => {
    fixture.componentRef.setInput('title', makeTitle());
    fixture.componentRef.setInput('titleStatusFn', () => 'running');
    fixture.componentRef.setInput('titleProgressValueFn', () => 65);
    fixture.detectChanges();
    expect(component.statusPillLabel()).toBe('Ripping 65%');
    expect(component.showStatusPill()).toBeTrue();
  });

  describe('Component clips section', () => {
    it('does not render when componentClips is empty', () => {
      fixture.componentRef.setInput('title', makeTitle());
      fixture.componentRef.setInput('componentClips', []);
      fixture.detectChanges();
      const root = fixture.nativeElement as HTMLElement;
      expect(root.textContent || '').not.toContain('Component clips');
    });

    it('renders the section + one row per clip when provided', () => {
      fixture.componentRef.setInput('title', makeTitle({ title_id: 'mpls-1' }));
      fixture.componentRef.setInput('componentClips', [
        { title_id: 'c1', source_file: '02807.m2ts', duration: 98 },
        { title_id: 'c2', source_file: '02808.m2ts', duration: 126 },
        { title_id: 'c3', source_file: '02809.m2ts', duration: 99 },
      ]);
      fixture.detectChanges();
      const root = fixture.nativeElement as HTMLElement;
      expect(root.textContent || '').toContain('Component clips');
      // The section uses the same dupe-list class; count clickable rows.
      const sections = root.querySelectorAll('section.title-editor__dupe');
      // Two sections: duplicate group (empty here → skipped) + component clips.
      const compSection = Array.from(sections).find(s => (s.textContent || '').includes('Component clips'));
      expect(compSection).toBeTruthy();
      expect(compSection!.querySelectorAll('.title-editor__dupe-row--clickable').length).toBe(3);
    });

    it('emits switchToSibling when a clip row is clicked', () => {
      const clip = { title_id: 'c1', source_file: '02807.m2ts', duration: 98 };
      fixture.componentRef.setInput('title', makeTitle({ title_id: 'mpls-1' }));
      fixture.componentRef.setInput('componentClips', [clip]);
      fixture.detectChanges();
      let emitted: any = null;
      component.switchToSibling.subscribe((c) => (emitted = c));
      const row = (fixture.nativeElement as HTMLElement)
        .querySelector('.title-editor__dupe-row--clickable') as HTMLElement;
      row.click();
      expect(emitted).toBe(clip);
    });

    // #602: component clips render below the description + metadata footer
    // (reference content, not part of the active edit). Pin the DOM order so
    // a future template reshuffle can't silently regress.
    it('#602 — clips section renders after description and metadata footer', () => {
      fixture.componentRef.setInput('title', makeTitle({ title_id: 'mpls-1' }));
      fixture.componentRef.setInput('componentClips', [
        { title_id: 'c1', source_file: '02807.m2ts', duration: 98 },
      ]);
      fixture.detectChanges();
      const root = fixture.nativeElement as HTMLElement;
      const clipsSection = Array.from(root.querySelectorAll('section.title-editor__dupe'))
        .find((s) => (s.textContent || '').includes('Component clips'));
      const metadata = root.querySelector('.title-editor__metadata');
      const description = root.querySelector('.title-editor__textarea');
      expect(clipsSection).toBeTruthy();
      expect(metadata).toBeTruthy();
      expect(description).toBeTruthy();
      // documentPosition: FOLLOWING (4) means clipsSection comes AFTER the
      // other element in document order.
      expect(metadata!.compareDocumentPosition(clipsSection!) & Node.DOCUMENT_POSITION_FOLLOWING)
        .withContext('clips section must follow metadata in DOM order')
        .toBeTruthy();
      expect(description!.compareDocumentPosition(clipsSection!) & Node.DOCUMENT_POSITION_FOLLOWING)
        .withContext('clips section must follow description in DOM order')
        .toBeTruthy();
    });
  });

  // ---- #371: TMDB episode picker -------------------------------------------

  describe('TMDB episode picker (#371)', () => {
    function seedSeriesCatalog(workflow: WorkflowService) {
      const ctx: any = {
        id: 'job-x', type: 'job',
        labelForm: { group_type: 'series', tmdb_id: '60625', primary_season: 1, tracks: [] },
        discInfo: null, titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
        labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: true, discdbHit: false, discMode: 'rip',
        lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '',
        postProcessFiles: [], transferDestination: null, releaseDiscs: [], boxsetMovies: [],
        movieCover: null, movieName: null, productionYear: null,
        labelSaving: false, lastAutosaveOk: true, hasLabelContent: false,
        devMode: false, showTitleStatus: false, jobStatus: null,
        tmdbEpisodeCatalog: {
          tmdb_id: '60625', numberOfSeasons: 1, seriesName: 'Rick and Morty',
          loadingSeasons: new Set<number>(),
          errorSeasons: new Set<number>(),
          seasons: new Map([[1, {
            tmdb_id: '60625', season_number: 1, number_of_seasons: 1, series_name: 'Rick and Morty',
            episodes: [
              { season_number: 1, episode_number: 1, name: 'Pilot',
                overview: null, air_date: null, runtime: null, still_url: null },
              { season_number: 1, episode_number: 2, name: 'Lawnmower Dog',
                overview: null, air_date: null, runtime: null, still_url: null },
            ],
          }]]),
        },
      };
      (workflow as any)._activeContext$.next(ctx);
    }

    it('renders the picker only when isSeries and catalog has the row season', async () => {
      const workflow = TestBed.inject(WorkflowService);
      seedSeriesCatalog(workflow);
      fixture.componentRef.setInput('title', makeTitle({ type: 'episode', season: 1, episode: null }));
      fixture.componentRef.setInput('isSeries', true);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();
      const root = fixture.nativeElement as HTMLElement;
      const picker = root.querySelector('select[aria-label="TMDB episode picker"]');
      expect(picker).toBeTruthy();
      // Two episodes + "Select episode…" placeholder = 3 options.
      expect(picker!.querySelectorAll('option').length).toBe(3);
    });

    it('does not render the picker when not a series', () => {
      const workflow = TestBed.inject(WorkflowService);
      seedSeriesCatalog(workflow);
      fixture.componentRef.setInput('title', makeTitle({ type: 'movie' }));
      fixture.componentRef.setInput('isSeries', false);
      fixture.detectChanges();
      const root = fixture.nativeElement as HTMLElement;
      expect(root.querySelector('select[aria-label="TMDB episode picker"]')).toBeNull();
    });

    it('onEpisodePicked auto-fills title/season/episode and emits titleChanged', () => {
      const workflow = TestBed.inject(WorkflowService);
      seedSeriesCatalog(workflow);
      const trk = makeTitle({ type: 'episode', season: 1, episode: null, title: '' });
      fixture.componentRef.setInput('title', trk);
      fixture.componentRef.setInput('isSeries', true);
      fixture.detectChanges();
      let changedFired = false;
      component.titleChanged.subscribe(() => (changedFired = true));
      const ep = { season_number: 1, episode_number: 2, name: 'Lawnmower Dog',
                   overview: null, air_date: null, runtime: null, still_url: null };
      component.onEpisodePicked([ep], '0');
      expect(trk.season).toBe(1);
      expect(trk.episode).toBe(2);
      expect(trk.title).toBe('Lawnmower Dog');
      expect(changedFired).toBe(true);
    });

    it('findEpisodeIndex matches the current (season, episode)', () => {
      const opts = [
        { season_number: 1, episode_number: 1, name: 'Pilot',
          overview: null, air_date: null, runtime: null, still_url: null },
        { season_number: 1, episode_number: 2, name: 'Lawnmower Dog',
          overview: null, air_date: null, runtime: null, still_url: null },
      ];
      fixture.componentRef.setInput('title', makeTitle({ season: 1, episode: 2 }));
      expect(component.findEpisodeIndex(opts)).toBe(1);
      (component.title as any).episode = 99;
      expect(component.findEpisodeIndex(opts)).toBe(-1);
    });

    // #602: when TMDB has the episode catalog for the row, the manual
    // title / season / episode inputs hide — the picker covers all three.
    // When TMDB doesn't have data, the manuals stay so the user is never
    // stranded.
    it('#602 — hides title / season / episode inputs on Episode rows when TMDB has the catalog', async () => {
      const workflow = TestBed.inject(WorkflowService);
      seedSeriesCatalog(workflow);
      fixture.componentRef.setInput('title', makeTitle({ type: 'episode', season: 1 }));
      fixture.componentRef.setInput('isSeries', true);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();

      const root = fixture.nativeElement as HTMLElement;
      // Picker present (sanity).
      expect(root.querySelector('select[aria-label="TMDB episode picker"]')).toBeTruthy();
      // Title input + season + episode all hidden.
      const labels = Array.from(root.querySelectorAll('label')).map((l) => (l.textContent || '').trim());
      expect(labels).not.toContain('Episode title');
      expect(labels).not.toContain('Season');
      expect(labels).not.toContain('Episode');
    });

    it('#602 — keeps the inputs visible when TMDB returns no catalog (movie disc / no TMDB hit)', () => {
      // Don't seed — workflow context stays empty, so episodeOptions$
      // emits 'unavailable' / never emits.
      fixture.componentRef.setInput('title', makeTitle({ type: 'episode', season: 1 }));
      fixture.componentRef.setInput('isSeries', true);
      fixture.detectChanges();

      const root = fixture.nativeElement as HTMLElement;
      const labels = Array.from(root.querySelectorAll('label')).map((l) => (l.textContent || '').trim());
      // Without TMDB data the manual inputs must still render so the
      // user can edit by hand.
      expect(labels).toContain('Episode title');
      expect(labels).toContain('Season');
      expect(labels).toContain('Episode');
    });
  });
});
