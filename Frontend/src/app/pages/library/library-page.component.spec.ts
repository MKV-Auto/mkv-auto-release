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
import { ComponentFixture, TestBed } from '@angular/core/testing';
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
