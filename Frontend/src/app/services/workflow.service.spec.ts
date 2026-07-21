/**
 * WorkflowService Tests
 * 
 * Tests all workflow service functionality including:
 * - Context management
 * - WebSocket connections
 * - Workflow orchestration
 * - Create+Link methods
 * - Action methods
 */
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { BehaviorSubject, of, throwError } from 'rxjs';
import { WorkflowService, WorkflowContext } from './workflow.service';
import { JobService } from './job.service';
import { DriveService } from './drive.service';
import { MetadataService } from './metadata.service';
import { LoggerService } from './logger.service';

describe('WorkflowService', () => {
  let service: WorkflowService;
  let jobService: jasmine.SpyObj<JobService>;
  let driveService: jasmine.SpyObj<DriveService>;
  let metadataService: jasmine.SpyObj<MetadataService>;
  let logger: jasmine.SpyObj<LoggerService>;
  let httpTestingController: HttpTestingController;

  beforeEach(() => {
    const jobServiceSpy = jasmine.createSpyObj('JobService', [
      'startRip',
      'startPostProcess',
      'transferJob',
      'getJobStatus',
      'titleJobProgress',
      'completeWorkflowStep',
      'completeLabel',
    ]);
    const driveServiceSpy = jasmine.createSpyObj(
      'DriveService',
      ['currentSelected', 'getDrives'],
      { drives$: new BehaviorSubject<any[]>([]) }
    );
    const metadataServiceSpy = jasmine.createSpyObj('MetadataService', [
      'createAndLinkMovie',
      'createAndLinkRelease',
      'createAndLinkBoxset',
      'createBoxsetForDisc',
      'getMovie',
      'getRelease',
      'getBoxset',
      'getCachedOptions',
      'loadWorkflowOptions',
      'refreshWorkflowOptions',
    ]);
    // Provide default return values for options methods
    metadataServiceSpy.getCachedOptions.and.returnValue({
      movieOptions: [],
      boxsetOptions: [],
      releaseOptions: [],
      groupOptions: [],
    });
    metadataServiceSpy.loadWorkflowOptions.and.returnValue(of({
      movieOptions: [],
      boxsetOptions: [],
      releaseOptions: [],
      groupOptions: [],
    }));
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);

    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        WorkflowService,
        { provide: JobService, useValue: jobServiceSpy },
        { provide: DriveService, useValue: driveServiceSpy },
        { provide: MetadataService, useValue: metadataServiceSpy },
        { provide: LoggerService, useValue: loggerSpy },
      ],
    });

    service = TestBed.inject(WorkflowService);
    jobService = TestBed.inject(JobService) as jasmine.SpyObj<JobService>;
    driveService = TestBed.inject(DriveService) as jasmine.SpyObj<DriveService>;
    metadataService = TestBed.inject(MetadataService) as jasmine.SpyObj<MetadataService>;
    logger = TestBed.inject(LoggerService) as jasmine.SpyObj<LoggerService>;
    httpTestingController = TestBed.inject(HttpTestingController);
    driveService.currentSelected.and.returnValue(null as any);
    driveService.getDrives.and.returnValue([]);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('Context management', () => {
    it('should get current context', () => {
      const context = service.getCurrentContext();
      expect(context).toBeNull(); // Initially null
    });

    it('should get active context observable', (done) => {
      service.getActiveContext().subscribe(context => {
        expect(context).toBeDefined();
        done();
      });
    });

    it('should set context by card', (done) => {
      const card = { type: 'drive' as const, id: 'mount1' };

      service.setContextByCard(card).subscribe(context => {
        expect(context).toBeDefined();
        done();
      });

      const req = httpTestingController.expectOne(
        (r) => r.url.includes('discs/workflow-context') && r.params.get('mount_point') === 'mount1'
      );
      req.flush({ type: 'drive', id: 'mount1', discInfo: { disc_num: '1', mount_point: 'mount1' } });
    });

    it('should preserve titles keyed by file/src when updating context', () => {
      const seededContext: WorkflowContext = {
        id: 'mount1',
        type: 'drive',
        labelForm: {},
        jobStatus: null,
        discInfo: { disc_num: '1', mount_point: 'mount1' } as any,
        titles: [{ title_id: 'title-1', title: 'Initial', type: 'MainMovie' }],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false
      };
      // Set active context directly (no cache)
      (service as any)._activeContext$.next(seededContext);
      (service as any).syncStateFromContext(seededContext);

      service.updateContext({
        titles: [{ title_id: 'title-1', title: 'Updated', type: 'MainMovie' }]
      });

      const current = service.getCurrentContext();
      expect(current?.titles?.length).toBe(1);
      expect(current?.titles?.[0]?.title).toBe('Updated');
    });

    it('should map disc API response to drive context id = mount_point for carousel', () => {
      const ctx = (service as any)._convertApiResponseToContext({
        type: 'disc',
        id: 'uuid-disc-123',
        discId: 'uuid-disc-123',
        mountPoint: '/dev/sr1',
        discInfo: { disc_id: 'uuid-disc-123', mount_point: '/dev/sr1' },
        labelForm: {},
        titles: [],
      });
      expect(ctx.type).toBe('drive');
      expect(ctx.id).toBe('/dev/sr1');
    });

    it('buildContextFromJob: explicit null disc_number on disc_payload clears stale label_payload', () => {
      const job: any = {
        jobId: 'job-1',
        stage_profile: 'miss',
        rip_state: 'completed',
        disc_payload: {
          disc_id: 'disc-1',
          disc_num: '0',
          mount_point: '/mnt',
          disc_number: null,
          disc_name: 'API Name',
          disc_format: 'Blu-Ray',
          label_required: true,
          titles: {},
          label_payload: {
            disc_number: 3,
            disc_name: 'Stale Name',
            group_type: 'movie',
            mode: 'movie',
            release_slug: 'rel',
          },
        },
      };
      const ctx = service.buildContextFromJob(job, [], [], [], []);
      expect(ctx.discInfo?.disc_number).toBeNull();
      expect(ctx.labelForm?.disc_number).toBeNull();
      expect(ctx.discInfo?.disc_name).toBe('API Name');
      expect(ctx.labelForm?.disc_name).toBe('API Name');
    });

    it('applyLastReleaseDefaults does not copy edition fields from a different release', () => {
      const svc = service as any;
      const baseForm = {
        mode: 'movie',
        group_type: 'movie',
        disc_group: '',
        release_name: '',
        release_slug: '',
        release_id: 'release-target',
        tmdb_id: '',
        upc: null,
        asin: null,
        cover_front_url: null,
        cover_back_url: null,
        release_year: null,
        production_year: null,
        disc_name: '',
        disc_slug: '',
        tracks: [],
      };
      const wrongRef = {
        release_id: 'release-other',
        group_type: 'series',
        release_name: 'Wrong',
        tmdb_id: '999',
        upc: '1234567890128',
        asin: 'B00WRONG',
        cover_front_url: 'https://example.com/front.jpg',
        cover_back_url: 'https://example.com/back.jpg',
        release_year: 2017,
        production_year: 2016,
      };
      const out = svc.applyLastReleaseDefaults({ ...baseForm }, wrongRef, null, false, false);
      expect(out.upc).toBeNull();
      expect(out.cover_front_url).toBeNull();
      expect(out.group_type).toBe('movie');
      expect(out.tmdb_id).toBe('');
    });

    it('applyLastReleaseDefaults copies edition fields when release_id matches', () => {
      const svc = service as any;
      const rid = 'same-release';
      const baseForm: any = {
        mode: 'movie',
        group_type: 'movie',
        disc_group: '',
        release_name: '',
        release_slug: '',
        release_id: rid,
        tmdb_id: '',
        upc: null,
        asin: null,
        cover_front_url: null,
        cover_back_url: null,
        release_year: null,
        production_year: null,
        disc_name: '',
        disc_slug: '',
        tracks: [],
      };
      const ref = {
        release_id: rid,
        group_type: 'movie',
        release_name: 'Edition',
        tmdb_id: '42',
        upc: '5901234123457',
        asin: null,
        cover_front_url: 'https://example.com/c.jpg',
        cover_back_url: null,
        release_year: 2020,
        production_year: 2019,
      };
      const out = svc.applyLastReleaseDefaults({ ...baseForm }, ref, null, false, false);
      expect(out.upc).toBe('5901234123457');
      expect(out.cover_front_url).toBe('https://example.com/c.jpg');
      expect(out.release_year).toBe(2020);
    });

    it('_discContextPatchForActiveJob merges disc_number only; keeps in-progress disc_name/slug/format', () => {
      const base: WorkflowContext = {
        id: 'job-1',
        type: 'job',
        labelForm: {
          disc_number: null,
          disc_name: 'TypingName',
          disc_slug: 'typing-slug',
          disc_format: 'Blu-Ray',
          movie_id: 'm1',
        } as any,
        jobStatus: { jobId: 'job-1' } as any,
        discInfo: { disc_id: 'd1' } as any,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
        movieOptions: [],
        boxsetOptions: [],
        releaseOptions: [],
        groupOptions: [],
        labelDraftProcessed: false,
        discNameLocked: false,
        discSlugLocked: false,
        isSeries: false,
        discdbHit: false,
        discMode: 'copy',
        lastReleaseDetails: null,
        releaseNameHint: '',
        releaseSlugHint: '',
        postProcessFiles: [],
        transferDestination: null,
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: true,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: true,
      };
      (service as any)._activeContext$.next(base);
      const patch = (service as any)._discContextPatchForActiveJob({
        discInfo: { disc_id: 'd1', mount_point: '/mnt', disc_num: '0' } as any,
        labelForm: {
          disc_number: 2,
          disc_name: 'ServerName',
          disc_slug: 'server-slug',
          disc_format: 'UHD',
        } as any,
      });
      expect(patch.discInfo).toBeDefined();
      expect(patch.labelForm?.disc_number).toBe(2);
      expect(patch.labelForm?.disc_name).toBe('TypingName');
      expect(patch.labelForm?.disc_slug).toBe('typing-slug');
      expect(patch.labelForm?.disc_format).toBe('Blu-Ray');
      expect(patch.labelForm?.movie_id).toBe('m1');
    });

    it('should not replace drive context id with disc UUID when updateContext merges API-shaped payload', () => {
      const seededContext: WorkflowContext = {
        id: '/dev/sr1',
        type: 'drive',
        labelForm: { movie_id: 'm1' } as any,
        jobStatus: null,
        discInfo: { disc_id: 'uuid-1', mount_point: '/dev/sr1' } as any,
        titles: [],
        titleOrder: [],
        titlesComplete: true,
        movieOptions: [],
        boxsetOptions: [],
        releaseOptions: [],
        groupOptions: [],
        labelDraftProcessed: true,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: true,
        hasLabelContent: true,
        devMode: false,
        showTitleStatus: true,
      };
      (service as any)._activeContext$.next(seededContext);
      service.updateContext({
        id: 'uuid-1',
        type: 'drive',
        labelForm: { movie_id: 'm1', release_id: 'r9' } as any,
        discInfo: seededContext.discInfo,
      } as any);
      expect(service.getCurrentContext()?.id).toBe('/dev/sr1');
      expect(service.getCurrentContext()?.type).toBe('drive');
    });
  });

  describe('Title seq gating', () => {
    it('skips stale title_seq patch responses', () => {
      const context = {
        id: 'disc-1',
        type: 'drive',
        discInfo: { disc_id: 'disc-1' },
        titles: [{ title_id: 'title-1', title: 'Local', title_seq: 2 }],
        titleOrder: ['title-1'],
        labelForm: {},
        jobStatus: null,
      } as unknown as WorkflowContext;
      (service as any)._activeContext$.next(context);
      (service as any).syncTitleSeqsFromTitles(context.titles);

      (service as any).applyTitlePatchResults('disc-1', [
        {
          title_id: 'title-1',
          success: true,
          updated_title: { title_id: 'title-1', title: 'Stale', title_seq: 1 },
        }
      ], 1);

      expect(service.getCurrentContext()?.titles?.[0]?.title).toBe('Local');
    });

    it('#383: stale_seq result toasts the user and refetches titles', (done) => {
      const context = {
        id: 'disc-1',
        type: 'drive',
        discInfo: { disc_id: 'disc-1' },
        titles: [{ title_id: 'title-1', title: 'Local', title_seq: 2 }],
        titleOrder: ['title-1'],
        labelForm: {},
        jobStatus: null,
      } as unknown as WorkflowContext;
      (service as any)._activeContext$.next(context);
      (service as any).syncTitleSeqsFromTitles(context.titles);

      const toastSpy = spyOn((service as any).toastSvc, 'show').and.callThrough();

      (service as any).applyTitlePatchResults('disc-1', [
        {
          title_id: 'title-1',
          success: false,
          error: 'Stale title update',
          error_code: 'stale_seq',
        },
      ], 1);

      // Toast fires synchronously before the refetch promise resolves.
      expect(toastSpy).toHaveBeenCalledWith(
        jasmine.stringMatching(/conflicted with a newer change/),
        'error',
        jasmine.any(Number),
      );

      // Refetch happens against /discs/{id}/titles?limit=500. Flush it with
      // an updated row so latestTitleSeqById catches up.
      const req = httpTestingController.expectOne((r) =>
        r.url.includes('/discs/disc-1/titles') && r.url.includes('limit=500')
      );
      req.flush({
        items: [{ title_id: 'title-1', title: 'Winner', title_seq: 3 }],
      });

      // Allow the promise chain to settle before asserting context update.
      setTimeout(() => {
        const seq = (service as any).latestTitleSeqById.get('title-1');
        expect(seq).toBe(3);
        const updated = service.getCurrentContext()?.titles?.[0];
        expect(updated?.title).toBe('Winner');
        expect(updated?.title_seq).toBe(3);
        done();
      }, 0);
    });
  });

  describe('Last selected card persistence', () => {
    const key = WorkflowService.LAST_SELECTED_CARD_KEY;

    beforeEach(() => {
      sessionStorage.removeItem(key);
    });

    afterEach(() => {
      sessionStorage.removeItem(key);
    });

    it('should persist card to sessionStorage when setSelectedCard is called with non-null card', () => {
      service.setSelectedCard({ type: 'drive', id: '/dev/sr0' });
      expect(sessionStorage.getItem(key)).toBe(JSON.stringify({ type: 'drive', id: '/dev/sr0' }));
    });

    it('should remove from sessionStorage when setSelectedCard is called with null', () => {
      sessionStorage.setItem(key, JSON.stringify({ type: 'job', id: 'j1' }));
      service.setSelectedCard(null);
      expect(sessionStorage.getItem(key)).toBeNull();
    });

    it('should remove from sessionStorage when clearCardSelection is called', () => {
      sessionStorage.setItem(key, JSON.stringify({ type: 'drive', id: '/dev/sr0' }));
      service.clearCardSelection();
      expect(sessionStorage.getItem(key)).toBeNull();
    });
  });

  describe('Observables', () => {
    it('should expose unfinishedJobs$', (done) => {
      service.unfinishedJobs$.subscribe(jobs => {
        expect(Array.isArray(jobs)).toBe(true);
        done();
      });
    });

    it('should expose insertedDiscs$', (done) => {
      service.insertedDiscs$.subscribe(discs => {
        expect(Array.isArray(discs)).toBe(true);
        done();
      });
    });

    it('should expose active context observable', (done) => {
      service.getActiveContext().subscribe(context => {
        expect(context).toBeDefined();
        done();
      });
    });
  });

  describe('Action methods', () => {
    it('should start rip', (done) => {
      const mockContext: WorkflowContext = {
        id: 'test',
        type: 'drive',
        labelForm: {},
        jobStatus: null,
        discInfo: { disc_num: '1', mount_point: '/mnt' } as any,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false
      };
      spyOn(service, 'getCurrentContext').and.returnValue(mockContext);
      
      jobService.startRip.and.returnValue(of({
        jobId: 'job1',
        job_status: 'running',
        rip_progress: 0,
        post_progress: 0,
        logs: [],
        workflow_step: 'boxset',
      } as any));

      service.startRip().subscribe(result => {
        expect(jobService.startRip).toHaveBeenCalled();
        expect(result?.workflow_step).toBe('boxset');
        done();
      });
    });
  });

  describe('Step navigation', () => {
    const baseContext: WorkflowContext = {
      id: 'test',
      type: 'drive',
      labelForm: {
        movie_id: 'movie-1',
        release_id: 'release-1',
        disc_id: 'disc-1',
        disc_name: 'Disc 1',
        disc_format: 'Blu-Ray'
      },
      jobStatus: null,
      discInfo: null as any,
      titles: [{ title: 'Title 1', type: 'movie' } as any],
      titleOrder: [],
      titlesComplete: false,
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
      releaseDiscs: [],
      boxsetMovies: [],
      movieCover: null,
      movieName: null,
      productionYear: null,
      labelSaving: false,
      lastAutosaveOk: false,
      hasLabelContent: false,
      devMode: false,
      showTitleStatus: false
    };

    it('should block boxset step before copy starts', () => {
      const context = {
        ...baseContext,
        workflowStep: 'film'
      } as WorkflowContext;

      const result = service.canNavigateToStep(context, 'boxset');
      expect(result.allowed).toBeFalse();
    });

    it('should allow boxset step when copy is running', () => {
      const context = {
        ...baseContext,
        workflowStep: 'film',
        jobStatus: { job_status: 'running', rip_state: 'running' } as any
      } as WorkflowContext;

      const result = service.canNavigateToStep(context, 'boxset');
      expect(result.allowed).toBeTrue();
    });
  });

  describe('computeFurthestStep', () => {
    it('returns film when rip_state is pending or unset (copy not started)', () => {
      const ctx = {
        id: 'j1', type: 'job' as const, discdbHit: false,
        labelForm: {}, jobStatus: { rip_state: 'pending' } as any, discInfo: null, titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [], labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: false, discMode: 'rip' as const, lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '', postProcessFiles: [], transferDestination: null,
        releaseDiscs: [], boxsetMovies: [], movieCover: null, movieName: null, productionYear: null, labelSaving: false, lastAutosaveOk: false, hasLabelContent: false, devMode: false, showTitleStatus: false
      } as WorkflowContext;
      expect(service.computeFurthestStep(ctx)).toBe('film');
    });

    it('returns boxset when rip_state is running (copy started, not pending)', () => {
      const ctx = {
        id: 'j1', type: 'job' as const, discdbHit: false,
        labelForm: {}, jobStatus: { rip_state: 'running' } as any, discInfo: null, titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [], labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: false, discMode: 'rip' as const, lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '', postProcessFiles: [], transferDestination: null,
        releaseDiscs: [], boxsetMovies: [], movieCover: null, movieName: null, productionYear: null, labelSaving: false, lastAutosaveOk: false, hasLabelContent: false, devMode: false, showTitleStatus: false
      } as WorkflowContext;
      expect(service.computeFurthestStep(ctx)).toBe('boxset');
    });

    it('returns transfer when post_state is completed', () => {
      const ctx = {
        id: 'j1', type: 'job' as const, discdbHit: false,
        labelForm: {}, jobStatus: { rip_state: 'completed', post_state: 'completed', transfer_state: 'completed' } as any, discInfo: null, titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [], labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: false, discMode: 'rip' as const, lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '', postProcessFiles: [], transferDestination: null,
        releaseDiscs: [], boxsetMovies: [], movieCover: null, movieName: null, productionYear: null, labelSaving: false, lastAutosaveOk: false, hasLabelContent: false, devMode: false, showTitleStatus: false
      } as WorkflowContext;
      expect(service.computeFurthestStep(ctx)).toBe('transfer');
    });
  });

  describe('canNavigateToStep with furthest', () => {
    it('blocks forward when target is beyond furthestStep', () => {
      const ctx = {
        id: 'j1', type: 'job' as const, discdbHit: false, workflowStep: 'film' as const,
        labelForm: { movie_id: 'm1' }, jobStatus: { rip_state: 'pending' } as any, discInfo: null, titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [], labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: false, discMode: 'rip' as const, lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '', postProcessFiles: [], transferDestination: null,
        releaseDiscs: [], boxsetMovies: [], movieCover: null, movieName: null, productionYear: null, labelSaving: false, lastAutosaveOk: false, hasLabelContent: false, devMode: false, showTitleStatus: false
      } as WorkflowContext;
      const r = service.canNavigateToStep(ctx, 'boxset');
      expect(r.allowed).toBeFalse();
    });

    it('allows backward to any prior step', () => {
      const ctx = {
        id: 'j1', type: 'job' as const, discdbHit: false, workflowStep: 'titles' as const,
        labelForm: { movie_id: 'm1', release_id: 'r1', disc_name: 'D1', disc_format: 'Blu-Ray', tracks: [{ title_id: 't1', title: 'T1' }] },
        jobStatus: { rip_state: 'completed' } as any, discInfo: null, titles: [], titleOrder: [], titlesComplete: true,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [], labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: false, discMode: 'rip' as const, lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '', postProcessFiles: [], transferDestination: null,
        releaseDiscs: [], boxsetMovies: [], movieCover: null, movieName: null, productionYear: null, labelSaving: false, lastAutosaveOk: false, hasLabelContent: false, devMode: false, showTitleStatus: false
      } as WorkflowContext;
      const r = service.canNavigateToStep(ctx, 'disc');
      expect(r.allowed).toBeTrue();
    });

    // #363 H1 — backward into labeling steps is gated on label/post state.
    function lockedCtx(jobStatus: any, workflowStep: any = 'transfer'): WorkflowContext {
      return {
        id: 'j1', type: 'job' as const, discdbHit: false, workflowStep,
        labelForm: { movie_id: 'm1', release_id: 'r1', disc_name: 'D1', disc_format: 'Blu-Ray', tracks: [{ title_id: 't1', title: 'T1' }] },
        jobStatus, discInfo: null, titles: [], titleOrder: [], titlesComplete: true,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [], labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: false, discMode: 'rip' as const, lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '', postProcessFiles: [], transferDestination: null,
        releaseDiscs: [], boxsetMovies: [], movieCover: null, movieName: null, productionYear: null, labelSaving: false, lastAutosaveOk: false, hasLabelContent: false, devMode: false, showTitleStatus: false
      } as WorkflowContext;
    }

    it('allows backward into titles when only label_state is completed but postprocess has not started (#363 H1 relaxed)', () => {
      // Post-complete_label window: labels are staged but nothing has been
      // renamed/moved yet — user can go back to fix mistakes discovered on
      // the Transfer preview until they click Start Transfer.
      const ctx = lockedCtx({
        rip_state: 'completed', label_state: 'completed', transfer_state: 'pending',
      });
      const r = service.canNavigateToStep(ctx, 'titles');
      expect(r.allowed).toBeTrue();
    });

    it('blocks backward into titles while postprocess is running (#363 H1)', () => {
      const ctx = lockedCtx({ rip_state: 'completed', post_state: 'running' });
      expect(service.canNavigateToStep(ctx, 'titles').allowed).toBeFalse();
      expect(service.canNavigateToStep(ctx, 'film').allowed).toBeFalse();
    });

    it('blocks backward into titles while transfer is running (#363 H1)', () => {
      const ctx = lockedCtx({ rip_state: 'completed', label_state: 'completed', transfer_state: 'running' });
      expect(service.canNavigateToStep(ctx, 'titles').allowed).toBeFalse();
    });

    it('still allows backward into titles before label completes', () => {
      const ctx = lockedCtx({ rip_state: 'completed', label_state: 'running' });
      expect(service.canNavigateToStep(ctx, 'titles').allowed).toBeTrue();
    });

    it('areLabelsLocked triggers only on postprocess/transfer running-or-completed', () => {
      // label_state='completed' ALONE no longer locks — postprocess is
      // deferred until Start Transfer, so the window is safe for edits.
      expect(service.areLabelsLocked(lockedCtx({ label_state: 'completed', transfer_state: 'pending' }))).toBeFalse();
      expect(service.areLabelsLocked(lockedCtx({ post_state: 'running' }))).toBeTrue();
      expect(service.areLabelsLocked(lockedCtx({ post_state: 'completed' }))).toBeTrue();
      expect(service.areLabelsLocked(lockedCtx({ transfer_state: 'running' }))).toBeTrue();
      expect(service.areLabelsLocked(lockedCtx({ transfer_state: 'completed' }))).toBeTrue();
      expect(service.areLabelsLocked(lockedCtx({ rip_state: 'running' }))).toBeFalse();
      expect(service.areLabelsLocked(null)).toBeFalse();
    });

    it('navigateToPreviousStep is a no-op when postprocess is actually running (#363 H1)', () => {
      const ctx = lockedCtx({ rip_state: 'completed', label_state: 'completed', post_state: 'running' }, 'transfer');
      spyOn(service, 'getCurrentContext').and.returnValue(ctx);
      const updateSpy = spyOn(service, 'updateContext');
      service.navigateToPreviousStep();
      expect(updateSpy).not.toHaveBeenCalled();
    });
  });

  describe('createAndLinkBoxset', () => {
    it('PATCH workflow-context includes release_id from createBoxsetForDisc (not stale null)', (done) => {
      const jobId = 'job-box-1';
      const seededContext = {
        id: jobId,
        type: 'job' as const,
        workflowStep: 'boxset',
        labelForm: {
          movie_id: 'movie-1',
          release_id: null,
          release_slug: null,
          workflow_step: 'boxset',
        },
        jobStatus: { jobId, disc_id: 'disc-1' },
        discInfo: { disc_id: 'disc-1', mount_point: '/dev/sr0' },
        titles: [],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false,
      } as unknown as WorkflowContext;

      (service as any)._activeContext$.next(seededContext);
      (service as any).syncStateFromContext(seededContext);

      metadataService.createBoxsetForDisc.and.returnValue(
        of({
          boxset: { id: 'box-1', slug: 'box-slug' },
          release: {
            id: 'rel-new',
            slug: 'rel-slug',
            total_discs: 1,
            completed_discs: 0,
            finalized_discs: 0,
          },
          linked: true,
        } as any)
      );

      service.createAndLinkBoxset({ name: 'Set', year: 2020 }, jobId, 'job').subscribe({
        next: () => {
          const current = service.getCurrentContext();
          expect(current?.labelForm?.release_id).toBe('rel-new');
          expect(current?.labelForm?.release_slug).toBe('rel-slug');
          expect(current?.labelForm?.boxset_id).toBe('box-1');
          done();
        },
        error: done.fail,
      });

      const patchReq = httpTestingController.expectOne(
        (r) => r.method === 'PATCH' && r.url.includes(`/jobs/${jobId}/workflow-context`)
      );
      expect(patchReq.request.body.labelForm.release_id).toBe('rel-new');
      expect(patchReq.request.body.labelForm.release_slug).toBe('rel-slug');
      expect(patchReq.request.body.labelForm.boxset_id).toBe('box-1');
      patchReq.flush({
        type: 'job',
        id: jobId,
        labelForm: patchReq.request.body.labelForm,
        discInfo: seededContext.discInfo,
        titles: [],
        titleOrder: [],
        jobStatus: seededContext.jobStatus,
      });
    });
  });

  describe('getEffectiveWorkflowStep', () => {
    const baseCtx = (overrides: Partial<WorkflowContext>): WorkflowContext => ({
      id: 'j1',
      type: 'job',
      discdbHit: false,
      labelForm: {},
      discInfo: null,
      titles: [],
      titleOrder: [],
      titlesComplete: false,
      movieOptions: [],
      boxsetOptions: [],
      releaseOptions: [],
      groupOptions: [],
      labelDraftProcessed: false,
      discNameLocked: false,
      discSlugLocked: false,
      isSeries: false,
      discMode: 'rip',
      lastReleaseDetails: null,
      releaseNameHint: '',
      releaseSlugHint: '',
      postProcessFiles: [],
      transferDestination: null,
      releaseDiscs: [],
      boxsetMovies: [],
      movieCover: null,
      movieName: null,
      productionYear: null,
      labelSaving: false,
      lastAutosaveOk: false,
      hasLabelContent: false,
      devMode: false,
      showTitleStatus: false,
      ...overrides,
    } as WorkflowContext);

    it('prefers jobStatus.workflow_step over stale context.workflowStep (auto progression)', () => {
      const ctx = baseCtx({
        workflowStep: 'disc',
        jobStatus: { jobId: 'j1', workflow_step: 'titles' } as any,
      });
      expect(service.getEffectiveWorkflowStep(ctx)).toBe('titles');
    });

    it('honors context.workflowStep when stepNavigationSource=user (Back button)', () => {
      // Backend says we are on postprocess (already advanced) but the user
      // clicked Back to disc. Without honoring the user nav, canContinue$ /
      // onContinue would validate against transfer and the Continue
      // button would be stuck disabled until refresh.
      // #365 Phase 2 § 6.4 — was workflow_step="postprocess" before the
      // backend writes were flipped to "transfer".
      const ctx = baseCtx({
        workflowStep: 'disc',
        stepNavigationSource: 'user',
        jobStatus: { jobId: 'j1', workflow_step: 'transfer' } as any,
      });
      expect(service.getEffectiveWorkflowStep(ctx)).toBe('disc');
    });

    it('still prefers backend step when stepNavigationSource is automatic/initial', () => {
      const ctxAuto = baseCtx({
        workflowStep: 'disc',
        stepNavigationSource: 'automatic',
        jobStatus: { jobId: 'j1', workflow_step: 'titles' } as any,
      });
      expect(service.getEffectiveWorkflowStep(ctxAuto)).toBe('titles');

      const ctxInitial = baseCtx({
        workflowStep: 'disc',
        stepNavigationSource: 'initial',
        jobStatus: { jobId: 'j1', workflow_step: 'titles' } as any,
      });
      expect(service.getEffectiveWorkflowStep(ctxInitial)).toBe('titles');
    });
  });

  describe('saveJobWorkflowContext stale response filter', () => {
    it('drops an older PATCH completion when a newer save was started for the same job', (done) => {
      const jobId = 'job-seq-1';
      const seededContext = {
        id: jobId,
        type: 'job' as const,
        workflowStep: 'disc',
        labelForm: { movie_id: 'm1', disc_name: 'D' },
        jobStatus: { jobId, workflow_step: 'disc' },
        discInfo: null,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false,
      } as unknown as WorkflowContext;

      (service as any)._activeContext$.next(seededContext);
      (service as any).syncStateFromContext(seededContext);

      let firstCount = 0;
      let secondCount = 0;
      let completed = 0;
      const finish = () => {
        completed += 1;
        if (completed === 2) {
          expect(firstCount).toBe(0);
          expect(secondCount).toBe(1);
          done();
        }
      };
      service.saveJobWorkflowContext(jobId, { movie_id: 'm1', patch: 'slow' } as any).subscribe({
        next: () => {
          firstCount += 1;
        },
        complete: finish,
      });
      service.saveJobWorkflowContext(jobId, { movie_id: 'm1', patch: 'fast' } as any).subscribe({
        next: () => {
          secondCount += 1;
        },
        complete: finish,
      });

      const reqs = httpTestingController.match((r) => r.method === 'PATCH' && r.url.includes(`/jobs/${jobId}/workflow-context`));
      expect(reqs.length).toBe(2);
      reqs[1].flush({
        type: 'job',
        id: jobId,
        labelForm: { movie_id: 'm1', patch: 'fast' },
        jobStatus: seededContext.jobStatus,
        titles: [],
        titleOrder: [],
      });
      reqs[0].flush({
        type: 'job',
        id: jobId,
        labelForm: { movie_id: 'm1', patch: 'slow' },
        jobStatus: seededContext.jobStatus,
        titles: [],
        titleOrder: [],
      });
    });
  });

  describe('saveJobWorkflowContext strips stale tmdb_id when movie_id present', () => {
    it('should omit tmdb_id from PATCH body', (done) => {
      const jobId = 'job-strip-tmdb';
      const seededContext = {
        id: jobId,
        type: 'job' as const,
        workflowStep: 'boxset',
        labelForm: { movie_id: 'm1' },
        jobStatus: { jobId, workflow_step: 'boxset' },
        discInfo: null,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false,
      } as unknown as WorkflowContext;

      (service as any)._activeContext$.next(seededContext);
      (service as any).syncStateFromContext(seededContext);

      service
        .saveJobWorkflowContext(jobId, {
          movie_id: 'movie-uuid-1',
          tmdb_id: '99999',
          release_id: 'r1',
        } as any)
        .subscribe({
          next: () => undefined,
          complete: () => done(),
          error: done.fail,
        });

      const req = httpTestingController.expectOne(
        (r) => r.method === 'PATCH' && r.url.includes(`/jobs/${jobId}/workflow-context`)
      );
      const body = req.request.body as { labelForm: Record<string, unknown> };
      expect(body.labelForm['movie_id']).toBe('movie-uuid-1');
      expect(body.labelForm['tmdb_id']).toBeUndefined();
      req.flush({
        type: 'job',
        id: jobId,
        labelForm: { movie_id: 'movie-uuid-1', release_id: 'r1' },
        jobStatus: seededContext.jobStatus,
        titles: [],
        titleOrder: [],
      });
    });
  });

  describe('validateStepCompletion boxset', () => {
    it('treats boxset_id === __pending__ as not filled', () => {
      const result = service.validateStepCompletion('boxset', { boxset_id: '__pending__' } as any);
      expect(result.valid).toBeFalse();
      expect(result.errors.length).toBeGreaterThan(0);
    });

    it('accepts boxset when release_id is set AND required fields populated', () => {
      // #580: release_id alone no longer satisfies — name/slug/year required too.
      const result = service.validateStepCompletion('boxset', {
        release_id: 'r1',
        release_name: 'Venom (2018)',
        release_slug: 'venom-2018',
        release_year: 2018,
      } as any);
      expect(result.valid).toBeTrue();
    });

    it('accepts boxset when boxset_id is set AND required fields populated', () => {
      // #580: boxset_id alone no longer satisfies — same fields required.
      const result = service.validateStepCompletion('boxset', {
        boxset_id: 'b1',
        release_name: 'Venom (2018)',
        release_slug: 'venom-2018',
        release_year: 2018,
      } as any);
      expect(result.valid).toBeTrue();
    });

    it('rejects release_id with empty fields and surfaces what is missing (#580)', () => {
      const result = service.validateStepCompletion('boxset', {
        release_id: 'r1',
        release_name: '',
        release_slug: '',
        release_year: null,
      } as any);
      expect(result.valid).toBeFalse();
      const msg = result.errors[0] || '';
      expect(msg).toContain('release name');
      expect(msg).toContain('release slug');
      expect(msg).toContain('release year');
    });

    it('rejects release_id with just one field missing (#580)', () => {
      const result = service.validateStepCompletion('boxset', {
        release_id: 'r1',
        release_name: 'Venom (2018)',
        release_slug: 'venom-2018',
        release_year: null,
      } as any);
      expect(result.valid).toBeFalse();
      expect(result.errors[0]).toContain('release year');
      // Only the missing field is named, not the populated ones.
      expect(result.errors[0]).not.toContain('release name');
      expect(result.errors[0]).not.toContain('release slug');
    });
  });

  describe('validateStepCompletion titles and postprocess', () => {
    it('titles requires at least one track', () => {
      const result = service.validateStepCompletion('titles', { tracks: [] } as any);
      expect(result.valid).toBeFalse();
      expect(result.errors.some((e) => e.includes('title'))).toBeTrue();
    });

    it('titles accepts when tracks have names', () => {
      const result = service.validateStepCompletion('titles', { tracks: [{ title: 'Main', type: 'MainMovie' }] } as any);
      expect(result.valid).toBeTrue();
    });

    it('titles invalid when track has no type or name (grouped rules)', () => {
      const result = service.validateStepCompletion('titles', {
        tracks: [{ title_id: 'a', title: '', type: null }],
      } as any);
      expect(result.valid).toBeFalse();
      expect(result.errors.some((e) => e.includes('labeled'))).toBeTrue();
    });

    it('transfer does not require labelForm fields', () => {
      // #365 Phase 2 § 6.4 — was validateStepCompletion('postprocess', …)
      // before the standalone postprocess step was collapsed; the
      // transfer step inherits the same labelForm-agnostic behaviour.
      const result = service.validateStepCompletion('transfer', {} as any);
      expect(result.valid).toBeTrue();
    });
  });

  describe('continueToNextStep titles vs stale labelForm', () => {
    it('calls completeLabel when stepCompletionState.titles is true even if validateStepCompletion(titles) would fail', (done) => {
      // #365 Phase 2 § 6.4 — backend now returns workflow_step="transfer"
      // (was "postprocess").
      jobService.completeLabel.and.returnValue(
        of({ jobId: 'j1', workflow_step: 'transfer' } as any)
      );

      const seededContext: WorkflowContext = {
        id: 'j1',
        type: 'job',
        workflowStep: 'titles',
        discdbHit: false,
        labelForm: {
          movie_id: 'm1',
          release_id: 'r1',
          disc_name: 'Disc 1',
          disc_format: 'Blu-Ray',
          tracks: [{ title_id: 'a', title: '', type: null }],
        } as any,
        jobStatus: { jobId: 'j1', rip_state: 'completed' } as any,
        titles: [{ title_id: 'a', title: 'Main', type: 'MainMovie' }] as any,
        titleOrder: [],
        titlesComplete: true,
        stepCompletionState: {
          film: true,
          boxset: true,
          disc: true,
          titles: true,
          postprocess: false,
          transfer: false,
        },
        movieOptions: [],
        boxsetOptions: [],
        releaseOptions: [],
        groupOptions: [],
        discInfo: null,
        labelDraftProcessed: false,
        discNameLocked: false,
        discSlugLocked: false,
        isSeries: false,
        discMode: 'rip',
        lastReleaseDetails: null,
        releaseNameHint: '',
        releaseSlugHint: '',
        postProcessFiles: [],
        transferDestination: null,
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false,
      };

      (service as any)._activeContext$.next(seededContext);
      (service as any).syncStateFromContext(seededContext);

      expect(service.validateStepCompletion('titles', seededContext.labelForm).valid).toBeFalse();

      const obs = service.continueToNextStep();
      expect(obs).toBeTruthy();
      (obs as any).subscribe({
        next: () => {
          expect(jobService.completeLabel).toHaveBeenCalled();
          expect(logger.warn).not.toHaveBeenCalledWith(
            `Cannot continue: current step 'titles' is not complete:`,
            ['All titles must be labeled or ignored']
          );
          done();
        },
        error: done.fail,
      });
    });
  });

  describe('error handling', () => {
    it('surfaces error when getJobStatus fails', (done) => {
      service.setContextByCard({ type: 'drive', id: 'm1' }).subscribe({
        next: () => done.fail('expected error'),
        error: (err) => {
          expect(err?.message || err).toBeTruthy();
          done();
        },
      });

      const req = httpTestingController.expectOne(
        (r) => r.url.includes('discs/workflow-context') && r.params.get('mount_point') === 'm1'
      );
      req.flush('API error', { status: 500, statusText: 'Server Error' });
    });
  });

  describe('context_changed and determineWorkflowStep (titles->postprocess)', () => {
    it('skips context_changed fetch when job_id is in _postTransitionIgnore and within 500ms', () => {
      (service as any)._postTransitionIgnore = { jobId: 'j1', until: Date.now() + 5000 };
      const fetchJobSpy = spyOn(service as any, 'fetchJobWorkflowContextHttp').and.returnValue(of(null));
      const fetchDiscSpy = spyOn(service as any, 'fetchDiscWorkflowContextHttp').and.returnValue(of(null));

      (service as any).handleUnifiedMessage({ type: 'context_changed', job_id: 'j1' });

      expect(fetchJobSpy).not.toHaveBeenCalled();
      expect(fetchDiscSpy).not.toHaveBeenCalled();
    });

    it('skips context_changed fetch when _contextApplySuppressUntil is in future and message matches active card', () => {
      const ctx = { id: 'j1', type: 'job' as const, jobStatus: { jobId: 'j1' }, discInfo: null } as any;
      (service as any)._activeContext$.next(ctx);
      (service as any)._contextApplySuppressUntil = Date.now() + 10000;
      const fetchJobSpy = spyOn(service as any, 'fetchJobWorkflowContextHttp').and.returnValue(of(null));
      const fetchDiscSpy = spyOn(service as any, 'fetchDiscWorkflowContextHttp').and.returnValue(of(null));

      (service as any).handleUnifiedMessage({ type: 'context_changed', job_id: 'j1' });

      expect(fetchJobSpy).not.toHaveBeenCalled();
      expect(fetchDiscSpy).not.toHaveBeenCalled();
    });

    it('determineWorkflowStep returns transfer when post_state is ready and label_state is completed (miss)', () => {
      // #365 Phase 2 § 6.4 — postprocess collapsed into transfer's
      // preparing sub-phase; this state used to return 'postprocess'.
      const context: WorkflowContext = {
        id: 'job-1',
        type: 'job',
        discdbHit: false,
        labelForm: { movie_id: 'm1', tracks: [{ title_id: 't1', title: 'T1' }] } as any,
        jobStatus: {
          jobId: 'job-1',
          job_status: 'running',
          rip_state: 'completed',
          label_state: 'completed',
          post_state: 'ready',
        } as any,
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
        discMode: 'rip',
        lastReleaseDetails: null,
        releaseNameHint: '',
        releaseSlugHint: '',
        postProcessFiles: [],
        transferDestination: null,
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false
      };

      const step = service.determineWorkflowStep(context, {
        respectUserNavigation: true,
        considerJobStates: true,
        updateHighestStepVisited: false
      });

      expect(step).toBe('transfer');
    });
  });

  describe('WebSocket event handling: disc_ejected', () => {
    it('should reset ejected in_drive slot to empty drive and keep other drives', () => {
      // Setup: add a disc to the array
      const disc1 = {
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
        disc_hash: 'hash123',
        movie_name: 'Test Movie',
        disc_state: 'in_drive' as const,
        scan_state: 'ready' as const,
      };
      const disc2 = {
        disc_id: 'disc-2',
        disc_num: '2',
        mount_point: '/dev/sr1',
        disc_hash: 'hash456',
        movie_name: 'Another Movie',
        disc_state: 'in_drive' as const,
        scan_state: 'ready' as const,
      };
      (service as any)._discs.next([disc1, disc2]);

      // Act: simulate disc_ejected event
      (service as any).handleUnifiedMessage({
        type: 'disc_ejected',
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
      });

      // Assert: disc-1 slot becomes empty-*, disc-2 unchanged (rows are not removed)
      const discs = (service as any)._discs.value;
      expect(discs.length).toBe(2);
      expect(discs.find((d: any) => d.disc_id === 'disc-2')).toBeTruthy();
      const emptySlot = discs.find((d: any) => d.disc_id === 'empty-1');
      expect(emptySlot).toBeTruthy();
      expect(emptySlot.disc_state).toBe('in_drive');
      expect(emptySlot.mount_point).toBe('/dev/sr0');
      expect(discs.find((d: any) => d.disc_id === 'disc-1')).toBeUndefined();
    });

    it('should reset disc to empty drive slot when matched by mount_point', () => {
      const disc = {
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
        disc_state: 'in_drive' as const,
      };
      (service as any)._discs.next([disc]);

      (service as any).handleUnifiedMessage({
        type: 'disc_ejected',
        mount_point: '/dev/sr0',
      });

      const discs = (service as any)._discs.value;
      expect(discs.length).toBe(1);
      expect(discs[0].disc_id).toBe('empty-1');
      expect(discs[0].disc_state).toBe('in_drive');
    });

    it('should reset disc to empty drive slot when matched by disc_num', () => {
      const disc = {
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
        disc_state: 'in_drive' as const,
      };
      (service as any)._discs.next([disc]);

      (service as any).handleUnifiedMessage({
        type: 'disc_ejected',
        disc_num: '1',
      });

      const discs = (service as any)._discs.value;
      expect(discs.length).toBe(1);
      expect(discs[0].disc_id).toBe('empty-1');
      expect(discs[0].disc_state).toBe('in_drive');
    });

    it('should not convert unfinished job cards that share mount_point into empty drive slots', () => {
      const driveRow = {
        disc_id: 'disc-physical',
        disc_num: '1',
        mount_point: '/dev/sr0',
        disc_hash: 'abc',
        movie_name: 'In Tray',
        disc_state: 'in_drive' as const,
        scan_state: 'ready' as const,
      };
      const unfinishedOther = {
        disc_id: 'other-disc-id',
        mount_point: '/dev/sr0',
        disc_hash: 'def',
        movie_name: 'Other Job',
        disc_state: 'unfinished' as const,
        scan_state: 'ready' as const,
        job_id: 'job-other',
        job_status: 'failed' as const,
      };
      (service as any)._discs.next([driveRow, unfinishedOther]);

      (service as any).handleUnifiedMessage({
        type: 'disc_ejected',
        mount_point: '/dev/sr0',
        disc_id: 'disc-physical',
        disc_num: '1',
      });

      const discs = (service as any)._discs.value;
      const unfinished = discs.find((d: any) => d.job_id === 'job-other');
      expect(unfinished).toBeTruthy();
      expect(unfinished.disc_state).toBe('unfinished');
      expect(unfinished.movie_name).toBe('Other Job');
      expect(unfinished.disc_id).toBe('other-disc-id');

      const emptySlot = discs.find((d: any) => d.disc_id === 'empty-1');
      expect(emptySlot).toBeTruthy();
      expect(emptySlot.disc_state).toBe('in_drive');
    });

    it('should also remove from _insertedDiscs array', () => {
      const insertedDisc = {
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
        disc_hash: 'hash123',
      };
      (service as any)._insertedDiscs.next([insertedDisc]);

      (service as any).handleUnifiedMessage({
        type: 'disc_ejected',
        disc_id: 'disc-1',
        mount_point: '/dev/sr0',
      });

      const insertedDiscs = (service as any)._insertedDiscs.value;
      expect(insertedDiscs.length).toBe(0);
    });

    it('should not remove discs that do not match ejection criteria', () => {
      const disc1 = {
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
        disc_state: 'in_drive' as const,
      };
      const disc2 = {
        disc_id: 'disc-2',
        disc_num: '2',
        mount_point: '/dev/sr1',
        disc_state: 'in_drive' as const,
      };
      (service as any)._discs.next([disc1, disc2]);

      // Eject a disc that doesn't match any
      (service as any).handleUnifiedMessage({
        type: 'disc_ejected',
        disc_id: 'disc-99',
        mount_point: '/dev/sr99',
      });

      const discs = (service as any)._discs.value;
      expect(discs.length).toBe(2);
    });
  });

  describe('WebSocket event handling: disc_scanning after ejection', () => {
    it('should update empty drive slot when disc_scanning arrives after eject', () => {
      // Setup: add a disc
      const disc = {
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
        disc_state: 'in_drive' as const,
        scan_state: 'ready' as const,
      };
      (service as any)._discs.next([disc]);

      // Eject the disc
      (service as any).handleUnifiedMessage({
        type: 'disc_ejected',
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
      });

      // Eject resets the row to an empty in_drive slot (not removed)
      let discs = (service as any)._discs.value;
      expect(discs.length).toBe(1);
      expect(discs[0].disc_id).toBe('empty-1');

      // Simulate stale disc_scanning event
      (service as any).handleUnifiedMessage({
        type: 'disc_scanning',
        disc_id: 'disc-1',
        disc_num: '1',
        mount_point: '/dev/sr0',
      });

      discs = (service as any)._discs.value;
      expect(discs.length).toBe(1);
      const row = discs[0];
      expect(row.scan_state).toBe('scanning');
      expect(row.disc_id === 'disc-1' || String(row.disc_id).includes('pending')).toBe(true);
    });
  });

  describe('Metadata selection', () => {
    it('should clear release and boxset fields when selecting a new movie', (done) => {
      const seededContext: WorkflowContext = {
        id: 'mount2',
        type: 'drive',
        labelForm: {
          movie_id: 'movie-old',
          tmdb_id: 'old-tmdb',
          release_id: 'release-old',
          release_slug: 'old-release',
          release_name: 'Old Release',
          release_year: 2001,
          boxset_id: 'boxset-old',
          boxset_slug: 'old-boxset'
        } as any,
        jobStatus: null,
        discInfo: { disc_num: '2', mount_point: 'mount2' } as any,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false
      };

      // Set active context directly (no cache)
      (service as any)._activeContext$.next(seededContext);
      (service as any).syncStateFromContext(seededContext);
      // saveDiscWorkflowContext's tap(updatedContext) overwrites context; must emit a context
      // whose labelForm is the updated one (the 2nd arg), not seededContext
      spyOn(service, 'saveDiscWorkflowContext').and.callFake((_id: string, labelForm: any) =>
        of({ ...seededContext, labelForm } as any)
      );

      service.applyMetadataSelectionToActiveContext({
        movieId: 'movie-new',
        tmdbId: 'new-tmdb',
        releaseId: null,
        releaseSlug: null,
        releaseName: null,
        releaseYear: null,
        boxsetId: null,
        boxsetSlug: null
      }).subscribe({
        next: () => {
          const updated = service.getCurrentContext();
          expect(updated?.labelForm?.movie_id).toBe('movie-new');
          expect(updated?.labelForm?.tmdb_id).toBe('new-tmdb');
          expect(updated?.labelForm?.release_id).toBeNull();
          expect(updated?.labelForm?.release_slug).toBeNull();
          expect(updated?.labelForm?.release_name).toBeNull();
          expect(updated?.labelForm?.release_year).toBeNull();
          expect(updated?.labelForm?.boxset_id).toBeNull();
          expect(updated?.labelForm?.boxset_slug).toBeNull();
          done();
        },
        error: done.fail
      });
    });

    it('should strip stale tmdb_id when linking release without tmdbId in selection', (done) => {
      const jobId = '22222222-2222-2222-2222-222222222222';
      const seededContext: WorkflowContext = {
        id: jobId,
        type: 'job',
        labelForm: {
          movie_id: 'movie-keep',
          tmdb_id: 'stale-wrong-tmdb',
        } as any,
        jobStatus: {} as any,
        discInfo: { disc_id: 'd1', disc_num: '1' } as any,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false,
      };

      (service as any)._activeContext$.next(seededContext);
      (service as any).syncStateFromContext(seededContext);
      spyOn(service, 'saveJobWorkflowContext').and.callFake((_id: string, labelForm: any) =>
        of({ ...seededContext, labelForm } as any)
      );

      service
        .applyMetadataSelectionToActiveContext({
          releaseId: 'release-new',
          releaseSlug: 'new-slug',
          releaseName: 'New Name',
        })
        .subscribe({
          next: () => {
            expect(service.saveJobWorkflowContext).toHaveBeenCalled();
            const [, passed] = (service.saveJobWorkflowContext as jasmine.Spy).calls.mostRecent().args;
            expect(passed.movie_id).toBe('movie-keep');
            expect(passed.release_id).toBe('release-new');
            expect(passed.tmdb_id).toBeUndefined();
            done();
          },
          error: done.fail,
        });
    });

    it('should cascade-clear release, boxset, and movie hints when clearing movie (film step Change)', (done) => {
      const jobId = '11111111-1111-1111-1111-111111111111';
      const seededContext: WorkflowContext = {
        id: jobId,
        type: 'job',
        labelForm: {
          movie_id: 'movie-old',
          tmdb_id: 'tmdb-old',
          movie_name: 'Old',
          movie_production_year: 1999,
          movie_cover_url: 'http://cover',
          release_id: 'release-old',
          release_slug: 'old-slug',
          release_name: 'Old Release',
          release_year: 2001,
          boxset_id: 'boxset-old',
          boxset_slug: 'old-boxset',
          cover_front_url: 'http://front',
          cover_back_url: 'http://back',
          upc: '111',
          asin: 'B00OLD',
        } as any,
        jobStatus: {} as any,
        discInfo: { disc_id: 'd1', disc_num: '1' } as any,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false,
      };

      (service as any)._activeContext$.next(seededContext);
      (service as any).syncStateFromContext(seededContext);
      spyOn(service, 'saveJobWorkflowContext').and.callFake((_id: string, labelForm: any) =>
        of({ ...seededContext, labelForm } as any)
      );

      service.applyMetadataSelectionToActiveContext({ movieId: null }).subscribe({
        next: () => {
          expect(service.saveJobWorkflowContext).toHaveBeenCalled();
          const [, passed] = (service.saveJobWorkflowContext as jasmine.Spy).calls.mostRecent().args;
          expect(passed.movie_id).toBeNull();
          expect(passed.release_id).toBeNull();
          expect(passed.boxset_id).toBeNull();
          expect(passed.boxset_slug).toBeNull();
          expect(passed.release_slug).toBeNull();
          expect(passed.tmdb_id).toBeNull();
          expect(passed.upc).toBeNull();
          done();
        },
        error: done.fail,
      });
    });
  });

  // ---- TMDB episode catalog (#370) -------------------------------------

  describe('TMDB episode catalog prefetch (#370)', () => {
    const apiBase = '/api';  // environment.apiBase value used in test build

    function seedSeriesContext(opts: { tmdb_id: string; hintSeason?: number; isResume?: boolean }) {
      const ctx: WorkflowContext = {
        id: 'job-1', type: 'job',
        labelForm: {
          group_type: 'series',
          tmdb_id: opts.isResume ? opts.tmdb_id : '',
          tracks: [],
        },
        jobStatus: null,
        discInfo: opts.hintSeason
          ? ({ tmdb_suggestion: { hints: { season: opts.hintSeason } } } as any)
          : null,
        titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
        labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: true, discdbHit: false, discMode: 'rip',
        lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '',
        postProcessFiles: [], transferDestination: null, releaseDiscs: [], boxsetMovies: [],
        movieCover: null, movieName: null, productionYear: null,
        labelSaving: false, lastAutosaveOk: true, hasLabelContent: false,
        devMode: false, showTitleStatus: false,
      };
      (service as any)._activeContext$.next(ctx);
    }

    it('Path A: createAndLink with tmdb_type=tv triggers /seasons/1/episodes when no hint', (done) => {
      seedSeriesContext({ tmdb_id: '60625' });
      // Stub the underlying create-and-link method since this test only cares
      // about the tap() prefetch hook in createAndLinkMovieToActiveContext.
      spyOn(service, 'createAndLinkMovie').and.returnValue(of({
        movie: { tmdb_id: '60625', tmdb_type: 'tv' },
        linked: true,
      } as any));

      service.createAndLinkMovieToActiveContext({ tmdb_id: '60625', tmdb_type: 'tv' }).subscribe({
        next: () => {
          const req = httpTestingController.expectOne(`${apiBase}/movies/60625/seasons/1/episodes`);
          req.flush({
            tmdb_id: '60625', season_number: 1, episodes: [],
            number_of_seasons: 4, series_name: 'Rick and Morty',
          });
          const ctx = (service as any)._activeContext$.value;
          expect(ctx.tmdbEpisodeCatalog?.numberOfSeasons).toBe(4);
          expect(ctx.tmdbEpisodeCatalog?.seriesName).toBe('Rick and Morty');
          expect(ctx.labelForm?.primary_season).toBe(1);
          done();
        },
        error: done.fail,
      });
    });

    it('Path A: hints.season=3 triggers /seasons/3/episodes and seeds primary_season=3', (done) => {
      seedSeriesContext({ tmdb_id: '106379', hintSeason: 3 });
      spyOn(service, 'createAndLinkMovie').and.returnValue(of({
        movie: { tmdb_id: '106379', tmdb_type: 'tv' },
        linked: true,
      } as any));

      service.createAndLinkMovieToActiveContext({ tmdb_id: '106379', tmdb_type: 'tv' }).subscribe({
        next: () => {
          httpTestingController.expectNone(`${apiBase}/movies/106379/seasons/1/episodes`);
          const req = httpTestingController.expectOne(`${apiBase}/movies/106379/seasons/3/episodes`);
          req.flush({ tmdb_id: '106379', season_number: 3, episodes: [], number_of_seasons: 5, series_name: 'Fallout' });
          expect((service as any)._activeContext$.value.labelForm.primary_season).toBe(3);
          done();
        },
        error: done.fail,
      });
    });

    it('Path A: tmdb_type=movie does not fire any catalog request', (done) => {
      seedSeriesContext({ tmdb_id: 'irrelevant' });
      // Movie selection — flip group_type so isSeries check fails.
      (service as any)._activeContext$.value.labelForm.group_type = 'movie';
      (service as any)._activeContext$.value.isSeries = false;
      spyOn(service, 'createAndLinkMovie').and.returnValue(of({
        movie: { tmdb_id: '475557', tmdb_type: 'movie' },
        linked: true,
      } as any));

      service.createAndLinkMovieToActiveContext({ tmdb_id: '475557', tmdb_type: 'movie' }).subscribe({
        next: () => {
          httpTestingController.expectNone((req) => /\/seasons\/.+\/episodes/.test(req.url));
          done();
        },
        error: done.fail,
      });
    });

    it('fetchTmdbSeasonEpisodes dedupes concurrent calls for the same (id, season)', () => {
      // No active context needed; this exercises the cache layer directly.
      const sub1 = service.fetchTmdbSeasonEpisodes('1', 1).subscribe();
      const sub2 = service.fetchTmdbSeasonEpisodes('1', 1).subscribe();
      const req = httpTestingController.expectOne(`${apiBase}/movies/1/seasons/1/episodes`);
      req.flush({ tmdb_id: '1', season_number: 1, episodes: [], number_of_seasons: 1, series_name: null });
      sub1.unsubscribe();
      sub2.unsubscribe();
      // A third call after the first lands hits the success cache.
      service.fetchTmdbSeasonEpisodes('1', 1).subscribe();
      httpTestingController.expectNone(`${apiBase}/movies/1/seasons/1/episodes`);
    });

    it('setPrimarySeason propagates to unedited tracks only', () => {
      const ctx: WorkflowContext = {
        id: 'job-x', type: 'job',
        labelForm: {
          group_type: 'series', tmdb_id: '60625',
          primary_season: 1,
          tracks: [
            { title_id: 'a', season: 1 },        // inherits — should follow
            { title_id: 'b', season: 2 },        // overridden — should stay
            { title_id: 'c', season: null },     // null — should follow
          ],
        },
        jobStatus: null, discInfo: null,
        titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
        labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: true, discdbHit: false, discMode: 'rip',
        lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '',
        postProcessFiles: [], transferDestination: null, releaseDiscs: [], boxsetMovies: [],
        movieCover: null, movieName: null, productionYear: null,
        labelSaving: false, lastAutosaveOk: true, hasLabelContent: false,
        devMode: false, showTitleStatus: false,
        tmdbEpisodeCatalog: {
          tmdb_id: '60625', numberOfSeasons: 4, seriesName: 'Rick and Morty',
          seasons: new Map(), loadingSeasons: new Set(), errorSeasons: new Set(),
        },
      };
      (service as any)._activeContext$.next(ctx);
      service.setPrimarySeason(3);
      const updated = (service as any)._activeContext$.value.labelForm;
      expect(updated.primary_season).toBe(3);
      expect(updated.tracks[0].season).toBe(3);    // was 1 (old primary) → 3
      expect(updated.tracks[1].season).toBe(2);    // overridden → unchanged
      expect(updated.tracks[2].season).toBe(3);    // was null → 3

      // Side-effect: kicks a fetch for the new primary.
      httpTestingController.expectOne(`${apiBase}/movies/60625/seasons/3/episodes`).flush({
        tmdb_id: '60625', season_number: 3, episodes: [],
        number_of_seasons: 4, series_name: 'Rick and Morty',
      });
    });

    it('setPrimarySeason persists via PATCH workflow-context when disc_id is set (#536)', () => {
      const discId = 'disc-uuid-1';
      const ctx: WorkflowContext = {
        id: 'job-x', type: 'job',
        labelForm: { group_type: 'series', tmdb_id: '60625', primary_season: 1, tracks: [] },
        jobStatus: null, discInfo: { disc_id: discId } as any,
        titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
        labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: true, discdbHit: false, discMode: 'rip',
        lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '',
        postProcessFiles: [], transferDestination: null, releaseDiscs: [], boxsetMovies: [],
        movieCover: null, movieName: null, productionYear: null,
        labelSaving: false, lastAutosaveOk: true, hasLabelContent: false,
        devMode: false, showTitleStatus: false,
        tmdbEpisodeCatalog: {
          tmdb_id: '60625', numberOfSeasons: 4, seriesName: 'Rick and Morty',
          seasons: new Map(), loadingSeasons: new Set(), errorSeasons: new Set(),
        },
      };
      (service as any)._activeContext$.next(ctx);

      service.setPrimarySeason(2);

      const patch = httpTestingController.expectOne(`${apiBase}/discs/${discId}/workflow-context`);
      expect(patch.request.method).toBe('PATCH');
      expect(patch.request.body?.labelForm?.primary_season).toBe(2);
      patch.flush({});

      // Also drains the episode-fetch side-effect so the test doesn't fail
      // with an outstanding-request assertion at teardown.
      httpTestingController.expectOne(`${apiBase}/movies/60625/seasons/2/episodes`).flush({
        tmdb_id: '60625', season_number: 2, episodes: [],
        number_of_seasons: 4, series_name: 'Rick and Morty',
      });
    });

    it('buildLabelForm preserves primary_season from the backend draft (#536)', () => {
      // `buildLabelForm` is a private whitelist used by the JOB-context path.
      // Without primary_season in the whitelist the field gets dropped, and
      // the disc-card season selector reverts to Season 1 on every job-context
      // load even though the backend persisted the user's pick.
      const draft = { primary_season: 3, group_type: 'series', tmdb_id: '60625' };
      const form = (service as any).buildLabelForm(draft, false, null, null);
      expect(form.primary_season).toBe(3);
    });

    it('buildLabelForm coerces missing/zero/negative primary_season to null', () => {
      for (const bad of [undefined, null, 0, -1, 'junk']) {
        const draft = { primary_season: bad, group_type: 'series' };
        const form = (service as any).buildLabelForm(draft, false, null, null);
        expect(form.primary_season).withContext(`bad=${String(bad)}`).toBeNull();
      }
    });

    it('setPrimarySeason skips PATCH when there is no disc_id', () => {
      const ctx: WorkflowContext = {
        id: 'job-x', type: 'job',
        labelForm: { group_type: 'series', tmdb_id: '60625', primary_season: 1, tracks: [] },
        jobStatus: null, discInfo: null,
        titles: [], titleOrder: [], titlesComplete: false,
        movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
        labelDraftProcessed: false, discNameLocked: false, discSlugLocked: false,
        isSeries: true, discdbHit: false, discMode: 'rip',
        lastReleaseDetails: null, releaseNameHint: '', releaseSlugHint: '',
        postProcessFiles: [], transferDestination: null, releaseDiscs: [], boxsetMovies: [],
        movieCover: null, movieName: null, productionYear: null,
        labelSaving: false, lastAutosaveOk: true, hasLabelContent: false,
        devMode: false, showTitleStatus: false,
        tmdbEpisodeCatalog: {
          tmdb_id: '60625', numberOfSeasons: 4, seriesName: 'Rick and Morty',
          seasons: new Map(), loadingSeasons: new Set(), errorSeasons: new Set(),
        },
      };
      (service as any)._activeContext$.next(ctx);

      service.setPrimarySeason(2);

      // No discs/{id}/workflow-context PATCH should fire.
      httpTestingController.expectNone(`${apiBase}/discs/undefined/workflow-context`);
      // Drain the episode-fetch.
      httpTestingController.expectOne(`${apiBase}/movies/60625/seasons/2/episodes`).flush({
        tmdb_id: '60625', season_number: 2, episodes: [],
        number_of_seasons: 4, series_name: 'Rick and Morty',
      });
    });
  });

  // #604: Verifying spinner stuck after rip — root cause was that the
  // in-process rip-progress emit suppressed context_changed AND dropped
  // rip_state from the payload. Frontend now copies rip_state from the
  // progress message onto the local jobStatus.
  describe('progress_update propagates rip_state (#604)', () => {
    const KEY = 'job:job-604';

    function activateContextWithJobId(jobId: string, ripState: string | null = 'running') {
      const ctx = {
        id: 'job-604', type: 'job', mode: 'workflow',
        jobStatus: {
          jobId, job_status: 'running', rip_state: ripState,
          rip_progress: 50, rip_phase: 'verification', post_progress: 0,
        } as any,
        discInfo: { disc_id: 'd-604' } as any,
      } as any;
      (service as any)._activeContext$.next(ctx);
    }

    function sendProgress(msg: any) {
      (service as any).handleWorkflowMessage(KEY, msg);
    }

    it('copies rip_state from a progress_update onto the pending jobStatus', () => {
      activateContextWithJobId('job-604', 'running');
      sendProgress({
        type: 'progress_update', job_id: 'job-604',
        rip_progress: 100, rip_phase: null, rip_state: 'completed',
        post_progress: 0,
      });
      const pending = (service as any)._pendingProgressUpdate;
      expect(pending).toBeTruthy();
      expect(pending.jobId).toBe('job-604');
      // The verifying spinner clears as soon as rip_state flips to
      // 'completed' (calculateStageProgress gates on `ripState !==
      // 'completed'` at the >= 100 threshold) — that's the load-bearing
      // signal #604 fixes.
      expect(pending.jobStatus.rip_state).toBe('completed');
    });

    it('preserves the existing rip_state when the message omits it (defensive)', () => {
      activateContextWithJobId('job-604', 'running');
      sendProgress({
        type: 'progress_update', job_id: 'job-604',
        rip_progress: 75, rip_phase: 'copy',
        post_progress: 0,
        // rip_state intentionally omitted — mid-rip ticks shouldn't clobber it.
      });
      const pending = (service as any)._pendingProgressUpdate;
      expect(pending.jobStatus.rip_state).toBe('running');
    });

    it('uses null when neither the message nor the existing jobStatus has rip_state', () => {
      // Fresh context with jobStatus.rip_state explicitly null.
      activateContextWithJobId('job-604', null);
      sendProgress({
        type: 'progress_update', job_id: 'job-604',
        rip_progress: 10, rip_phase: 'copy',
        post_progress: 0,
      });
      const pending = (service as any)._pendingProgressUpdate;
      expect(pending.jobStatus.rip_state).toBeNull();
    });
  });

  // #605: progress_update propagates post_state + transfer_state — same
  // staleness pattern as #604's rip_state, applied to the other two stage
  // states so the transfer-step UI advances under the auto-dispatch flow
  // without depending on the debounced context_changed refetch.
  describe('progress_update propagates post_state + transfer_state (#605)', () => {
    const KEY = 'job:job-605';

    function activateContextWithStates(
      jobId: string,
      ripState: string | null = 'completed',
      postState: string | null = 'ready',
      transferState: string | null = 'ready',
    ) {
      const ctx = {
        id: 'job-605', type: 'job', mode: 'workflow',
        jobStatus: {
          jobId, job_status: 'running',
          rip_state: ripState,
          post_state: postState,
          transfer_state: transferState,
          rip_progress: 100, post_progress: 0,
        } as any,
        discInfo: { disc_id: 'd-605' } as any,
      } as any;
      (service as any)._activeContext$.next(ctx);
    }

    function sendProgress(msg: any) {
      (service as any).handleWorkflowMessage(KEY, msg);
    }

    it('copies post_state from a progress_update onto the pending jobStatus', () => {
      activateContextWithStates('job-605', 'completed', 'ready', 'ready');
      sendProgress({
        type: 'progress_update', job_id: 'job-605',
        rip_progress: 100, post_progress: 50,
        post_state: 'running',
        transfer_state: 'ready',
      });
      const pending = (service as any)._pendingProgressUpdate;
      expect(pending.jobStatus.post_state).toBe('running');
    });

    it('copies transfer_state from a progress_update onto the pending jobStatus', () => {
      // The exact bug scenario: backend auto-dispatches transfer, flips
      // transfer_state ready → running; without #605 the frontend stayed
      // at 'ready' and the CTA gate stuck in pre-prep mode.
      activateContextWithStates('job-605', 'completed', 'completed', 'ready');
      sendProgress({
        type: 'progress_update', job_id: 'job-605',
        rip_progress: 100, post_progress: 100,
        transfer_state: 'running',
        transfer_progress: 10,
      });
      const pending = (service as any)._pendingProgressUpdate;
      expect(pending.jobStatus.transfer_state).toBe('running');
    });

    it('preserves existing post_state / transfer_state when the message omits them', () => {
      activateContextWithStates('job-605', 'completed', 'running', 'pending');
      sendProgress({
        type: 'progress_update', job_id: 'job-605',
        rip_progress: 100, post_progress: 75,
        // post_state / transfer_state intentionally omitted.
      });
      const pending = (service as any)._pendingProgressUpdate;
      expect(pending.jobStatus.post_state).toBe('running');
      expect(pending.jobStatus.transfer_state).toBe('pending');
    });

    it('flips transfer_state to completed for the end-of-transfer message', () => {
      // Symmetric to #604's verifying → terminal repro: the final progress
      // tick after _post_transfer_complete_callback runs should carry
      // transfer_state='completed' so the workflow-actions CTA flips to
      // "Finish" without waiting for the HTTP refetch.
      activateContextWithStates('job-605', 'completed', 'completed', 'running');
      sendProgress({
        type: 'progress_update', job_id: 'job-605',
        rip_progress: 100, post_progress: 100,
        transfer_progress: 100,
        transfer_state: 'completed',
      });
      const pending = (service as any)._pendingProgressUpdate;
      expect(pending.jobStatus.transfer_state).toBe('completed');
    });
  });

  describe('#693: card switch must not bleed previous card state', () => {
    /** Minimal job context for seeding active/cache state (no saveCallback on purpose). */
    function mkCtx(id: string, workflowStep: string): WorkflowContext {
      return {
        id,
        type: 'job',
        workflowStep,
        labelForm: {},
        jobStatus: null,
        discInfo: null,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
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
        releaseDiscs: [],
        boxsetMovies: [],
        movieCover: null,
        movieName: null,
        productionYear: null,
        labelSaving: false,
        lastAutosaveOk: false,
        hasLabelContent: false,
        devMode: false,
        showTitleStatus: false,
      } as unknown as WorkflowContext;
    }

    it('carousel ordering (selectedCard advanced first) does not poison the new card cache', (done) => {
      // Viewing job A on 'transfer'…
      (service as any)._activeContext$.next(mkCtx('jobA', 'transfer'));
      service.setSelectedCard({ type: 'job', id: 'jobA' });
      // …then the carousel clicks job B: selectedCard is advanced BEFORE the switch.
      service.setSelectedCard({ type: 'job', id: 'jobB' });
      service.setContextByCard({ type: 'job', id: 'jobB' }).subscribe((ctx) => {
        expect(ctx.id).toBe('jobB');
        expect(ctx.workflowStep).toBe('film'); // B's own step, not A's 'transfer'
        const recachedB = (service as any).getCachedContext('job:jobB');
        expect(recachedB.workflowStep).toBe('film'); // re-cache holds fresh truth
        done();
      });
      // Pre-flush: A saved under A's OWN key; B's key must not hold A's context.
      expect((service as any).getCachedContext('job:jobA')?.id).toBe('jobA');
      expect((service as any).getCachedContext('job:jobB')?.id === 'jobA').toBe(false);
      const req = httpTestingController.expectOne((r) => r.url.includes('jobs/jobB/workflow-context'));
      req.flush({ type: 'job', id: 'jobB', labelForm: { workflow_step: 'film' }, jobStatus: null });
    });

    it('a foreign on-screen context cannot donate its workflowStep to a fetched card', (done) => {
      // B was legitimately viewed earlier (cached at 'titles').
      (service as any).cacheContext('job:jobB', mkCtx('jobB', 'titles'));
      service.setSelectedCard({ type: 'job', id: 'jobB' });
      service.setContextByCard({ type: 'job', id: 'jobB' }).subscribe((ctx) => {
        expect(ctx.workflowStep).toBe('film'); // fresh truth wins over the foreign 'transfer'
        done();
      });
      // Race: another card's context lands on screen while B's fetch is in flight.
      (service as any)._activeContext$.next(mkCtx('jobA', 'transfer'));
      const req = httpTestingController.expectOne((r) => r.url.includes('jobs/jobB/workflow-context'));
      req.flush({ type: 'job', id: 'jobB', labelForm: { workflow_step: 'film' }, jobStatus: null });
    });

    it('same-card navigation while fresh data loads is still preserved (#617 semantics)', (done) => {
      (service as any).cacheContext('job:jobB', mkCtx('jobB', 'film'));
      service.setSelectedCard({ type: 'job', id: 'jobB' });
      service.setContextByCard({ type: 'job', id: 'jobB' }).subscribe((ctx) => {
        expect(ctx.workflowStep).toBe('disc'); // the user's in-flight navigation survives
        done();
      });
      // User navigates on the SAME card while the fetch is in flight.
      (service as any)._activeContext$.next(mkCtx('jobB', 'disc'));
      const req = httpTestingController.expectOne((r) => r.url.includes('jobs/jobB/workflow-context'));
      req.flush({ type: 'job', id: 'jobB', labelForm: { workflow_step: 'film' }, jobStatus: null });
    });
  });
});
