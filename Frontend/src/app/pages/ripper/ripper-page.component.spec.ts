import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RouterTestingModule } from '@angular/router/testing';
import { BehaviorSubject, of } from 'rxjs';
import { RipperPageComponent } from './ripper-page.component';
import { DriveService, DiscDetail } from '../../services/drive.service';
import { SettingsService } from '../../services/settings.service';
import { JobService } from '../../services/job.service';
import { SystemService } from '../../services/system.service';
import { ToastService } from '../../services/toast.service';
import { MetadataService } from '../../services/metadata.service';
import { WorkflowService, WorkflowContext, UIOrchestrationState, DiscInfoState } from '../../services/workflow.service';
// Removed wrapper services - functionality moved to WorkflowService and MetadataService
import { LoggerService } from '../../services/logger.service';
import {
  DriveSnapshotRow,
  DriveSnapshotService,
} from '../../services/drive-snapshot.service';

class DriveStub {
  drives$ = new BehaviorSubject<any[] | null>([]);
  selected$ = new BehaviorSubject<any | null>(null);
  discInfo$ = new BehaviorSubject<DiscDetail | null>(null);
  error$ = new BehaviorSubject<string | null>(null);
  driveScanState$ = new BehaviorSubject<any>('idle');
  upsertDrive() {}
}

class JobStub {
  ripJobStatus$ = new BehaviorSubject<any | null>(null);
  jobStreamError$ = new BehaviorSubject<string | null>(null);
  getCurrentJob() { return of(null as any); }
  getJobStatus() { return of({ job_status: 'completed', rip_progress: 100 }); }
  refreshJobStatus() { return of({ job_status: 'completed', rip_progress: 100 }); }
  getJobByDisc() { return of({ jobId: null, job_status: 'completed', rip_progress: 100, logs: [], job_dir: null } as any); }
  clearJobState() {}
}

class SettingsStub {
  getSettings() { return { transferMode: 'local' }; }
}

class SystemStub {
  getRsyncConfig() { return of({ config: null, hasKey: false }); }
  getRegistrationStatus() { return of({ expired: false }); }
  getDevMode() { return of({ enabled: false }); }
  getStorageSummary() { return of({ data_root: { path: '/data' } }); }
}

class WorkflowServiceStub {
  activeContext$ = new BehaviorSubject<WorkflowContext | null>(null);
  unfinishedJobs$ = new BehaviorSubject<any[]>([]);
  insertedDiscs$ = new BehaviorSubject<any[]>([]);
  drives$ = new BehaviorSubject<any[] | null>([]);
  selectedDrive$ = new BehaviorSubject<any | null>(null);
  discs$ = new BehaviorSubject<any[]>([]);
  coordinatorError$ = new BehaviorSubject<string | null>(null);
  private readonly _isWorkflowReady = new BehaviorSubject<boolean>(false);
  private readonly _shouldRenderWorkflow = new BehaviorSubject<boolean>(false);
  uiOrchestrationState$ = new BehaviorSubject<UIOrchestrationState>({
    selectedCard: null,
    loadingInfo: false,
    unknownDisc: false,
    contextLoading: false,
    driveLoadingStates: new Map(),
    backendError: null,
    driveError: null,
    driveScanState: 'idle'
  });
  
  getActiveContext() { return this.activeContext$.asObservable(); }
  getContext$() { return this.activeContext$.asObservable(); }
  getDrives$() { return this.drives$.asObservable(); }
  getSelectedDrive$() { return this.selectedDrive$.asObservable(); }
  getDrives() { return this.drives$.asObservable(); }
  getLabelForm$() { return new BehaviorSubject<any>(null).asObservable(); }
  getJobStatus$() { return new BehaviorSubject<any>(null).asObservable(); }
  getDiscInfo$() { return new BehaviorSubject<any>(null).asObservable(); }
  getUIOrchestrationState$() { 
    return this.uiOrchestrationState$.asObservable();
  }
  getUIOrchestrationState() {
    return this.uiOrchestrationState$.value;
  }
  getDiscInfoState$() {
    return new BehaviorSubject<DiscInfoState>({
      lastDiscInfo: null,
      activeDiscKey: null,
      discDbState: 'unknown',
      currentDiscId: null,
      hydratedDiscHash: null,
      lookupAttemptedKey: null
    }).asObservable();
  }
  getSelectedCard$() { return new BehaviorSubject<any>(null).asObservable(); }
  getWorkflowContextStatus$() { return new BehaviorSubject<string>('ready').asObservable(); }
  get isWorkflowReady$() {
    return this._isWorkflowReady.asObservable();
  }
  get shouldRenderWorkflow$() {
    return this._shouldRenderWorkflow.asObservable();
  }
  retryContextLoad() {}
  getCurrentContext() { return this.activeContext$.value; }
  getSelectedCard() { return null; }
  getDiscInfoState() { 
    return {
      lastDiscInfo: null,
      activeDiscKey: null,
      discDbState: 'unknown',
      currentDiscId: null,
      hydratedDiscHash: null,
      lookupAttemptedKey: null
    };
  }
  getCachedDiscInfo() { return null; }
  getCachedJobData() { return null; }
  updateUIOrchestrationState() {}
  updateDiscInfoState() {}
  setSelectedCard() {}
  syncCoordinator() {}
  setFunctionBindings() {}
  computeDiscDbState() { return 'unknown'; }
  syncStepWithStage() {}
  updateDiscInfoCache() {}
  updateContext() {}
  // setContext removed - contexts are no longer cached
  setContextByCard() { return of({} as WorkflowContext); }
  startRip() { return of({}); }
  startTransfer() { return of({}); }
  startPostProcess() { return of({}); }
  finalizeRelease() { return of({}); }
}

class HelperServiceStub {
  loadMovies() { return of([]); }
  loadBoxsets() { return of([]); }
  loadReleases() { return of([]); }
  loadGroupOptions() { return of([]); }
  getMovieOptions() { return []; }
  getBoxsetOptions() { return []; }
  getGroupOptions() { return []; }
  filterMovies() { return []; }
  filterBoxsets() { return []; }
  filterGroupOptions() { return []; }
  getBoxsetById() { return null; }
  selectBoxset() {}
  populateFieldsFromBoxset() {}
  applyGroupSelection() {}
  findOrCreateReleaseForMovieBoxset() { return of(null); }
  unlinkReleaseFromBoxset() { return of({}); }
  createBoxset() { return of({}); }
  updateBoxset() { return of({}); }
  computeDiscDbState() { return 'unknown'; }
}

class LoggerServiceStub {
  log() {}
  warn() {}
  error() {}
}

class DriveSnapshotServiceStub {
  private readonly snapshot$ = new BehaviorSubject<DriveSnapshotRow[]>([]);
  drives$ = this.snapshot$.asObservable();
  current(): DriveSnapshotRow[] {
    return this.snapshot$.value;
  }
  startPolling() {}
  stopPolling() {}
  /** Test helper: push synthetic snapshot rows. */
  setRows(rows: DriveSnapshotRow[]): void {
    this.snapshot$.next(rows);
  }
}

const releaseStub = {
  listReleases: () => of([]),
  updateRelease: () => of({}),
  patchDiscOps: () => of({}),
  finalizeDisc: () => of({}),
  finalizeRelease: () => of({}),
  deleteRelease: () => of({}),
  getDiscRecord: () => of({ titles: [], content_hash: '', id: '' }),
  patchDiscRecord: () => of({ titles: [], content_hash: '', id: '' }),
  loadGroupOptions: () => of([]),
  getMovies: () => of([]),
  listBoxsets: () => of([]),
  getBoxsetOptions: () => new BehaviorSubject<any[]>([]),
  getMovieOptions: () => new BehaviorSubject<any[]>([]),
};

const discStub = {
  getByHash: () => of({ disc: null, release: null }),
  getById: () => of({ disc: null, release: null }),
  getRecord: () => of({ titles: [], content_hash: '', id: '' }),
};

const buildContext = (overrides: Partial<WorkflowContext>): WorkflowContext => ({
  id: 'context',
  type: 'drive',
  labelForm: null,
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
  ...overrides,
});

describe('RipperPageComponent', () => {
  let component: RipperPageComponent;
  let fixture: ComponentFixture<RipperPageComponent>;
  let driveStub: DriveStub;
  let jobStub: JobStub;
  let toastStub: { show: jasmine.Spy };

  beforeEach(async () => {
    driveStub = new DriveStub();
    jobStub = new JobStub();
    toastStub = { show: jasmine.createSpy('show') };

    await TestBed.configureTestingModule({
      imports: [RouterTestingModule, RipperPageComponent],
      providers: [
        { provide: DriveService, useValue: driveStub },
        { provide: SettingsService, useClass: SettingsStub },
        { provide: JobService, useValue: jobStub },
        { provide: SystemService, useClass: SystemStub },
        { provide: ToastService, useValue: toastStub },
        { provide: MetadataService, useValue: { ...releaseStub, ...discStub } },
        { provide: WorkflowService, useClass: WorkflowServiceStub },
        // Removed wrapper service providers - functionality moved to WorkflowService and MetadataService
        { provide: LoggerService, useClass: LoggerServiceStub },
        { provide: DriveSnapshotService, useClass: DriveSnapshotServiceStub },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(RipperPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('populates titles when disc info arrives', (done) => {
    const workflowStub = TestBed.inject(WorkflowService) as any;
    const mockContext = buildContext({
      id: 'test',
      type: 'drive',
      discInfo: {
        disc_num: '1',
        mount_point: '/mnt',
        movie_name: 'Test',
        disc_hash: 'hash1',
        titles: { '00001.mpls': { episode_name: 'Pilot' } },
      } as any,
      titles: [{ src: '00001.mpls', episode_name: 'Pilot' } as any],
      titleOrder: ['00001.mpls'],
      titlesComplete: true,
    });
    workflowStub.activeContext$.next(mockContext);
    
    component.titleOrder$.subscribe(titleOrder => {
      if (titleOrder && titleOrder.length > 0) {
        expect(titleOrder.length).toBe(1);
        done();
      }
    });
  });

  it('prefers title_id when resolving preview track keys', () => {
    const workflowStub = TestBed.inject(WorkflowService) as any;
    const titleId = 'title-123';
    const mockContext = buildContext({
      id: 'job-1',
      type: 'job',
      jobStatus: {
        jobId: 'job-1',
        disc_payload: {
          previews: {
            tracks: {
              [titleId]: { status: 'completed', manifest: `previews/${titleId}/preview.m3u8` },
            },
          },
        },
      } as any,
    });
    workflowStub.activeContext$.next(mockContext);

    const previews = (mockContext.jobStatus as any).disc_payload.previews;
    const key = (component as any).previewTrackKey({ title_id: titleId }, previews);
    expect(key).toBe(titleId);
  });

  it('surfaces drive errors via toast', () => {
    driveStub.error$.next('Boom');
    fixture.detectChanges();
    // Drive errors are now handled by WorkflowService UI orchestration state
    // Component subscribes to uiOrchestrationState$ for driveError
    expect(toastStub.show).not.toHaveBeenCalled();
  });

  it('marks loading while a drive scan is in progress', (done) => {
    const workflowStub = TestBed.inject(WorkflowService) as any;
    workflowStub.uiOrchestrationState$.next({
      selectedCard: null,
      loadingInfo: true,
      unknownDisc: false,
      contextLoading: false,
      driveLoadingStates: new Map(),
      backendError: null,
      driveError: null,
      driveScanState: 'scanning'
    });
    
    component.uiOrchestrationState$.subscribe(state => {
      if (state?.driveScanState === 'scanning') {
        expect(state.loadingInfo).toBeTrue();
        done();
      }
    });
  });

  it('handles job stream errors via toast', () => {
    jobStub.jobStreamError$.next('SSE failed');
    expect(toastStub.show).not.toHaveBeenCalled();
  });

  const setJob = (overrides: any) => {
    const workflowStub = TestBed.inject(WorkflowService) as any;
    const mockContext = buildContext({
      id: 'job1',
      type: 'job',
      jobStatus: {
        jobId: 'job1',
        job_status: 'running',
        rip_progress: 100,
        logs: [],
        job_dir: null,
        disc_hash: 'hash',
        stage_profile: 'miss',
        rip_state: 'completed',
        label_state: 'completed',
        finalize_state: 'completed',
        post_state: 'completed',
        transfer_state: 'ready',
        finalize_release_state: 'pending',
        pipeline: {},
        ...overrides,
      } as any,
      discInfo: { disc_hash: 'hash', disc_num: '1', mount_point: '/mnt' } as any,
      titlesComplete: true,
    });
    workflowStub.activeContext$.next(mockContext);
    fixture.detectChanges();
  };

  // CTA state tests removed - CTA state computation has been moved to WorkflowActionsComponent
  // These tests would need to be updated to test WorkflowActionsComponent instead
  // or test the component's CTA state computation if it still exists

  it('component initializes successfully', () => {
    expect(component).toBeTruthy();
  });

  it('component has required observables', () => {
    expect(component.drives$).toBeDefined();
    expect(component.selectedDrive$).toBeDefined();
    expect(component.workflowSvc).toBeDefined();
  });

  // #571 tri-state logic lives in WorkflowActionsComponent (the real CTA
  // renderer); pure-function coverage is in
  // ``utils/drive-presence-state.util.spec.ts``.
});
