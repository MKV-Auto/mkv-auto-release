import { ComponentFixture, TestBed } from '@angular/core/testing';
import { UnfinishedJobsComponent } from './unfinished-jobs.component';
import { JobStatus } from '../../services/job.service';

describe('UnfinishedJobsComponent', () => {
  let component: UnfinishedJobsComponent;
  let fixture: ComponentFixture<UnfinishedJobsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UnfinishedJobsComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(UnfinishedJobsComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onJobClick emits jobId', () => {
    let emitted: string | undefined;
    component.jobSelected.subscribe((id) => (emitted = id));
    component.onJobClick('job-1');
    expect(emitted).toBe('job-1');
  });

  it('getJobCardTitle uses movie_name from job', () => {
    component.jobs = [{ jobId: 'j1', movie_name: 'A Movie' } as JobStatus];
    expect(component.getJobCardTitle(component.jobs[0])).toBe('A Movie');
  });

  it('getJobCardTitle falls back to payload then Unknown Disc', () => {
    expect(component.getJobCardTitle({ jobId: 'j1' } as JobStatus)).toBe('Unknown Disc');
    component.jobs = [{ jobId: 'j1', label_draft: { release_name: 'R' } } as JobStatus];
    expect(component.getJobCardTitle(component.jobs[0])).toBe('R');
  });

  it('getJobProductionYear uses production_year from job', () => {
    component.jobs = [{ jobId: 'j1', production_year: 2020 } as JobStatus];
    expect(component.getJobProductionYear(component.jobs[0])).toBe('2020');
  });

  it('getJobResolutionFormat returns resolution and format', () => {
    component.jobs = [
      { jobId: 'j1', resolution: '2160p', disc_payload: { disc_format: 'UHD' } } as JobStatus,
    ];
    const out = component.getJobResolutionFormat(component.jobs[0]);
    expect(out).toContain('2160p');
    expect(out).toContain('UHD');
  });
});
