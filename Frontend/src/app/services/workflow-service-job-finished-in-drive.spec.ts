/**
 * Isolated tests for coordinator job_finished handling on in-drive cards (startup HTTP flushed).
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

describe('WorkflowService job_finished (in-drive)', () => {
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

  it('sets in-drive card to failed when job_status is failed', () => {
    (service as any)._discs.next([
      {
        disc_id: 'd1',
        disc_state: 'in_drive',
        mount_point: '/dev/sr0',
        disc_num: '1',
        job_id: 'job-f1',
        scan_state: 'ready',
        job_status: 'running',
      },
    ]);
    (service as any).handleUnifiedMessage({
      type: 'job_finished',
      job_id: 'job-f1',
      disc_id: 'd1',
      job_status: 'failed',
    });
    const discs = (service as any)._discs.value as any[];
    expect(discs.length).toBe(1);
    expect(discs[0].job_status).toBe('failed');
    expect(discs[0].job_id).toBe('job-f1');
    expect(discs[0].has_completed_job).toBe(false);
  });

  it('clears job_id and sets has_completed_job when completed', () => {
    (service as any)._discs.next([
      {
        disc_id: 'd1',
        disc_state: 'in_drive',
        mount_point: '/dev/sr0',
        job_id: 'job-ok',
        scan_state: 'ready',
      },
    ]);
    (service as any).handleUnifiedMessage({
      type: 'job_finished',
      job_id: 'job-ok',
      disc_id: 'd1',
      job_status: 'completed',
    });
    const discs = (service as any)._discs.value as any[];
    expect(discs[0].job_id).toBeNull();
    expect(discs[0].has_completed_job).toBe(true);
    expect(discs[0].job_status).toBeNull();
  });

  it('finishJob optimistically removes the unfinished card before WS roundtrip', () => {
    // Seed an unfinished card (post-eject state) and a separate in-drive card.
    (service as any)._discs.next([
      {
        disc_id: 'd-unfinished',
        disc_state: 'unfinished',
        job_id: 'job-x',
        job_status: 'running',
      },
      {
        disc_id: 'd-other',
        disc_state: 'in_drive',
        mount_point: '/dev/sr1',
        scan_state: 'ready',
      },
    ]);

    service.finishJob('job-x').subscribe();

    const req = httpMock.expectOne((r) => r.url.endsWith('/jobs/job-x/finish'));
    expect(req.request.method).toBe('POST');
    req.flush('', { status: 204, statusText: 'No Content' });

    // Card should already be gone — without waiting for the job_finished WS message.
    const discs = (service as any)._discs.value as any[];
    expect(discs.length).toBe(1);
    expect(discs[0].disc_id).toBe('d-other');
  });

  it('job_finished clears active context if it matches the finished job', () => {
    // Place the finished job into active context.
    (service as any)._activeContext$.next({
      id: 'job-active',
      type: 'job',
      labelForm: null,
      jobStatus: { jobId: 'job-active', job_status: 'running', rip_progress: 100, post_progress: 100, logs: [] },
      discInfo: null,
      titles: [],
      titleOrder: [],
      titlesComplete: true,
      movieOptions: [],
      boxsetOptions: [],
      releaseOptions: [],
      groupOptions: [],
      labelDraftProcessed: false,
      discNameLocked: false,
      discSlugLocked: false,
      isSeries: false,
      discdbHit: false,
      discMode: 'rip',
      lastReleaseDetails: null,
      releaseNameHint: '',
      releaseSlugHint: '',
      postProcessFiles: [],
      transferDestination: null,
    });
    (service as any).handleUnifiedMessage({
      type: 'job_finished',
      job_id: 'job-active',
      job_status: 'completed',
    });
    expect((service as any)._activeContext$.value).toBeNull();
  });
});
