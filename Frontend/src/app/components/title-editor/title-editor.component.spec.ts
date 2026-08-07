import { ComponentFixture, TestBed, fakeAsync, tick, flush } from '@angular/core/testing';
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

    // #798 reverses #602. #602 hid the manual title / season / episode
    // inputs once TMDB had the catalog, on the theory that the picker
    // covered all three. It does not: a disc can split one TMDB episode
    // across two files (#796, "Steps Into Shadow"), and with the inputs
    // hidden there is no way to correct it. The fields now always render
    // for an Episode row.
    it('#798 — keeps title / season / episode editable on Episode rows even with a TMDB catalog', async () => {
      const workflow = TestBed.inject(WorkflowService);
      seedSeriesCatalog(workflow);
      fixture.componentRef.setInput('title', makeTitle({ type: 'episode', season: 1 }));
      fixture.componentRef.setInput('isSeries', true);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();

      const root = fixture.nativeElement as HTMLElement;
      // Picker present (sanity) — it is still the fast path.
      expect(root.querySelector('select[aria-label="TMDB episode picker"]')).toBeTruthy();
      // ...and the manual inputs sit alongside it rather than being replaced.
      const labels = Array.from(root.querySelectorAll('label')).map((l) => (l.textContent || '').trim());
      expect(labels).toContain('Episode title');
      expect(labels).toContain('Season');
      expect(labels).toContain('Episode');
    });

    it('#798 — keeps the inputs visible when TMDB returns no catalog (movie disc / no TMDB hit)', () => {
      // Don't seed — workflow context stays empty, so episodeOptions$
      // emits 'unavailable' / never emits.
      fixture.componentRef.setInput('title', makeTitle({ type: 'episode', season: 1 }));
      fixture.componentRef.setInput('isSeries', true);
      fixture.detectChanges();

      const root = fixture.nativeElement as HTMLElement;
      const labels = Array.from(root.querySelectorAll('label')).map((l) => (l.textContent || '').trim());
      expect(labels).toContain('Episode title');
      expect(labels).toContain('Season');
      expect(labels).toContain('Episode');
    });

    // The complaint that motivated #798: on a series disc an extra could
    // not be named, because the form keyed on the disc rather than the row.
    it('#798 — an extra on a series disc gets a name field and no episode picker', async () => {
      const workflow = TestBed.inject(WorkflowService);
      seedSeriesCatalog(workflow);
      fixture.componentRef.setInput('title', makeTitle({ type: 'Featurette', season: null }));
      fixture.componentRef.setInput('isSeries', true);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();

      const root = fixture.nativeElement as HTMLElement;
      expect(root.querySelector('select[aria-label="TMDB episode picker"]')).toBeNull();
      const labels = Array.from(root.querySelectorAll('label')).map((l) => (l.textContent || '').trim());
      expect(labels).toContain('Title name');
      expect(labels).not.toContain('Season');
      expect(labels).not.toContain('Episode');
    });
  });

  describe('typed fields write on blur, not per keystroke', () => {
    /** The reported symptom: "I type, then it refetches, resets, and slowly
     *  re-types everything I just typed, sometimes losing the last couple of
     *  characters." Cause: every ngModelChange issued a PATCH, and each
     *  response echoed the server value back into the ngModel-bound input,
     *  so late echoes replayed the field character by character. */
    const bindTitle = () => {
      component.title = { title_id: 'tid-1', title: 'Original', type: null } as any;
      fixture.detectChanges();
    };

    it('saves after an idle pause even if blur NEVER fires', fakeAsync(() => {
      // The regression this replaces: writes happened only on blur, so if
      // focus never left the field the edit was silently lost and the next
      // refresh showed the last value that did save.
      bindTitle();
      const spy = spyOn(component.titlePatched, 'emit');
      component.onTitleNameChange('Typed but never blurred');

      tick(699);
      expect(spy).not.toHaveBeenCalled();   // still coalescing
      tick(1);
      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith(
        jasmine.objectContaining({ title: 'Typed but never blurred' }));
      flush();
    }));

    it('a typing burst still collapses to ONE write, not one per keystroke', fakeAsync(() => {
      bindTitle();
      const spy = spyOn(component.titlePatched, 'emit');
      const text = 'Behind The Scenes';
      for (let i = 1; i <= text.length; i++) {
        component.onTitleNameChange(text.slice(0, i));
        tick(50);                            // fast typing, under the idle window
      }
      expect(spy).not.toHaveBeenCalled();
      tick(700);
      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith(jasmine.objectContaining({ title: text }));
      flush();
    }));

    it('blur still saves immediately, without waiting out the idle timer', fakeAsync(() => {
      bindTitle();
      const spy = spyOn(component.titlePatched, 'emit');
      component.onTitleNameChange('Quick');
      component.flushPendingFieldEdits();
      expect(spy).toHaveBeenCalledTimes(1);
      tick(1000);
      expect(spy).toHaveBeenCalledTimes(1);   // timer did not double-write
      flush();
    }));

    it('typing a whole word issues ZERO writes', () => {
      bindTitle();
      const spy = spyOn(component.titlePatched, 'emit');
      const text = 'Behind The Scenes';
      for (let i = 1; i <= text.length; i++) {
        component.onTitleNameChange(text.slice(0, i));
      }
      expect(spy).not.toHaveBeenCalled();
    });

    it('blur writes once, with the final value', () => {
      bindTitle();
      const text = 'Behind The Scenes';
      for (let i = 1; i <= text.length; i++) component.onTitleNameChange(text.slice(0, i));

      const spy = spyOn(component.titlePatched, 'emit');
      component.flushPendingFieldEdits();

      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith(
        jasmine.objectContaining({ title_id: 'tid-1', title: 'Behind The Scenes' }));
    });

    it('no keystroke is lost — the last character always makes it', () => {
      bindTitle();
      component.onTitleNameChange('abc');
      component.onTitleNameChange('abcd');   // final keystroke
      const spy = spyOn(component.titlePatched, 'emit');
      component.flushPendingFieldEdits();
      expect(spy).toHaveBeenCalledWith(jasmine.objectContaining({ title: 'abcd' }));
    });

    it('blurring an untouched field costs no request', () => {
      bindTitle();
      const spy = spyOn(component.titlePatched, 'emit');
      component.flushPendingFieldEdits();
      component.flushPendingFieldEdits();
      expect(spy).not.toHaveBeenCalled();
    });

    it('several typed fields coalesce into one write', () => {
      bindTitle();
      component.onTitleNameChange('A Name');
      component.onEditionChange('Extended');
      component.onSeasonChange(2);
      const spy = spyOn(component.titlePatched, 'emit');
      component.flushPendingFieldEdits();
      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith(jasmine.objectContaining({
        title_id: 'tid-1', title: 'A Name', edition: 'Extended', season: 2,
      }));
    });

    it('changing the type carries the pending name in the SAME write', () => {
      // One gesture, one request. Flushing the buffered name as a separate
      // write raced the type write — both left with the same base_seq, so
      // one of them always lost (measured on rc.3: 7/7 same-tick pairs
      // collided). Users don't wait for autosave; the next action must
      // carry the pending edit with it.
      bindTitle();
      component.onTitleNameChange('Renamed');
      const seen: any[] = [];
      spyOn(component.titlePatched, 'emit').and.callFake((p: any) => { seen.push(p); return true as any; });
      component.onTypeChange('Featurette');
      expect(seen.length).toBe(1);
      expect(seen[0]).toEqual(jasmine.objectContaining({
        title_id: 'tid-1', title: 'Renamed', type: 'Featurette',
      }));
    });

    it('a pending name never flushes late after the type write consumed it', () => {
      bindTitle();
      component.onTitleNameChange('Renamed');
      component.onTypeChange('Featurette');
      const spy = spyOn(component.titlePatched, 'emit');
      component.flushPendingFieldEdits();
      expect(spy).not.toHaveBeenCalled();
    });

    it('ignoring with a pending name clears it in the same write (no resurrection)', () => {
      bindTitle();
      component.onTitleNameChange('Half Typed');
      const seen: any[] = [];
      spyOn(component.titlePatched, 'emit').and.callFake((p: any) => { seen.push(p); return true as any; });
      component.markAsIgnore();
      expect(seen.length).toBe(1);
      expect(seen[0]).toEqual(jasmine.objectContaining({ type: 'ignore', title: null }));
      component.flushPendingFieldEdits();
      expect(seen.length).toBe(1); // nothing buffered survived to flush later
    });

    it('teardown mid-edit still saves', () => {
      bindTitle();
      component.onTitleNameChange('Unsaved');
      const spy = spyOn(component.titlePatched, 'emit');
      component.ngOnDestroy();
      expect(spy).toHaveBeenCalledWith(jasmine.objectContaining({ title: 'Unsaved' }));
    });
  });

  describe('multi-part episode layout (#796)', () => {
    function seed(extra: Record<string, any> = {}) {
      fixture.componentRef.setInput('title', makeTitle({
        type: 'episode', title: 'Steps Into Shadow', season: 3, episode: 1, ...extra,
      }));
      fixture.detectChanges();
    }

    it('derives layout from the stored fields rather than tracking its own', () => {
      seed();
      expect(component.episodeLayout).toBe('single');
      seed({ part: 1, part_of: 2 });
      expect(component.episodeLayout).toBe('split');
      seed({ episode_end: 2 });
      expect(component.episodeLayout).toBe('span');
    });

    it('switching to split seeds part 1 of 2 and clears any range', () => {
      seed({ episode_end: 5 });
      const emitted: any[] = [];
      component.titlePatched.subscribe((p) => emitted.push(p));

      component.onEpisodeLayoutChange('split');

      expect(component.title.part).toBe(1);
      expect(component.title.part_of).toBe(2);
      expect(component.title.episode_end).toBeNull();
      // One write carrying all three, not three writes.
      expect(emitted.length).toBe(1);
      expect(emitted[0].episode_end).toBeNull();
    });

    it('switching back to single clears every layout field', () => {
      seed({ part: 2, part_of: 2 });
      const emitted: any[] = [];
      component.titlePatched.subscribe((p) => emitted.push(p));

      component.onEpisodeLayoutChange('single');

      expect(emitted[0].part).toBeNull();
      expect(emitted[0].part_of).toBeNull();
      expect(emitted[0].episode_end).toBeNull();
    });

    it('carries a buffered name edit in the layout write', () => {
      // Layout is picked, so it writes immediately. A name typed just before is
      // still buffered; two same-tick writes share a base_seq and one loses.
      seed();
      const emitted: any[] = [];
      component.titlePatched.subscribe((p) => emitted.push(p));

      component.onTitleNameChange('Steps Into Shadow (corrected)');
      component.onEpisodeLayoutChange('split');

      expect(emitted.length).toBe(1);
      expect(emitted[0].title).toBe('Steps Into Shadow (corrected)');
      expect(emitted[0].part).toBe(1);
    });

    it('previews the stacking suffix and the range', () => {
      seed({ part: 1, part_of: 2 });
      expect(component.getFilenamePreview()).toBe('S03E01 - Steps Into Shadow - part1.mkv');
      seed({ episode_end: 2 });
      expect(component.getFilenamePreview()).toBe('S03E01-E02 - Steps Into Shadow.mkv');
      seed();
      expect(component.getFilenamePreview()).toBe('S03E01 - Steps Into Shadow.mkv');
    });
  });
});
