import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { SystemService } from './system.service';
import { environment } from '../environments/environment';

describe('SystemService', () => {
  let service: SystemService;
  let httpMock: HttpTestingController;
  const apiUrl = environment.apiBase ?? 'http://localhost:8000';

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [SystemService],
    });
    service = TestBed.inject(SystemService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('getRsyncConfig', () => {
    it('GETs /system/transfer/rsync/config and returns RsyncConfigResponse', (done) => {
      const res = { config: null, hasKey: false };
      service.getRsyncConfig().subscribe(data => {
        expect(data.hasKey).toBe(false);
        done();
      });
      httpMock.expectOne(`${apiUrl}/system/transfer/rsync/config`).flush(res);
    });
  });

  describe('getDevMode', () => {
    it('GETs /system/devmode and returns DevModeStatus', (done) => {
      const res = { enabled: false, repo_url: '', branch: '', repo_path: '', export_root: '' };
      service.getDevMode().subscribe(data => {
        expect(data.enabled).toBe(false);
        done();
      });
      httpMock.expectOne(`${apiUrl}/system/devmode`).flush(res);
    });

    it('#206: subsequent subscriptions share the same HTTP call (shareReplay)', (done) => {
      const res = { enabled: false, repo_url: '', branch: '', repo_path: '', export_root: '' };
      let seen = 0;
      service.getDevMode().subscribe(() => { seen += 1; });
      service.getDevMode().subscribe(() => { seen += 1; });
      service.getDevMode().subscribe(() => { seen += 1; });
      // Only one HTTP request even though three subscribers registered — the
      // fix that unblocks Library cold-load (was 3× on the same endpoint).
      httpMock.expectOne(`${apiUrl}/system/devmode`).flush(res);
      setTimeout(() => {
        expect(seen).toBe(3);
        done();
      }, 0);
    });
  });

  describe('getStorageSummary', () => {
    it('GETs /system/storage/summary and returns StorageSummary', (done) => {
      const res = {
        data_root: { path: '/data', total: 1000, used: 100, free: 900 },
        transfer_root: { path: '/xfer', total: 500, used: 50, free: 450 },
      };
      service.getStorageSummary().subscribe(data => {
        expect(data.data_root.path).toBe('/data');
        done();
      });
      httpMock.expectOne(`${apiUrl}/system/storage/summary`).flush(res);
    });
  });

  describe('getRegistrationStatus', () => {
    it('GETs /system/makemkv/registration and returns MakeMKVRegistrationStatus', (done) => {
      const res = { expired: false };
      service.getRegistrationStatus().subscribe(data => {
        expect(data.expired).toBe(false);
        done();
      });
      httpMock.expectOne(`${apiUrl}/system/makemkv/registration`).flush(res);
    });
  });

  describe('auto-rip config (#331)', () => {
    it('GETs /system/auto-rip/config and returns AutoRipConfig', (done) => {
      service.getAutoRipConfig().subscribe(data => {
        expect(data.auto_rip_enabled).toBe(true);
        done();
      });
      httpMock.expectOne(`${apiUrl}/system/auto-rip/config`).flush({ auto_rip_enabled: true });
    });

    it('POSTs the toggle to /system/auto-rip/config and round-trips the value', (done) => {
      service.saveAutoRipConfig({ auto_rip_enabled: true }).subscribe(data => {
        expect(data.auto_rip_enabled).toBe(true);
        done();
      });
      const req = httpMock.expectOne(`${apiUrl}/system/auto-rip/config`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ auto_rip_enabled: true });
      req.flush({ auto_rip_enabled: true });
    });
  });

  describe('getAppVersion (#718)', () => {
    it('GETs /system/version and unwraps the version string', (done) => {
      service.getAppVersion().subscribe((v) => {
        expect(v).toBe('1.0.3');
        done();
      });
      const req = httpMock.expectOne(`${apiUrl}/system/version`);
      expect(req.request.method).toBe('GET');
      req.flush({ version: '1.0.3' });
    });

    it('caches — a second subscription shares the one HTTP call (shareReplay)', (done) => {
      service.getAppVersion().subscribe();
      const req = httpMock.expectOne(`${apiUrl}/system/version`);
      req.flush({ version: '1.0.3' });
      service.getAppVersion().subscribe((v) => {
        expect(v).toBe('1.0.3');
        httpMock.expectNone(`${apiUrl}/system/version`); // no second call
        done();
      });
    });

    it('falls back to "dev" when the response has no version', (done) => {
      service.getAppVersion().subscribe((v) => {
        expect(v).toBe('dev');
        done();
      });
      httpMock.expectOne(`${apiUrl}/system/version`).flush({});
    });
  });
});
