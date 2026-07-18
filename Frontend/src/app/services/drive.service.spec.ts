/**
 * Comprehensive Drive Service Tests
 * 
 * Tests all drive service functionality including:
 * - Drive listing
 * - Disc info retrieval
 * - Disc scanning
 * - Drive selection
 */
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { DriveService } from './drive.service';
import { environment } from '../environments/environment';
import { LoggerService } from './logger.service';

describe('DriveService', () => {
  let service: DriveService;
  let httpMock: HttpTestingController;
  const apiUrl = environment.apiBase ?? 'http://localhost:8000';

  beforeEach(() => {
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        DriveService,
        { provide: LoggerService, useValue: loggerSpy },
      ]
    });
    service = TestBed.inject(DriveService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  describe('drives$ observable', () => {
    it('should emit drives from SSE', (done) => {
      service.drives$.subscribe(drives => {
        if (drives) {
          expect(Array.isArray(drives)).toBe(true);
          done();
        }
      });
      // Simulate SSE event
      const mockDrives = [
        { disc_num: '1', mount_point: '/dev/sr0', name: 'Drive 1' },
        { disc_num: '2', mount_point: '/dev/sr1', name: 'Drive 2' }
      ];
      (service as any)._drives.next(mockDrives);
    });
  });

  describe('discInfo$ observable', () => {
    it('should emit disc info from SSE', (done) => {
      service.discInfo$.subscribe(info => {
        if (info) {
          expect(info.disc_num).toBe('1');
          done();
        }
      });
      // Simulate SSE event
      const mockInfo = {
        disc_num: '1',
        mount_point: '/dev/sr0',
        info_title: 'Test Disc',
        titles: {}
      };
      (service as any)._discInfo.next(mockInfo);
    });
  });

  describe('refreshDiscInfo', () => {
    it('should refresh disc info successfully', async () => {
      const discNum = '1';
      const mountPoint = '/dev/sr0';
      const mockResponse = [
        { disc_num: '1', mount_point: '/dev/sr0', name: 'Drive 1' }
      ];

      const promise = service.refreshDiscInfo(discNum, mountPoint);
      
      const req = httpMock.expectOne(`${apiUrl}/events/drive/rescan?stream=0`);
      expect(req.request.method).toBe('POST');
      req.flush(mockResponse);
      
      const result = await promise;
      expect(result).toBeDefined();
    });
  });

  describe('selectDrive', () => {
    it('should select a drive and update observable', () => {
      const drive = { disc_num: '1', mount_point: '/dev/sr0', name: 'Drive 1' };

      service.selectDrive(drive);

      service.selected$.subscribe(selected => {
        expect(selected).toEqual(drive);
      });
    });
  });

  describe('getDrives', () => {
    it('should return current drives from state', () => {
      const mockDrives = [
        { disc_num: '1', mount_point: '/dev/sr0', name: 'Drive 1' },
      ];
      (service as any)._drives.next(mockDrives);
      expect(service.getDrives()).toEqual(mockDrives);
    });
  });

  describe('currentSelected', () => {
    it('should return last selected drive', () => {
      const drive = { disc_num: '1', mount_point: '/dev/sr0', name: 'Drive 1' };
      service.selectDrive(drive);
      expect(service.currentSelected()).toEqual(drive);
    });
  });

  describe('refreshDiscInfo error', () => {
    it('should reject when API returns 500', async () => {
      const promise = service.refreshDiscInfo('1', '/dev/sr0');
      const req = httpMock.expectOne((r) => (r.url ?? '').includes('/events/drive/rescan') && r.method === 'POST');
      req.flush('', { status: 500, statusText: 'Server Error' });
      await expectAsync(promise).toBeRejected();
    });

    it('should reject when API returns 404', async () => {
      const promise = service.refreshDiscInfo('1', '/dev/sr0');
      const req = httpMock.expectOne((r) => (r.url ?? '').includes('/events/drive/rescan') && r.method === 'POST');
      req.flush('', { status: 404, statusText: 'Not Found' });
      await expectAsync(promise).toBeRejected();
    });

    it('should reject when API returns 400', async () => {
      const promise = service.refreshDiscInfo('1', '/dev/sr0');
      const req = httpMock.expectOne((r) => (r.url ?? '').includes('/events/drive/rescan') && r.method === 'POST');
      req.flush('', { status: 400, statusText: 'Bad Request' });
      await expectAsync(promise).toBeRejected();
    });
  });
});
