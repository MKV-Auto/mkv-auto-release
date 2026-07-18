/**
 * library-release-card — Phase 3 spec (#500).
 * Covers view ↔ edit toggle, PATCH wiring, delete confirmation, and the
 * disc-disclosure expand. Drawer-open emission is mocked because the
 * drawer lands in Phase 4.
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { of, throwError } from 'rxjs';

import { LibraryReleaseCardComponent } from './library-release-card.component';
import {
  MetadataService,
  ReleaseSummary,
  DiscSummary,
} from '../../services/metadata.service';
import { ToastService } from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';

function makeRelease(over: Partial<ReleaseSummary> = {}): ReleaseSummary {
  return {
    id: over.id ?? 'rel-1',
    slug: over.slug ?? 'rel-1',
    type: over.type ?? 'movie',
    name: over.name ?? 'Test Movie',
    production_year: over.production_year ?? 2024,
    boxset_id: over.boxset_id ?? null,
    ...over,
  } as ReleaseSummary;
}

function makeDisc(over: Partial<DiscSummary> = {}): DiscSummary {
  return {
    id: over.id ?? 'd-1',
    content_hash: over.content_hash ?? 'h-1',
    label_present: over.label_present ?? false,
    finalized: over.finalized ?? false,
    ...over,
  } as DiscSummary;
}

describe('LibraryReleaseCardComponent (Phase 3)', () => {
  let fixture: ComponentFixture<LibraryReleaseCardComponent>;
  let component: LibraryReleaseCardComponent;
  let metadataSpy: jasmine.SpyObj<MetadataService>;
  let toastSpy: jasmine.SpyObj<ToastService>;

  beforeEach(async () => {
    metadataSpy = jasmine.createSpyObj('MetadataService', ['updateRelease', 'deleteRelease']);
    toastSpy = jasmine.createSpyObj('ToastService', ['show']);
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);

    await TestBed.configureTestingModule({
      imports: [LibraryReleaseCardComponent, HttpClientTestingModule],
      providers: [
        { provide: MetadataService, useValue: metadataSpy },
        { provide: ToastService, useValue: toastSpy },
        { provide: LoggerService, useValue: loggerSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(LibraryReleaseCardComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('release', makeRelease());
    fixture.componentRef.setInput('discs', [makeDisc()]);
    fixture.detectChanges();
  });

  it('renders the release name + year + meta in view mode', () => {
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('Test Movie (2024)');
    expect(root.textContent).toContain('1 disc');
  });

  describe('#647: posterUrl cascade', () => {
    it('prefers movie.cover_url over release.cover_front_url', () => {
      const rel = makeRelease({
        cover_front_url: 'https://example.com/release.jpg',
        movie: {
          id: 'm-1',
          name: 'Test Movie',
          cover_url: 'https://image.tmdb.org/t/p/w500/tmdb.jpg',
        },
      } as any);
      fixture.componentRef.setInput('release', rel);
      fixture.detectChanges();
      expect(component.posterUrl).toBe('https://image.tmdb.org/t/p/w500/tmdb.jpg');
    });

    it('falls back to release.cover_front_url when movie has no cover_url', () => {
      const rel = makeRelease({
        cover_front_url: 'https://example.com/release.jpg',
        movie: { id: 'm-1', name: 'Test Movie', cover_url: null },
      } as any);
      fixture.componentRef.setInput('release', rel);
      fixture.detectChanges();
      expect(component.posterUrl).toBe('https://example.com/release.jpg');
    });

    it('returns null when neither movie nor release cover is set', () => {
      const rel = makeRelease({
        cover_front_url: null,
        movie: { id: 'm-1', name: 'Test Movie', cover_url: null },
      } as any);
      fixture.componentRef.setInput('release', rel);
      fixture.detectChanges();
      expect(component.posterUrl).toBeNull();
    });
  });

  it('startEdit / cancelEdit toggle editing without persisting', () => {
    expect(component.editing).toBe(false);
    component.startEdit();
    expect(component.editing).toBe(true);
    expect(component.editForm.release_name).toBe('Test Movie');
    component.cancelEdit();
    expect(component.editing).toBe(false);
  });

  it('startEdit is blocked on finalized releases', () => {
    fixture.componentRef.setInput('release', makeRelease({ finalize_state: 'completed' } as any));
    fixture.detectChanges();
    component.startEdit();
    expect(component.editing).toBe(false);
  });

  it('saveEdit PATCHes only changed fields and emits updated', () => {
    component.startEdit();
    component.editForm.release_name = 'Renamed Movie';
    component.editForm.release_year = 2025;
    const updated = makeRelease({ name: 'Renamed Movie', production_year: 2025 });
    metadataSpy.updateRelease.and.returnValue(of(updated));

    let emitted: ReleaseSummary | null = null;
    component.updated.subscribe((r) => (emitted = r));
    component.saveEdit();

    expect(metadataSpy.updateRelease).toHaveBeenCalledWith('rel-1', {
      release_name: 'Renamed Movie',
      release_year: 2025,
    });
    expect(component.editing).toBe(false);
    expect(component.saving).toBe(false);
    expect(emitted!.name).toBe('Renamed Movie');
  });

  it('saveEdit with no changes exits edit mode without calling backend', () => {
    component.startEdit();
    component.saveEdit();
    expect(metadataSpy.updateRelease).not.toHaveBeenCalled();
    expect(component.editing).toBe(false);
  });

  it('saveEdit surfaces backend errors on the card without exiting edit mode', () => {
    component.startEdit();
    component.editForm.release_name = 'X';
    metadataSpy.updateRelease.and.returnValue(throwError(() => ({
      error: { detail: 'Cannot rename — release is finalized' },
    })));
    component.saveEdit();
    expect(component.editing).toBe(true);
    expect(component.saving).toBe(false);
    expect(component.saveError).toContain('finalized');
  });

  it('confirmDelete calls deleteRelease + emits deleted on confirm', () => {
    spyOn(window, 'confirm').and.returnValue(true);
    metadataSpy.deleteRelease.and.returnValue(of({} as any));
    let deleted: ReleaseSummary | null = null;
    component.deleted.subscribe((r) => (deleted = r));
    component.confirmDelete();
    expect(metadataSpy.deleteRelease).toHaveBeenCalledWith('rel-1');
    expect(deleted!.id).toBe('rel-1');
    expect(toastSpy.show).toHaveBeenCalledWith(jasmine.stringMatching(/Deleted/), 'success', jasmine.any(Number));
  });

  it('confirmDelete does nothing when user cancels the prompt', () => {
    spyOn(window, 'confirm').and.returnValue(false);
    component.confirmDelete();
    expect(metadataSpy.deleteRelease).not.toHaveBeenCalled();
  });

  it('toggleExpanded flips expanded state', () => {
    // OnPush + imperative state-flip from a unit test won't trigger CD
    // through the template — we only assert the component-side toggle.
    // DOM presence of `.library-release-card__discs` is verified via the
    // Playwright e2e in Phase 6.
    expect(component.expanded).toBe(false);
    component.toggleExpanded();
    expect(component.expanded).toBe(true);
    component.toggleExpanded();
    expect(component.expanded).toBe(false);
  });

  it('emits discOpen when a disc row is activated (Phase 4 drawer hook)', () => {
    component.expanded = true;
    fixture.detectChanges();
    let opened: DiscSummary | null = null;
    component.discOpen.subscribe((d) => (opened = d));
    component.openDisc({ id: 'd-1' } as any);
    expect(opened!.id).toBe('d-1');
  });
});
