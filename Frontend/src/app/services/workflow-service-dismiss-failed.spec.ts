/**
 * #877: dismissing failed jobs removes their cards, here at click time and
 * again (idempotently) when the jobs_dismissed coordinator event arrives.
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

describe('WorkflowService dismissFailedJob (#877)', () => {
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
    // Seeding unfinished jobs makes the service fetch each one's status.
    jobSpy.getJobStatus.and.callFake((id: string) => of({ jobId: id }));
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

  function seed(): void {
    (service as any)._discs.next([
      { disc_id: 'd1', disc_state: 'unfinished', job_id: 'j-new', job_status: 'failed' },
      { disc_id: 'd2', disc_state: 'unfinished', job_id: 'j-other', job_status: 'running' },
      { disc_id: 'd1', disc_state: 'in_drive', mount_point: '/dev/sr0', job_id: 'j-new', job_status: 'failed' },
    ]);
    (service as any)._unfinishedJobs.next([{ job_id: 'j-new' }, { job_id: 'j-other' }]);
  }

  function discs(): any[] {
    return (service as any)._discs.value;
  }

  it('posts the choice and drops the cards when the server answers', () => {
    seed();
    let result: string[] | undefined;
    service.dismissFailedJob('j-new', true).subscribe(ids => (result = ids));

    const req = httpMock.expectOne(r => r.url.endsWith('/jobs/j-new/dismiss'));
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ delete_files: true });
    expect(discs().length).toBe(3);   // nothing removed before the server agrees
    req.flush({ job_ids: ['j-new', 'j-old'], deleting_files: true });

    expect(result).toEqual(['j-new', 'j-old']);
    expect(discs().filter(d => d.disc_state === 'unfinished').map(d => d.job_id)).toEqual(['j-other']);
    const drive = discs().find(d => d.disc_state === 'in_drive');
    expect(drive.job_id).toBeNull();
    expect(drive.job_status).toBeNull();
    expect((service as any)._unfinishedJobs.value).toEqual([{ job_id: 'j-other' }]);
  });

  it('keeps the cards when the server refuses', () => {
    seed();
    service.dismissFailedJob('j-new', false).subscribe({ error: () => {} });
    httpMock.expectOne(r => r.url.endsWith('/jobs/j-new/dismiss'))
      .flush({ detail: 'Job is running' }, { status: 409, statusText: 'Conflict' });
    expect(discs().length).toBe(3);
  });

  it('applies the jobs_dismissed event from another client or the expiry cleanup', () => {
    seed();
    (service as any).handleUnifiedMessage({ type: 'jobs_dismissed', job_ids: ['j-new'], disc_id: 'd1' });
    expect(discs().map(d => d.job_id)).toEqual(['j-other', null]);
    (service as any).handleUnifiedMessage({ type: 'jobs_dismissed', job_ids: ['j-new'], disc_id: 'd1' });
    expect(discs().length).toBe(2);
  });

  it('reads the preview', () => {
    let bytes = -1;
    service.getDismissFailedPreview('j-new').subscribe(p => (bytes = p.bytes));
    const req = httpMock.expectOne(r => r.url.endsWith('/jobs/j-new/dismiss'));
    expect(req.request.method).toBe('GET');
    req.flush({ job_ids: ['j-new'], bytes: 42, has_finished_rip: false, retention_days: 7 });
    expect(bytes).toBe(42);
  });
});
