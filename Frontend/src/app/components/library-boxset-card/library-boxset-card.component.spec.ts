/**
 * library-boxset-card — Phase 3 spec (#500).
 * Covers view ↔ edit toggle, PATCH wiring via updateBoxset, delete
 * confirmation, and nested-release expand. Nested release card events
 * forward through.
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { of, throwError } from 'rxjs';

import { LibraryBoxsetCardComponent } from './library-boxset-card.component';
import {
  MetadataService,
  BoxsetSummary,
  ReleaseSummary,
  DiscSummary,
} from '../../services/metadata.service';
import { ToastService } from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';

function makeBoxset(over: Partial<BoxsetSummary> = {}): BoxsetSummary {
  return {
    id: over.id ?? 'bs-1',
    slug: over.slug ?? 'bs-1',
    name: over.name ?? 'Test Boxset',
    year: over.year ?? 2020,
    release_count: over.release_count ?? 2,
    disc_count: over.disc_count ?? 4,
    ...over,
  } as BoxsetSummary;
}

describe('LibraryBoxsetCardComponent (Phase 3)', () => {
  let fixture: ComponentFixture<LibraryBoxsetCardComponent>;
  let component: LibraryBoxsetCardComponent;
  let metadataSpy: jasmine.SpyObj<MetadataService>;
  let toastSpy: jasmine.SpyObj<ToastService>;

  beforeEach(async () => {
    metadataSpy = jasmine.createSpyObj('MetadataService', [
      'updateBoxset',
      'deleteBoxset',
      'updateRelease',
      'deleteRelease',
    ]);
    toastSpy = jasmine.createSpyObj('ToastService', ['show']);
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);

    await TestBed.configureTestingModule({
      imports: [LibraryBoxsetCardComponent, HttpClientTestingModule],
      providers: [
        { provide: MetadataService, useValue: metadataSpy },
        { provide: ToastService, useValue: toastSpy },
        { provide: LoggerService, useValue: loggerSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(LibraryBoxsetCardComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('boxset', makeBoxset());
    fixture.detectChanges();
  });

  it('renders boxset name + year + meta in view mode', () => {
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('Test Boxset (2020)');
    expect(root.textContent).toContain('2 releases');
    expect(root.textContent).toContain('4 discs');
  });

  describe('#649: posterUrl uses boxset cover, never a nested movie poster', () => {
    it('renders boxset.cover_front_url when set', () => {
      fixture.componentRef.setInput('boxset', makeBoxset({
        cover_front_url: 'https://example.com/hp-collection.jpg',
      }));
      fixture.componentRef.setInput('releases', [
        {
          id: 'r-1', slug: 'r-1', name: 'HP1', type: 'movie',
          movie: { id: 'm-1', name: 'HP1', cover_url: 'https://image.tmdb.org/t/p/w500/hp1.jpg' },
        } as any,
      ]);
      fixture.detectChanges();
      expect(component.posterUrl).toBe('https://example.com/hp-collection.jpg');
    });

    it('does NOT fall through to a nested release-movie poster — boxsets are collections, not single titles', () => {
      fixture.componentRef.setInput('boxset', makeBoxset({ cover_front_url: null }));
      fixture.componentRef.setInput('releases', [
        {
          id: 'r-1', slug: 'r-1', name: 'HP1', type: 'movie',
          movie: { id: 'm-1', name: 'HP1', cover_url: 'https://image.tmdb.org/t/p/w500/hp1.jpg' },
        } as any,
      ]);
      fixture.detectChanges();
      expect(component.posterUrl).toBeNull();
    });

    it('returns null when boxset cover is null', () => {
      fixture.componentRef.setInput('boxset', makeBoxset({ cover_front_url: null }));
      fixture.componentRef.setInput('releases', []);
      fixture.detectChanges();
      expect(component.posterUrl).toBeNull();
    });
  });

  it('saveEdit PATCHes only changed fields and emits updated', () => {
    component.startEdit();
    component.editForm.name = 'Renamed Boxset';
    const updated = makeBoxset({ name: 'Renamed Boxset' });
    metadataSpy.updateBoxset.and.returnValue(of(updated));

    let emitted: BoxsetSummary | null = null;
    component.updated.subscribe((b) => (emitted = b));
    component.saveEdit();

    expect(metadataSpy.updateBoxset).toHaveBeenCalledWith('bs-1', {
      name: 'Renamed Boxset',
    });
    expect(component.editing).toBe(false);
    expect(emitted!.name).toBe('Renamed Boxset');
  });

  it('saveEdit with no changes exits edit mode without calling backend', () => {
    component.startEdit();
    component.saveEdit();
    expect(metadataSpy.updateBoxset).not.toHaveBeenCalled();
    expect(component.editing).toBe(false);
  });

  it('saveEdit surfaces backend errors on the card without exiting edit mode', () => {
    component.startEdit();
    component.editForm.name = 'X';
    metadataSpy.updateBoxset.and.returnValue(throwError(() => ({
      error: { detail: 'Boxset is finalized' },
    })));
    component.saveEdit();
    expect(component.editing).toBe(true);
    expect(component.saveError).toContain('finalized');
  });

  it('confirmDelete + nested-release pass-throughs work', () => {
    spyOn(window, 'confirm').and.returnValue(true);
    metadataSpy.deleteBoxset.and.returnValue(of({} as any));
    let deleted: BoxsetSummary | null = null;
    component.deleted.subscribe((b) => (deleted = b));
    component.confirmDelete();
    expect(metadataSpy.deleteBoxset).toHaveBeenCalledWith('bs-1');
    expect(deleted!.id).toBe('bs-1');

    // Pass-through helpers forward the events
    let relUpdated: ReleaseSummary | null = null;
    let relDeleted: ReleaseSummary | null = null;
    let discOpened: DiscSummary | null = null;
    component.releaseUpdated.subscribe((r) => (relUpdated = r));
    component.releaseDeleted.subscribe((r) => (relDeleted = r));
    component.discOpen.subscribe((d) => (discOpened = d));

    component.onReleaseUpdated({ id: 'r-1' } as any);
    component.onReleaseDeleted({ id: 'r-1' } as any);
    component.onDiscOpen({ id: 'd-1' } as any);

    expect((relUpdated as any).id).toBe('r-1');
    expect((relDeleted as any).id).toBe('r-1');
    expect((discOpened as any).id).toBe('d-1');
  });
});
