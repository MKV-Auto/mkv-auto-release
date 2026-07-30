/**
 * LibraryPageComponent — Phase 2 skeleton tests (#500).
 *
 * Covers the two things Phase 2 actually delivers:
 *   1. The page wires up `MetadataService.getLibraryPage` and renders the
 *      results it gets back.
 *   2. The completed-rips filter excludes releases whose discs are still
 *      mid-pipeline (the "stuck pending" cohort from the production data
 *      lives on Ripper, not Library).
 *
 * Drawer / inline-edit / DiscDB-chip behaviour belongs to later phases.
 */
import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { of } from 'rxjs';

import { LibraryPageComponent } from './library-page.component';
import {
  MetadataService,
  LibraryPageResponse,
  ReleaseSummary,
  DiscSummary,
} from '../../services/metadata.service';
import { LoggerService } from '../../services/logger.service';

function makeRelease(over: Partial<ReleaseSummary> = {}): ReleaseSummary {
  return {
    id: over.id ?? 'rel-1',
    slug: over.slug ?? 'rel-1',
    type: over.type ?? 'movie',
    name: over.name ?? 'Test Release',
    production_year: over.production_year ?? 2024,
    boxset_id: over.boxset_id ?? null,
    ...over,
  } as ReleaseSummary;
}

function makeDisc(over: Partial<DiscSummary> = {}): DiscSummary {
  return {
    content_hash: over.content_hash ?? 'hash-1',
    label_present: over.label_present ?? false,
    finalized: over.finalized ?? false,
    ...over,
  } as DiscSummary;
}

function makePage(items: ReleaseSummary[], release_discs: Record<string, DiscSummary[]>): LibraryPageResponse {
  return {
    items,
    release_discs,
    boxsets: [],
    boxset_details: [],
    next_cursor: null,
    has_more: false,
  };
}

describe('LibraryPageComponent (Phase 2)', () => {
  let fixture: ComponentFixture<LibraryPageComponent>;
  let component: LibraryPageComponent;
  let metadataSpy: jasmine.SpyObj<MetadataService>;

  beforeEach(async () => {
    metadataSpy = jasmine.createSpyObj('MetadataService', ['getLibraryPage']);
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);

    await TestBed.configureTestingModule({
      imports: [LibraryPageComponent, HttpClientTestingModule],
      providers: [
        { provide: MetadataService, useValue: metadataSpy },
        { provide: LoggerService, useValue: loggerSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(LibraryPageComponent);
    component = fixture.componentInstance;
  });

  it('renders releases whose discs are completed (finalized or transferred)', () => {
    const releases = [
      makeRelease({ id: 'rel-completed-finalized', name: 'Finalized release' }),
      makeRelease({ id: 'rel-completed-transferred', name: 'Transferred release' }),
    ];
    const discs = {
      'rel-completed-finalized': [makeDisc({ id: 'd-1', finalized: true })],
      'rel-completed-transferred': [makeDisc({ id: 'd-2', transfer_state: 'completed' })],
    };
    metadataSpy.getLibraryPage.and.returnValue(of(makePage(releases, discs)));

    fixture.detectChanges();

    expect(component.releases.length).toBe(2);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('Finalized release');
    expect(root.textContent).toContain('Transferred release');
  });

  it('filters out releases whose discs are stuck pending (the Library is completed-only)', () => {
    const releases = [
      makeRelease({ id: 'rel-pending', name: 'Stuck pending release' }),
      makeRelease({ id: 'rel-done', name: 'Done release' }),
    ];
    const discs = {
      // Stuck-pending — like Midway/Joker were before the carousel fix.
      'rel-pending': [makeDisc({ id: 'd-3', transfer_state: 'pending', finalized: false })],
      'rel-done': [makeDisc({ id: 'd-4', finalized: true })],
    };
    metadataSpy.getLibraryPage.and.returnValue(of(makePage(releases, discs)));

    fixture.detectChanges();

    expect(component.releases.map((r) => r.name)).toEqual(['Done release']);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).not.toContain('Stuck pending release');
    expect(root.textContent).toContain('Done release');
  });

  it('filters out releases whose only discs are failed (no completed work to show)', () => {
    const releases = [
      makeRelease({ id: 'rel-failed', name: 'Failed-only release' }),
    ];
    const discs = {
      'rel-failed': [
        makeDisc({ id: 'd-5', transfer_state: 'failed', finalized: false }),
      ],
    };
    metadataSpy.getLibraryPage.and.returnValue(of(makePage(releases, discs)));

    fixture.detectChanges();

    expect(component.releases.length).toBe(0);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('Nothing here yet');
  });

  it('does NOT render Continue Workflow affordances (Library is done media only)', () => {
    const releases = [makeRelease({ id: 'rel-1', name: 'Done release' })];
    const discs = {
      'rel-1': [makeDisc({ id: 'd-1', finalized: true })],
    };
    metadataSpy.getLibraryPage.and.returnValue(of(makePage(releases, discs)));

    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).not.toContain('Continue Workflow');
  });

  it('tab counts reflect the post-filter view', () => {
    const releases = [
      makeRelease({ id: 'rel-movie', name: 'A Movie', type: 'movie' }),
      makeRelease({ id: 'rel-tv', name: 'A Series', type: 'series' }),
      makeRelease({ id: 'rel-pending', name: 'Pending', type: 'movie' }),
    ];
    const discs = {
      'rel-movie': [makeDisc({ id: 'd-1', finalized: true })],
      'rel-tv': [makeDisc({ id: 'd-2', transfer_state: 'completed' })],
      'rel-pending': [makeDisc({ id: 'd-3', transfer_state: 'pending' })],
    };
    metadataSpy.getLibraryPage.and.returnValue(of(makePage(releases, discs)));

    fixture.detectChanges();

    expect(component.tabCounts.all).toBe(2);
    expect(component.tabCounts.movies).toBe(1);
    expect(component.tabCounts.series).toBe(1);
  });

  // #500 Phase 6: drawer wiring close-out coverage.

  it('onDiscOpen sets drawerDisc + resolves the enclosing release', () => {
    const release = makeRelease({ id: 'rel-1' });
    metadataSpy.getLibraryPage.and.returnValue(of(makePage([release], {
      'rel-1': [makeDisc({ id: 'd-1', release_id: 'rel-1', finalized: true })],
    })));
    fixture.detectChanges();

    const disc = makeDisc({ id: 'd-1', release_id: 'rel-1' });
    component.onDiscOpen(disc);
    expect(component.drawerDisc?.id).toBe('d-1');
    expect(component.drawerRelease?.id).toBe('rel-1');
  });

  it('closeDrawer clears drawer state so the overlay unmounts', () => {
    component.drawerDisc = makeDisc({ id: 'd-1' });
    component.drawerRelease = makeRelease({ id: 'rel-1' });
    component.closeDrawer();
    expect(component.drawerDisc).toBeNull();
    expect(component.drawerRelease).toBeNull();
  });

  it('onDrawerDiscUpdated merges disc-level edits into releaseDiscs (cards stay in sync)', () => {
    component.releaseDiscs = {
      'rel-1': [
        { id: 'd-1', content_hash: 'h', label_present: true, finalized: false,
          disc_name: 'Old Name', format: 'Blu-Ray' } as any,
        { id: 'd-2', content_hash: 'h2', label_present: true, finalized: false,
          disc_name: 'Other', format: 'DVD' } as any,
      ],
    };
    component.drawerDisc = component.releaseDiscs['rel-1'][0];
    component.onDrawerDiscUpdated({ id: 'd-1', disc_name: 'New Name', format: 'UHD' });
    const updated = component.releaseDiscs['rel-1'][0];
    expect(updated.disc_name).toBe('New Name');
    expect(updated.format).toBe('UHD');
    // Sibling disc untouched.
    expect(component.releaseDiscs['rel-1'][1].disc_name).toBe('Other');
    // Drawer disc reference catches up too so the drawer reflects the
    // new state without a refetch.
    expect(component.drawerDisc?.disc_name).toBe('New Name');
  });
});

// ── #741: TheDiscDB contribution surface ─────────────────────────────────

import { SystemService } from '../../services/system.service';

describe('LibraryPageComponent (#741 contribution surface)', () => {
  let fixture: ComponentFixture<LibraryPageComponent>;
  let component: LibraryPageComponent;
  let sysSpy: jasmine.SpyObj<SystemService>;

  const page: LibraryPageResponse = {
    items: [
      makeRelease({ id: 'rel-a', name: 'Exportable' }),
      makeRelease({ id: 'rel-b', name: 'Contributed' }),
    ],
    release_discs: {
      'rel-a': [makeDisc({ id: 'd-a', content_hash: 'h-a', transfer_state: 'completed' })],
      'rel-b': [makeDisc({ id: 'd-b', content_hash: 'h-b', discdb_hit: true, transfer_state: 'completed' })],
    },
    boxsets: [],
    boxset_details: [],
    next_cursor: null,
    has_more: false,
  } as unknown as LibraryPageResponse;

  beforeEach(async () => {
    sysSpy = jasmine.createSpyObj<SystemService>('SystemService', [
      'getDiscDbEligible', 'startDiscDbExport', 'getDiscDbExportStatus',
      'cancelDiscDbExport', 'downloadDiscDbExport',
    ]);
    sysSpy.getDiscDbEligible.and.returnValue(of({ count: 1, disc_ids: ['d-a'], update_disc_ids: [], new_count: 1, update_count: 0 }));

    const metaSpy = jasmine.createSpyObj<MetadataService>('MetadataService', ['getLibraryPage']);
    metaSpy.getLibraryPage.and.returnValue(of(page));

    await TestBed.configureTestingModule({
      imports: [LibraryPageComponent, HttpClientTestingModule],
      providers: [
        { provide: MetadataService, useValue: metaSpy },
        { provide: SystemService, useValue: sysSpy },
        { provide: LoggerService, useValue: jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']) },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(LibraryPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('builds the strip from the same endpoint the export uses', () => {
    // The count shown must be what "Export all" will act on — no client-side
    // guessing from discdb_hit, which cannot see the finished-job rule.
    expect(component.eligibleCount).toBe(1);
    expect(component.eligibleDiscIds.has('d-a')).toBe(true);
    const strip = fixture.nativeElement.querySelector('.library-contrib-strip');
    expect(strip?.textContent).toContain("1 disc isn't in TheDiscDB yet");
  });

  it('the contribute tab shows only entries with something to export', () => {
    component.selectTab('contribute');
    const names = component.visibleReleases.map(r => r.name);
    expect(names).toEqual(['Exportable']);
  });

  it('a card export request starts a scoped job', () => {
    sysSpy.startDiscDbExport.and.returnValue(of({
      job_id: 'j1', status: 'running', done: 0, total: 1, current: '',
      error: null, included: 0, skipped: 0, cancelled: false, download_ready: false,
    } as any));
    sysSpy.getDiscDbExportStatus.and.returnValue(of({
      job_id: 'j1', status: 'running', done: 0, total: 1, current: '',
      error: null, included: 0, skipped: 0, cancelled: false, download_ready: false,
    } as any));

    component.startExport(['d-a']);

    expect(sysSpy.startDiscDbExport).toHaveBeenCalledWith(['d-a']);
    expect(component.exportJob?.job_id).toBe('j1');
    component.ngOnDestroy();
  });

  it('a second export while one runs is ignored, not stacked', () => {
    component.exportJob = { job_id: 'j1', status: 'running' } as any;
    component.startExport(['d-a']);
    expect(sysSpy.startDiscDbExport).not.toHaveBeenCalled();
  });

  it('leaving the contribute tab when it empties returns to All', () => {
    component.selectTab('contribute');
    sysSpy.getDiscDbEligible.and.returnValue(of({ count: 0, disc_ids: [], update_disc_ids: [], new_count: 0, update_count: 0 }));
    (component as any).loadEligible();
    expect(component.activeTab).toBe('all');
    expect(component.eligibleCount).toBe(0);
  });

  it('a finished export with updates raises the replaces dialog', () => {
    // Nobody should learn their zip overwrites upstream files from a
    // surprise git diff — the page says so the moment the download lands.
    sysSpy.downloadDiscDbExport.and.returnValue(of({
      blob: new Blob(['zip']), filename: 'thediscdb-submissions.zip',
    }));
    const job = {
      job_id: 'j1', status: 'completed', included: 1, skipped: 0,
      updates: [{
        target: 'data/movie/Predators (2010)/predator-4-movie-collection-4k',
        files: ['disc02.json', 'disc02-summary.txt'],
        subject: 'Update Predators (2010)/predator-4-movie-collection-4k disc02',
        changes: ['Name: "Predators Blu-ray" -> "Corrected"'],
      }],
    } as any;

    (component as any).downloadExport(job);
    fixture.detectChanges();

    expect(component.exportUpdates?.length).toBe(1);
    const dialog = fixture.nativeElement.querySelector('.library-export-updates');
    expect(dialog?.textContent).toContain('predator-4-movie-collection-4k');
    expect(dialog?.textContent).toContain('replaces disc02.json, disc02-summary.txt');
    expect(dialog?.textContent).toContain('Name: "Predators Blu-ray" -> "Corrected"');

    component.dismissExportUpdates();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.library-export-updates')).toBeNull();
  });

  it('a finished export with only new entries shows no dialog', () => {
    sysSpy.downloadDiscDbExport.and.returnValue(of({
      blob: new Blob(['zip']), filename: 'thediscdb-submissions.zip',
    }));
    (component as any).downloadExport({
      job_id: 'j1', status: 'completed', included: 2, skipped: 0, updates: [],
    } as any);
    expect(component.exportUpdates).toBeNull();
  });

  it('the dialog commit message matches the README shape', () => {
    // Attribution, what gets replaced, and every correction with its prior
    // value — continuation lines (leading spaces) nest under their bullet.
    const text = component.commitMessageFor({
      target: 'data/movie/Predators (2010)/x', files: ['disc02.json'],
      subject: 'Update Predators (2010)/x disc02',
      changes: [
        'Name: "a" -> "b"',
        '2 title comments corrected:',
        '  title 0: "old.mkv" -> "new.mkv"',
        '  title 1: "old1.mkv" -> "new1.mkv"',
      ],
    });
    expect(text).toBe(
      'Update Predators (2010)/x disc02\n\n' +
      'Update provided by MKV-Auto (https://github.com/MKV-Auto/mkv-auto-release)\n\n' +
      'Replacing: data/movie/Predators (2010)/x\n' +
      '  disc02.json\n\n' +
      'Corrections:\n' +
      '  - Name: "a" -> "b"\n' +
      '  - 2 title comments corrected:\n' +
      '      title 0: "old.mkv" -> "new.mkv"\n' +
      '      title 1: "old1.mkv" -> "new1.mkv"'
    );
  });

  it('the copy button puts the commit message on the clipboard', fakeAsync(() => {
    const writeText = jasmine.createSpy('writeText').and.returnValue(Promise.resolve());
    // navigator.clipboard is a readonly accessor in headless Chrome — swap it
    // via defineProperty, the same seam-level mocking localStorage needs.
    const original = Object.getOwnPropertyDescriptor(Navigator.prototype, 'clipboard');
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText }, configurable: true,
    });
    try {
      const update = {
        target: 't', files: ['disc02.json'],
        subject: 'Update t disc02', changes: [],
      };
      component.copyCommitMessage(update);
      tick();
      expect(writeText).toHaveBeenCalledWith(component.commitMessageFor(update));
      expect(component.copiedCommitTarget).toBe('t');
      tick(2000);
      expect(component.copiedCommitTarget).toBeNull();
    } finally {
      delete (navigator as any).clipboard;
      if (original) Object.defineProperty(Navigator.prototype, 'clipboard', original);
    }
  }));
});
