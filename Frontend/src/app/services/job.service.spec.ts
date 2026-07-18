/**
 * Comprehensive Job Service Tests
 * 
 * Tests all job service functionality including:
 * - Starting rips
 * - Getting job status
 * - Labeling
 * - Transfer operations
 */
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { JobService } from './job.service';
import { environment } from '../environments/environment';
import { LoggerService } from './logger.service';
import { ToastService } from './toast.service';

describe('JobService', () => {
  let service: JobService;
  let httpMock: HttpTestingController;
  const apiUrl = environment.apiBase ?? 'http://localhost:8000';

  beforeEach(() => {
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);
    const toastSpy = jasmine.createSpyObj('ToastService', ['show', 'dismiss']);
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        JobService,
        { provide: LoggerService, useValue: loggerSpy },
        { provide: ToastService, useValue: toastSpy },
      ]
    });
    service = TestBed.inject(JobService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => req.flush({}));
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('startRip', () => {
    it('POSTs to /jobs/rip and returns JobStatus', (done) => {
      const body = { disc_num: '1', mount_point: '/mnt' };
      const res = { jobId: 'j1', job_status: 'running', rip_progress: 0, post_progress: 0, logs: [] };
      service.startRip(body).subscribe(data => {
        expect(data.jobId).toBe('j1');
        expect(data.job_status).toBe('running');
        done();
      });
      const req = httpMock.expectOne(`${apiUrl}/jobs/rip`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual(body);
      req.flush(res);
    });
  });

  describe('getJobStatus', () => {
    it('GETs /jobs/:id/status and returns JobStatus', (done) => {
      const res = { jobId: 'j1', job_status: 'completed', rip_progress: 100, post_progress: 100, logs: [] };
      service.getJobStatus('j1').subscribe(data => {
        expect(data.jobId).toBe('j1');
        expect(data.job_status).toBe('completed');
        done();
      });
      const req = httpMock.expectOne(`${apiUrl}/jobs/j1/status`);
      expect(req.request.method).toBe('GET');
      req.flush(res);
    });
  });

  describe('transferJob', () => {
    it('POSTs to /jobs/:id/transfer and returns JobStatus', (done) => {
      const res = { jobId: 'j1', job_status: 'running', rip_progress: 100, post_progress: 100, logs: [] };
      service.transferJob('j1', { type: 'local' }).subscribe(data => {
        expect(data.jobId).toBe('j1');
        done();
      });
      const req = httpMock.expectOne(`${apiUrl}/jobs/j1/transfer`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ type: 'local' });
      req.flush(res);
    });
  });

  describe('completeWorkflowStep', () => {
    it('POSTs to /jobs/:id/workflow/step/complete and returns JobStatus', (done) => {
      const res = { jobId: 'j1', workflow_step: 'disc', job_status: 'running', rip_progress: 100, post_progress: 0, logs: [] };
      service.completeWorkflowStep('j1', 'disc').subscribe(data => {
        expect(data.workflow_step).toBe('disc');
        done();
      });
      const req = httpMock.expectOne(`${apiUrl}/jobs/j1/workflow/step/complete`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ to_step: 'disc' });
      req.flush(res);
    });
  });

  describe('completeLabel', () => {
    it('POSTs to /jobs/:id/label/complete and returns JobStatus', (done) => {
      const res = { jobId: 'j1', workflow_step: 'postprocess', job_status: 'running', rip_progress: 100, post_progress: 0, logs: [] };
      service.completeLabel('j1').subscribe(data => {
        expect(data.workflow_step).toBe('postprocess');
        done();
      });
      const req = httpMock.expectOne(`${apiUrl}/jobs/j1/label/complete`);
      expect(req.request.method).toBe('POST');
      req.flush(res);
    });
  });

  describe('getJobStatus error', () => {
    it('propagates HTTP error', (done) => {
      service.getJobStatus('j1').subscribe({ error: err => { expect(err.status).toBe(404); done(); } });
      httpMock.expectOne(`${apiUrl}/jobs/j1/status`).flush('', { status: 404, statusText: 'Not Found' });
    });
  });
});
