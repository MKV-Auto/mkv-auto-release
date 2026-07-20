import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SimpleChange } from '@angular/core';
import { of } from 'rxjs';
import { OverlayModule } from '@angular/cdk/overlay';
import { ReleaseSelectorComponent } from './release-selector.component';
import { MetadataService } from '../../services/metadata.service';
import { MobileService } from '../../services/mobile.service';
import { LoggerService } from '../../services/logger.service';
import { ToastService } from '../../services/toast.service';

describe('ReleaseSelectorComponent', () => {
  let component: ReleaseSelectorComponent;
  let fixture: ComponentFixture<ReleaseSelectorComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ReleaseSelectorComponent, OverlayModule],
      providers: [
        { provide: MetadataService, useValue: { listReleases: () => of([]) } },
        { provide: MobileService, useValue: { isMobile$: of(false) } },
        { provide: LoggerService, useValue: { error: () => {} } },
        { provide: ToastService, useValue: { show: () => {} } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(ReleaseSelectorComponent);
    component = fixture.componentInstance;
    component.releaseOptions = [{ id: 'r1', name: 'R1', slug: 'r1' }] as any;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onReleaseCleared emits releaseCleared', () => {
    let emitted = false;
    component.releaseCleared.subscribe(() => (emitted = true));
    component.onReleaseCleared();
    expect(emitted).toBe(true);
  });

  it('_validateReleaseYear validates year range when provided', () => {
    expect(component._validateReleaseYear(2020)).toBe(true);
    expect(component._validateReleaseYear(999)).toBe(false);
    expect(component._validateReleaseYear(10000)).toBe(false);
    expect(component._validateReleaseYear(null)).toBe(true); // optional
  });

  it('pendingReleaseEditionPrefill is set when selecting an incomplete release', () => {
    const release = {
      id: 'r1',
      name: 'Edition',
      slug: 'ed',
      release_link_ready: false,
      release_year: 2022,
      upc: '123456789012',
      asin: 'B00TEST',
      cover_front_url: 'https://example.com/f.jpg',
      cover_back_url: 'https://example.com/b.jpg',
    } as any;
    component.isOpen = true;
    component.selectRelease(release);
    expect(component.pendingReleaseEditionPrefill).toEqual(
      jasmine.objectContaining({
        name: 'Edition',
        year: 2022,
        upc: '123456789012',
        asin: 'B00TEST',
        cover_front_url: 'https://example.com/f.jpg',
        cover_back_url: 'https://example.com/b.jpg',
      })
    );
  });

  it('releaseMetaLine surfaces resolution so 4K vs 1080p releases are distinguishable', () => {
    // Before this, a 4K release and a Blu-ray of the same title differed only by slug.
    expect(
      component.releaseMetaLine({ production_year: 2018, resolution: '2160p', slug: 'predator-2-4k-2018' } as any)
    ).toBe('2018 • 2160p • predator-2-4k-2018');
    // Blanks are omitted — no leading/double separators.
    expect(component.releaseMetaLine({ production_year: 2001, slug: 'hp-bluray-2001' } as any)).toBe(
      '2001 • hp-bluray-2001'
    );
    expect(component.releaseMetaLine({ resolution: '1080p' } as any)).toBe('1080p');
    expect(component.releaseMetaLine({} as any)).toBe('');
  });

  describe('#685: create form survives a rejected submit', () => {
    function pushCreateResult(result: { ok: boolean; error?: string; token: number }) {
      component.createResult = result;
      component.ngOnChanges({
        createResult: new SimpleChange(null, result, false),
      });
    }

    it('submit keeps the panel open in a saving state (no optimistic close)', () => {
      component.isOpen = true;
      component.showCreateForm = true;
      component.onReleaseEditionCreate({
        name: 'The Matrix 4K', year: 2021, upc: '123456789012', asin: '', cover_front_url: 'https://x/f.jpg', cover_back_url: '',
      } as any);
      expect(component.isOpen).toBe(true);
      expect(component.showCreateForm).toBe(true);
      expect(component.createSaving).toBe(true);
    });

    it('a failed create surfaces the error inline and keeps the form (values intact)', () => {
      component.isOpen = true;
      component.showCreateForm = true;
      component.createSaving = true;
      pushCreateResult({ ok: false, error: 'Release already exists', token: 1 });
      expect(component.isOpen).toBe(true);
      expect(component.showCreateForm).toBe(true);
      expect(component.createErrors).toEqual(['Release already exists']);
      expect(component.createSaving).toBe(false);
    });

    it('a successful create closes the panel and clears errors', () => {
      component.isOpen = true;
      component.showCreateForm = true;
      component.createSaving = true;
      pushCreateResult({ ok: true, token: 2 });
      expect(component.isOpen).toBe(false);
      expect(component.showCreateForm).toBe(false);
      expect(component.createErrors).toEqual([]);
    });
  });

  describe('#685: panel survives window/tab refocus', () => {
    const outside = () => document.body;

    it('the click that refocuses the window does not dismiss the panel', () => {
      component.isOpen = true;
      (component as any).isMobile = false;
      component.onWindowFocus();
      component.onDocumentClick({ target: outside() } as unknown as Event);
      expect(component.isOpen).toBe(true);
    });

    it('a click with no in-page mousedown (synthetic/refocus) does not dismiss', () => {
      component.isOpen = true;
      (component as any).isMobile = false;
      (component as any).windowFocusedAt = 0;
      component.onDocumentClick({ target: outside() } as unknown as Event);
      expect(component.isOpen).toBe(true);
    });

    it('a genuine outside gesture (mousedown + click outside) still dismisses', () => {
      component.isOpen = true;
      (component as any).isMobile = false;
      (component as any).windowFocusedAt = 0;
      component.onDocumentMouseDown({ target: outside() } as unknown as Event);
      component.onDocumentClick({ target: outside() } as unknown as Event);
      expect(component.isOpen).toBe(false);
    });
  });
});
