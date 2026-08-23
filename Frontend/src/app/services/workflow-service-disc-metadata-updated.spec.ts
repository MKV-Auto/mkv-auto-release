/**
 * #832 — `disc_metadata_updated` merges identity fields into the matching
 * card without touching its state. A label save on an ejected (unfinished)
 * job must update the card, not drop it.
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

describe('WorkflowService disc_metadata_updated (#832)', () => {
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

  it('merges show / release / disc number into the unfinished card and keeps its state', () => {
    (service as any)._discs.next([
      {
        disc_id: 'd1', disc_state: 'unfinished', mount_point: null, job_id: 'job-1',
        scan_state: 'ready', movie_name: 'Star Wars Rebels', release_name: null, disc_number: null,
      },
      { disc_id: 'd2', disc_state: 'in_drive', mount_point: '/dev/sr0', job_id: 'job-2', scan_state: 'ready' },
    ]);
    (service as any).handleUnifiedMessage({
      type: 'disc_metadata_updated', disc_id: 'd1', job_id: 'job-1',
      movie_name: 'Star Wars Rebels', release_name: 'Season Two', disc_number: 2,
      disc_format: 'DVD', release_year: 2016,
    });
    const discs = (service as any)._discs.value as any[];
    expect(discs.length).toBe(2);
    const card = discs.find(d => d.disc_id === 'd1');
    expect(card.disc_state).toBe('unfinished');
    expect(card.release_name).toBe('Season Two');
    expect(card.disc_number).toBe(2);
    expect(card.disc_format).toBe('DVD');
    expect(card.info_title).toBe('Star Wars Rebels');
    expect(discs.find(d => d.disc_id === 'd2').release_name).toBeUndefined();
  });

  it('matches by job_id when the card has no disc_id yet and ignores unknown cards', () => {
    (service as any)._discs.next([
      { disc_id: 'job-9', disc_state: 'unfinished', job_id: 'job-9', scan_state: 'ready' },
    ]);
    (service as any).handleUnifiedMessage({
      type: 'disc_metadata_updated', disc_id: 'real-disc', job_id: 'job-9', disc_number: 4,
    });
    expect(((service as any)._discs.value as any[])[0].disc_number).toBe(4);
    const before = (service as any)._discs.value;
    (service as any).handleUnifiedMessage({ type: 'disc_metadata_updated', disc_id: 'nobody', job_id: 'nobody' });
    expect((service as any)._discs.value).toBe(before);
  });

  it('job_card_state merges the card contract without touching state (#839)', () => {
    (service as any)._discs.next([
      { disc_id: 'd1', disc_state: 'unfinished', job_id: 'job-1', scan_state: 'ready', job_status: 'running' },
    ]);
    (service as any).handleUnifiedMessage({
      type: 'job_card_state', job_id: 'job-1', disc_id: 'd1',
      card_state: 'verifying', family: 'working', pill: 'Verifying', progress: null,
      path: '/activity?jobId=job-1',
    });
    const card = ((service as any)._discs.value as any[])[0];
    expect(card.card_state).toBe('verifying');
    expect(card.card_family).toBe('working');
    expect(card.card_pill).toBe('Verifying');
    expect(card.disc_state).toBe('unfinished');

    (service as any).handleUnifiedMessage({
      type: 'job_card_state', job_id: 'job-1', disc_id: 'd1',
      card_state: 'ready_to_finish', family: 'your_turn', pill: 'Finish', progress: 100,
    });
    const after = ((service as any)._discs.value as any[])[0];
    expect(after.card_state).toBe('ready_to_finish');
    expect(after.card_progress).toBe(100);
  });

  it('progress_update feeds card_progress for the state it names (#839)', () => {
    (service as any)._discs.next([
      { disc_id: 'd1', disc_state: 'unfinished', job_id: 'job-1', scan_state: 'ready',
        card_state: 'copying', card_family: 'working', card_pill: 'Copying', card_progress: 10 },
    ]);
    (service as any).mergeCardProgress({ type: 'progress_update', job_id: 'job-1', rip_progress: 55 });
    expect(((service as any)._discs.value as any[])[0].card_progress).toBe(55);
    // A state the message does not speak to stays untouched.
    (service as any)._discs.next([
      { disc_id: 'd1', disc_state: 'unfinished', job_id: 'job-1', scan_state: 'ready',
        card_state: 'awaiting_label', card_progress: null },
    ]);
    (service as any).mergeCardProgress({ type: 'progress_update', job_id: 'job-1', rip_progress: 99 });
    expect(((service as any)._discs.value as any[])[0].card_progress).toBeNull();
  });
});
