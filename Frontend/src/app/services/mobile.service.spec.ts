import { TestBed } from '@angular/core/testing';
import { MobileService } from './mobile.service';

describe('MobileService', () => {
  it('should be created', () => {
    TestBed.configureTestingModule({ providers: [MobileService] });
    expect(TestBed.inject(MobileService)).toBeTruthy();
  });

  it('isMobile is true when innerWidth < 768', () => {
    Object.defineProperty(window, 'innerWidth', { value: 500, configurable: true });
    TestBed.configureTestingModule({ providers: [MobileService] });
    expect(TestBed.inject(MobileService).isMobile).toBe(true);
  });

  it('isMobile is false when innerWidth >= 768', () => {
    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true });
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [MobileService] });
    expect(TestBed.inject(MobileService).isMobile).toBe(false);
  });

  it('isMobile$ emits boolean', (done) => {
    Object.defineProperty(window, 'innerWidth', { value: 500, configurable: true });
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [MobileService] });
    const service = TestBed.inject(MobileService);
    service.isMobile$.subscribe((v) => {
      expect(typeof v).toBe('boolean');
      expect(v).toBe(true);
      done();
    });
  });
});
