// src/app/components/job-status-display/job-status-display.component.spec.ts
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { JobStatusDisplayComponent } from './job-status-display.component';
import { JobStatus } from '../../services/job.service';

describe('JobStatusDisplayComponent', () => {
  let component: JobStatusDisplayComponent;
  let fixture: ComponentFixture<JobStatusDisplayComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [JobStatusDisplayComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(JobStatusDisplayComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should emit actionRequested when action is clicked', () => {
    const mockJobStatus: JobStatus = {
      jobId: 'job-1',
      job_status: 'ripping',
      rip_progress: 50,
      post_progress: 0,
      logs: [],
      job_dir: null,
      disc_id: 'disc-1',
    };

    const mockCtaState = {
      label: 'Start',
      disabled: false,
      spinner: false,
      action: 'start' as const,
      intent: 'start' as const,
    };

    component.jobStatus = mockJobStatus;
    component.ctaState = mockCtaState;
    spyOn(component.actionRequested, 'emit');

    component.onActionClick();

    expect(component.actionRequested.emit).toHaveBeenCalledWith({
      action: 'start',
      jobId: 'job-1',
    });
  });

  it('should not emit actionRequested if jobStatus is null', () => {
    component.jobStatus = null;
    component.ctaState = {
      label: 'Start',
      disabled: false,
      spinner: false,
      action: 'start' as const,
      intent: 'start' as const,
    };
    spyOn(component.actionRequested, 'emit');

    component.onActionClick();

    expect(component.actionRequested.emit).not.toHaveBeenCalled();
  });

  it('should not emit actionRequested if ctaState is disabled', () => {
    const mockJobStatus: JobStatus = {
      jobId: 'job-1',
      job_status: 'ripping',
      rip_progress: 50,
      post_progress: 0,
      logs: [],
      job_dir: null,
      disc_id: 'disc-1',
    };

    component.jobStatus = mockJobStatus;
    component.ctaState = {
      label: 'Start',
      disabled: true,
      spinner: false,
      action: 'start' as const,
      intent: 'start' as const,
    };
    spyOn(component.actionRequested, 'emit');

    component.onActionClick();

    expect(component.actionRequested.emit).not.toHaveBeenCalled();
  });

  it('should return stage progress correctly', () => {
    component.stageProgress = {
      rip: 50,
      label: 0,
      postprocess: 0,
      transfer: 0,
      upload: 0,
    };
    component.isStageCompleted = {
      rip: false,
      label: false,
      postprocess: false,
      transfer: false,
      upload: false,
    };

    expect(component.getStageProgress('rip')).toBe(50);
    expect(component.getStageProgress('postprocess')).toBe(0);
    expect(component.getStageProgress('done')).toBeNull();
  });
});

