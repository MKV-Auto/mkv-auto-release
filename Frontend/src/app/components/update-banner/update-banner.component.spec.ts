import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { UpdateBannerComponent } from './update-banner.component';
import { SystemService, UpdateStatus } from '../../services/system.service';
import { LoggerService } from '../../services/logger.service';

describe('UpdateBannerComponent (#699)', () => {
  let component: UpdateBannerComponent;
  let fixture: ComponentFixture<UpdateBannerComponent>;
  let systemSvc: jasmine.SpyObj<SystemService>;
  const KEY = UpdateBannerComponent.DISMISSED_STORAGE_KEY;

  function status(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
    return {
      current_version: '1.0.1',
      latest_version: '1.0.2',
      update_available: true,
      release_url: 'https://github.com/MKV-Auto/mkv-auto-release/releases/tag/v1.0.2',
      release_name: 'MKV-Auto 1.0.2',
      published_at: '2026-07-20T22:28:47Z',
      checked_at: '2026-07-21T00:00:00Z',
      ...overrides,
    };
  }

  /**
   * Control the persisted-dismissal the component reads, without touching real
   * localStorage. CI headless Chrome treats localStorage as no-op (writes don't
   * persist to reads), so any test asserting through real storage is
   * non-deterministic — and a GLOBAL Storage.prototype spy leaks into unrelated
   * code that reads storage during change detection. Spying the component's own
   * readDismissedVersion() seam is hermetic and side-effect-free.
   */
  const withDismissed = (v: string | null) =>
    spyOn(component as any, 'readDismissedVersion').and.returnValue(v);

  beforeEach(async () => {
    const systemSpy = jasmine.createSpyObj('SystemService', ['getUpdateStatus']);
    // Safe default so the refocus subscription can never hit an undefined observable.
    systemSpy.getUpdateStatus.and.returnValue(of(status()));
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error']);

    await TestBed.configureTestingModule({
      imports: [UpdateBannerComponent],
      providers: [
        { provide: SystemService, useValue: systemSpy },
        { provide: LoggerService, useValue: loggerSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(UpdateBannerComponent);
    component = fixture.componentInstance;
    systemSvc = TestBed.inject(SystemService) as jasmine.SpyObj<SystemService>;
  });

  // Guaranteed teardown even when an assertion throws mid-test — otherwise the
  // component's timer + visibilitychange subscriptions leak into later specs
  // (the cause of failures that "move" between tests across CI runs).
  afterEach(() => {
    component?.ngOnDestroy();
  });

  // ── Core decision logic (deterministic: no timer, no DOM, no real storage) ──
  describe('evaluate()', () => {
    const evaluate = (s: UpdateStatus, dismissed: string | null = null) => {
      withDismissed(dismissed);
      (component as any).evaluate(s);
    };

    it('shows the banner when an update is available and not dismissed', () => {
      evaluate(status());
      expect(component.visible).toBe(true);
      expect(component.status?.latest_version).toBe('1.0.2');
    });

    it('stays hidden when already on the latest version', () => {
      evaluate(status({ update_available: false, latest_version: '1.0.1' }));
      expect(component.visible).toBe(false);
    });

    it('stays hidden for a version the user already dismissed', () => {
      evaluate(status(), '1.0.2');
      expect(component.visible).toBe(false);
    });

    it('a newer version overrides an older dismissal', () => {
      evaluate(status({ latest_version: '1.0.3' }), '1.0.2');
      expect(component.visible).toBe(true);
    });
  });

  // ── Dismissal (public API + persistence) ────────────────────────────────────
  // Persistence is asserted through the component's writeDismissedVersion() seam
  // (instance-scoped spy) — NOT through the localStorage API, which is
  // unobservable in CI headless Chrome (no-op writes; setItem is an own property
  // so a Storage.prototype spy never fires).
  describe('dismiss()', () => {
    it('hides the banner and persists the current version', () => {
      const write = spyOn(component as any, 'writeDismissedVersion');
      withDismissed(null);
      (component as any).evaluate(status());
      expect(component.visible).toBe(true);
      component.dismiss();
      expect(component.visible).toBe(false);
      expect(write).toHaveBeenCalledWith('1.0.2');
    });

    it('persists nothing when there is no known latest version', () => {
      const write = spyOn(component as any, 'writeDismissedVersion');
      component.dismiss(); // never evaluated → status is null
      expect(component.visible).toBe(false);
      expect(write).not.toHaveBeenCalled();
    });
  });

  // ── Integration: the 5s startup delay + render (real pipeline) ──────────────
  // Uses real localStorage.getItem (returns null in CI's no-op storage → not
  // dismissed → banner shows), so it is deterministic without any global spy.
  it('checks after a 5s delay, then renders the banner with the release link', fakeAsync(() => {
    systemSvc.getUpdateStatus.and.returnValue(of(status()));
    fixture.detectChanges();               // ngOnInit arms the delayed check
    expect(component.visible).toBe(false); // gated by the delay — keeps boot lean (#652)
    tick(5000);
    expect(component.visible).toBe(true);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('v1.0.2');
    expect(el.querySelector('a')?.getAttribute('href')).toContain('/releases/tag/v1.0.2');
  }));

  it('a failed check leaves the banner hidden and does not throw', fakeAsync(() => {
    systemSvc.getUpdateStatus.and.returnValue(throwError(() => new Error('offline')));
    fixture.detectChanges();
    tick(5000);
    expect(component.visible).toBe(false);
  }));
});
