/**
 * #723: disc_scan_failed must not leave the PREVIOUS disc's identity on the card.
 *
 * The reported fault: a drive wedged while its row still carried "Thor" from an
 * earlier successful scan. The scan-failed handler spreads the existing row, so
 * movie_name/info_title survived and the card kept headlining the wrong movie
 * for a tray that physically held a different disc.
 *
 * The backend now sets clear_identity on drive-level faults only — an
 * empty-scan failure keeps its volume-label title, which was read from the
 * disc actually in the drive.
 */
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { BehaviorSubject, of } from 'rxjs';
import { WorkflowService } from './workflow.service';
import { JobService } from './job.service';
import { DriveService } from './drive.service';
import { MetadataService } from './metadata.service';
import { LoggerService } from './logger.service';
import { SystemService } from './system.service';
import { ToastService } from './toast.service';

describe('WorkflowService disc_scan_failed identity handling', () => {
  let service: WorkflowService;
  let httpMock: HttpTestingController;

  function flushStartupRequests(): void {
    const pending = httpMock.match(() => true);
    for (const t of pending) {
      const u = t.request.url;
      if (u.includes('devmode')) {
        t.flush({ enabled: false });
      } else if (u.includes('coordinator/initial-state') || u.includes('initial-state')) {
        t.flush({ type: 'initial_state', discs: [], unfinished_jobs: [] });
      } else {
        t.flush({});
      }
    }
  }

  function seedThorInDrive(): void {
    (service as any)._discs.next([
      {
        disc_id: 'disc-thor',
        disc_state: 'in_drive',
        mount_point: '/dev/sr0',
        disc_num: '0',
        disc_hash: '60912EF2',
        scan_state: 'ready',
        movie_name: 'Thor',
        info_title: 'THOR',
        release_name: 'Thor (2011)',
        disc_format: 'DVD',
        production_year: 2011,
      },
    ]);
  }

  beforeEach(() => {
    const jobSpy = jasmine.createSpyObj('JobService', ['getJobStatus']);
    const driveSpy = jasmine.createSpyObj('DriveService', ['currentSelected', 'getDrives'], {
      drives$: new BehaviorSubject<any[]>([]),
    });
    driveSpy.currentSelected.and.returnValue(null);
    driveSpy.getDrives.and.returnValue([]);
    const metaSpy = jasmine.createSpyObj('MetadataService', [
      'getCachedOptions',
      'loadWorkflowOptions',
      'refreshWorkflowOptions',
    ]);
    metaSpy.getCachedOptions.and.returnValue({
      movieOptions: [],
      boxsetOptions: [],
      releaseOptions: [],
      groupOptions: [],
    });
    metaSpy.loadWorkflowOptions.and.returnValue(
      of({ movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [] })
    );
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);

    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        WorkflowService,
        { provide: JobService, useValue: jobSpy },
        { provide: DriveService, useValue: driveSpy },
        { provide: MetadataService, useValue: metaSpy },
        { provide: LoggerService, useValue: loggerSpy },
        { provide: SystemService, useValue: { getDevMode: () => of({ enabled: false }) } },
        { provide: ToastService, useValue: jasmine.createSpyObj('ToastService', ['error', 'info']) },
      ],
    });

    service = TestBed.inject(WorkflowService);
    httpMock = TestBed.inject(HttpTestingController);
    flushStartupRequests();
  });

  it('drops the previous disc identity when the drive reports a fault', () => {
    seedThorInDrive();
    (service as any).handleUnifiedMessage({
      type: 'disc_scan_failed',
      disc_id: 'drive-error-0',
      disc_num: '0',
      mount_point: '/dev/sr0',
      scan_state: 'failed',
      scan_error:
        'Drive is not responding (mount timed out after 30s). Try power cycling the drive.',
      drive_error_code: 'drive_unresponsive',
      clear_identity: true,
    });

    const discs = (service as any)._discs.value as any[];
    expect(discs.length).toBe(1);
    expect(discs[0].scan_state).toBe('failed');
    expect(discs[0].scan_error).toContain('power cycling');
    expect(discs[0].movie_name).toBeNull();
    expect(discs[0].info_title).toBeNull();
    expect(discs[0].release_name).toBeNull();
    expect(discs[0].disc_hash).toBeNull();
    expect(discs[0].disc_format).toBeNull();
    expect(discs[0].production_year).toBeNull();
    // The card itself stays so the user still sees the drive and its error.
    expect(discs[0].mount_point).toBe('/dev/sr0');
  });

  it('keeps the identity for an empty-scan failure (no clear_identity flag)', () => {
    (service as any)._discs.next([
      {
        disc_id: 'disc-swr',
        disc_state: 'in_drive',
        mount_point: '/dev/sr0',
        disc_num: '0',
        disc_hash: 'C7DC38D2',
        scan_state: 'ready',
        info_title: 'Star Wars Rebels S3 D1',
      },
    ]);
    (service as any).handleUnifiedMessage({
      type: 'disc_scan_failed',
      disc_id: 'disc-swr',
      disc_num: '0',
      mount_point: '/dev/sr0',
      scan_state: 'failed',
      scan_error: 'Empty scan output — no format and no tracks enumerated.',
    });

    const discs = (service as any)._discs.value as any[];
    expect(discs[0].scan_state).toBe('failed');
    expect(discs[0].info_title).toBe('Star Wars Rebels S3 D1');
    expect(discs[0].disc_hash).toBe('C7DC38D2');
  });

  it('creates a failed drive card when no row exists for the mount point yet', () => {
    (service as any)._discs.next([]);
    (service as any).handleUnifiedMessage({
      type: 'disc_scan_failed',
      disc_id: 'drive-error-0',
      disc_num: '0',
      mount_point: '/dev/sr0',
      scan_state: 'failed',
      scan_error: 'Drive is not responding (mount timed out after 30s).',
      clear_identity: true,
    });

    const discs = (service as any)._discs.value as any[];
    expect(discs.length).toBe(1);
    expect(discs[0].mount_point).toBe('/dev/sr0');
    expect(discs[0].scan_state).toBe('failed');
    expect(discs[0].movie_name).toBeUndefined();
  });
});
