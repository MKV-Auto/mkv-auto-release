import { TestBed } from '@angular/core/testing';
import { MobileService } from './mobile.service';

// The service reads document.documentElement.clientWidth (the true visible
// width), NOT window.innerWidth (the layout viewport, which horizontal
// overflow inflates). Mock both on the INSTANCE so each test states the full
// picture; instance-level defineProperty is the reliable seam in Karma.
function setViewport(clientWidth: number, innerWidth: number): void {
  Object.defineProperty(document.documentElement, 'clientWidth', {
    value: clientWidth, configurable: true,
  });
  Object.defineProperty(window, 'innerWidth', {
    value: innerWidth, configurable: true,
  });
}

describe('MobileService', () => {
  afterEach(() => {
    // Remove the instance overrides so later suites see the real geometry.
    delete (document.documentElement as any).clientWidth;
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    TestBed.configureTestingModule({ providers: [MobileService] });
    expect(TestBed.inject(MobileService)).toBeTruthy();
  });

  it('is mobile below the breakpoint', () => {
    setViewport(500, 500);
    TestBed.configureTestingModule({ providers: [MobileService] });
    expect(TestBed.inject(MobileService).isMobile).toBe(true);
  });

  it('is desktop at and above the breakpoint', () => {
    setViewport(1024, 1024);
    TestBed.configureTestingModule({ providers: [MobileService] });
    expect(TestBed.inject(MobileService).isMobile).toBe(false);
  });

  it('stays mobile when overflow inflates innerWidth past the breakpoint', () => {
    // The regression this service change exists for: a 654px action bar on a
    // 375px phone pushed the layout viewport (innerWidth) to ~700, and a
    // slightly wider element would cross 768 and flip a phone into the
    // desktop layout. The true visible width is what must decide.
    setViewport(375, 800);
    TestBed.configureTestingModule({ providers: [MobileService] });
    expect(TestBed.inject(MobileService).isMobile).toBe(true);
  });

  it('falls back to innerWidth when clientWidth is unavailable', () => {
    Object.defineProperty(document.documentElement, 'clientWidth', {
      value: 0, configurable: true,
    });
    Object.defineProperty(window, 'innerWidth', { value: 500, configurable: true });
    TestBed.configureTestingModule({ providers: [MobileService] });
    expect(TestBed.inject(MobileService).isMobile).toBe(true);
  });

  it('isMobile$ emits boolean', (done) => {
    setViewport(500, 500);
    TestBed.configureTestingModule({ providers: [MobileService] });
    TestBed.inject(MobileService).isMobile$.subscribe((v) => {
      expect(v).toBe(true);
      done();
    });
  });
});
