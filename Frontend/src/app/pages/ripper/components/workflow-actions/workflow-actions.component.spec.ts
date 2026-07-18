import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { of, BehaviorSubject } from 'rxjs';
import { take } from 'rxjs/operators';
import { WorkflowActionsComponent } from './workflow-actions.component';
import { WorkflowService, WorkflowContext, WorkflowStep } from '../../../../services/workflow.service';
import { LoggerService } from '../../../../services/logger.service';
import { ToastService } from '../../../../services/toast.service';
import { DriveSnapshotService } from '../../../../services/drive-snapshot.service';

describe('WorkflowActionsComponent', () => {
  let component: WorkflowActionsComponent;
  let fixture: ComponentFixture<WorkflowActionsComponent>;
  let loggerWarnSpy: jasmine.Spy;
  let mockWorkflow: {
    getActiveContext: jasmine.Spy;
    tryRecoverStartRipAfterAmbiguousError: jasmine.Spy;
    getStartRipInProgress$: jasmine.Spy;
    getFilmStepSaveInProgress$: jasmine.Spy;
    getDiscStepContinueInProgress$: jasmine.Spy;
    saveDiscStepAndContinueToNext: jasmine.Spy;
    getStageProgress$: jasmine.Spy;
    getStageCompletion$: jasmine.Spy;
    startRip: jasmine.Spy;
    goToStep: jasmine.Spy;
    navigateToPreviousStep: jasmine.Spy;
    validateStepCompletion: jasmine.Spy;
    getStepCompletionState: jasmine.Spy;
    determineWorkflowStep: jasmine.Spy;
    getEffectiveWorkflowStep: jasmine.Spy;
    canNavigateToStep: jasmine.Spy;
    resumeJob: jasmine.Spy;
    startPostProcess: jasmine.Spy;
    continueToNextStep: jasmine.Spy;
    advanceStepTo: jasmine.Spy;
    getSelectedCard$: jasmine.Spy;
    discs$: any;
  };

  beforeEach(async () => {
    loggerWarnSpy = jasmine.createSpy('loggerWarn');
    mockWorkflow = {
      getActiveContext: jasmine.createSpy('getActiveContext').and.returnValue(of(null)),
      tryRecoverStartRipAfterAmbiguousError: jasmine
        .createSpy('tryRecoverStartRipAfterAmbiguousError')
        .and.returnValue(of(false)),
      getStartRipInProgress$: jasmine.createSpy('getStartRipInProgress$').and.returnValue(of(false)),
      getFilmStepSaveInProgress$: jasmine.createSpy('getFilmStepSaveInProgress$').and.returnValue(of(false)),
      getDiscStepContinueInProgress$: jasmine.createSpy('getDiscStepContinueInProgress$').and.returnValue(of(false)),
      saveDiscStepAndContinueToNext: jasmine.createSpy('saveDiscStepAndContinueToNext').and.returnValue(of(undefined)),
      getStageProgress$: jasmine.createSpy('getStageProgress$').and.returnValue(of({})),
      getStageCompletion$: jasmine.createSpy('getStageCompletion$').and.returnValue(of({})),
      startRip: jasmine.createSpy('startRip').and.returnValue(of(undefined)),
      goToStep: jasmine.createSpy('goToStep').and.returnValue(of(undefined)),
      navigateToPreviousStep: jasmine.createSpy('navigateToPreviousStep'),
      validateStepCompletion: jasmine.createSpy('validateStepCompletion').and.returnValue({ valid: true, errors: [] }),
      getStepCompletionState: jasmine.createSpy('getStepCompletionState'),
      determineWorkflowStep: jasmine.createSpy('determineWorkflowStep').and.returnValue('titles'),
      getEffectiveWorkflowStep: jasmine.createSpy('getEffectiveWorkflowStep').and.callFake((c: WorkflowContext | null): WorkflowStep => {
        if (!c) return 'film';
        // #365 Phase 2 § 6.4 — 'postprocess' removed from both orders.
        const order: WorkflowStep[] = c.discdbHit
          ? ['summary', 'transfer']
          : ['film', 'boxset', 'disc', 'titles', 'transfer'];
        const fromJob = (c.jobStatus as { workflow_step?: string } | undefined)?.workflow_step;
        if (fromJob && order.includes(fromJob as WorkflowStep)) {
          return fromJob as WorkflowStep;
        }
        const fromContext = c.workflowStep;
        if (fromContext && order.includes(fromContext as WorkflowStep)) {
          return fromContext as WorkflowStep;
        }
        return (mockWorkflow.determineWorkflowStep(c) as WorkflowStep) || 'film';
      }),
      canNavigateToStep: jasmine.createSpy('canNavigateToStep').and.returnValue({ allowed: true, reason: '' }),
      resumeJob: jasmine.createSpy('resumeJob').and.returnValue(of(undefined)),
      startPostProcess: jasmine.createSpy('startPostProcess').and.returnValue(of(undefined)),
      continueToNextStep: jasmine.createSpy('continueToNextStep').and.returnValue(null),
      advanceStepTo: jasmine.createSpy('advanceStepTo').and.returnValue(of(undefined)),
      // #571 additions — drive-presence tri-state combines getSelectedCard$
      // with discs$ to decide if the CTA should override to "Insert Disc"
      // or "Drive Not Connected". Stub both as empty so the gate is
      // permissive (presence='available'); none of these tests exercise it.
      getSelectedCard$: jasmine.createSpy('getSelectedCard$').and.returnValue(of(null)),
      discs$: of([]),
    };
    // #573 added DriveSnapshotService injection but the spec scaffolding
    // wasn't updated; without this stub every test in the file hit
    // ``NullInjectorError: No provider for HttpClient``. Stub it with an
    // empty observable so the drive-presence tri-state is permissive (the
    // tests below predate #571 and don't exercise the gate).
    const mockDriveSnapshot = {
      drives$: of([]),
      current: () => [] as any[],
      startPolling: jasmine.createSpy('startPolling'),
      stopPolling: jasmine.createSpy('stopPolling'),
    };

    await TestBed.configureTestingModule({
      imports: [WorkflowActionsComponent],
      providers: [
        { provide: WorkflowService, useValue: mockWorkflow },
        { provide: LoggerService, useValue: { warn: loggerWarnSpy } },
        { provide: ToastService, useValue: { show: () => {} } },
        { provide: DriveSnapshotService, useValue: mockDriveSnapshot },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(WorkflowActionsComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('context$ and other observables are defined', () => {
    expect(component.context$).toBeDefined();
    expect(component.stageTimeline$).toBeDefined();
    expect(component.canContinue$).toBeDefined();
    expect(component.canGoBack$).toBeDefined();
  });

  it('onContinue and onBack can be invoked without throw', () => {
    expect(() => component.onContinue()).not.toThrow();
    expect(() => component.onBack()).not.toThrow();
  });

  describe('canContinue$ reactive updates for titles step', () => {
    let contextSubject: BehaviorSubject<WorkflowContext | null>;
    let testComponent: WorkflowActionsComponent;
    let testFixture: ComponentFixture<WorkflowActionsComponent>;

    beforeEach(() => {
      loggerWarnSpy.calls.reset();
      contextSubject = new BehaviorSubject<WorkflowContext | null>(null);
      mockWorkflow.getActiveContext.and.returnValue(contextSubject.asObservable());
      mockWorkflow.getStartRipInProgress$.and.returnValue(of(false));
      mockWorkflow.getFilmStepSaveInProgress$.and.returnValue(of(false));
      mockWorkflow.validateStepCompletion.and.returnValue({ valid: true, errors: [] });
      mockWorkflow.determineWorkflowStep.and.returnValue('titles');
      mockWorkflow.canNavigateToStep.and.returnValue({ allowed: true, reason: '' });
      
      // Create a new component instance with the updated mocks
      testFixture = TestBed.createComponent(WorkflowActionsComponent);
      testComponent = testFixture.componentInstance;
      testFixture.detectChanges();
    });

    it('should emit false when titles are incomplete on titles step', (done) => {
      const context: WorkflowContext = {
        id: 'test-job',
        type: 'job',
        workflowStep: 'titles',
        discdbHit: false,
        labelForm: {
          movie_id: 'movie-1',
          release_id: 'release-1',
          disc_name: 'Disc 1',
          disc_format: 'Blu-Ray'
        },
        jobStatus: {
          job_status: 'completed',
          rip_state: 'completed'
        } as any,
        titles: [{ title_id: 'title-1', title: '', type: null }] as any,
        titleOrder: [],
        titlesComplete: true,
        stepCompletionState: {
          film: true,
          boxset: true,
          disc: true,
          titles: false, // Titles incomplete
          postprocess: false,
          transfer: false
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
        showTitleStatus: false
      };

      contextSubject.next(context);
      testFixture.detectChanges();

      testComponent.canContinue$.pipe(take(1)).subscribe(canContinue => {
        expect(canContinue).toBe(false);
        done();
      });
    });

    it('should emit true when titles completion changes from false to true (simulating WebSocket update)', (done) => {
      const incompleteContext: WorkflowContext = {
        id: 'test-job',
        type: 'job',
        workflowStep: 'titles',
        discdbHit: false,
        labelForm: {
          movie_id: 'movie-1',
          release_id: 'release-1',
          disc_name: 'Disc 1',
          disc_format: 'Blu-Ray'
        },
        jobStatus: {
          job_status: 'completed',
          rip_state: 'completed'
        } as any,
        titles: [{ title_id: 'title-1', title: '', type: null }] as any,
        titleOrder: [],
        titlesComplete: true,
        stepCompletionState: {
          film: true,
          boxset: true,
          disc: true,
          titles: false, // Initially incomplete
          postprocess: false,
          transfer: false
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
        showTitleStatus: false
      };

      const completeContext: WorkflowContext = {
        ...incompleteContext,
        titles: [{ title_id: 'title-1', title: 'Main Movie', type: 'MainMovie' }] as any,
        stepCompletionState: {
          film: true,
          boxset: true,
          disc: true,
          titles: true, // Now complete (simulating WebSocket update)
          postprocess: false,
          transfer: false
        }
      };

      // Set initial incomplete state
      contextSubject.next(incompleteContext);
      testFixture.detectChanges();

      // Track emissions
      const emissions: boolean[] = [];
      testComponent.canContinue$.pipe(take(2)).subscribe(canContinue => {
        emissions.push(canContinue);
        if (emissions.length === 2) {
          // First emission should be false, second should be true
          expect(emissions[0]).toBe(false);
          expect(emissions[1]).toBe(true);
          done();
        }
      });

      // Simulate WebSocket update: titles become complete
      contextSubject.next(completeContext);
      testFixture.detectChanges();
    });

    it('should bypass validateStepCompletion for titles step when stepCompletionState.titles is true (fixes stale labelForm.tracks issue)', (done) => {
      // Reset the spy to track calls
      mockWorkflow.validateStepCompletion.calls.reset();
      
      // Set up a scenario where stepCompletionState.titles is true (correct, updated via WebSocket)
      // but labelForm.tracks would fail validation (stale data)
      const context: WorkflowContext = {
        id: 'test-job',
        type: 'job',
        workflowStep: 'titles',
        discdbHit: false,
        labelForm: {
          movie_id: 'movie-1',
          release_id: 'release-1',
          disc_name: 'Disc 1',
          disc_format: 'Blu-Ray',
          // Stale tracks data - would fail validation (no names)
          tracks: [{ title: '', type: null }] as any
        },
        jobStatus: {
          job_status: 'completed',
          rip_state: 'completed'
        } as any,
        titles: [{ title_id: 'title-1', title: 'Main Movie', type: 'MainMovie' }] as any,
        titleOrder: [],
        titlesComplete: true,
        stepCompletionState: {
          film: true,
          boxset: true,
          disc: true,
          titles: true, // Correct: updated reactively via WebSocket
          postprocess: false,
          transfer: false
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
        showTitleStatus: false
      };

      contextSubject.next(context);
      testFixture.detectChanges();

      testComponent.canContinue$.pipe(take(1)).subscribe(canContinue => {
        // Should return true because stepCompletionState.titles is true
        // (bypassing validateStepCompletion which would fail on stale labelForm.tracks)
        expect(canContinue).toBe(true);
        
        // Verify validateStepCompletion was NOT called for titles step in canContinue$ logic
        // (it should be bypassed for titles step)
        const titlesStepCalls = mockWorkflow.validateStepCompletion.calls.all().filter(
          call => call.args[0] === 'titles'
        );
        expect(titlesStepCalls.length).toBe(0, 'validateStepCompletion should not be called for titles step in canContinue$');
        
        done();
      });
    });

    it('onContinue proceeds when stepCompletionState.titles is true even if validateStepCompletion would fail (stale tracks)', fakeAsync(() => {
      mockWorkflow.validateStepCompletion.and.returnValue({ valid: false, errors: ['1 title(s) need names'] });
      mockWorkflow.continueToNextStep.and.returnValue(of(undefined));

      const context: WorkflowContext = {
        id: 'test-job',
        type: 'job',
        workflowStep: 'titles',
        discdbHit: false,
        labelForm: {
          movie_id: 'movie-1',
          release_id: 'release-1',
          disc_name: 'Disc 1',
          disc_format: 'Blu-Ray',
          tracks: [{ title: '', type: null }] as any,
        },
        jobStatus: {
          job_status: 'completed',
          rip_state: 'completed',
        } as any,
        titles: [{ title_id: 'title-1', title: 'Main Movie', type: 'MainMovie' }] as any,
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

      contextSubject.next(context);
      testFixture.detectChanges();

      testComponent.onContinue();
      tick();

      expect(loggerWarnSpy).not.toHaveBeenCalledWith('Cannot continue: all titles must be labeled or ignored');
      expect(mockWorkflow.continueToNextStep).toHaveBeenCalled();
    }));
  });

  describe('transfer step with failed post_state (was: postprocess step)', () => {
    // #365 Phase 2 § 6.4 — these tests previously seeded workflowStep
    // ='postprocess'; that step was collapsed into transfer's preparing
    // sub-phase. workflowStep is now 'transfer' (where the failure-retry
    // click handler lives in the consolidated branch).
    let contextSubject: BehaviorSubject<WorkflowContext | null>;
    let testComponent: WorkflowActionsComponent;
    let testFixture: ComponentFixture<WorkflowActionsComponent>;

    function baseHitPostprocessContext(overrides: Partial<WorkflowContext> = {}): WorkflowContext {
      return {
        id: 'job-1',
        type: 'job',
        workflowStep: 'transfer',
        discdbHit: true,
        labelForm: {},
        jobStatus: {
          jobId: 'job-1',
          job_status: 'running',
          rip_state: 'completed',
          post_state: 'failed',
          pipeline: { rip: 'completed', postprocess: 'failed' },
        } as any,
        titles: [],
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
        ...overrides,
      };
    }

    beforeEach(() => {
      contextSubject = new BehaviorSubject<WorkflowContext | null>(null);
      mockWorkflow.getActiveContext.and.returnValue(contextSubject.asObservable());
      mockWorkflow.determineWorkflowStep.and.returnValue('transfer');
      mockWorkflow.validateStepCompletion.and.returnValue({ valid: true, errors: [] });
      mockWorkflow.getStepCompletionState.and.returnValue({
        film: true,
        boxset: true,
        disc: true,
        titles: true,
        postprocess: false,
        transfer: false,
      });
      mockWorkflow.resumeJob.calls.reset();
      mockWorkflow.startPostProcess.calls.reset();

      testFixture = TestBed.createComponent(WorkflowActionsComponent);
      testComponent = testFixture.componentInstance;
      testFixture.detectChanges();
    });

    it('canContinue$ is true when post_state is failed and rip is completed (DiscDB hit)', (done) => {
      contextSubject.next(baseHitPostprocessContext());
      testFixture.detectChanges();
      testComponent.canContinue$.pipe(take(1)).subscribe((can) => {
        expect(can).toBe(true);
        done();
      });
    });

    it('buttonText$ shows Retry Preparing when post_state is failed', (done) => {
      // #365 Phase 2 § 6.4 — postprocess collapsed into transfer's
      // "preparing" sub-phase. Failure label reworded accordingly.
      contextSubject.next(baseHitPostprocessContext());
      testFixture.detectChanges();
      testComponent.buttonText$.pipe(take(1)).subscribe((text) => {
        expect(text).toBe('Retry Preparing');
        done();
      });
    });

    it('onContinue calls resumeJob when post_state is failed', () => {
      contextSubject.next(baseHitPostprocessContext());
      testFixture.detectChanges();
      testComponent.onContinue();
      expect(mockWorkflow.resumeJob).toHaveBeenCalled();
      expect(mockWorkflow.startPostProcess).not.toHaveBeenCalled();
    });
  });

  describe('boxset step while assigning release/boxset', () => {
    let contextSubject: BehaviorSubject<WorkflowContext | null>;
    let filmSave$: BehaviorSubject<boolean>;
    let testComponent: WorkflowActionsComponent;
    let testFixture: ComponentFixture<WorkflowActionsComponent>;

    function boxsetContext(): WorkflowContext {
      return {
        id: 'test-job',
        type: 'job',
        workflowStep: 'boxset',
        discdbHit: false,
        labelForm: {
          movie_id: 'movie-1',
          release_id: 'release-1',
        } as any,
        jobStatus: {
          job_status: 'completed',
          rip_state: 'completed',
          workflow_step: 'boxset',
        } as any,
        titles: [],
        titleOrder: [],
        titlesComplete: false,
        stepCompletionState: {
          film: true,
          boxset: true,
          disc: false,
          titles: false,
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
    }

    beforeEach(() => {
      contextSubject = new BehaviorSubject<WorkflowContext | null>(null);
      filmSave$ = new BehaviorSubject<boolean>(true);
      mockWorkflow.getActiveContext.and.returnValue(contextSubject.asObservable());
      mockWorkflow.getStartRipInProgress$.and.returnValue(of(false));
      mockWorkflow.getFilmStepSaveInProgress$.and.returnValue(filmSave$.asObservable());
      mockWorkflow.getDiscStepContinueInProgress$.and.returnValue(of(false));
      mockWorkflow.validateStepCompletion.and.returnValue({ valid: true, errors: [] });
      mockWorkflow.determineWorkflowStep.and.returnValue('boxset');
      mockWorkflow.canNavigateToStep.and.returnValue({ allowed: true, reason: '' });
      mockWorkflow.getStepCompletionState.and.returnValue({
        film: true,
        boxset: true,
        disc: false,
        titles: false,
        postprocess: false,
        transfer: false,
      });

      testFixture = TestBed.createComponent(WorkflowActionsComponent);
      testComponent = testFixture.componentInstance;
      testFixture.detectChanges();
    });

    it('disables Continue and shows spinner while label context save is in progress', fakeAsync(() => {
      contextSubject.next(boxsetContext());
      testFixture.detectChanges();
      tick();

      let can: boolean | undefined;
      let spin: boolean | undefined;
      testComponent.canContinue$.pipe(take(1)).subscribe((c) => {
        can = c;
      });
      testComponent.buttonSpinner$.pipe(take(1)).subscribe((s) => {
        spin = s;
      });
      tick();
      expect(can).toBe(false);
      expect(spin).toBe(true);

      filmSave$.next(false);
      tick();
      testComponent.canContinue$.pipe(take(1)).subscribe((c) => {
        can = c;
      });
      testComponent.buttonSpinner$.pipe(take(1)).subscribe((s) => {
        spin = s;
      });
      tick();
      expect(can).toBe(true);
      expect(spin).toBe(false);
    }));
  });

  describe('handleStartRipError multi-drive policy codes (#540 / #550)', () => {
    let toastSvc: { show: jasmine.Spy };

    beforeEach(async () => {
      toastSvc = { show: jasmine.createSpy('show') };
      const mockDriveSnapshot = {
        drives$: of([]),
        current: () => [] as any[],
        startPolling: jasmine.createSpy('startPolling'),
        stopPolling: jasmine.createSpy('stopPolling'),
      };
      await TestBed.resetTestingModule()
        .configureTestingModule({
          imports: [WorkflowActionsComponent],
          providers: [
            { provide: WorkflowService, useValue: mockWorkflow },
            { provide: LoggerService, useValue: { warn: loggerWarnSpy, error: () => {}, debug: () => {} } },
            { provide: ToastService, useValue: toastSvc },
            { provide: DriveSnapshotService, useValue: mockDriveSnapshot },
          ],
        })
        .compileComponents();
      fixture = TestBed.createComponent(WorkflowActionsComponent);
      component = fixture.componentInstance;
    });

    it('suppresses local toast for drive_unsafe_with_others (backend emits via notification system)', () => {
      const err = {
        status: 409,
        error: {
          detail: {
            code: 'drive_unsafe_with_others',
            error: 'Multi-drive ripping is not supported for this drive.',
            mount_point: '/dev/sr1',
          },
        },
      };
      (component as any).handleStartRipError(err, { discMode: 'rip' } as any);

      // Backend's emit_notification_sync handles both the WS toast and
      // Discord delivery — the local handler must NOT also toast or we
      // double-fire on the WebUI side.
      expect(toastSvc.show).not.toHaveBeenCalled();
    });

    it('suppresses local toast for drive_unidentifiable', () => {
      const err = {
        status: 409,
        error: {
          detail: {
            code: 'drive_unidentifiable',
            error: 'This drive could not be identified.',
          },
        },
      };
      (component as any).handleStartRipError(err, { discMode: 'copy' } as any);

      expect(toastSvc.show).not.toHaveBeenCalled();
    });

    it('does not short-circuit other 409 codes (needs_user_choice still flows through)', () => {
      const err = {
        status: 409,
        error: {
          detail: {
            code: 'needs_user_choice',
            projected_rip_bytes: 1,
            available_disk_bytes: 1,
          },
        },
      };
      expect(() =>
        (component as any).handleStartRipError(err, {
          discMode: 'rip',
          discInfo: {},
        } as any),
      ).not.toThrow();
      expect(toastSvc.show).not.toHaveBeenCalled();
    });

    it('falls through to the generic error toast for non-policy 409 errors with no special code', () => {
      const err = {
        status: 409,
        error: { detail: { code: 'some_other_code', message: 'something else' } },
      };
      (component as any).handleStartRipError(err, { discMode: 'rip' } as any);
      // Generic fallback at the end of handleStartRipError calls toast.show.
      expect(toastSvc.show).toHaveBeenCalledTimes(1);
    });
  });
});
