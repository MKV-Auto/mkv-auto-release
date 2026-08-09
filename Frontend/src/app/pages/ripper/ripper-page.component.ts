import { Component, DestroyRef, OnInit, OnDestroy, inject, ChangeDetectorRef, ChangeDetectionStrategy, ViewChild, ApplicationRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, RouterModule, ActivatedRoute } from '@angular/router';
import { Observable, Subscription, timer, tap, combineLatest, BehaviorSubject, merge, of, firstValueFrom } from 'rxjs';
import { map, startWith, tap as rxTap, catchError, switchMap } from 'rxjs/operators';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Drive, DiscDetail, TitleInfo, DriveScanState } from '../../services/drive.service';
import { SettingsService } from '../../services/settings.service';
import { JobService, JobStatus, PostProcessFile, isNoActiveTransferConfigError, isTransferConfigOrPathError } from '../../services/job.service';
import { SystemService, MakeMKVRegistrationStatus, RsyncConfig } from '../../services/system.service';
import { ToastService, formatHttpErrorDetail } from '../../services/toast.service';
import { SetupModalService } from '../../services/setup-modal.service';
import { LoadingCardComponent } from '../../components/loading-card/loading-card.component';
import { TitleModalComponent } from '../../components/title-modal/title-modal.component';
import { CardCarouselComponent } from './components/card-carousel/card-carousel.component';
import { AlreadyInLibraryCardComponent } from './components/already-in-library-card/already-in-library-card.component';
import { WorkflowLabelingComponent } from './components/workflow-labeling/workflow-labeling.component';
import { WorkflowActionsComponent } from './components/workflow-actions/workflow-actions.component';
// PathAWorkspaceComponent is now rendered inside WorkflowLabelingComponent as
// the `exploratory_rip` step — no longer imported directly here.
import { MobileService } from '../../services/mobile.service';
import { HistoryItem } from '../../services/job.service';
import { MetadataService, ReleaseSummary, MovieSummary, BoxsetSummary } from '../../services/metadata.service';
import { environment } from '../../environments/environment';
import { LoggerService } from '../../services/logger.service';
import {
  DriveSnapshotRow,
  DriveSnapshotService,
} from '../../services/drive-snapshot.service';
import { WorkflowService, WorkflowContext, TitlePatchRequest } from '../../services/workflow.service';
import { InsertedDisc, UnfinishedJob, DiscMetadata, WorkflowStep } from '../../services/workflow.service';
import { LabelForm } from './services/label-form.service';
import { TITLE_TYPE_STATS_ORDER } from '../../constants/title-type-options';
import { UIOrchestrationState, DiscInfoState, WorkflowContextStatus } from '../../services/workflow.service';
import {
  isAmbiguousStartRipTransportError,
  START_RIP_AMBIGUOUS_RESPONSE_COPY,
  startRipFailureVerb,
} from '../../utils/start-rip-error.util';

interface TitleEntry extends TitleInfo {
  src: string;
}

type StageKey = 'rip' | 'label' | 'postprocess' | 'transfer' | 'upload';
type CtaAction = 'start' | 'postprocess' | 'transfer' | 'none';

interface CtaState {
  label: string;
  disabled: boolean;
  spinner: boolean;
  action: CtaAction;
  intent: 'start' | 'progress' | 'transfer' | 'finalize' | 'done' | 'retry';
}

// State organization interfaces
interface DiscState {
  lastDiscInfo: DiscDetail | null;
  // Removed - currentDiscId - use WorkflowService.getDiscInfoState().currentDiscId instead
  activeDiscKey: string | null;
  lookupAttemptedKey: string | null;
  hydratedDiscHash: string | null;
  discDbState: 'unknown' | 'hit' | 'miss';
  discNameLocked: boolean;
  discSlugLocked: boolean;
  discMode: 'copy' | 'rip';
  discNameHint: string;
  discSlugHint: string;
  discFormatAuto: boolean;
}

interface LabelState {
  labelForm: LabelForm | null;
  labelDraft: LabelForm | null;
  labelDraftProcessed: boolean;
  // Removed - labelErrors, labelSaving - these come from WorkflowContext
  labelLoading: boolean;
  labelProgress: { filled: number; total: number; releaseFilled: number; releaseTotal: number; discFilled: number; discTotal: number; titleFilled: number; titleTotal: number };
  lastAutosaveOk: boolean;
  lastAutosaveError: string | null;
  creatingRelease: boolean;
  prefillAllowed: boolean;
  prefillDecided: boolean;
  previousLabelForm: LabelForm | null;
}

interface ReleaseState {
  lastReleaseId: string | null;
  lastReleaseSlug: string | null;
  lastReleaseDetails: any | null;
  lastManualReleaseDetails: any | null;
  lastManualReleaseSlug: string | null;
  releaseNameHint: string;
  releaseSlugHint: string;
}

interface MovieState {
  lastMovieDetails: any | null;
  movieOptions: MovieSummary[];
  movieComboOpen: boolean;
  movieSearch: string;
  tmdbUrl: string;
  filmLookupLoading: boolean;
  filmLookupError: string | null;
}

interface BoxsetState {
  boxsetOptions: BoxsetSummary[];
  selectedBoxset: BoxsetSummary | null;
  boxsetOpen: boolean;
  boxsetSearch: string;
  explicitlyUnlinkedBoxset: boolean;
  showBoxsetCreateModal: boolean;
  showBoxsetSelectModal: boolean;
  editingBoxsetId: string | null;
  newBoxset: { name: string; year: number | null; upc?: string; asin?: string; cover_front_url?: string; cover_back_url?: string };
}

interface JobState {
  currentJobStatus: JobStatus | null;
  trackedJobId: string | null;
  // Removed - jobDiscKey - use WorkflowService.getCurrentContext()?.jobStatus instead
  isRipping: boolean;
  isTransferring: boolean;
  // postProcessFiles removed - use WorkflowContext.postProcessFiles
  // Removed - caching handled by services
  lastPostProgress: number | null;
  completedTitleIds: Set<string>;
  // Removed - polling replaced by WebSocket updates from WorkflowService
}

interface TitleState {
  titles: TitleEntry[];
  titleOrder: string[];
}

interface UIState {
  loadingInfo: boolean;
  unknownDisc: boolean;
  driveError: string | null;
  backendError: string | null;
  driveLoadingStates: Map<string, boolean>;
  driveScanState: DriveScanState;
  selectedCard: { type: 'drive' | 'job', id: string } | null;
  selectedTitleForModal: any | null;
  showFinalizeModal: boolean;
  workflowMode: 'card' | 'modal' | 'drawer';
  timeoutSub: Subscription | null;
  devMode: boolean;
  showUploadStep: boolean;
  previewErrorNotified: boolean;
  lastLoggedError: string | null;
  lastLoggedJobId: string | null;
}

@Component({
  selector: 'app-ripper-page',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, LoadingCardComponent, TitleModalComponent, CardCarouselComponent, AlreadyInLibraryCardComponent, WorkflowLabelingComponent, WorkflowActionsComponent],
  templateUrl: 'ripper-page.component.html',
  styleUrls: ['./ripper-page.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RipperPageComponent implements OnInit, OnDestroy {
  // Observable streams (from WorkflowService)
  drives$!: Observable<Drive[]>;
  selectedDrive$!: Observable<Drive | null>;
  discInfo$!: Observable<DiscDetail | null>; // From WorkflowContext.discInfo
  // WorkflowService observables (source of truth for workflow state)
  activeContext$!: Observable<WorkflowContext | null>;
  workflowJobStatus$!: Observable<JobStatus | null>;
  // workflowLabelForm$, workflowDiscInfo$, workflowTitles$ removed - not used in template
  
  // WorkflowService observables (UI orchestration state)
  uiOrchestrationState$!: Observable<UIOrchestrationState>;
  discInfoState$!: Observable<DiscInfoState>;
  selectedCard$!: Observable<{ type: 'drive' | 'job', id: string } | null>;
  isWorkflowReady$!: Observable<boolean>;
  shouldRenderWorkflow$!: Observable<boolean>;
  workflowContextStatus$!: Observable<WorkflowContextStatus>;

  /** Combined UI state for template: one subscription instead of many async pipes. */
  ripperState$!: Observable<{
    uiOrchestrationState: UIOrchestrationState;
    workflowContextStatus: WorkflowContextStatus;
    selectedCard: { type: 'drive' | 'job'; id: string } | null;
    isWorkflowReady: boolean;
    shouldRenderWorkflow: boolean;
  }>;
  
  // Title state from WorkflowContext
  titleProgress$!: Observable<Record<string, number>>;
  titleOrder$!: Observable<string[]>;
  
  // Removed - titles, titleProgress, titleOrder come from WorkflowContext observables
  // labelProgressUpdateTrigger removed - progress updates now handled by WorkflowService observables
  
  // UI state - removed, using WorkflowService.getUIOrchestrationState$() instead
  // loadingInfo, unknownDisc, driveError, backendError, driveScanState, driveLoadingStates, selectedCard
  selectedTitleForModal: any | null = null;
  showFinalizeModal = false;
  workflowMode: 'card' | 'modal' | 'drawer' = 'card';
  devMode = false;
  private showUploadStep = false;
  private previewErrorNotified = false;
  private lastLoggedError: string | null = null;
  private lastLoggedJobId: string | null = null;
  private autoSelectionAttempted = false;
  
  // Job state - removed, using WorkflowContext.jobStatus instead
  // currentJobStatus, trackedJobId, isRipping, isTransferring, ripJobProgress come from WorkflowContext
  jobCreationInProgress = false;
  lastPostProgress: number | null = null;
  // postProcessFiles removed - use WorkflowContext.postProcessFiles
  // Removed - caching handled by services
  // Removed - title state comes from WorkflowContext
  private completedTitleIds = new Set<string>(); // Keep for title status tracking
  // Title progress tracking (private - used internally for notifications)
  private lastTitleProgress: Record<string, number> = {};
  private isFirstStatusUpdate: boolean = true;
  private previewKeyMissLog = new Set<string>();

  // Disc state - removed, using WorkflowService.getDiscInfoState() instead
  // lastDiscInfo, currentDiscId, activeDiscKey, lookupAttemptedKey, hydratedDiscHash, discDbState
  discMode: 'copy' | 'rip' = this.loadDiscMode();
  discNameLocked = false;
  discSlugLocked = false;
  discNameHint = '';
  discSlugHint = '';
  private discFormatAuto = true;
  // Removed - use WorkflowService.getCachedDiscInfo() instead
  
  // Workflow context loading state
  // contextLoading moved to RipperStateService UIState
  private contextSubscription: any = null;
  
  // Workflow component reference
  @ViewChild(WorkflowLabelingComponent) workflowComponent?: WorkflowLabelingComponent;
  
  // Label state - removed, using WorkflowContext instead
  // labelForm, labelSaving, lastAutosaveOk come from WorkflowContext
  // labelDraft is temporary local state for building labelForm
  labelDraft: LabelForm | null = null;
  labelDraftProcessed: boolean = false;
  labelLoading = false;
  labelProgress = { filled: 0, total: 0, releaseFilled: 0, releaseTotal: 0, discFilled: 0, discTotal: 0, titleFilled: 0, titleTotal: 0 };
  lastAutosaveError: string | null = null;
  creatingRelease = false;
  prefillAllowed = false;
  prefillDecided = false;
  pendingGroupType: 'movie' | 'series' = 'movie';
  private previousLabelForm: LabelForm | null = null;
  
  // Release state
  private lastReleaseId: string | null = null;
  private lastReleaseSlug: string | null = null;
  lastReleaseDetails: any = null;
  private lastManualReleaseDetails: any = null;
  private lastManualReleaseSlug: string | null = null;
  releaseNameHint = '';
  releaseSlugHint = '';
  // releaseOptions and groupOptions now come from getters (MetadataService)
  groupOpen = false;
  groupSearch = '';
  private _filteredGroupOptionsCache: any[] | null = null;
  private _groupSearchCache: string = '';
  
  // Movie state
  lastMovieDetails: any = null;
  // movieOptions now comes from getter (MetadataService)
  movieComboOpen = false;
  movieSearch = '';
  tmdbUrl = '';
  filmLookupLoading = false;
  filmLookupError: string | null = null;
  private _filteredMovieOptionsCache: MovieSummary[] | null = null;
  private _movieSearchCache: string = '';
  
  // Boxset state
  // boxsetOptions now comes from getter (MetadataService)
  selectedBoxset: BoxsetSummary | null = null;
  boxsetOpen = false;
  boxsetSearch = '';
  _filteredBoxsetOptionsCache: BoxsetSummary[] | null = null;
  private _boxsetSearchCache: string = '';
  private explicitlyUnlinkedBoxset = false;
  showBoxsetCreateModal = false;
  showBoxsetSelectModal = false;
  editingBoxsetId: string | null = null;
  newBoxset: { name: string; year: number | null; upc?: string; asin?: string; cover_front_url?: string; cover_back_url?: string } = { name: '', year: null };
  
  // Drive state (legacy - being replaced by discs from coordinator)
  // drives and selectedDrive come from drives$ and selectedDrive$ observables
  unfinishedJobs: JobStatus[] = [];
  
  // Unified disc metadata from coordinator
  discs: DiscMetadata[] = [];
  coordinatorConnected: boolean = false;
  coordinatorError: string | null = null;

  // #571: OS-level drive snapshot (loaded AND unloaded drives) used by ctaState
  // to distinguish "no disc loaded" from "no drive connected".
  driveSnapshot: DriveSnapshotRow[] = [];
  
  // Settings and configuration
  settings = { outputFolder: '', transferFolder: '', transferMode: 'local' as 'local' | 'rsync' };
  private rsyncConfig: RsyncConfig | null = null;
  private hasRsyncKey = false;
  regStatus: MakeMKVRegistrationStatus | null = null;
  regError: string | null = null;
  showRegSetup = false;
  regSetupKey = '';
  regChecked = false;
  
  // Internal state and caches
  private readonly CURRENT_JOB_KEY = 'current-rip-job';
  private autoTransferTriggered = false;
  private lastArtifactsJobId: string | null = null;
  lastArtifacts: { job_dir?: string | null; ripped_files?: Record<string, string> | null; post_paths?: Record<string, string> | null } | null = null;
  
  // Dependency injection
  private destroyRef = inject(DestroyRef);
  cdr = inject(ChangeDetectorRef);
  appRef = inject(ApplicationRef);
  private timeoutSub: Subscription | null = null;
  
  get discDbStateForTemplate(): 'unknown' | 'hit' | 'miss' {
    return this.workflowSvc.getDiscInfoState().discDbState;
  }
  
  get isDiscDbHit(): boolean {
    return this.workflowSvc.getDiscInfoState().discDbState === 'hit';
  }
  
  // Getters for state from WorkflowService (replacing local properties)
  get labelForm(): LabelForm | null {
    return this.workflowSvc.getCurrentContext()?.labelForm || null;
  }
  
  get currentJobStatus(): JobStatus | null {
    return this.workflowSvc.getCurrentContext()?.jobStatus || null;
  }
  
  get lastDiscInfo(): DiscDetail | null {
    return this.workflowSvc.getDiscInfoState().lastDiscInfo;
  }
  
  get titles(): TitleEntry[] {
    return this.workflowSvc.getCurrentContext()?.titles || [];
  }
  
  get selectedCard(): { type: 'drive' | 'job', id: string } | null {
    return this.workflowSvc.getSelectedCard();
  }
  
  get loadingInfo(): boolean {
    return this.workflowSvc.getUIOrchestrationState().loadingInfo;
  }
  
  get contextLoading(): boolean {
    return this.workflowSvc.getUIOrchestrationState().contextLoading;
  }

  /** Retry loading workflow context after error (uses current card selection). */
  retryContextLoad(): void {
    this.workflowSvc.retryContextLoad();
  }
  
  get driveError(): string | null {
    return this.workflowSvc.getUIOrchestrationState().driveError;
  }
  
  get backendError(): string | null {
    return this.workflowSvc.getUIOrchestrationState().backendError;
  }
  
  get movieOptions(): MovieSummary[] {
    return this.metadataSvc.getMovieOptions().value;
  }
  
  get boxsetOptions(): BoxsetSummary[] {
    return this.metadataSvc.getBoxsetOptions().value;
  }
  
  get releaseOptions(): ReleaseSummary[] {
    // Release options are loaded per-movie, so we need to get them from context or load them
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return [];
    const movieId = context?.labelForm?.movie_id;
    if (movieId) {
      // Return cached value or empty array - actual loading happens via loadReleaseOptions()
      return this._releaseOptionsCache || [];
    }
    return [];
  }
  
  get groupOptions(): any[] {
    return this.metadataSvc.getGroupOptions().value;
  }
  
  // Cache for release options (since they're loaded per-movie)
  private _releaseOptionsCache: ReleaseSummary[] = [];

  hasMovieId(info: DiscDetail | null): boolean {
    // Check both current info and lastDiscInfo (stored version) to handle timing issues
    return !!(info as any)?.movie_id || !!(this.workflowSvc.getDiscInfoState().lastDiscInfo as any)?.movie_id;
  }

  shouldShowTmdbUrl(info: DiscDetail | null): boolean {
    if (!info) return false;
    
    // Never show for DiscDB hits - check both computed state and payload
    const isDiscDbHitFromInfo = (info as any)?.discdb_hit === true;
    if (this.isDiscDbHit || isDiscDbHitFromInfo) return false;
    
    // Don't show if DiscDB result is still unknown
    if ((info as any)?.discdb_hit === null || (info as any)?.discdb_hit === undefined) {
      return false; // Still waiting for DiscDB lookup
    }
    
    // Check if we already have a TMDB ID - if so, don't show the input
    // Allow TMDB URL input even if we have movie_id/movie_name from disc_info, 
    // as long as we don't have a TMDB ID yet
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return false;
    const hasTmdbId = !!(context?.labelForm?.tmdb_id || 
                         context?.labelForm?.movie_tmdb_id || 
                         (info as any)?.tmdb_id || 
                         (info as any)?.movie_tmdb_id);
    
    const hasLabelForm = !!context?.labelForm;
    const isWaiting = this.isWaitingForDiscDbInfo;
    
    // Show TMDB URL input if we have a labelForm, aren't waiting, and don't have a TMDB ID yet
    return !hasTmdbId && hasLabelForm && !isWaiting;
  }

  /** Check if there are no discs in any drives and no unfinished discs */
  hasNoDiscs(): boolean {
    
    // If coordinator is not connected or has an error, don't show empty state (we don't know the real state)
    if (!this.coordinatorConnected || this.coordinatorError) {
      return false; // Don't show empty state if we can't determine the real state
    }
    
    // If discs array is not available or empty, check if we have any discs
    if (!this.discs || this.discs.length === 0) {
      return true;
    }
    
    // Check for discs in drives (disc_state === 'in_drive')
    const hasDiscsInDrives = this.discs.some(disc => disc.disc_state === 'in_drive');
    
    // Check for unfinished discs (disc_state === 'unfinished')
    const hasUnfinishedDiscs = this.discs.some(disc => disc.disc_state === 'unfinished');
    
    const result = !hasDiscsInDrives && !hasUnfinishedDiscs;
    
    
    // Return true only if there are no discs in drives AND no unfinished discs
    return result;
  }

  /** Check if we're still waiting for DiscDB information to determine hit/miss status */
  get isWaitingForDiscDbInfo(): boolean {
    const discInfoState = this.workflowSvc.getDiscInfoState();
    if (!discInfoState.lastDiscInfo) return false;
    
    // If discDbState is already determined (hit or miss), don't wait
    if (discInfoState.discDbState === 'hit' || discInfoState.discDbState === 'miss') {
      return false;
    }
    
    const hasHash = !!(discInfoState.lastDiscInfo.disc_hash || (discInfoState.lastDiscInfo as any)?.content_hash);
    if (!hasHash) {
      return false; // No hash means we can't do DiscDB lookup yet
    }
    
    // If we have a hash but discdb_hit is null/undefined, DiscDB lookup is still in progress
    const discdbHitUndetermined = (discInfoState.lastDiscInfo as any)?.discdb_hit === null || 
                                   (discInfoState.lastDiscInfo as any)?.discdb_hit === undefined;
    
    // Only wait if discDbState is 'unknown' AND discdb_hit is still null/undefined
    // This prevents infinite waiting if the state is already determined
    return discInfoState.discDbState === 'unknown' && discdbHitUndetermined;
  }

  /** Check if the workflow is ready to be displayed (labelForm exists and workflow_step is determined) */
  get isWorkflowReady(): boolean {
    
    // If context is still loading, workflow is not ready
    const contextLoading = this.workflowSvc.getUIOrchestrationState().contextLoading;
    if (contextLoading) {
      return false;
    }
    
    // For jobs: Check if contextLoading is false AND (labelForm exists OR labelDraftProcessed is true)
    const selectedCard = this.workflowSvc.getSelectedCard();
    if (selectedCard?.type === 'job') {
      // For unfinished jobs, if selected and (labelForm exists OR labelDraftProcessed is true), workflow is ready
      // labelDraftProcessed can be true even without labelForm for DiscDB hits
      const context = this.workflowSvc.getCurrentContext();
      if (!context) return false;
      const result = !!(context?.labelForm || this.labelDraftProcessed);
      return result;
    }
    
    // For drives: Check if contextLoading is false AND (labelForm?.workflow_step exists OR disc info is enriched)
    if (selectedCard?.type === 'drive') {
      // Allow shell workflow to render if workflow_step is present (e.g., "film")
      // This allows newly inserted discs to show the workflow even if disc info isn't fully enriched
      const context = this.workflowSvc.getCurrentContext();
    if (!context) return false;
      if (context?.labelForm?.workflow_step) {
        return true;
      }
      
      // For DiscDB hits, check labelDraftProcessed first (should be true) and then verify disc info
      // This allows workflow to render as soon as context is synced
      if (this.isDiscDbHit) {
        // For DiscDB hits, we need labelDraftProcessed to be true (set by syncStateFromContext)
        // and disc info to be available (enriched or at least has disc_hash)
        if (!this.labelDraftProcessed) {
          return false;
        }
        
        // Check if we have disc info (can be from lastDiscInfo, cache, or discs array)
        const discInfoState = this.workflowSvc.getDiscInfoState();
        let info = discInfoState.lastDiscInfo;
        const selectedCard = this.workflowSvc.getSelectedCard();
        if (!info && selectedCard?.id) {
          const cardId = selectedCard.id;
          // Get drive from coordinator discs array
          const discFromCoordinator = this.discs.find(d => 
            d.disc_state === 'in_drive' && d.mount_point === cardId
          );
          if (discFromCoordinator?.disc_num) {
            info = this.workflowSvc.getCachedDiscInfo(discFromCoordinator.disc_num) || null;
          }
          if (!info && discFromCoordinator) {
            if (discFromCoordinator && discFromCoordinator.disc_hash) {
              // For DiscDB hits, we just need disc_hash to exist, not necessarily full enrichment
              // Convert to DiscDetail format
              info = {
                disc_num: discFromCoordinator.disc_num || '',
                mount_point: discFromCoordinator.mount_point || cardId,
                disc_hash: discFromCoordinator.disc_hash,
                movie_name: discFromCoordinator.movie_name || undefined,
                release_image: discFromCoordinator.release_image || undefined,
                disc_format: discFromCoordinator.disc_format || undefined,
                resolution: discFromCoordinator.resolution || undefined,
              } as DiscDetail;
              (info as any).disc_id = discFromCoordinator.disc_id;
            }
          }
        }
        
        // For DiscDB hits, if we have labelDraftProcessed=true and disc_hash exists, we're ready
        if (info?.disc_hash) {
          return true;
        }
        // If no disc_hash yet, wait for it
        return false;
      }
      
      // For non-DiscDB hits, require enriched disc info and labelDraftProcessed
        // Try to get disc info from lastDiscInfo first, then from cache, then from discs array
        const discInfoState = this.workflowSvc.getDiscInfoState();
        let info = discInfoState.lastDiscInfo;
        if (!info && selectedCard?.id) {
          // Try to get from cached disc info using mount_point
          const cardId = selectedCard.id;
          // Get drive from coordinator discs array
          const discFromCoordinator = this.discs.find(d => 
            d.disc_state === 'in_drive' && d.mount_point === cardId
          );
          if (discFromCoordinator?.disc_num) {
            info = this.workflowSvc.getCachedDiscInfo(discFromCoordinator.disc_num) || null;
          }
          // If not in cache, use disc from coordinator
          if (!info && discFromCoordinator) {
          if (discFromCoordinator) {
            // Convert DiscMetadata to DiscDetail format
            info = {
              disc_num: discFromCoordinator.disc_num || '',
              mount_point: discFromCoordinator.mount_point || cardId,
              disc_hash: discFromCoordinator.disc_hash || undefined,
              movie_name: discFromCoordinator.movie_name || undefined,
              release_image: discFromCoordinator.release_image || undefined,
              disc_format: discFromCoordinator.disc_format || undefined,
              resolution: discFromCoordinator.resolution || undefined,
              production_year: discFromCoordinator.production_year || undefined,
              release_year: discFromCoordinator.release_year || undefined,
            } as DiscDetail;
            (info as any).disc_id = discFromCoordinator.disc_id;
            (info as any).info_title = discFromCoordinator.movie_name || undefined;
          }
        }
      }
      
      
      // If no disc info yet, not ready
      if (!info) {
        return false;
      }
      // If disc info is pending, not ready
      if (info.pending) {
        return false;
      }
      // Check if disc info is enriched
      if (!this.isDiscInfoEnriched(info)) {
        return false;
      }
      // For non-DiscDB hits, wait until we've finished processing label_draft
      // This ensures workflow_step is set before showing the workflow
      if (!this.labelDraftProcessed) {
        return false;
      }
      // Once label_draft is processed, workflow_step should be set (or confirmed as null)
      return true;
    }
    
    // Default: not ready if no card selected
    return false;
  }

  /** Get workflow render condition - for drive cards with labelForm */
  get workflowRenderCondition(): boolean {
    // For drive cards: show if we have labelForm (and not DiscDB hit) OR if DiscDB hit and workflow ready
    // For job cards: this condition shouldn't be used (they have their own section)
    const selectedCard = this.workflowSvc.getSelectedCard();
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return false;
    const part1 = context?.labelForm && !this.isDiscDbHit && selectedCard?.type !== 'job';
    const part2 = selectedCard?.type === 'drive' && this.isDiscDbHit && this.isWorkflowReady;
    const condition = part1 || part2;
    return condition;
  }

  /** Check if workflow should be displayed (for both drive and unfinished job contexts) */
  get shouldShowWorkflow(): boolean {
    // Show if job is selected and workflow is ready
    const selectedCard = this.workflowSvc.getSelectedCard();
    if (selectedCard?.type === 'job') {
      return this.isWorkflowReady;
    }
    // For drive context, require disc info to be available
    return false; // Will be handled by template with discInfo$ check
  }

  // shouldRenderWorkflow moved to RipperStateService.shouldRenderWorkflow$ observable

  /** Check if discinfo has been enriched with release/movie metadata from backend */
  isDiscInfoEnriched(info: DiscDetail | null): boolean {
    // If a job is selected, we don't need disc info enrichment - job has its own context
    const selectedCard = this.workflowSvc.getSelectedCard();
    if (selectedCard?.type === 'job') {
      return true;
    }
    if (!info) {
      return false;
    }
    // Wait until we have disc_hash (indicates backend processing completed)
    const hasDiscHash = !!(info.disc_hash || (info as any)?.content_hash);
    if (!hasDiscHash) {
      return false;
    }
    
    // Check if it's a DiscDB hit from the info itself (more reliable than discDbState which might not be set yet)
    // Must be explicitly true, not just truthy (null/undefined means still loading)
    const isDiscDbHit = (info as any)?.discdb_hit === true;
    const hasReleaseId = !!(info as any)?.release_id;
    
    // For DiscDB hits, if we have a hash and makemkv_disc_name, consider it enriched enough to display
    // The backend may not have release/movie metadata yet, but we have the essential disc info
    if (isDiscDbHit) {
      const hasReleaseResolution = !!(info as any)?.release_resolution;
      const hasMovieId = !!(info as any)?.movie_id;
      const hasReleaseId = !!(info as any)?.release_id;
      const hasMakemkvDiscName = !!(info as any)?.makemkv_disc_name;
      // If we have enriched fields, use them; otherwise, if we have hash and makemkv_disc_name, that's enough
      return hasReleaseResolution || hasMovieId || hasReleaseId || hasMakemkvDiscName;
    }
    
    // For non-DiscDB hits with a release_id, wait for enriched fields too
    // If we have a release_id, enrichment should have run and added fields
    if (hasReleaseId) {
      const hasReleaseResolution = !!(info as any)?.release_resolution;
      const hasMovieId = !!(info as any)?.movie_id;
      const hasResolution = !!(info as any)?.resolution;
      return hasReleaseResolution || hasMovieId || hasResolution;
    }
    
    // For non-DiscDB hits without release_id, having disc_hash is enough
    return true;
  }

  /** Get merged disc info with DiscDB tracks data for DiscDB hits */
  get mergedDiscInfo(): DiscDetail | null {
    return this.workflowSvc.getDiscInfoState().lastDiscInfo;
  }
  
  /** Get last disc info for template access */
  get currentDiscInfo(): DiscDetail | null {
    return this.workflowSvc.getDiscInfoState().lastDiscInfo;
  }
  
  /** Get disc info for display - use merged info if available, otherwise use current info from observable */
  get displayDiscInfo(): DiscDetail | null {
    // For DiscDB hits, prefer mergedDiscInfo (has merged titles), otherwise use current observable value
    if (this.isDiscDbHit && this.mergedDiscInfo) {
      return this.mergedDiscInfo;
    }
    // Try to get from observable via lastDiscInfo if available, otherwise return null
    return this.workflowSvc.getDiscInfoState().lastDiscInfo || null;
  }

  /** Get the active stage based on current job status */
  getActiveStage(): StageKey | null {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    const stage = this.workflowSvc.getActiveStage(context?.jobStatus || null);
    // Map 'done' to null since StageKey doesn't include 'done'
    return stage === 'done' ? null : stage;
  }

  private lastFormOptionSlug: string | null = null;
  private readonly apiBase = environment.apiBase ?? 'http://localhost:8000';
  private loadedLabelDiscIds: Set<string> = new Set();

  private mobileService = inject(MobileService);

  trackByMovieId(_index: number, movie: { id?: string }): string {
    return movie?.id ?? '';
  }
  trackByBoxsetId(_index: number, boxset: { id?: string; slug?: string }): string {
    return boxset?.id ?? boxset?.slug ?? '';
  }

  constructor(
    // Essential services (per plan Phase 3.4)
    private settingsSvc: SettingsService,
    private jobSvc: JobService,
    private systemSvc: SystemService,
    private metadataSvc: MetadataService,
    private router: Router,
    private route: ActivatedRoute,
    private toast: ToastService,
    public workflowSvc: WorkflowService, // Public for template access
    private logger: LoggerService,
    private setupModalSvc: SetupModalService,
    private driveSnapshotSvc: DriveSnapshotService,
  ) {}

  ngOnInit(): void {
    this.bootstrapFromBackend();
    this.systemSvc.getDevMode().subscribe({
      next: status => {
        this.devMode = !!status?.enabled;
        this.cdr.markForCheck();
      },
      error: () => {
        this.devMode = false;
        this.cdr.markForCheck();
      },
    });

    // #571: poll the OS-level drive snapshot. Distinct from drives$ (which
    // only carries loaded drives via the coordinator) — the snapshot also
    // returns unloaded drives so the CTA can show "Insert Disc" (drive
    // connected, no media) vs "Drive Not Connected".
    this.driveSnapshotSvc.startPolling();
    this.driveSnapshotSvc.drives$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(rows => {
        this.driveSnapshot = rows;
        this.cdr.markForCheck();
      });

    // Get drives from WorkflowService (derived from coordinator discs$)
    this.drives$ = this.workflowSvc.getDrives$();
    // Removed excessive logging: drives$ observable fires frequently on state changes
    
    // Get selected drive from WorkflowService (derived from selectedCard$)
    this.selectedDrive$ = this.workflowSvc.getSelectedDrive$();
    // Removed excessive logging: selectedDrive$ observable fires frequently on state changes
    
    // Get disc info from WorkflowContext
    this.discInfo$ = this.workflowSvc.getContext$().pipe(
      map(context => context?.discInfo || null)
    );
    
  // Initialize WorkflowService observables (source of truth for workflow state)
  this.activeContext$ = this.workflowSvc.getContext$();
  this.workflowJobStatus$ = this.workflowSvc.getJobStatus$();
  // Path A — segment-reorder workspace overrides labeling/actions while
  // the segment-reorder workflow is in flight. Once the canonical rip
  // completes (stage = canonical_complete) or the user cancels, hand
  // off to the regular labeling/actions UI for the rest of the
  // workflow (label → postprocess → transfer).
  // Path A: the exploratory_rip step now renders inside
  // WorkflowLabelingComponent as one of the workflow steps (with the same
  // breadcrumb + step-card shell as the other steps). Backend still drives
  // workflow_step = 'exploratory_rip' from start_rip_with_segment_reorder
  // and bumps off when canonical_complete is reached
  // (_maybe_advance_canonical_complete in jobs.py).
  // workflowLabelForm$, workflowDiscInfo$, workflowTitles$ removed - not used in template
  // activeWorkflowContext$ removed - use activeContext$ instead
    
    // Initialize WorkflowService observables (UI orchestration state)
    this.uiOrchestrationState$ = this.workflowSvc.getUIOrchestrationState$();
    this.discInfoState$ = this.workflowSvc.getDiscInfoState$();
    this.selectedCard$ = this.workflowSvc.getSelectedCard$();
    
    // Title state from WorkflowContext
    this.titleProgress$ = this.workflowSvc.getContext$().pipe(
      map(context => {
        if (!context?.titleProgressValueFn) return {};
        // Build titleProgress map from titleProgressValueFn
        const progress: Record<string, number> = {};
        (context.titles || []).forEach(title => {
          const titleId = title.src || title.title_id;
          if (titleId) {
            progress[titleId] = context.titleProgressValueFn!(titleId) || 0;
          }
        });
        return progress;
      })
    );
    this.titleOrder$ = this.workflowSvc.getContext$().pipe(
      map(context => context?.titleOrder || [])
    );
    
    // Computed observables for template
    this.isWorkflowReady$ = this.workflowSvc.isWorkflowReady$;
    this.shouldRenderWorkflow$ = this.workflowSvc.shouldRenderWorkflow$;
    this.workflowContextStatus$ = this.workflowSvc.getWorkflowContextStatus$();

    // Single combined state to reduce duplicate async subscriptions in template.
    // startWith ensures the template always has a defined state on first paint so the gate doesn't hide the whole UI.
    const ripperStateDefault = {
      uiOrchestrationState: {
        selectedCard: null,
        loadingInfo: false,
        unknownDisc: false,
        contextLoading: false,
        driveLoadingStates: new Map<string, boolean>(),
        backendError: null,
        driveError: null,
        driveScanState: 'idle' as const,
      } as UIOrchestrationState,
      workflowContextStatus: 'ready' as WorkflowContextStatus,
      selectedCard: null as { type: 'drive' | 'job'; id: string } | null,
      isWorkflowReady: false,
      shouldRenderWorkflow: false,
    };
    this.ripperState$ = combineLatest([
      this.uiOrchestrationState$,
      this.workflowContextStatus$,
      this.selectedCard$,
      this.isWorkflowReady$,
      this.shouldRenderWorkflow$,
    ]).pipe(
      map(([uiOrchestrationState, workflowContextStatus, selectedCard, isWorkflowReady, shouldRenderWorkflow]) => ({
        uiOrchestrationState,
        workflowContextStatus,
        selectedCard,
        isWorkflowReady,
        shouldRenderWorkflow,
      })),
      startWith(ripperStateDefault)
    );
    
    // Expose services to template (for accessing observables)
    // Note: Services are made public for template access
    
    // Set up function bindings for context service
    this.setupContextFunctionBindings();
    
    // Phase 2: Sync step with stage when context changes
    this.workflowSvc.getActiveContext()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(context => {
        if (context) {
          this.workflowSvc.syncStepWithStage();
        }
      });
    
    // Defer non-critical data loading to improve initial load time
    // Load these after a short delay to prioritize critical UI rendering
    setTimeout(() => {
      this.loadGroupOptions();
      this.loadFilmOptions();
      this.loadBoxsets();
      this.loadReleaseOptions();
    }, 100);
  }

  /**
   * Set up function bindings for the context service
   * These functions will be included in all context objects
   */
  private setupContextFunctionBindings(): void {
    this.workflowSvc.setFunctionBindings({
      titleStatusFn: (id: string | null | undefined) => {
        const ctx1 = this.workflowSvc.getCurrentContext();
        if (!ctx1) return 'pending';
        const track = ctx1?.titles?.find((t: any) => this.titleKey(t) === id);
        return track ? this.titleStatus(track) : 'pending';
      },
      titleProgressValueFn: (id: string | null | undefined) => {
        const ctx2 = this.workflowSvc.getCurrentContext();
        if (!ctx2) return 0;
        const track = ctx2?.titles?.find((t: any) => this.titleKey(t) === id);
        return track ? this.titleProgressValue(track) : 0;
      },
      titleActiveFn: (id: string | null | undefined) => {
        const ctx3 = this.workflowSvc.getCurrentContext();
        if (!ctx3) return false;
        const track = ctx3?.titles?.find((t: any) => this.titleKey(t) === id);
        return track ? this.titleIsActive(track) : false;
      },
      previewUrlFn: (t: any) => this.previewUrlForTitle(t),
      previewStateFn: (t: any) => this.titlePreviewState(t),
      titlePathFn: (t: any) => this.titlePath(t),
      // stageProgressFn and isStageCompletedFn removed - now handled by WorkflowService observables
      // saveCallback removed - WorkflowLabelingComponent handles saving directly
    });

    // Track the last selected mount_point so we only set loading states when
    // the physical drive actually changes (not when metadata like name updates).
    let lastSelectedMount: string | null = null;

    this.selectedDrive$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(drive => {
        // selectedDrive comes from selectedDrive$ observable, no need to assign
        // Update unified selectedCard when drive changes externally (but preserve job selection)
        if (drive) {
          // Only update if no job is currently selected, or if this is a different drive
          const currentCard = this.workflowSvc.getSelectedCard();
          if (!currentCard || (currentCard.type === 'drive' && currentCard.id !== drive.mount_point)) {
            this.workflowSvc.setSelectedCard({ type: 'drive', id: drive.mount_point });
          }
        } else if (!drive && this.workflowSvc.getSelectedCard()?.type === 'drive') {
          // Only clear if currently a drive is selected, preserve job selection
          this.workflowSvc.clearCardSelection();
        }

        const newMount = drive?.mount_point ?? null;
        // Only set loading state when the selected drive changes (new mount_point),
        // not on every metadata update. selectedDrive$ re-emits when disc name/info
        // changes via WebSocket, which would otherwise re-set loading state and show
        // a false spinner on cards that are already scanned and ready.
        if (newMount === lastSelectedMount) {
          return;
        }
        lastSelectedMount = newMount;

        this.timeoutSub?.unsubscribe();
        if (drive) {
          // Set loading state for this drive
          const currentLoadingStates = new Map(this.workflowSvc.getUIOrchestrationState().driveLoadingStates);
          currentLoadingStates.set(drive.mount_point, true);
          this.workflowSvc.updateUIOrchestrationState({ 
            driveLoadingStates: currentLoadingStates,
            loadingInfo: true,
            unknownDisc: false
          });
          this.cdr.markForCheck();
          this.timeoutSub = timer(10_000)
            .pipe(takeUntilDestroyed(this.destroyRef))
            .subscribe(() => {
              const uiState = this.workflowSvc.getUIOrchestrationState();
              if (uiState.loadingInfo) {
                const updatedLoadingStates = new Map(uiState.driveLoadingStates);
                updatedLoadingStates.set(drive.mount_point, false);
                this.workflowSvc.updateUIOrchestrationState({ 
                  unknownDisc: true,
                  loadingInfo: false,
                  driveLoadingStates: updatedLoadingStates
                });
                this.cdr.markForCheck();
              }
            });
        } else {
          this.workflowSvc.updateUIOrchestrationState({ 
            loadingInfo: false,
            unknownDisc: false
          });
          this.cdr.markForCheck();
        }
      });

    // Subscribe to drives from WorkflowService
    this.workflowSvc.getDrives()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(drives => {
        // drives come from drives$ observable, no need to assign
        // Update disc info cache in service when drives change
        if (drives) {
          drives.forEach(drive => {
            // If we have disc info for this drive, cache it in service
            const discInfoState = this.workflowSvc.getDiscInfoState();
            if (discInfoState.lastDiscInfo && discInfoState.lastDiscInfo.disc_num === drive.disc_num) {
              this.workflowSvc.updateDiscInfoCache(drive.disc_num, discInfoState.lastDiscInfo);
            }
          });
          // Auto-select first drive if no drive is selected
          const currentSelectedCard = this.workflowSvc.getSelectedCard();
          if (drives.length > 0 && (!currentSelectedCard || currentSelectedCard.type !== 'drive')) {
            this.onSelectDrive(drives[0].mount_point);
          }
        }
        this.cdr.markForCheck();
      });

    // Subscribe to unified discs from coordinator (replaces separate drives and unfinishedJobs)
    // REMOVED: Deduplication logic - no longer needed as coordinator provides unified list
    // The coordinator already handles deduplication by providing a single unified discs array
    this.workflowSvc.discs$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((discs: DiscMetadata[]) => {
        
        // Detect when a disc becomes ready (scan_state changes to 'ready')
        // If this disc is currently selected, trigger context fetch
        if (this.workflowSvc.getSelectedCard()?.type === 'drive') {
          const selectedDisc = discs.find(d => 
            d.disc_state === 'in_drive' && 
            (d.mount_point === this.workflowSvc.getSelectedCard()!.id || d.disc_num === this.discs.find(dr => dr.mount_point === this.workflowSvc.getSelectedCard()!.id)?.disc_num)
          );
          
          if (selectedDisc && selectedDisc.scan_state === 'ready' && selectedDisc.disc_hash) {
            // Disc just became ready - trigger context fetch if we're waiting for it
            const contextLoading = this.workflowSvc.getUIOrchestrationState().contextLoading;
            if (contextLoading) {
              this.logger.debug('[RipperPage] Disc became ready, fetching context', {
                disc_id: selectedDisc.disc_id,
                mount_point: selectedDisc.mount_point,
                card_id: this.workflowSvc.getSelectedCard()?.id,
              });
              // Trigger context fetch by setting card again (now that scan_state is ready)
              // This will trigger context loading via CardCarouselComponent
              const selectedCard = this.workflowSvc.getSelectedCard();
              if (selectedCard) {
                this.workflowSvc.setContextByCard(selectedCard).subscribe();
              }
            }
          }
        }
        
        // Detect disc ejection: compare previous and current discs arrays
        const previousDiscs = this.discs || [];
        const ejectedDiscs = previousDiscs.filter(prevDisc => 
          prevDisc.disc_state === 'in_drive' && 
          !discs.find(currDisc => currDisc.disc_id === prevDisc.disc_id)
        );
        
        // If a disc was ejected and it matches the currently selected drive card, clear workflow state
        if (ejectedDiscs.length > 0 && this.workflowSvc.getSelectedCard()?.type === 'drive' && this.workflowSvc.getSelectedCard()) {
          const ejectedDisc = ejectedDiscs.find(ejected => 
            ejected.mount_point === this.workflowSvc.getSelectedCard()!.id || 
            (this.workflowSvc.getDiscInfoState().lastDiscInfo && (this.workflowSvc.getDiscInfoState().lastDiscInfo as any)?.disc_id === ejected.disc_id)
          );
          
          if (ejectedDisc) {
            // Clear workflow state for the ejected disc
            this.workflowSvc.updateDiscInfoState({ 
              activeDiscKey: null,
              discDbState: 'unknown'
            });
            // NOTE: labelForm comes from WorkflowService.activeContext$, not RipperStateService
            // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
            this.completedTitleIds.clear();
            
            // Clear active context - the selected card is now invalid
            const selectedCard = this.workflowSvc.getSelectedCard();
            if (selectedCard?.id) {
              // Clear active context directly (no cache to clear)
              (this.workflowSvc as any)._activeContext$.next(null);
              this.workflowSvc.syncStateFromContext(null);
            }
            
            this.cdr.markForCheck();
          }
        }
        
        this.discs = discs;
        
        // Restore or auto-select: no selected card and we have discs
        if (!this.workflowSvc.getSelectedCard() && !this.autoSelectionAttempted && discs.length > 0) {
          const trackedJobId = localStorage.getItem(this.CURRENT_JOB_KEY);
          let cardRestored = false;

          // 1. Restore last selected card from sessionStorage (skip if tracked job will be restored by bootstrap)
          if (!trackedJobId) {
            try {
              const raw = sessionStorage.getItem(WorkflowService.LAST_SELECTED_CARD_KEY);
              if (raw) {
                const persisted = JSON.parse(raw) as { type: 'drive' | 'job'; id: string } | null;
                if (persisted?.type && persisted?.id) {
                  const exists =
                    persisted.type === 'drive'
                      ? discs.some(
                          d =>
                            d.disc_state === 'in_drive' &&
                            (d.mount_point === persisted.id || d.disc_id === persisted.id)
                        )
                      : discs.some(
                          d =>
                            d.disc_state === 'unfinished' &&
                            (d.job_id === persisted.id || d.disc_id === persisted.id)
                        );
                  if (exists) {
                    this.workflowSvc.setSelectedCard(persisted);
                    this.workflowSvc.setContextByCard(persisted).subscribe({
                      next: () => {},
                      error: (err: unknown) => this.logger.error('[RipperPage] Failed to restore last selected card:', err),
                    });
                    this.autoSelectionAttempted = true;
                    cardRestored = true;
                  } else {
                    sessionStorage.removeItem(WorkflowService.LAST_SELECTED_CARD_KEY);
                  }
                }
              }
            } catch {
              sessionStorage.removeItem(WorkflowService.LAST_SELECTED_CARD_KEY);
            }
          }

          // 2. Fallback: select first in list (inserted discs first, then unfinished by rip time; first = discs[0])
          if (!cardRestored) {
            const discToSelect = discs[0];
            const isDrive = discToSelect.disc_state === 'in_drive';
            const scanReady = discToSelect.scan_state === 'ready' || discToSelect.scan_state == null;
            if (isDrive && !scanReady) {
              // Defer until scan becomes ready; do not set autoSelectionAttempted so we can retry on next emission
              this.cdr.markForCheck();
              return;
            }
            const cardType = isDrive ? ('drive' as const) : ('job' as const);
            const cardId =
              cardType === 'drive'
                ? (discToSelect.mount_point || discToSelect.disc_id || '')
                : (discToSelect.job_id || discToSelect.disc_id || '');
            if (cardId) {
              this.workflowSvc.setSelectedCard({ type: cardType, id: cardId });
              this.workflowSvc.setContextByCard({ type: cardType, id: cardId }).subscribe({
                next: () => {},
                error: (err: unknown) => this.logger.error('[RipperPage] Failed to auto-select first disc:', err),
              });
              this.autoSelectionAttempted = true;
            }
          }
        }
        
        this.cdr.markForCheck();
      });
    
    // Removed - connected$ doesn't exist on WorkflowService
    // Connection state is handled via WebSocket subscriptions in WorkflowService
    // Subscribe to coordinator connection state (stub - connection handled by WorkflowService)
    of(true).pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((connected: boolean) => {
        this.coordinatorConnected = connected;
        this.cdr.markForCheck();
      });
    
    // Subscribe to coordinator error state
    this.workflowSvc.coordinatorError$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((error: string | null) => {
        this.coordinatorError = error;
        this.cdr.markForCheck();
      });

    // activeContext$ subscription removed - sub-components handle their own context subscriptions
    // Removed redundant coordination logic (270 lines)
    // WorkflowService and sub-components handle context management directly

    // discInfo$ subscription removed - disc info comes from WorkflowContext via coordinator
    // WorkflowService handles all disc info state management

    // driveScanState$ and error$ subscriptions removed - WorkflowService handles via coordinator
    // Drive scan state and errors come from WorkflowService UI orchestration state

    // Removed - jobStreamError$ doesn't exist on JobService
    // Error handling is done via WorkflowService WebSocket error handling
    // Job stream errors are handled via WorkflowService error handling
    of(null).pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((err: any) => {
        if (err) {
          // Set backend error instead of showing toast
          this.workflowSvc.updateUIOrchestrationState({ backendError: 'Unable to Connect to Backend Service' });
          localStorage.removeItem(this.CURRENT_JOB_KEY);
          // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
          // WorkflowService manages jobStatus via WorkflowContext - no need to set local state
          // postProcessFiles come from WorkflowContext // Update post process files when job status is cleared
          this.tryReattachTrackedJob();
        } else {
          // Clear backend error when connection is restored
          this.workflowSvc.updateUIOrchestrationState({ backendError: null });
          this.cdr.markForCheck();
        }
        this.cdr.markForCheck();
      });

    // Removed - ripJobStatus$ subscription - use workflowSvc.getJobStatus$() instead
    // Job status updates are now handled via WorkflowService.activeContext$ subscription
    this.workflowSvc.getJobStatus$()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((status: JobStatus | null) => {
        // For unfinished jobs, allow status updates through (don't check disc matching)
        const discInfoState = this.workflowSvc.getDiscInfoState();
        const selectedCard = this.workflowSvc.getSelectedCard();
        if (selectedCard?.type !== 'job' && status && discInfoState.lastDiscInfo && !this.jobMatchesDisc(discInfoState.lastDiscInfo, status)) {
          return; // ignore updates for other discs so they don't steal focus
        }
        // If we have a trackedJobId and this status is for a different job, ignore it
        // This prevents old job status updates from interfering when starting a new job
        // UNLESS we have a selected job - then we want updates for that job
        const currentContext = this.workflowSvc.getCurrentContext();
        if (currentContext?.jobStatus?.jobId && status?.jobId && status.jobId !== currentContext.jobStatus.jobId) {
          if (selectedCard?.type !== 'job' || status.jobId !== selectedCard.id) {
            return;
          }
        }
        const prevStatus = currentContext?.jobStatus?.job_status;
        // Check preview status before updating to detect transitions
        const prevPreviewStatus = this.previewGenerationStatus();
        // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
        // postProcessFiles come from WorkflowContext // Update post process files when job status changes
        
        // Update context with computed properties when jobStatus changes (important for workflow step progression)
        // This ensures getActiveStage() and stageTimeline use the latest currentJobStatus
        // updateContextWithComputedProperties() removed - WorkflowActionsComponent computes stageTimeline and activeStage
        
        this.cdr.markForCheck();
        // Reset preview error notification when preview status changes from failed to something else
        const newPreviewStatus = this.previewGenerationStatus();
        if (prevPreviewStatus === 'failed' && newPreviewStatus !== 'failed') {
          this.previewErrorNotified = false;
        }
        const localDiscInfoState = this.workflowSvc.getDiscInfoState();
        const computedState = this.computeDiscDbState(localDiscInfoState.lastDiscInfo, status);
        if (computedState !== 'unknown') {
          this.workflowSvc.updateDiscInfoState({ discDbState: computedState });
        }
        const newlyCompleted: string[] = [];
        if (this.jobMatchesCurrentDisc && status?.perTitleProgress) {
          // On first status update after page load, initialize lastTitleProgress with current values
          // to prevent notifications for titles that were already completed before page load
          if (this.isFirstStatusUpdate) {
            // Initialize with current progress values to prevent false "newly completed" detections
            this.lastTitleProgress = { ...status.perTitleProgress };
            // Mark already-completed titles so we don't notify for them
            for (const [key, val] of Object.entries(status.perTitleProgress)) {
              if (val >= 100) {
                this.completedTitleIds.add(key);
              }
            }
            this.isFirstStatusUpdate = false;
          } else {
            // Normal flow: detect newly completed titles by comparing with previous progress
            for (const [key, val] of Object.entries(status.perTitleProgress)) {
              const prev = this.lastTitleProgress[key] ?? 0;
              if (prev < 100 && val >= 100) {
                newlyCompleted.push(key);
              }
            }
            // Update lastTitleProgress for next comparison
            this.lastTitleProgress = { ...status.perTitleProgress };
          }
        } else {
          this.completedTitleIds.clear();
          this.lastTitleProgress = {};
          this.isFirstStatusUpdate = true; // Reset flag if job doesn't match disc
        }
        newlyCompleted.forEach(id => {
          this.completedTitleIds.add(id);
          this.toast.show(`Title ${id} completed`, 'success', 3000);
        });
        // WorkflowService manages isRipping/isTransferring via WorkflowContext - no need to set local state
        if (status?.jobId) {
          // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
        }
        // Copy-failed toast is emitted by the backend (notification) and displayed via ToastService subscription
        const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
        if (status) {
          // WorkflowService manages isRipping/isTransferring via WorkflowContext - no need to set local state
          // These values are computed from context.jobStatus in the template
          const roundPct = (v: number | null | undefined) =>
            Math.max(0, Math.min(100, Math.round(((v ?? 0) + Number.EPSILON) * 100) / 100));
          // Cache post_progress to prevent bouncing when status updates arrive without it
          if (typeof status.post_progress === 'number' && status.post_progress >= 0) {
            // Only update cache if value actually changed (reduces unnecessary updates)
            if (this.lastPostProgress !== status.post_progress) {
              this.lastPostProgress = status.post_progress;
            }
          }
          if (status.perTitleProgress) {
            // Preserve backend-provided rip order the first time we see it; append any new keys as they arrive.
            const currentContext = this.workflowSvc.getCurrentContext();
            let titleOrder = currentContext?.titleOrder || [];
            for (const key of Object.keys(status.perTitleProgress)) {
              if (!titleOrder.includes(key)) {
                titleOrder = [...titleOrder, key];
              }
            }
            // Update titleOrder in context if it changed
            const contextBefore = this.workflowSvc.getCurrentContext();
            if (titleOrder.length !== (contextBefore?.titleOrder?.length || 0)) {
              this.workflowSvc.updateContext({ titleOrder });
            }
            // Get updated context for subsequent use
            const ctx13 = this.workflowSvc.getCurrentContext();
            if (!ctx13) return;
            // Rebuild the progress map in rip order for stable table ordering.
            const ordered: Record<string, number> = {};
            for (const key of titleOrder) {
              if (key in status.perTitleProgress) {
                ordered[key] = roundPct(status.perTitleProgress[key]);
              }
            }
            // Backfill using title_id so table keys resolve.
            for (const t of (context?.titles || [])) {
              const titleId = (t as any)?.title_id;
              if (titleId && titleId in status.perTitleProgress) {
                ordered[titleId] = roundPct(status.perTitleProgress[titleId]);
              }
            }
            const discInfoState = this.workflowSvc.getDiscInfoState();
            if (discInfoState.lastDiscInfo?.titles) {
              for (const [, t] of Object.entries(discInfoState.lastDiscInfo.titles)) {
                const titleId = (t as any)?.title_id;
                if (titleId && titleId in status.perTitleProgress) {
                  ordered[titleId] = roundPct(status.perTitleProgress[titleId]);
                }
              }
            }
            // Removed excessive logging: titleProgress map built (was logging huge arrays every second)
            // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
          } else {
            const map: Record<string, number> = {};
            const ctx14 = this.workflowSvc.getCurrentContext();
            if (!ctx14) return;
            const jobState = ctx14?.jobStatus || null;
            // titleOrder comes from WorkflowContext, not JobStatus
            const order = ctx14?.titleOrder?.length ? ctx14.titleOrder : (ctx14?.titles?.length ? ctx14.titles.map((t: any) => t.src) : []);
            const completed = status.titlesCompleted ?? 0;
            order.forEach((id, idx) => {
              if (idx < completed) map[id] = 100;
            });
            if (status.currentTitleId) {
              map[status.currentTitleId] = roundPct(status.currentTitleProgress ?? 0);
              const currentOrder = context?.titleOrder || [];
              const updatedOrder = [...currentOrder];
              if (!updatedOrder.includes(status.currentTitleId)) {
                updatedOrder.push(status.currentTitleId);
                // Update titleOrder in context
                this.workflowSvc.updateContext({ titleOrder: updatedOrder });
              }
            }
          }

          const perTitle = status.perTitleProgress || {};
          const totalTitles = status.totalTitles ?? Object.keys(perTitle).length;
          let pct = typeof status.rip_progress === 'number' ? status.rip_progress : 0;
          if (totalTitles && totalTitles > 0) {
            if (Object.keys(perTitle).length > 0) {
              const ctx15 = this.workflowSvc.getCurrentContext();
              if (!ctx15) return;
              const keys = [...new Set([...(ctx15?.titleOrder || []), ...Object.keys(perTitle)])];
              while (keys.length < totalTitles) {
                keys.push(`title-${String(keys.length + 1).padStart(2, '0')}`);
              }
              const sum = keys.reduce((acc, k) => acc + (perTitle[k] ?? 0), 0);
              pct = sum / totalTitles;
            } else {
              const completed = status.titlesCompleted ?? 0;
              const current = status.currentTitleProgress ?? 0;
              pct = ((completed + current / 100) / totalTitles) * 100;
            }
          }
          // ripJobProgress comes from WorkflowContext.jobStatus.rip_progress - no need to set local state
          // Progress updates now handled automatically by WorkflowService observables
          
          // WorkflowService will update context with latest progress via WebSocket
          // We only update if context still exists and matches the current job to avoid race conditions
          const contextAfterProgress = this.workflowSvc.getCurrentContext();
          if (contextAfterProgress && contextAfterProgress.jobStatus?.jobId === status?.jobId) {
            // Only update if we have a valid stageTimeline to avoid clearing it
            const currentStageTimeline = this.stageTimeline;
            if (currentStageTimeline && currentStageTimeline.length > 0) {
              // Computed properties handled by WorkflowActionsComponent
            }
          }
          
          if (status.job_status === 'completed' || status.job_status === 'failed') {
            // Ensure isRipping is cleared when job completes - this allows CTA to transition to Label
            // WorkflowService will update context - no need to set local state
            const finishedJobId = localStorage.getItem(this.CURRENT_JOB_KEY) || status.jobId;
            localStorage.removeItem(this.CURRENT_JOB_KEY);
            if (finishedJobId) {
              this.loadArtifacts(finishedJobId);
            }
            
            // Check if previews are still generating before stopping poll
            const previewStatus = this.previewGenerationStatus();
            const isPreviewGenerating = previewStatus === 'generating';
            
            // Only stop polling if transfer isn't running AND previews aren't generating
            if (status.transfer_state !== 'running' && !isPreviewGenerating) {
              // WebSocket updates from WorkflowService handle job status updates
            }
          }
          if (status.transfer_state) {
            // WorkflowService manages isTransferring via WorkflowContext - no need to set local state
            if (status.transfer_state !== 'running') {
              // WebSocket updates from WorkflowService handle transfer status updates
            }
          }

          // Auto-start transfer when backend reports ready
          if (
            this.shouldShowTransferCta &&
            this.transferDestinationConfigured() &&
            !(context?.jobStatus?.transfer_state === 'running') &&
            !this.autoTransferTriggered &&
            (status.transfer_state !== 'failed')
          ) {
            const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
            const trackedJobId = context?.jobStatus?.jobId || null;
            const jobId = status.jobId || trackedJobId;
            if (jobId) {
              this.autoTransferTriggered = true;
              // Use WorkflowService to start transfer
              this.workflowSvc.startTransfer().subscribe({
                next: () => {
                  // Transfer started - WorkflowService handles context updates via WebSocket
                },
                error: (err: any) => {
                  this.logger.error('[Ripper] Auto-transfer failed', err);
                  if (isNoActiveTransferConfigError(err)) {
                    this.setupModalSvc.open({ targetStep: 2, closeOnComplete: true });
                  } else if (isTransferConfigOrPathError(err)) {
                    this.router.navigate(['/settings']);
                  }
                }
              });
            }
          }
        }
        this.ensureLiveLabelForm(status);
      });

    this.loadSettings();
    this.loadTransferConfig();
    this.loadRegStatus();
    // Clean up stale PENDING_TRANSFER_JOB key from previous versions.
    // Transfer is now always user-initiated via the "Start Transfer" / "Retry Transfer"
    // button — no auto-trigger on page load.
    localStorage.removeItem('PENDING_TRANSFER_JOB');

    const storedJob = localStorage.getItem(this.CURRENT_JOB_KEY);
    if (storedJob) {
      this.attachExistingJob(storedJob);
    }
  }

  loadDiscMode(): 'copy' | 'rip' {
    const stored = localStorage.getItem('discMode');
    return stored === 'rip' ? 'rip' : 'copy';
  }

  saveDiscMode(mode: 'copy' | 'rip'): void {
    localStorage.setItem('discMode', mode);
    this.discMode = mode;
  }

  toggleDiscMode(): void {
    this.saveDiscMode(this.discMode === 'copy' ? 'rip' : 'copy');
  }

  onDriveSelected(d: Drive): void {
    this.workflowSvc.selectDrive(d.mount_point);
  }

  startRip(info: DiscDetail | null): void {
    if (!info) {
      return;
    }
    // Save label draft before starting rip to ensure movie selection is persisted
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    if (context?.labelForm) {
      this.saveLabelDraft();
    }
    // Prevent duplicate submissions. `job_status` alone isn't enough: it stays
    // 'running' through labeling and transfer, so a job parked awaiting labels
    // would make this refuse to start a new rip. Same guard the CTA logic uses.
    const jobStatus = context?.jobStatus;
    const isRipping =
      (jobStatus?.job_status === 'running' || jobStatus?.job_status === 'pending') &&
      jobStatus?.rip_state !== 'completed';
    if (isRipping) {
      this.logger.warn('[Ripper] startRip called while already ripping, ignoring duplicate request');
      return;
    }
    
    this.autoTransferTriggered = false;
    this.lastPostProgress = null;
    // updatePostProcessFiles() removed - use WorkflowContext.postProcessFiles

    const payload: any = { disc_num: info.disc_num, mount_point: info.mount_point, disc_hash: info.disc_hash, mode: this.discMode };
    if (this.settings.outputFolder) {
      payload.output_dir = this.settings.outputFolder;
    }
    this.jobSvc.startRip(payload).subscribe({
      next: ({ jobId }) => {
        localStorage.setItem(this.CURRENT_JOB_KEY, jobId);
        // Removed - WorkflowService manages title progress via WorkflowContext
        // WorkflowService will update context via WebSocket
      },
      error: err => {
        this.logger.error('[Ripper] startRip error', err);

        if (err?.status === 503 && err?.error?.action_required === 'reinstall_makemkv') {
          const errorMsg = err?.error?.error || 'MakeMKV is not properly installed. Please reinstall.';
          this.toast.show(errorMsg, 'error');
          this.setupModalSvc.open({ targetStep: 1, closeOnComplete: true });
          return;
        }

        if (isAmbiguousStartRipTransportError(err)) {
          this.workflowSvc.tryRecoverStartRipAfterAmbiguousError().subscribe((recovered) => {
            if (recovered) {
              const jid = this.workflowSvc.getCurrentContext()?.jobStatus?.jobId;
              if (jid) {
                localStorage.setItem(this.CURRENT_JOB_KEY, jid);
              }
            } else {
              this.toast.show(START_RIP_AMBIGUOUS_RESPONSE_COPY, 'warning', 7000);
            }
          });
          return;
        }

        const detail = formatHttpErrorDetail(err);
        const verb = startRipFailureVerb(this.discMode);
        this.toast.show(`Failed to start ${verb}: ${detail}`, 'error', 8000);
      },
    });
  }

  // REMOVED: startRipFromWorkflow() - use WorkflowService.startRip() directly
  // WorkflowActionsComponent calls workflowService.startRip() which handles context extraction

  // REMOVED: onPrimaryAction(), startTransfer(), startPostProcess(), finalizeRelease() - not used in template
  // WorkflowActionsComponent handles these actions via WorkflowService.startRip(), startPostProcess(), startTransfer()
  // WorkflowService.finalizeRelease() is available for release finalization

  private monitorTransfer(jobId: string): void {
    // Removed - WebSocket updates from WorkflowService handle transfer status updates
    // WorkflowService manages transfer status via WebSocket subscriptions
  }

  private stopTransferMonitor(): void {
    // Removed - WebSocket updates from WorkflowService handle transfer status updates
  }

  private transferDestinationConfigured(): boolean {
    if (this.settings.transferMode === 'rsync') {
      return !!(this.rsyncConfig && this.hasRsyncKey);
    }
    return !!(this.settings.transferFolder && this.settings.transferFolder.trim());
  }

  // REMOVED: attemptPendingTransfer() - logic moved inline to callers, using WorkflowService.startTransfer()

  // REMOVED: getTitleProgress() - not used, titleProgressValueFn is used directly from context

  goToHistory(): void {
    this.router.navigate(['/history']);
  }

  private loadArtifacts(jobId: string): void {
    if (this.lastArtifactsJobId === jobId) {
      return; // already loaded for this job; avoid hammering the endpoint
    }
    this.lastArtifactsJobId = jobId;
    this.jobSvc.getJobArtifacts(jobId).subscribe({
      next: art => {
        this.lastArtifacts = art;
      },
      error: err => {
        this.logger.warn('Failed to load artifacts', err);
        // allow retry on failure
        this.lastArtifactsJobId = null;
      },
    });
  }

  private loadSettings(): void {
    // Load settings from local storage
    const s = this.settingsSvc.getSettings();
    if (s) {
      this.settings = { transferMode: 'local', ...s };
    } else {
      // Load defaults from storage summary
      this.systemSvc.getStorageSummary().subscribe({
        next: summary => {
          if (!this.settings.outputFolder) {
            this.settings.outputFolder = summary.data_root.path;
          }
          this.settingsSvc.saveSettings(this.settings);
        },
        error: err => {
          this.notifyBackendError(err);
          this.settingsSvc.saveSettings(this.settings);
        },
      });
    }
  }

  private loadTransferConfig(): void {
    // Transfer configs are now managed in Settings > Transfer Configs.
    // Transfer is always user-initiated via the CTA button.
    this.systemSvc.getRsyncConfig().subscribe({
      next: res => {
        this.rsyncConfig = res.config || null;
        this.hasRsyncKey = res.hasKey;
      },
      error: () => {
        this.rsyncConfig = null;
        this.hasRsyncKey = false;
      },
    });
  }

  private loadRegStatus(): void {
    this.systemSvc.getRegistrationStatus().subscribe({
      next: res => {
        this.regStatus = res;
        this.regError = null;
      },
      error: err => {
        this.regError = err?.message || 'Unable to load MakeMKV registration status';
        this.notifyBackendError(err);
      },
      complete: () => {
        this.regChecked = true;
        const needsKey = !this.regStatus?.currentKey || this.regStatus?.expired;
        if (needsKey) {
          this.showRegSetup = true;
        }
      },
    });
  }

  private tryResumeMatchingJob(info: DiscDetail): void {
    // Don't try to resume matching job if a job is already selected
    if (this.workflowSvc.getSelectedCard()?.type === 'job') return;
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    if (context?.jobStatus?.jobId) return;
    this.lookupJobByDisc(info);
  }

  private lookupJobByDisc(info: DiscDetail): void {
    const key = `${info.disc_hash || ''}:${info.disc_num}`;
    const discInfoState = this.workflowSvc.getDiscInfoState();
    if (discInfoState.lookupAttemptedKey === key) {
      return; // already tried for this disc; avoid repeated 404 spam
    }
    this.workflowSvc.updateDiscInfoState({ lookupAttemptedKey: key });
    
    // Try multiple lookup strategies to ensure we find the job if it exists
    const infoDiscId = (info as any)?.disc_id || null;
    const lookupStrategies = [
      // Strategy 1: By disc_id (most reliable if available)
      ...(infoDiscId ? [{ disc_id: infoDiscId }] : []),
      // Strategy 2: By disc_hash (reliable for unique discs)
      ...(info.disc_hash ? [{ disc_hash: info.disc_hash }] : []),
      // Strategy 3: By drive_num/disc_num (fallback)
      { drive_num: info.disc_num }
    ];
    
    // Try strategies in order until one succeeds
    let strategyIndex = 0;
    const tryNextStrategy = () => {
      if (strategyIndex >= lookupStrategies.length) {
        // All strategies failed - this is expected if no job exists
        this.logger.log('[Ripper] No matching job found after trying all strategies', { key });
        return;
      }
      
      const strategy = lookupStrategies[strategyIndex];
      this.jobSvc.getJobByDisc(strategy).subscribe({
        next: job => {
          // Verify job matches the disc before resuming
          if (this.workflowSvc.jobMatchesDisc(info, job)) {
            this.workflowSvc.updateDiscInfoState({ lookupAttemptedKey: null }); // success; allow future lookups if disc changes
            // Use WorkflowService to set context with job
            this.workflowSvc.setContextByCard({ type: 'job', id: job.jobId }).subscribe({
              next: () => {
                this.logger.log('[Ripper] Resumed matching job', { jobId: job.jobId });
              },
              error: (err: any) => {
                this.logger.error('[Ripper] Failed to resume job', err);
              },
            });
          } else {
            // Job doesn't match - try next strategy
            this.logger.warn('[Ripper] Job found but doesn\'t match disc, trying next strategy', { 
              jobId: job.jobId, 
              strategy 
            });
            strategyIndex++;
            tryNextStrategy();
          }
        },
        error: err => {
          if (err.status === 404) {
            // 404 is expected - try next strategy
            strategyIndex++;
            tryNextStrategy();
          } else {
            // Non-404 error - log and try next strategy
            this.logger.warn('[Ripper] Error looking up job by disc', { err, strategy });
            strategyIndex++;
            tryNextStrategy();
          }
        },
      });
    };
    
    tryNextStrategy();
  }

  private resumeJob(jobId: string, discKey?: string | null): void {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    const currentTrackedJobId = context?.jobStatus?.jobId || null;
    if (currentTrackedJobId === jobId) return;
    
    // Use WorkflowService to set context with job
    this.workflowSvc.setContextByCard({ type: 'job', id: jobId }).subscribe({
      next: () => {
        localStorage.setItem(this.CURRENT_JOB_KEY, jobId);
        this.loadArtifacts(jobId);
        this.logger.log('[Ripper] Resumed job', { jobId });
      },
      error: (err: any) => {
        this.logger.error('[Ripper] Failed to resume job', err);
      },
    });
    // Polling removed - WebSocket updates from WorkflowService handle job status updates
  }

  refreshDiscInfo(): void {
    const selectedCard = this.workflowSvc.getSelectedCard();
    const mountPoint = selectedCard?.type === 'drive' ? selectedCard.id : null;
    if (mountPoint) {
      this.workflowSvc.updateUIOrchestrationState({ loadingInfo: true });
      this.workflowSvc.refreshDiscInfo(mountPoint)
        .subscribe({
          next: () => {
            // Disc info will be updated via coordinator WebSocket
          },
          error: (err) => {
            this.logger.error('[Ripper] Failed to refresh disc info', err);
          },
          complete: () => {
            this.workflowSvc.updateUIOrchestrationState({ loadingInfo: false });
          }
        });
    } else {
      this.logger.warn('[Ripper] Cannot refresh disc info: missing mount_point');
    }
  }

  submitRegSetup(): void {
    const key = this.regSetupKey.trim();
    if (!key) return;
    this.systemSvc.registerKey(key).subscribe({
      next: res => {
        this.regStatus = res;
        if (!res.expired) {
          this.showRegSetup = false;
          this.regSetupKey = '';
          this.loadRegStatus();
          window.location.reload();
        }
      },
      error: err => {
        this.regError = err.error?.detail ?? err.message ?? 'Registration failed';
        this.toast.show(this.regError || 'Registration failed', 'error');
        this.notifyBackendError(err);
      },
    });
  }

  private bootstrapFromBackend(): void {
    // Prefer jobId from route (e.g. "Continue Workflow" from Library) over localStorage
    const queryJobId = this.route.snapshot.queryParamMap.get('jobId');
    const trackedJobId = queryJobId || localStorage.getItem(this.CURRENT_JOB_KEY);
    if (trackedJobId) {
      if (queryJobId) {
        localStorage.setItem(this.CURRENT_JOB_KEY, queryJobId);
        this.router.navigate([], { relativeTo: this.route, queryParams: { jobId: null }, queryParamsHandling: 'merge', replaceUrl: true });
      }
      this.workflowSvc.setContextByCard({ type: 'job', id: trackedJobId }).subscribe({
        next: () => {
          this.logger.log('[Ripper] Bootstrapped tracked job', { jobId: trackedJobId });
        },
        error: (err: any) => {
          if (err.status === 404) {
            localStorage.removeItem(this.CURRENT_JOB_KEY);
          }
          this.logger.error('[Ripper] Failed to bootstrap tracked job', err);
        },
      });
    }
  }
  
  // Legacy method - kept for compatibility but logic moved above
  private bootstrapFromBackendLegacy(): void {
    this.workflowSvc.updateUIOrchestrationState({ loadingInfo: true });
    // REMOVED: getCurrentJob() endpoint removed - replaced by Workflow Coordinator
    // Bootstrap now uses coordinator's initial_state and localStorage tracked job ID
    
    // Wait for coordinator's initial state, then restore tracked job if available
    this.workflowSvc.unfinishedJobs$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(unfinishedJobs => {
        const trackedJobId = localStorage.getItem(this.CURRENT_JOB_KEY);
        if (trackedJobId) {
          // Check if tracked job is in unfinished jobs list
          const trackedJob = unfinishedJobs.find(j => j.job_id === trackedJobId);
          if (trackedJob) {
            // Try to reattach to the tracked job
            // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
            this.tryReattachTrackedJob();
          } else {
            // Tracked job not in unfinished jobs - might be completed or not exist
            // Clear tracked job and let normal flow handle it
            localStorage.removeItem(this.CURRENT_JOB_KEY);
            // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
          }
        }
        this.workflowSvc.updateUIOrchestrationState({ loadingInfo: false });
        this.cdr.markForCheck();
      });
    
    // If coordinator doesn't emit (shouldn't happen, but fallback), clear loading after timeout
    setTimeout(() => {
      if (this.loadingInfo) {
        this.workflowSvc.updateUIOrchestrationState({ loadingInfo: false });
        this.cdr.markForCheck();
      }
    }, 5000);
  }

  private attachExistingJob(jobId: string): void {
    this.jobSvc.getJobStatus(jobId).subscribe({
      next: status => {
        // WorkflowService will update context via WebSocket - no need to set local state
        // Reuse the normal resume path so SSE/status polling are reattached.
        const discInfoState = this.workflowSvc.getDiscInfoState();
        const key = discInfoState.lastDiscInfo ? this.discKey(discInfoState.lastDiscInfo) : null;
        this.resumeJob(jobId, key);
      },
      error: () => {
        localStorage.removeItem(this.CURRENT_JOB_KEY);
        // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
        // WorkflowService manages context - no need to set local state
      },
    });
  }

  private tryReattachTrackedJob(): void {
    // Contexts are now fetched via setContextByCard() - see bootstrapFromBackend()
    const trackedJobId = localStorage.getItem(this.CURRENT_JOB_KEY);
    if (!trackedJobId) return;
    
    // Use WorkflowService to set context with tracked job
    this.workflowSvc.setContextByCard({ type: 'job', id: trackedJobId as string }).subscribe({
      next: () => {
        this.logger.log('[Ripper] Reattached to tracked job', { jobId: trackedJobId });
      },
      error: (err: any) => {
        // Job doesn't exist or error - clear tracked job
        if (err.status === 404) {
          localStorage.removeItem(this.CURRENT_JOB_KEY);
        }
        this.logger.error('[Ripper] Failed to reattach tracked job', err);
      },
    });
    return;
    
    // Legacy code below (unreachable)
    const jobId = trackedJobId;
    if (!jobId) return; // Early return if no jobId
    
    // Add retry logic with exponential backoff
    let retryCount = 0;
    const maxRetries = 3;
    const retryDelay = 1000; // Start with 1 second
    
    const attemptReattach = () => {
      this.jobSvc.refreshJobStatus(jobId as string).subscribe({
        next: status => {
          // Validate job status before using it
          if (!status || !status.jobId) {
            this.logger.error('[Ripper] Invalid job status received', status);
            this.clearTrackedJob();
            return;
          }
          
          // Verify job matches current disc if we have disc info
          const discInfoState = this.workflowSvc.getDiscInfoState();
          if (discInfoState.lastDiscInfo && !this.jobMatchesDisc(discInfoState.lastDiscInfo, status)) {
            this.logger.warn('[Ripper] Tracked job does not match current disc, clearing', {
              jobId,
              discHash: (discInfoState.lastDiscInfo as any)?.disc_hash || (discInfoState.lastDiscInfo as any)?.content_hash
            });
            this.clearTrackedJob();
            return;
          }
          
          // WorkflowService will update context via WebSocket - no need to set local state
          // WorkflowService will update context via WebSocket - no need to set local state
          // WebSocket updates from WorkflowService handle job status updates
          // Removed - WorkflowService manages title progress via WorkflowContext
        },
        error: (err: any) => {
          if (err.status === 404) {
            // Job doesn't exist - clear local state
            this.logger.log('[Ripper] Tracked job not found (404), clearing', { jobId });
            this.clearTrackedJob();
          } else if (retryCount < maxRetries) {
            // Retry on transient errors
            retryCount++;
            const delay = retryDelay * Math.pow(2, retryCount - 1); // Exponential backoff
            this.logger.warn(`[Ripper] Failed to reattach job, retrying in ${delay}ms (attempt ${retryCount}/${maxRetries})`, err);
            setTimeout(attemptReattach, delay);
          } else {
            // Max retries exceeded - clear local state
            this.logger.error('[Ripper] Failed to reattach job after max retries, clearing', { jobId, err });
            this.clearTrackedJob();
          }
        },
      });
    };
    
    attemptReattach();
  }
  
  private clearTrackedJob(): void {
    localStorage.removeItem(this.CURRENT_JOB_KEY);
    // Removed - titleProgress and titleOrder come from WorkflowContext, not separate job state
    this.workflowSvc.updateContext({ jobStatus: null });
        // WorkflowService will update context on error - no need to set local state
    // WebSocket updates from WorkflowService handle job status updates
  }

  private startJobStatusPoll(jobId: string): void {
    // Removed - WebSocket updates from WorkflowService handle job status updates
    // WorkflowService manages job status via WebSocket subscriptions
  }

  private stopJobStatusPoll(): void {
    // Removed - WebSocket updates from WorkflowService handle job status updates
  }

  private resetDiscAndJobState(): void {
    this.resetJobStateOnly();
    this.workflowSvc.updateDiscInfoState({ 
      lastDiscInfo: null,
      activeDiscKey: null,
      lookupAttemptedKey: null,
      discDbState: 'unknown'
    });
  }

  private resetJobStateOnly(): void {
    // Removed - clearJobState doesn't exist on JobService
    // Job state is managed by WorkflowService
    this.workflowSvc.updateContext({ jobStatus: null });
    // trackedJobId comes from WorkflowContext - no need to set local state
    // Removed - jobDiscKey - use WorkflowService.getCurrentContext()?.jobStatus instead
    this.lastArtifactsJobId = null;
        // WorkflowService will update context on error - no need to set local state
    // WorkflowService manages isTransferring via WorkflowContext - no need to set local state
    this.autoTransferTriggered = false;
    this.lastArtifacts = null;
    // ripJobProgress comes from WorkflowContext - no need to set local state
    // titleProgress comes from WorkflowContext - no need to clear local state
    // WebSocket updates from WorkflowService handle job status updates
    // WebSocket updates from WorkflowService handle transfer status updates
    localStorage.removeItem(this.CURRENT_JOB_KEY);
    this.completedTitleIds.clear();
    this.lastTitleProgress = {};
    this.isFirstStatusUpdate = true;
    this.labelDraft = null;
    // labelForm comes from WorkflowContext - no need to set local state
    // Removed - labelErrors doesn't exist in UIOrchestrationState
    // Label errors are managed in WorkflowContext
    // Reset error logging state when job state is reset
    this.lastLoggedError = null;
    this.lastLoggedJobId = null;
  }

  private discKey(info: DiscDetail): string {
    return this.workflowSvc.discKey(info);
  }

  get jobMatchesCurrentDisc(): boolean {
    const discInfoState = this.workflowSvc.getDiscInfoState();
    if (!discInfoState.lastDiscInfo) return false;
    const discHash = discInfoState.lastDiscInfo.disc_hash;
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return false;
    const jobHash = context?.jobStatus?.disc_hash || (context?.jobStatus as any)?.disc_payload?.disc_hash;
    if (discHash && jobHash && discHash === jobHash) return true;
    if (!context?.jobStatus) return false;
    // Use discKey from context jobStatus instead of jobDiscKey property
    const jobDiscKey = this.workflowSvc.discKey(context.jobStatus as any);
    if (!jobDiscKey) return false;
    return jobDiscKey === this.discKey(discInfoState.lastDiscInfo);
  }

  get isDiscDbMissing(): boolean {
    // Check computed state first (from disc info or job status)
    let discInfoState = this.workflowSvc.getDiscInfoState();
    if (discInfoState.discDbState === 'miss') return true;
    if (discInfoState.discDbState === 'hit') return false;
    // Check disc info directly (for UI before job is created)
    if (discInfoState.lastDiscInfo?.discdb_hit === true) {
      return false;
    }
    if (discInfoState.lastDiscInfo?.discdb_hit === false) return true;
    if (discInfoState.lastDiscInfo?.discdb_miss === true) return true;
    // Check job status as fallback
    let currentContext = this.workflowSvc.getCurrentContext();
    const discdbResult = currentContext?.jobStatus?.discdb_result || null;
    if (discdbResult === 'miss') return true;
    if (discdbResult === 'hit') return false;
    const stageProfile = currentContext?.jobStatus?.stage_profile || null;
    if (stageProfile === 'hit') return false;
    if (stageProfile === 'miss') return true;
    // Additional fallback checks
    const errText = (this.workflowSvc.getUIOrchestrationState().driveError || '').toLowerCase();
    currentContext = this.workflowSvc.getCurrentContext();
    discInfoState = this.workflowSvc.getDiscInfoState();
    const jobMatches = discInfoState.lastDiscInfo ? this.jobMatchesDisc(discInfoState.lastDiscInfo, currentContext?.jobStatus || null) : false;
    if (errText.includes('thediscdb')) return true;
    if (discInfoState.lastDiscInfo?.pending) return true;
    if (discInfoState.lastDiscInfo?.label_required && !discInfoState.lastDiscInfo?.label_ready) return true;
    if (jobMatches && (currentContext?.jobStatus as any)?.disc_payload?.discdb_hit === false) return true;
    if (jobMatches && currentContext?.jobStatus?.label_required && !currentContext?.jobStatus?.label_ready) return true;
    return false;
  }

  releaseYear(info: DiscDetail | null): string | null {
    // Prefer production year from film
    const filmYear = this.labelForm?.production_year || this.lastMovieDetails?.production_year;
    if (filmYear) return String(filmYear);
    
    if (!info) return null;
    const rawYear =
      (info as any).production_year ??
      (info as any).release_year ??
      (info as any).year ??
      null;
    if (rawYear) return String(rawYear);
    const rawDate = (info as any).release_date;
    if (typeof rawDate === 'string' && rawDate.length >= 4) {
      const maybeYear = rawDate.match(/\d{4}/)?.[0];
      if (maybeYear) return maybeYear;
    }
    return null;
  }

  reproYear(info: DiscDetail | null): string | null {
    if (!info) return null;
    // Use release_year from enriched discinfo payload
    const rawYear = (info as any).release_year ?? (info as any).year ?? null;
    if (rawYear) return String(rawYear);
    const rawDate = (info as any).release_date;
    if (typeof rawDate === 'string' && rawDate.length >= 4) {
      const maybeYear = rawDate.match(/\d{4}/)?.[0];
      if (maybeYear) return maybeYear;
    }
    return null;
  }

  get stageTimeline(): Array<{ key: StageKey | 'done'; label: string }> {
    const steps = this.stageSteps.map(s => ({ key: s.key as StageKey | 'done', label: s.label }));
    steps.push({ key: 'done', label: 'Done' });
    return steps;
  }

  stageCompletion(key: StageKey | 'done'): number | null {
    if (key === 'done') {
      const context = this.workflowSvc.getCurrentContext();
    if (!context) return 0;
      const profile = this.jobProfile(context?.jobStatus || null);
      const transferDone = this.pipelineState('transfer') === 'completed';
      const finalizeRelease = context?.jobStatus?.finalize_release_state || context?.jobStatus?.pipeline?.['finalize_release'];
      if (context?.jobStatus?.job_status === 'completed' || context?.jobStatus?.phase === 'complete') return 100;
      if (profile === 'hit' && transferDone) return 100;
      if (profile === 'miss' && (finalizeRelease === 'completed' || (finalizeRelease === 'skipped' && transferDone))) return 100;
      return 0;
    }
    return this.stageProgress(key as StageKey);
  }

  // REMOVED: connectorProgress(), stageGridTemplate, dotColumn(), barColumn(), stagePercent(), activeStagePercent()
  // These methods are not used in the template - WorkflowActionsComponent has its own implementations

  isStageCompleted(key: StageKey | 'done'): boolean {
    if (key === 'done') {
      const context = this.workflowSvc.getCurrentContext();
    if (!context) return false;
      const profile = this.jobProfile(context?.jobStatus || null);
      const transferDone = this.pipelineState('transfer') === 'completed';
      const finalizeRelease = context?.jobStatus?.finalize_release_state || context?.jobStatus?.pipeline?.['finalize_release'];
      if (context?.jobStatus?.job_status === 'completed' || context?.jobStatus?.phase === 'complete') return true;
      if (profile === 'hit' && transferDone) return true;
      if (profile === 'miss' && (finalizeRelease === 'completed' || (finalizeRelease === 'skipped' && transferDone))) return true;
      return false;
    }
    const state = this.pipelineState(key);
    // If state is explicitly 'completed', it's completed
    if (state === 'completed') return true;
    // Also check if completion percentage is 100% - this handles cases where state might be missing
    // but the stage has actually completed (e.g., when status updates arrive without state info)
    const completion = this.stageCompletion(key);
    if (completion !== null && completion >= 100) {
      // Only consider it completed if we're past the active stage or the stage is not currently active
      // This prevents marking a stage as completed while it's still running
      const activeStage = this.activeStage;
      // If there's no active stage, or this stage is not the active one, and completion is 100%, it's completed
      if (activeStage === null || key !== activeStage) {
        return true;
      }
      // Special case: if this is postprocess and we're in transfer phase or later, it's definitely completed
      if (key === 'postprocess') {
        const context = this.workflowSvc.getCurrentContext();
    if (!context) return false;
        const status = context?.jobStatus || null;
        const ripState = status?.rip_state || status?.job_status || null;
        if (ripState === 'completed' && (status?.phase === 'transfer' || status?.phase === 'complete' || status?.transfer_state)) {
          return true;
        }
      }
    }
    return false;
  }

  private get isMobileWidth(): boolean {
    if (typeof window === 'undefined') return false;
    return window.innerWidth <= 720;
  }

  get sortedLabelTitles(): any[] {
    const ctx5 = this.workflowSvc.getCurrentContext();
    if (!ctx5) return [];
    if (!ctx5?.labelForm || !Array.isArray(ctx5.labelForm.tracks)) return [];
    const typeOrder = TITLE_TYPE_STATS_ORDER.filter((t) => t !== 'ignore');
    const normalizeType = (t: any): string => (t?.type || '').toString().trim();
    const isIgnore = (t: any): boolean => {
      const raw = normalizeType(t);
      if (raw) return raw.toLowerCase() === 'ignore';
      return t?.content === false; // back-compat for legacy payloads
    };
    const typeRank = (t: any): number => {
      if (isIgnore(t)) return 99; // force bottom
      const val = normalizeType(t);
      if (!val) return 50;
      const idx = typeOrder.findIndex(v => v.toLowerCase() === val.toLowerCase());
      return idx === -1 ? 40 : idx;
    };
    const sizeVal = (t: any): number => {
      const raw = t?.size;
      if (typeof raw === 'number') return raw;
      const parsed = Number(raw);
      return Number.isFinite(parsed) ? parsed : -1;
    };
    const orderVal = (t: any): number =>
      typeof t?.order_index === 'number' ? t.order_index : Number.MAX_SAFE_INTEGER;

    return [...ctx5.labelForm.tracks].sort((a, b) => {
      const aActive = this.titleIsActive(this.titleKey(a));
      const bActive = this.titleIsActive(this.titleKey(b));
      if (aActive !== bActive) return aActive ? -1 : 1;

      const aType = typeRank(a);
      const bType = typeRank(b);
      if (aType !== bType) return aType - bType;

      const aSize = sizeVal(a);
      const bSize = sizeVal(b);
      if (aSize !== bSize) return bSize - aSize; // larger first, smaller toward bottom

      return orderVal(a) - orderVal(b);
    });
  }

  onSelectDrive(drive: Drive | string): void {
    // Handle both Drive object and mountPoint string
    const mountPoint = typeof drive === 'string' ? drive : drive.mount_point;
    // Use WorkflowService to select drive
    this.workflowSvc.selectDrive(mountPoint);
    this.workflowSvc.updateUIOrchestrationState({ driveError: null });
    this.resetJobStateOnly();
    this.workflowSvc.updateDiscInfoState({
      activeDiscKey: null,
      discDbState: 'unknown'
    });
    this.completedTitleIds.clear();
    this.labelDraft = null;
    // labelForm comes from WorkflowContext - clear via WorkflowService
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    if (context) {
      this.workflowSvc.updateContext({ labelForm: null });
    }
    // Removed - labelErrors doesn't exist in UIOrchestrationState
    // Label errors are managed in WorkflowContext
  }

  /** Handle card selection from unified carousel */
  // REMOVED: onCardSelected() - logic moved to WorkflowService.setContextByCard()
  // CardCarouselComponent now handles card selection directly via workflowService.setContextByCard()
  // Component-specific post-load logic (updateContextWithComputedProperties, resumeJob, etc.) 
  // is handled via activeContext$ subscription in ngOnInit()
  
  /**
   * Update context with computed properties that depend on component state
   */
  // updateContextWithComputedProperties() removed - WorkflowActionsComponent computes stageTimeline and activeStage
  
  /**
   * Seamlessly switch from disc workflow context to job workflow context when a job is created.
   * Preserves UI state (scroll position, form inputs, focus) to avoid visual interruption.
   */
  private switchToJobContextSeamlessly(jobId: string, currentContext: WorkflowContext): void {
    
    // Preserve UI state before switching
    const preservedState = {
      scrollY: window.scrollY,
      activeElement: document.activeElement,
      formValues: currentContext?.labelForm ? { ...currentContext.labelForm } : null,
    };
    
    // Find job card in discs array
    const jobCard = this.discs?.find(d => d.job_id === jobId);
    if (!jobCard) {
      this.logger.warn('[RipperPage] Job card not found for seamless switch', { jobId, discsCount: this.discs?.length });
      // Job card might not be in discs array yet - try to switch anyway
      // The card will appear once the coordinator updates
    }
    
    // Update selected card to job (triggers minimal re-render due to OnPush)
    this.workflowSvc.setSelectedCard({ type: 'job', id: jobId });
    
    // IMPORTANT: Compute properties BEFORE calling setContext so they're ready immediately
    // Update currentJobStatus first so stageTimeline/activeStage can be computed correctly
    const tempJobStatus = currentContext.jobStatus;
    if (tempJobStatus && tempJobStatus.jobId === jobId) {
      // WorkflowService manages context - no need to set local state
      // postProcessFiles come from WorkflowContext
      // WorkflowService manages context - no need to set local state
    }
    
    // Pre-compute properties before context is emitted
    const precomputedStageTimeline = this.stageTimeline;
    const precomputedActiveStage = this.getActiveStage();
    
    // Fetch and merge job context
    this.workflowSvc.setContextByCard({ type: 'job', id: jobId }).subscribe({
      next: (jobContext: WorkflowContext) => {
        // Merge job context preserving any local edits and mount point
        const currentMountPoint = (currentContext as any).mountPoint || currentContext.discInfo?.mount_point || 
                                  (currentContext as any).discInfo?.mount_point;
        const jobMountPoint = (jobContext as any).mountPoint || jobContext.discInfo?.mount_point;
        const preservedMountPoint = jobMountPoint || currentMountPoint;
        
        // Update currentJobStatus from jobContext to ensure stageTimeline can be computed
        if (jobContext.jobStatus) {
          // WorkflowService manages context - no need to set local state
          // postProcessFiles come from WorkflowContext
          // WorkflowService manages context - no need to set local state
        }
        
        // Re-compute properties now that we have the correct jobStatus
        const stageTimeline = this.stageTimeline;
        const activeStage = this.getActiveStage();
        
        let mergedLabelForm = preservedState.formValues && this.hasLocalEdits(preservedState.formValues)
          ? { ...preservedState.formValues, ...jobContext.labelForm }
          : jobContext.labelForm;
        const mergedContext = {
          ...jobContext,
          // Preserve mount point if job context doesn't have it
          mountPoint: preservedMountPoint,
          // Preserve form values if user was editing (merge with job context form)
          labelForm: mergedLabelForm,
          // IMPORTANT: Include computed properties IMMEDIATELY so initializeFromContext receives them
          stageTimeline: stageTimeline || [],
          activeStage: activeStage || null,
          // progressUpdateTrigger, stageProgressFn and isStageCompletedFn removed - now handled by WorkflowService observables
          // Phase 1: Preserve workflowStep from jobContext (already migrated from labelForm)
          workflowStep: jobContext.workflowStep,
          stepNavigationSource: jobContext.stepNavigationSource,
        };
        
        // Also ensure discInfo has mount_point
        if (preservedMountPoint && mergedContext.discInfo && !mergedContext.discInfo.mount_point) {
          mergedContext.discInfo = {
            ...mergedContext.discInfo,
            mount_point: preservedMountPoint
          };
        }
        
        // Update context with merged context INCLUDING computed properties
        // This must happen IMMEDIATELY so initializeFromContext receives complete context
        this.workflowSvc.updateContext(mergedContext);
        
        // Set contextLoading to false now that context is loaded
        this.workflowSvc.updateUIOrchestrationState({ contextLoading: false });
        
        // Restore UI state after Angular has updated
        setTimeout(() => {
          window.scrollTo({ top: preservedState.scrollY, behavior: 'auto' });
          if (preservedState.activeElement && preservedState.activeElement instanceof HTMLElement) {
            preservedState.activeElement.focus();
          }
        }, 0);
        
        this.cdr.markForCheck(); // Trigger change detection without full refresh
        
        this.logger.log('[RipperPage] Seamlessly switched to job workflow context', {
          jobId,
          previousContext: currentContext.id,
          newContext: jobContext.id,
          preservedScroll: preservedState.scrollY
        });
      },
      error: (err: any) => {
        this.logger.error('[RipperPage] Failed to seamlessly switch to job context', err);
        // Set contextLoading to false even on error
        this.workflowSvc.updateUIOrchestrationState({ contextLoading: false });
        this.cdr.markForCheck();
        // Restore UI state even on error
        setTimeout(() => {
          window.scrollTo({ top: preservedState.scrollY, behavior: 'auto' });
        }, 0);
      }
    });
  }
  
  /**
   * Attempt to auto-navigate away from film step when job is created/received.
   * Uses recursive polling to wait for workflowComponent to be ready.
   */
  private attemptAutoNavigation(jobId: string, attempt: number, maxAttempts: number): void {
    const workflowComp = this.workflowComponent as any;
    
    if (!workflowComp) {
      if (attempt < maxAttempts) {
        setTimeout(() => this.attemptAutoNavigation(jobId, attempt + 1, maxAttempts), 50);
      } else {
        this.logger.warn('[RipperPage] Auto-navigation failed - workflowComponent not available after max attempts');
      }
      return;
    }

    // Check if we're on the film step
    if (workflowComp.currentStep === 'film') {
      // Navigate to next step
      workflowComp.currentStepIndex++;
      if (workflowComp.steps && workflowComp.currentStepIndex < workflowComp.steps.length) {
        workflowComp.currentStep = workflowComp.steps[workflowComp.currentStepIndex];
        workflowComp.saveCurrentStep();
        workflowComp.cdr.markForCheck();
        this.cdr.markForCheck();
        
        this.logger.log('[RipperPage] Auto-navigated from film step', {
          newStep: workflowComp.currentStep,
          newStepIndex: workflowComp.currentStepIndex,
          jobId
        });
      } else {
        this.logger.warn('[RipperPage] Auto-navigation failed - no next step available', {
          currentStepIndex: workflowComp.currentStepIndex,
          stepsLength: workflowComp.steps?.length,
          jobId
        });
      }
    }
  }

  /**
   * Check if user has made local edits that should be preserved during context switch.
   * This is a simple check - can be enhanced based on specific needs.
   */
  private hasLocalEdits(formValues: any): boolean {
    // Check if form has been modified from initial state
    // For now, we'll preserve if form has any non-empty values
    if (!formValues) return false;
    
    // Check for common editable fields that indicate user interaction
    const editableFields = ['movie_name', 'release_name', 'release_slug', 'disc_name', 'disc_slug', 'tmdb_id'];
    return editableFields.some(field => {
      const value = formValues[field];
      return value !== null && value !== undefined && value !== '';
    });
  }
  
  // syncStateFromContext removed - state is now updated directly from activeContext$ subscription

  /**
   * Merge tracks arrays while preserving local user edits.
   * Matches tracks by source_file, track_id, or title_id.
   * Preserves local edits for: type, title, description, season, episode.
   * Adds new tracks from context that don't exist locally.
   */
  private mergeTracksPreservingLocalEdits(localTracks: any[], contextTracks: any[]): any[] {
    if (!Array.isArray(localTracks) || localTracks.length === 0) {
      return Array.isArray(contextTracks) ? [...contextTracks] : [];
    }
    if (!Array.isArray(contextTracks) || contextTracks.length === 0) {
      return [...localTracks];
    }

    // Helper to get track identifier for matching
    const getTrackKey = (track: any): string => {
      return track.title_id || '';
    };

    // Build a map of local tracks by their key
    const localTracksMap = new Map<string, any>();
    for (const track of localTracks) {
      const key = getTrackKey(track);
      if (key) {
        localTracksMap.set(key, track);
      }
    }

    // Build a map of context tracks by their key
    const contextTracksMap = new Map<string, any>();
    for (const track of contextTracks) {
      const key = getTrackKey(track);
      if (key) {
        contextTracksMap.set(key, track);
      }
    }

    // Merge tracks: preserve local edits for matched tracks, add new ones from context
    const mergedTracks: any[] = [];
    const processedKeys = new Set<string>();

    // First, process all context tracks (to maintain order from context)
    for (const contextTrack of contextTracks) {
      const key = getTrackKey(contextTrack);
      if (!key) continue;

      processedKeys.add(key);
      const localTrack = localTracksMap.get(key);

      if (localTrack) {
        // Track exists in both - merge preserving local edits
        const merged = { ...contextTrack };
        
        // Preserve local edits for editable fields
        // Key strategy: preserve local value if it's explicitly set (not undefined/null)
        // For empty strings, preserve them if context also has empty (user cleared it)
        // Otherwise prefer context value when local is empty
        const fieldsToPreserve: (keyof any)[] = ['type', 'title', 'description', 'season', 'episode'];
        
        for (const field of fieldsToPreserve) {
          const localValue = localTrack[field];
          const contextValue = contextTrack[field];
          
          
          // Preserve local value if:
          // 1. Local has a non-empty value (user explicitly set it)
          // 2. Local is empty string but context is also empty (user cleared it)
          // 3. Local is explicitly null/undefined when context has a value (respect user's clear)
          if (localValue !== undefined && localValue !== null) {
            // Local has an explicit value (including empty string), preserve it
            merged[field] = localValue;
          } else if (contextValue !== undefined && contextValue !== null && contextValue !== '') {
            // Use context value if local wasn't explicitly set
            merged[field] = contextValue;
          }
        }
        
        // Also preserve note field (legacy alias for description)
        if (localTrack.note !== undefined && localTrack.note !== null) {
          merged.note = localTrack.note;
        } else if (merged.description && !merged.note) {
          merged.note = merged.description;
        }
        
        mergedTracks.push(merged);
      } else {
        // New track from context - add it
        mergedTracks.push({ ...contextTrack });
      }
    }

    // Add any local tracks that weren't in context (shouldn't happen often, but preserve them)
    for (const localTrack of localTracks) {
      const key = getTrackKey(localTrack);
      if (key && !processedKeys.has(key)) {
        mergedTracks.push({ ...localTrack });
      }
    }

    return mergedTracks;
  }
  
  /** Fallback to local context building for jobs */
  private fallbackToLocalJobContext(jobId: string): void {
    // Use only service cache (component cache is deprecated)
    const cachedJob = this.workflowSvc.getCachedJobData(jobId);
    if (cachedJob) {
      // Build context locally
      const context = this.workflowSvc.buildContextFromJob(
        cachedJob,
        this.movieOptions,
        this.boxsetOptions,
        this.releaseOptions,
        this.groupOptions,
        this.titleStatus.bind(this),
        this.titleProgressValue.bind(this),
        this.titleIsActive.bind(this),
        this.previewUrlForTitle.bind(this),
        this.titlePreviewState.bind(this),
        this.titlePath.bind(this),
        this.workflowSvc.getCurrentContext()?.postProcessFiles || [],
        this.transferDestination,
        this.releaseDiscs,
        this.boxsetMovies,
        this.devMode,
        null,
        null
      );
      
      // Update active context directly (no cache to set)
      // Access private _activeContext$ to set it directly, then sync state
      (this.workflowSvc as any)._activeContext$.next(context);
      this.workflowSvc.syncStateFromContext(context);
      
      // Don't call loadWorkflowFromJob here - it will overwrite the context data
      // Instead, just ensure job tracking is resumed
      this.resumeJob(cachedJob.jobId);
      // WorkflowService manages context - no need to set local state
      // postProcessFiles come from WorkflowContext
      this.workflowSvc.updateUIOrchestrationState({ contextLoading: false });
    } else {
      // Load job if not cached
      this.onSelectUnfinishedJob(jobId);
      // Ensure contextLoading is set to false even if job is not cached
      this.workflowSvc.updateUIOrchestrationState({ contextLoading: false });
      this.cdr.markForCheck();
    }
  }

  /** Load workflow data synchronously from cached job data 
   * @deprecated - WorkflowService handles hydration when building contexts via buildContextFromJob
   * This method should be removed - contexts are built by WorkflowService.setContextByCard()
   */
  private loadWorkflowFromJob(job: JobStatus): void {
    // TODO: Remove this method - WorkflowService.buildContextFromJob handles this
    // For now, just resume job tracking and update post-process files
    this.workflowSvc.updateDiscInfoState({ discDbState: 'miss' });
    this.workflowSvc.updateUIOrchestrationState({ driveError: null });
    this.resumeJob(job.jobId);
    // updatePostProcessFiles() removed - use WorkflowContext.postProcessFiles
    this.labelDraftProcessed = true;
  }

  selectedDriveFor(info: DiscDetail | null): Drive | null {
    if (!info) return null;
    // Get drive from WorkflowService
    const selectedCard = this.workflowSvc.getSelectedCard();
    if (selectedCard?.type === 'drive' && selectedCard.id === info.mount_point) {
      // Return drive info from selectedCard
      return {
        disc_num: info.disc_num,
        mount_point: info.mount_point,
        name: info.makemkv_disc_name || (info as any)?.info_title || undefined
      };
    }
    return null;
  }

  private notifyBackendError(err: any): void {
    if (!err) return;
    const status = err.status ?? err.code;
    // Only show backend errors for actual HTTP errors, not SSE connection interruptions during page load
    if ((status === 0 || status === 'ECONNREFUSED' || err.statusText === 'Unknown Error') && 
        err.name !== 'EventSource' && 
        !err.message?.includes('EventSource')) {
      // Check if this is an SSE error - if so, don't show toast as it's handled by retry logic
      const isSSEError = err.target?.constructor?.name === 'EventSource' || 
                         err.srcElement?.constructor?.name === 'EventSource';
      if (!isSSEError) {
        // Set backend error instead of showing toast
        this.workflowSvc.updateUIOrchestrationState({ backendError: 'Unable to Connect to Backend Service' });
      }
    }
  }

  get ctaState(): CtaState {
    const base: CtaState = {
      label: this.discMode === 'rip' ? 'Start Archive' : 'Start Copy',
      disabled: false,
      spinner: false,
      action: 'start',
      intent: 'start',
    };

    const ctx6 = this.workflowSvc.getCurrentContext();
    if (!ctx6) return base;
    const job = ctx6?.jobStatus || null;
    if (!job) return base;

    const profile = this.jobProfile(job);
    const ripState = this.pipelineState('rip');
    const labelState = this.pipelineState('label');
    const finalizeState = job.finalize_state || job.pipeline?.['finalize'] || null;
    const postState = this.pipelineState('postprocess');
    const transferState = this.pipelineState('transfer');
    const finalizeReleaseState = job.finalize_release_state || job.pipeline?.['finalize_release'] || null;
    const jobDone =
      job.job_status === 'completed' ||
      job.phase === 'complete' ||
      (profile === 'hit' && transferState === 'completed') ||
      (profile === 'miss' && finalizeReleaseState === 'completed') ||
      (profile === 'miss' && finalizeReleaseState === 'skipped' && transferState === 'completed');

    // Don't show "Copy failed" if rip completed successfully and there's evidence of success (result_location, artifacts, or completed states)
    // This handles cases where the backend incorrectly marks the job as failed after successful operations
    const ripAndTransferCompleted = ripState === 'completed' && transferState === 'completed';
    const ripCompletedAndFinalized = ripState === 'completed' && finalizeState === 'completed';
    const hasJobDir = !!(job.job_dir || this.lastArtifacts?.job_dir);
    const hasArtifacts = !!(this.lastArtifacts?.post_paths && Object.keys(this.lastArtifacts.post_paths).length > 0) ||
                         !!(this.lastArtifacts?.ripped_files && Object.keys(this.lastArtifacts.ripped_files).length > 0);
    const ripCompletedWithEvidence = ripState === 'completed' && (hasJobDir || hasArtifacts || transferState === 'completed' || finalizeState === 'completed');
    
    if ((job.job_status === 'failed' || ripState === 'failed' || postState === 'failed') && !ripAndTransferCompleted && !ripCompletedAndFinalized && !ripCompletedWithEvidence) {
      const reason = (job as any)?.error_reason;
      const jobId = job.jobId || null;
      
      // Log error to console (only once per job/error combination to prevent spam)
      if (reason && (reason !== this.lastLoggedError || jobId !== this.lastLoggedJobId)) {
        this.logger.error('[Ripper] Copy failed', {
          jobId: jobId,
          error_reason: reason,
          job_status: job.job_status,
          rip_state: ripState,
          post_state: postState
        });
        this.lastLoggedError = reason;
        this.lastLoggedJobId = jobId;
      }
      
      // Keep button label simple - don't include error message to prevent flashing
      return { label: 'Retry', disabled: false, spinner: false, action: 'start', intent: 'retry' };
    }

    if (job.job_status === 'failed' && ripState === 'completed' && (postState === 'pending' || postState === 'ready')) {
      return { label: 'Post-Process', disabled: false, spinner: false, action: 'postprocess', intent: 'progress' };
    }

    if (jobDone) {
      return { label: 'Done', disabled: true, spinner: false, action: 'none', intent: 'done' };
    }

    const ctx7 = this.workflowSvc.getCurrentContext();
    if (!ctx7) return base;
    if (transferState === 'running' || ctx7?.jobStatus?.transfer_state === 'running') {
      const pct = typeof job.transfer_progress === 'number' ? job.transfer_progress : null;
      const label = pct !== null && pct >= 0 ? `Transferring ${pct}%` : 'Transferring…';
      return { label, disabled: true, spinner: true, action: 'none', intent: 'transfer' };
    }

    if (profile === 'miss') {
      if (ripState === 'completed' && labelState !== 'completed') {
        const pct = this.labelCompletionPercent;
        const suffix = pct > 0 ? ` ${pct}%` : '';
        return { label: `Labeling${suffix}`, disabled: true, spinner: false, action: 'none', intent: 'progress' };
      }
      
      // After labels complete, show Post-Process button (skip finalize step)
      if (ripState === 'completed' && labelState === 'completed' && (postState === 'ready' || postState === 'pending')) {
        return { label: 'Post-Process', disabled: false, spinner: false, action: 'postprocess', intent: 'progress' };
      }
    }


    // Check for failed post-processing first
    if (postState === 'failed') {
      return { 
        label: 'Failed', 
        disabled: true, 
        spinner: false, 
        action: 'none', 
        intent: 'retry' 
      };
    }

    if (postState === 'completed' && transferState === 'ready') {
      return { label: 'Continue', disabled: false, spinner: false, action: 'none', intent: 'progress' };
    }

    if (postState === 'completed' && transferState !== 'completed') {
      return { label: 'Transfer', disabled: false, spinner: false, action: 'transfer', intent: 'transfer' };
    }

    // Check for post-processing: post_state is running, job_status is validating, phase is postprocess, or we have post_progress
    const hasPostProgress = typeof job.post_progress === 'number' && job.post_progress >= 0;
    const isPostProcessing = postState === 'running' || 
                             job.job_status === 'validating' || 
                             job.phase === 'postprocess' ||
                             (hasPostProgress && ripState === 'completed');
    if (isPostProcessing) {
      // When post-processing is running, show "Continue" disabled with spinner
      return { label: 'Continue', disabled: true, spinner: true, action: 'none', intent: 'progress' };
    }

    // Show Post-Process button when labels are complete (skip finalize step)
    if (ripState === 'completed' && labelState === 'completed' && (postState === 'pending' || postState === 'ready')) {
      return { label: 'Post-Process', disabled: false, spinner: false, action: 'postprocess', intent: 'progress' };
    }

    // Check isRipping AFTER checking for completed rip - this prevents "Copying…" from showing after completion
    // This ensures smooth transition from "Copy Failed" -> "Copying…" when retrying, but doesn't block Label after completion
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return base;
    const isRipping = context?.jobStatus?.job_status === 'running' || context?.jobStatus?.job_status === 'pending' || false;
    if (isRipping && ripState !== 'completed') {
      const context = this.workflowSvc.getCurrentContext();
      if (!context) return base;
      const pct = Math.max(context?.jobStatus?.rip_progress || 0, job.rip_progress || 0);
      const label = pct > 0 ? `Copying ${pct}%` : 'Copying…';
      return { label, disabled: true, spinner: true, action: 'none', intent: 'progress' };
    }

    if (ripState === 'running' || job.job_status === 'running' || job.job_status === 'pending') {
      const context = this.workflowSvc.getCurrentContext();
      if (!context) return base;
      const pct = Math.max(context?.jobStatus?.rip_progress || 0, job.rip_progress || 0);
      const label = pct > 0 ? `Copying ${pct}%` : 'Copying…';
      return { label, disabled: true, spinner: true, action: 'none', intent: 'progress' };
    }
    
    // Fallback: Also check if isRipping is true (even if ripState isn't 'running' yet)
    // This handles cases where job is starting but state hasn't propagated yet
    if (isRipping && ripState !== 'completed' && job.job_status !== 'completed' && job.job_status !== 'failed') {
      const context = this.workflowSvc.getCurrentContext();
      if (!context) return base;
      const pct = Math.max(context?.jobStatus?.rip_progress || 0, job.rip_progress || 0);
      const label = pct > 0 ? `Copying ${pct}%` : 'Copying…';
      return { label, disabled: true, spinner: true, action: 'none', intent: 'progress' };
    }

    return base;
  }

  get shouldShowTransferCta(): boolean {
    return this.ctaState.intent === 'transfer' && this.ctaState.action === 'transfer';
  }

  get startButtonLabel(): string {
    return this.ctaState.label;
  }

  get primaryCtaDisabled(): boolean {
    return this.ctaState.disabled;
  }

  private currentJobId(): string | null {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    return context?.jobStatus?.jobId || null;
  }

  loadLabelDraft(): void {
    const jobId = this.currentJobId();
    if (!jobId) return;
    if (!this.prefillAllowed && this.prefillDecided) return;
    this.labelLoading = true;
    this.jobSvc.prefillLabel(jobId).subscribe({
      next: draft => {
        this.labelDraft = draft;
        const context = this.workflowSvc.getCurrentContext();
        if (context?.labelForm && (draft.movie_id != null || draft.group_type != null)) {
          this.workflowSvc.updateContext({
            labelForm: {
              ...context.labelForm,
              ...(draft.movie_id != null && { movie_id: draft.movie_id }),
              ...(draft.group_type != null && { group_type: draft.group_type, mode: draft.group_type }),
            },
          });
        }
        this.validateLabelForm();
      },
      error: err => {
        this.toast.show(err?.error?.detail || 'Failed to load label draft', 'error');
      },
      complete: () => {
        this.labelLoading = false;
      },
    });
  }

  saveLabelDraft(queuedCardContext?: { type: 'drive' | 'job', id: string } | null, queuedDiscId?: string | null): void {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    
    // Validate card context hasn't changed if queued context was provided
    if (queuedCardContext && this.workflowSvc.getSelectedCard()) {
      const selectedCard = this.workflowSvc.getSelectedCard();
      const currentCardContext = selectedCard ? { type: selectedCard.type, id: selectedCard.id } : null;
      const cardChanged = !currentCardContext || queuedCardContext.type !== currentCardContext.type ||
                         queuedCardContext.id !== currentCardContext.id;
      
      if (cardChanged) {
        return; // Don't save if card changed
      }
    }
    
    const jobId = this.currentJobId();
    if (jobId && this.workflowSvc.isDiscStepContinueInProgress()) {
      return;
    }
    if (!this.labelForm) return;
    // Allow saving if we have release_year or release_name, even if other fields aren't filled
    // context already declared at line 3416
    if (!context) return;
    const hasReleaseData = !!(context?.labelForm?.release_year || context?.labelForm?.release_name);
    if (!this.hasLabelContent() && !hasReleaseData) {
      // Removed - labelSaving doesn't exist in UIOrchestrationState
      // Label saving state is managed in WorkflowContext
      this.workflowSvc.updateContext({ lastAutosaveOk: false });
      return;
    }
    const discId = this.workflowSvc.getDiscInfoState().currentDiscId;
    // Removed - labelSaving doesn't exist in UIOrchestrationState
    // Label saving state is managed in WorkflowContext
    this.rememberManualReleaseFromForm();
    const ctx8 = this.workflowSvc.getCurrentContext();
    if (!ctx8 || !ctx8.labelForm) return;
    const payload = { ...ctx8.labelForm, tracks: ctx8.labelForm?.tracks || [] };
    
    
    const canSaveDisc = !!(payload.disc_format && `${payload.disc_format}`.trim());
    if (!canSaveDisc) {
      delete (payload as any).disc_format;
    }
    // Note: disc_number will be calculated by backend if not provided
    // We don't need to set it to null - just omit it from the payload if we don't have a value
    if (!jobId && payload && payload.disc_number === undefined) {
      // Don't include disc_number in payload if we don't have a job yet
      // Backend will calculate it based on the release
      delete (payload as any).disc_number;
    }
    // Do not propagate lastReleaseId into payload: it may be from a different movie and would
    // wrongly link the disc to that release. Use only labelForm.release_id (from context or user).
    // Use workflow context endpoints for saving
    // Use queuedDiscId if provided (from when save was queued), otherwise use current discId
    const effectiveDiscId = queuedDiscId !== undefined ? queuedDiscId : discId;
    const isJob = !!jobId;
    const useDiscId = !isJob && !!effectiveDiscId;
    
    // Use queued card context if provided, otherwise use current selectedCard
    const selectedCard = this.workflowSvc.getSelectedCard();
    const cardContextForSave = queuedCardContext || (selectedCard ? { type: selectedCard.type, id: selectedCard.id } : null);
    const identifier = useDiscId ? effectiveDiscId : (isJob ? jobId : (cardContextForSave?.id || this.lastDiscInfo?.mount_point));
    
    if (!identifier) {
      this.workflowSvc.updateContext({ lastAutosaveOk: false });
      this.lastAutosaveError = 'No identifier available for saving';
      // Removed - labelSaving doesn't exist in UIOrchestrationState
      // Label saving state is managed in WorkflowContext
      return;
    }
    
    // For discs without disc_id yet, use mount_point
    const useMountPoint = !isJob && !useDiscId;
    
    const isFullUpdate = false; // Use PATCH for auto-save
    const saveObservable = isJob
      ? this.workflowSvc.saveJobWorkflowContext(identifier, payload, isFullUpdate)
      : this.workflowSvc.saveDiscWorkflowContext(identifier, payload, isFullUpdate, useDiscId);
    
    this.logLabelSave('ops', { identifier, isJob, useDiscId, useMountPoint, method: 'workflow-context' });
    saveObservable.subscribe({
      next: (updatedContext) => {
        // Apply only if selected card still matches (avoids overwriting with another job/disc data)
        if (!this.workflowSvc.applyContextIfMatchesSelection(updatedContext)) return;
        
        // Sync state from updated context
        if (updatedContext.labelForm) {
          if (updatedContext.labelForm.disc_id) {
            this.workflowSvc.updateDiscInfoState({ currentDiscId: updatedContext.labelForm.disc_id });
            // Update via WorkflowService
            const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
            if (context?.labelForm) {
              this.workflowSvc.updateContext({ 
                labelForm: { ...context.labelForm, disc_id: updatedContext.labelForm.disc_id }
              });
            }
        }
          if (updatedContext.labelForm.release_id) {
            this.lastReleaseId = updatedContext.labelForm.release_id;
          // Update via WorkflowService
          const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
          if (context?.labelForm) {
            this.workflowSvc.updateContext({ 
              labelForm: { 
                ...context.labelForm, 
                release_id: updatedContext.labelForm.release_id,
                release_slug: updatedContext.labelForm.release_slug,
                disc_group: updatedContext.labelForm.release_slug
              }
            });
          }
        }
          if (updatedContext.labelForm.disc_number !== undefined && updatedContext.labelForm.disc_number !== null && this.labelForm) {
            this.labelForm.disc_number = updatedContext.labelForm.disc_number;
        }
        // Clear recalculate_disc_numbers flag after successful save
        if (this.labelForm && this.labelForm.recalculate_disc_numbers) {
          delete this.labelForm.recalculate_disc_numbers;
        }
        }
        
        // Trigger change detection since label-shell uses OnPush
        this.cdr.markForCheck();
        // Reload disc data to get updated information
        if (this.lastDiscInfo) {
          this.loadDiscAndReleaseLabels(this.lastDiscInfo);
        }
        // Removed - lastAutosaveOk doesn't exist in UIOrchestrationState
        // Autosave state is managed in WorkflowContext
        this.lastAutosaveError = null;
      },
      error: (err: any) => {
        const msgRaw = err?.error?.detail || err?.message || 'Failed to save label';
        const msg = typeof msgRaw === 'string' ? msgRaw : JSON.stringify(msgRaw);
        // Removed - lastAutosaveOk doesn't exist in UIOrchestrationState
        // Autosave state is managed in WorkflowContext
        this.lastAutosaveError = msg;
      },
      complete: () => {
        // Removed - labelSaving doesn't exist in UIOrchestrationState
        // Label saving state is managed in WorkflowContext
      },
    });
  }

  // REMOVED: finalizeLabel() - moved to WorkflowService.finalizeLabel()
  // WorkflowLabelingComponent calls workflowService.finalizeLabel() directly

  private logLabelSave(target: 'release' | 'disc' | 'job' | 'ops', payload: any): void {
    try {
      this.logger.debug('[LabelSave]', target, JSON.stringify(payload || {}));
    } catch {
      this.logger.debug('[LabelSave]', target, payload);
    }
  }

  // buildLabelForm removed - WorkflowService handles labelForm building internally
  

  private buildMetadataPayload(): any {
    const clean = (v: any) => {
      if (v === undefined || v === null) return null;
      if (typeof v === 'string' && v.trim() === '') return null;
      return v;
    };
    const asNumber = (v: any) => {
      if (v === undefined || v === null || `${v}`.trim() === '') return null;
      const n = Number(v);
      return Number.isNaN(n) ? null : n;
    };
    const releaseSlug = clean(this.labelForm?.release_slug || this.labelForm?.disc_group);
    const isLinkedToBoxset = !!(this.labelForm?.boxset_id);
    const releasePayload: any = {
      release_id: this.labelForm?.release_id ?? null,
      release_slug: releaseSlug,
      release_name: clean(this.labelForm?.release_name),
      info_title: clean(this.labelForm?.info_title || this.labelForm?.info_label),
      production_year: asNumber(this.labelForm?.production_year),
      tmdb_id: clean(this.labelForm?.tmdb_id),
      group_type: clean(this.labelForm?.group_type || this.labelForm?.mode),
      mode: clean(this.labelForm?.mode),
      boxset_id: clean(this.labelForm?.boxset_id), // Use ID for association
    };
    // Only include boxset-owned fields if NOT linked to a boxset
    // When linked, these fields are owned by the boxset and should not be saved to the release
    if (!isLinkedToBoxset) {
      releasePayload.release_year = asNumber(this.labelForm?.release_year);
      releasePayload.upc = clean(this.labelForm?.upc);
      releasePayload.asin = clean(this.labelForm?.asin);
      releasePayload.cover_front_url = clean(this.labelForm?.cover_front_url);
      releasePayload.cover_back_url = clean(this.labelForm?.cover_back_url);
    }
    return {
      release: releasePayload,
      disc: {
        disc_number: asNumber(this.labelForm?.disc_number),
        disc_slug: clean(this.labelForm?.disc_slug),
        disc_name: clean(this.labelForm?.disc_name),
        disc_format: clean(this.labelForm?.disc_format),
        info_title: clean(this.labelForm?.info_title || this.labelForm?.info_label),
        disc_group: releaseSlug,
      },
      titles: Array.isArray(this.labelForm?.tracks)
        ? this.labelForm.tracks.map((t: any) => ({
            source_file: t.source_file ?? null,
            title_id: t.title_id ?? null,
            track_id: t.title_id ?? null,
            title: clean(t.title),
            description: clean(t.description ?? t.note),
            comment: clean(t.comment),
            season: asNumber(t.season),
            episode: asNumber(t.episode),
            type: clean(t.type),
            duration: asNumber(t.duration),
            size: asNumber(t.size),
            streams: t.streams ?? null,
          }))
        : [],
    };
  }

  private buildReleasePatchPayload(): any {
    if (!this.labelForm) return {};
    const clean = (v: any) => {
      if (v === undefined || v === null) return null;
      if (typeof v === 'string' && v.trim() === '') return null;
      return v;
    };
    const asNumber = (v: any) => {
      if (v === undefined || v === null || `${v}`.trim() === '') return null;
      const n = Number(v);
      return Number.isNaN(n) ? null : n;
    };
    const isLinkedToBoxset = !!this.labelForm.boxset_id;
    const payload: any = {
      // Note: release_slug is auto-generated by backend, not used for lookup
      // We don't send it to avoid confusion - backend uses movie_id + boxset_id for release identification
      release_name: clean(this.labelForm.release_name),
      production_year: asNumber(this.labelForm.production_year),
      movie_id: clean(this.labelForm.movie_id), // Include movie_id for release creation
      tmdb_id: clean(this.labelForm.tmdb_id),
      group_type: clean(this.labelForm.group_type || this.labelForm.mode),
      mode: clean(this.labelForm.mode),
      boxset_id: clean(this.labelForm.boxset_id), // Include boxset_id to link release to boxset
    };
    // Include recalculate_disc_numbers flag if set (for disc number normalization)
    if (this.labelForm.recalculate_disc_numbers) {
      payload.recalculate_disc_numbers = true;
    }
    // Only include boxset-owned fields if NOT linked to a boxset
    // When linked, these fields are owned by the boxset and should not be saved to the release
    if (!isLinkedToBoxset) {
      payload.release_year = asNumber(this.labelForm.release_year);
      payload.upc = clean(this.labelForm.upc);
      payload.asin = clean(this.labelForm.asin);
      payload.cover_front_url = clean(this.labelForm.cover_front_url);
      payload.cover_back_url = clean(this.labelForm.cover_back_url);
    }
    return payload;
  }

  private buildOpsPayload(): any[] {
    if (!this.labelForm) return [];
    const clean = (v: any) => {
      if (v === undefined || v === null) return null;
      if (typeof v === 'string' && v.trim() === '') return null;
      return v;
    };
    const asNumber = (v: any) => {
      if (v === undefined || v === null || `${v}`.trim() === '') return null;
      const n = Number(v);
      return Number.isNaN(n) ? null : n;
    };
    const ops: any[] = [];
    const releaseFields = this.buildReleasePatchPayload();
    const releaseUpdates: any = {};
    Object.entries(releaseFields).forEach(([k, v]) => {
      // Include release_year even if it's 0 (valid year), but exclude null/undefined
      // Exception: include boxset_id even if null when explicitly unlinked (for fallback)
      if (k === 'boxset_id' && this.explicitlyUnlinkedBoxset && v === null) {
        releaseUpdates[k] = null; // Explicitly include null to unlink via ops endpoint
      } else if (v !== null && v !== undefined) {
        releaseUpdates[k] = v;
      }
    });
    if (Object.keys(releaseUpdates).length > 0) {
      ops.push({ target: 'release', fields: releaseUpdates });
    }
    if (this.labelForm) {
      const discUpdates: any = {};
      if (this.labelForm.disc_name) discUpdates.disc_name = this.labelForm.disc_name;
      if (this.labelForm.disc_slug) discUpdates.disc_slug = this.labelForm.disc_slug;
      if (this.labelForm.disc_format) discUpdates.disc_format = this.labelForm.disc_format;
      if (this.labelForm.disc_number !== undefined && this.labelForm.disc_number !== null) discUpdates.disc_number = this.labelForm.disc_number;
      if (Object.keys(discUpdates).length > 0) {
        ops.push({ target: 'disc', fields: discUpdates });
      }
      const seenTitles = new Set<string>();
      if (Array.isArray(this.labelForm.tracks)) {
        this.labelForm.tracks.forEach((t: any) => {
          const titleId = t.title_id || null;
          if (!titleId || seenTitles.has(titleId)) return;
          seenTitles.add(titleId);
          const titleFields: any = {
            description: clean(t.description ?? t.note),
            comment: clean(t.comment),
            season: asNumber(t.season),
            episode: asNumber(t.episode),
            type: clean(t.type),
            duration: asNumber(t.duration),
            size: asNumber(t.size),
          };
          // Always include title field if it exists in form data (even if null/empty) to allow clearing it
          if (t.hasOwnProperty('title')) {
            titleFields.title = clean(t.title);
          }
          // Remove null values for other fields, but keep title even if null
          Object.keys(titleFields).forEach(k => {
            if (titleFields[k] === null && k !== 'title') delete titleFields[k];
          });
          if (Object.keys(titleFields).length > 0) {
            ops.push({ target: 'title', id: titleId, fields: titleFields });
          }
        });
      }
    }
    return ops;
  }

  onLabelChange(): void {
    // NOTE: labelForm "tracks" are MakeMKV titles (disc_titles); title_streams are DB stream rows
    // Titles are what the user edits in the UI, and they're saved separately
    // We don't sync titles to labelForm.tracks here - they're different data structures
    
    this.validateLabelForm();
    if (this.labelForm && this.labelForm.release_slug) {
      this.labelForm.disc_group = this.labelForm.release_slug;
    }
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    if (!this.discNameLocked && context?.labelForm?.disc_name) {
      this.discNameLocked = true;
    }
    if (!this.discSlugLocked && context?.labelForm?.disc_slug) {
      this.discSlugLocked = true;
    }
    this.syncReleaseDetailsFromForm();
    this.updateCurrentGroupOption();
    
    // Save immediately - no debounce
    const selectedCardForSave = this.workflowSvc.getSelectedCard();
    const cardContext = selectedCardForSave ? { type: selectedCardForSave.type, id: selectedCardForSave.id } : null;
    const discId = this.workflowSvc.getDiscInfoState().currentDiscId;
    this.saveLabelDraft(cardContext, discId);
    
    // Progress updates now handled automatically by WorkflowService observables
    this.cdr.markForCheck();
  }

  onNameChange(): void {
    this.onLabelChange();
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    if (context?.labelForm) this.discNameLocked = true;
  }

  onDiscFormatChange(): void {
    this.discFormatAuto = false;
    this.onLabelChange();
  }

  onSlugEdited(): void {
    this.discSlugLocked = true;
  }

  onNameBlur(): void {
    // force change detection-driven updates such as hero title
    this.heroTitle(this.lastDiscInfo);
  }

  /**
   * Persist a single-title field change from the mobile TitleModal to the
   * backend. Mirrors WorkflowLabelingComponent.onTitlePatch which handles
   * the desktop app-title-editor path. Both flow through the same
   * PATCH /api/discs/{id}/titles endpoint via workflowService.patchDiscTitle.
   */
  onTitleModalPatch(patch: TitlePatchRequest): void {
    const discId =
      this.workflowSvc.getDiscInfoState().currentDiscId ||
      (this.workflowSvc.getCurrentContext()?.discInfo as any)?.disc_id ||
      null;
    if (!discId) return;
    this.workflowSvc.patchDiscTitle(discId, patch).subscribe({
      error: (err) => {
        this.logger.error('[RipperPage] Title modal patch failed', err);
      },
    });
  }

  onFieldBlur(): void {
    // Save immediately on blur (same as onLabelChange now, but kept for explicit blur handling)
    const selectedCardForBlur = this.workflowSvc.getSelectedCard();
    const cardContext = selectedCardForBlur ? { type: selectedCardForBlur.type, id: selectedCardForBlur.id } : null;
    const discId = this.workflowSvc.getDiscInfoState().currentDiscId;
    this.saveLabelDraft(cardContext, discId);
  }

  onCoverChange(): void {
    this.onLabelChange();
  }


  toggleBoxset(): void {
    if (!this.labelForm) {
      return;
    }
    
    if (this.labelForm.boxset_id) {
      // Toggle OFF: Unlink from boxset
      const boxsetId = this.labelForm.boxset_id;
      const releaseId = this.labelForm.release_id ?? null;
      
      // Update UI immediately
      this.labelForm.boxset_id = null;
      this.selectedBoxset = null;
      this.explicitlyUnlinkedBoxset = true; // Mark that user explicitly unlinked
      
      // Call unlink endpoint if we have both IDs
      if (boxsetId && releaseId) {
        // First get the release to find its boxset
        this.metadataSvc.getRelease(releaseId).subscribe({
          next: (release) => {
            const releaseBoxsetId = (release as any)?.boxset_id;
            if (releaseBoxsetId) {
              this.metadataSvc.removeReleaseFromBoxset(releaseBoxsetId, releaseId).subscribe({
                next: () => {
                  // Unlink successful, update form state
                  this.onLabelChange();
                  this.cdr.markForCheck();
                },
                error: (err: any) => {
                  // If endpoint fails, log error and fall back to ops endpoint
                  this.logger.error('[Ripper] Failed to unlink release from boxset:', err);
                },
              });
            }
          },
          error: (err: any) => {
            this.logger.error('[Ripper] Failed to get release for unlinking:', err);
          },
        });
        return;
      } else {
        // Fall back to ops endpoint if we don't have release_id yet
        // This will happen when unlinking before a release is created
        this.onLabelChange();
      }
    } else {
      // Toggle ON: Enable boxset mode (selection handled by combobox in workflow)
      this.explicitlyUnlinkedBoxset = false; // Reset flag when linking again
      
      // Set boxset_id to enable boxset mode in UI (will be replaced with actual ID when boxset is selected)
      // Use a temporary truthy value so template switches to boxset combobox
      if (!this.labelForm.boxset_id) {
        this.labelForm.boxset_id = '__pending__'; // Temporary value to enable boxset mode
      }
      
      // Load boxsets if not already loaded (for combobox options)
      if (this.boxsetOptions.length === 0) {
        this.loadBoxsets();
      }
      
      // Trigger change detection
      this.onLabelChange();
    }
  }

  openBoxsetSelectModal(): void {
    // Load boxsets if not already loaded
    if (this.boxsetOptions.length === 0) {
      this.loadBoxsets(() => {
        this.showBoxsetSelectModal = true;
      });
    } else {
      this.showBoxsetSelectModal = true;
    }
  }

  closeBoxsetSelectModal(): void {
    this.showBoxsetSelectModal = false;
    this.boxsetSearch = '';
    this._filteredBoxsetOptionsCache = null;
    this.cdr.markForCheck();
  }

  selectBoxsetFromModal(boxset: BoxsetSummary): void {
    this.selectedBoxset = boxset;
    this.explicitlyUnlinkedBoxset = false; // Reset flag when linking
    if (this.labelForm) {
      this.labelForm.boxset_id = boxset.id || null;
      // Populate fields from boxset (overwrite existing fields when linking)
      this.populateFieldsFromBoxset(boxset);
    }
    this.closeBoxsetSelectModal();
    
    // If both movie and boxset are present, find or create release
    if (this.labelForm?.movie_id && boxset.id) {
      this.findOrCreateReleaseForMovieBoxset(this.labelForm.movie_id, boxset.id);
    } else {
      this.onLabelChange();
    }
  }
  
  private findOrCreateReleaseForMovieBoxset(movieId: string, boxsetId: string): void {
    // Look up release via MetadataService (read-only); link via WorkflowService (HTTP)
    this.metadataSvc.findReleaseByMovieBoxset(movieId, boxsetId).subscribe({
      next: (release) => {
        if (release && release.id) {
          const releaseMovieId = (release as any)?.movie_id;
          if (releaseMovieId && releaseMovieId !== movieId) {
            this.logger.warn(`[Ripper] Release ${release.id} has movie_id ${releaseMovieId}, but requested movie_id is ${movieId} - ignoring release`);
            this.onLabelChange();
            return;
          }
          // Link release (and boxset if present) through WorkflowService; it persists via HTTP
          const boxsetSlug = (release as any)?.boxset_slug ?? undefined;
          const selection: Parameters<typeof this.workflowSvc.applyMetadataSelectionToActiveContext>[0] = {
            releaseId: release.id,
            releaseSlug: release.slug,
            ...(release.boxset_id && release.boxset_id === boxsetId
              ? { boxsetId: release.boxset_id, boxsetSlug: boxsetSlug ?? undefined }
              : {}),
          };
          this.workflowSvc.applyMetadataSelectionToActiveContext(selection).subscribe({
            next: () => {
              const ctx = this.workflowSvc.getCurrentContext();
              this.lastReleaseId = ctx?.labelForm?.release_id ?? release.id;
              this.lastReleaseSlug = ctx?.labelForm?.release_slug ?? release.slug;
              this.cdr.markForCheck();
            },
            error: (err: any) => {
              this.logger.error('[Ripper] Failed to link release to context', err);
              this.cdr.markForCheck();
            },
          });
          return;
        }
        // No release exists yet, will be created when form is saved
        this.onLabelChange();
        this.cdr.markForCheck();
      },
      error: (err: any) => {
        this.logger.error('[Ripper] Failed to find release by movie+boxset', err);
        this.onLabelChange();
      },
    });
  }

  openCreateBoxsetFromSelectModal(): void {
    this.closeBoxsetSelectModal();
    this.openBoxsetCreateModal();
  }

  setContentType(type: 'movie' | 'series'): void {
    if (!this.labelForm) return;
    // Always update group_type (release type) - boxset is just a relationship, not a type
    this.labelForm.group_type = type;
    this.labelForm.mode = type;
    this.onLabelChange();
  }

  get isMovieActive(): boolean {
    if (!this.labelForm) return false;
    return this.labelForm.group_type === 'movie';
  }

  get isSeriesActive(): boolean {
    if (!this.labelForm) return false;
    return this.labelForm.group_type === 'series';
  }

  loadBoxsets(onComplete?: () => void): void {
    // Refresh global boxset cache so getter boxsetOptions shows newly created boxsets
    this.metadataSvc.loadBoxsetOptions();
    this.metadataSvc.listBoxsets().subscribe({
      next: (boxsets: BoxsetSummary[]) => {
        // boxsetOptions now comes from MetadataService getter - no need to set local state
        this._filteredBoxsetOptionsCache = null; // Invalidate cache when options change
        // If labelForm has a boxset_id, select that boxset and update context via WorkflowService (immutable)
        const context = this.workflowSvc.getCurrentContext();
        if (context?.labelForm?.boxset_id) {
          const matchingBoxset = this.metadataSvc.getBoxsetById(context.labelForm.boxset_id);
          if (matchingBoxset) {
            this.selectedBoxset = matchingBoxset;
            const updatedForm = this.metadataSvc.populateFieldsFromBoxset(context.labelForm, matchingBoxset);
            this.workflowSvc.updateContext({ labelForm: updatedForm });
            if (onComplete) onComplete();
            this.cdr.markForCheck();
            return;
          }
        }
        if (onComplete) onComplete();
        this.cdr.markForCheck();
      },
      error: () => {
        if (onComplete) onComplete();
      },
    });
  }

  loadReleaseOptions(onComplete?: () => void): void {
    // Filter to only show releases that aren't linked to a boxset (standalone releases)
    // and exclude DiscDB hits
    const movieId = this.labelForm?.movie_id;
    this.metadataSvc.listReleases(movieId ? { movie_id: movieId } : undefined).subscribe({
      next: (releases: ReleaseSummary[]) => {
        // Cache release options (they're loaded per-movie)
        this._releaseOptionsCache = (releases || [])
          .filter(r => 
            !r.boxset_id && 
            r.discdb_hit !== true &&
            r.slug !== 'pending' &&
            !r.slug?.startsWith('pending-')
          ); // Only standalone, manually created releases (exclude pending)
        if (onComplete) onComplete();
      },
      error: () => {
        this._releaseOptionsCache = [];
        if (onComplete) onComplete();
      },
    });
  }

  /**
   * Populate release fields from boxset.
   * When linking to a boxset, always use boxset values for boxset-owned fields
   * (even if null/empty) to ensure we display boxset data, not release data.
   */
  private populateFieldsFromBoxset(boxset: BoxsetSummary): void {
    // Use MetadataService method (immutable, returns new object)
    if (this.labelForm) {
      const updatedForm = this.metadataSvc.populateFieldsFromBoxset(this.labelForm, boxset);
      Object.assign(this.labelForm, updatedForm);
    }
  }

  toggleBoxsetCombo(): void {
    this.boxsetOpen = !this.boxsetOpen;
    if (this.boxsetOpen && this.boxsetOptions.length === 0) {
      this.loadBoxsets();
    }
  }

  closeBoxsetCombo(): void {
    this.boxsetOpen = false;
  }

  selectBoxset(boxset: BoxsetSummary): void {
    this.selectedBoxset = boxset;
    this.explicitlyUnlinkedBoxset = false; // Reset flag when linking
    if (this.labelForm && boxset.id) {
      // Use WorkflowService to link boxset to active context
      this.workflowSvc.linkBoxsetToActiveContext(boxset.id, boxset.slug).subscribe({
        next: () => {
          this.boxsetOpen = false;
          this.onLabelChange();
          this.cdr.markForCheck();
        },
        error: (err: any) => {
          this.logger.error('[Ripper] Failed to link boxset:', err);
          this.cdr.markForCheck();
        },
      });
    } else {
      this.boxsetOpen = false;
      this.cdr.markForCheck();
    }
  }

  openBoxsetCreateModal(): void {
    this.editingBoxsetId = null;
    this.showBoxsetCreateModal = true;
    this.newBoxset = { name: '', year: null };
  }

  openBoxsetEditModal(boxset: BoxsetSummary, event: Event): void {
    event.stopPropagation();
    this.editingBoxsetId = boxset.id || null;
    this.showBoxsetCreateModal = true;
    this.newBoxset = {
      name: boxset.name || '',
      year: boxset.year || null,
      upc: boxset.upc || undefined,
      asin: boxset.asin || undefined,
      cover_front_url: boxset.cover_front_url || undefined,
      cover_back_url: boxset.cover_back_url || undefined,
    };
  }

  closeBoxsetCreateModal(): void {
    this.showBoxsetCreateModal = false;
    this.editingBoxsetId = null;
    this.newBoxset = { name: '', year: null };
  }

  createBoxset(): void {
    if (!this.newBoxset.name || !this.newBoxset.year) {
      this.toast.show('Boxset name and year are required', 'error', 3000);
      return;
    }
    
    if (this.editingBoxsetId) {
      // Update existing boxset using MetadataService
      this.metadataSvc.updateBoxset(this.editingBoxsetId, {
        name: this.newBoxset.name,
        year: this.newBoxset.year,
        upc: this.newBoxset.upc,
        asin: this.newBoxset.asin,
        cover_front_url: this.newBoxset.cover_front_url,
        cover_back_url: this.newBoxset.cover_back_url,
      }).subscribe({
        next: (boxset) => {
          // If this was the selected boxset, update it
          if (this.selectedBoxset?.id === this.editingBoxsetId) {
            this.selectedBoxset = boxset;
            this.explicitlyUnlinkedBoxset = false; // Reset flag when linking
            if (this.labelForm && boxset.id) {
              // Link boxset to active context
              this.workflowSvc.linkBoxsetToActiveContext(boxset.id, boxset.slug).subscribe({
                next: () => {
                  this.loadBoxsets();
                  this.closeBoxsetCreateModal();
                  this.toast.show('Boxset updated', 'success', 2000);
                  this.cdr.markForCheck();
                },
                error: (err: any) => {
                  this.toast.show(err?.error?.detail || 'Failed to link boxset', 'error', 3000);
                },
              });
            } else {
              this.loadBoxsets();
              this.closeBoxsetCreateModal();
              this.toast.show('Boxset updated', 'success', 2000);
              this.cdr.markForCheck();
            }
          } else {
            this.loadBoxsets();
            this.closeBoxsetCreateModal();
            this.toast.show('Boxset updated', 'success', 2000);
            this.cdr.markForCheck();
          }
        },
        error: (err: any) => {
          this.toast.show(err?.error?.detail || 'Failed to update boxset', 'error', 3000);
        },
      });
    } else {
      // Create new boxset using WorkflowService (creates and links automatically)
      this.workflowSvc.createAndLinkBoxsetToActiveContext({
        name: this.newBoxset.name,
        year: this.newBoxset.year,
        upc: this.newBoxset.upc,
        asin: this.newBoxset.asin,
        cover_front_url: this.newBoxset.cover_front_url,
        cover_back_url: this.newBoxset.cover_back_url,
      }).subscribe({
        next: (result) => {
          const boxset = result.boxset;
          this.selectedBoxset = boxset;
          this.explicitlyUnlinkedBoxset = false; // Reset flag when linking
          this.loadBoxsets();
          this.closeBoxsetCreateModal();
          this.toast.show('Boxset created', 'success', 2000);
          
          // If a movie is selected, find or create release for it in the boxset
          if (this.labelForm?.movie_id && boxset.id) {
            this.findOrCreateReleaseForMovieBoxset(this.labelForm.movie_id, boxset.id);
          } else {
            // No movie yet, just save the boxset link
            this.onLabelChange();
          }
          this.cdr.markForCheck();
        },
        error: (err: any) => {
          this.toast.show(err?.error?.detail || 'Failed to create boxset', 'error', 3000);
        },
      });
    }
  }

  /**
   * Ensure a release exists for the given movie in the boxset.
   * Creates a release if it doesn't already exist, and links it to the boxset.
   */
  private ensureReleaseForBoxsetMovie(boxset: BoxsetSummary, movieId: string): void {
    // Get boxset details to check existing releases
    if (!boxset.id) return;
    this.metadataSvc.getBoxset(boxset.id).subscribe({
      next: (boxsetDetails) => {
        // Check if a release for this movie already exists in the boxset
        const existingRelease = boxsetDetails.releases?.find(
          (r: any) => r.movie_id === movieId
        );
        
        if (existingRelease) {
          // Release already exists, just update labelForm
          if (this.labelForm) {
            this.labelForm.release_id = existingRelease.id;
            this.labelForm.release_slug = existingRelease.slug;
          }
          this.cdr.markForCheck();
          return;
        }
        
        // No existing release, create one when disc metadata is saved
        // The release will be created with boxset info via populateFieldsFromBoxset
        // and linked to boxset in the saveLabelDraft method
      },
      error: (err: any) => {
        this.logger.warn('Failed to get boxset details:', err);
      },
    });
  }

  get filteredBoxsetOptions(): BoxsetSummary[] {
    // Use MetadataService's filter method
    return this.metadataSvc.filterBoxsets(this.boxsetSearch);
  }

  private loadGroupOptions(): void {
    // Get the selected movie_id from labelForm or lastDiscInfo
    const movieId = this.labelForm?.movie_id || (this.lastDiscInfo as any)?.movie_id || null;
    
    // Use MetadataService to load group options
    this.metadataSvc.loadGroupOptions(movieId || undefined).subscribe({
      next: (mapped) => {
        this.workflowSvc.updateContext({ groupOptions: mapped });
        this.updateLastReleaseDetailsFromOptions();
        this.cdr.markForCheck();
      },
      error: () => {},
    });
  }

  applyGroupSelection(group: any): void {
    if (!group) return;
    const context = this.workflowSvc.getCurrentContext();
    if (!context?.labelForm) return;

    const setLocalDisplayFromGroup = (): void => {
      if (group.movie_id) {
        if (group.movie) {
          this.lastMovieDetails = {
            id: group.movie.id,
            name: group.movie.name,
            production_year: group.movie.production_year,
            tmdb_id: group.movie.tmdb_id,
            tmdb_type: group.movie.tmdb_type,
            cover_url: group.movie.cover_url,
            cover_path: group.movie.cover_path,
          };
        } else {
          const movie = this.metadataSvc.getMovieOptions().value.find(m => m.id === group.movie_id);
          if (movie) {
            this.lastMovieDetails = {
              id: movie.id,
              name: movie.name,
              production_year: movie.production_year ?? null,
              tmdb_id: movie.tmdb_id,
              tmdb_type: movie.tmdb_type,
              cover_url: movie.cover_url,
              cover_path: movie.cover_path,
            };
          }
        }
      }
      const ctx = this.workflowSvc.getCurrentContext();
      this.lastReleaseId = ctx?.labelForm?.release_id ?? group.release_id ?? this.lastReleaseId;
      this.lastReleaseSlug = ctx?.labelForm?.release_slug ?? group.disc_group ?? group.release_slug ?? this.lastReleaseSlug;
      this.lastReleaseDetails = group;
      this.backfillReleaseFields();
      this.onLabelChange();
      this.groupSearch = '';
      this.groupOpen = false;
      this._filteredGroupOptionsCache = null;
      this.cdr.markForCheck();
    };

    if (group.release_id && group.release_slug) {
      // Apply full selection through WorkflowService (updates context and persists via HTTP)
      const selection = {
        movieId: group.movie_id ?? null,
        releaseId: group.release_id,
        releaseSlug: group.release_slug,
        releaseName: group.release_name ?? null,
        releaseYear: group.release_year ?? null,
        boxsetId: group.boxset_id ?? null,
        boxsetSlug: group.boxset_slug ?? null,
        groupType: (group.group_type as 'movie' | 'series') ?? null,
      };
      this.workflowSvc.applyMetadataSelectionToActiveContext(selection).subscribe({
        next: () => setLocalDisplayFromGroup(),
        error: (err: any) => {
          this.logger.error('[Ripper] Failed to link release:', err);
          this.cdr.markForCheck();
        },
      });
    } else {
      // Fallback: update labelForm via WorkflowService (immutable)
      const updatedForm = this.metadataSvc.applyGroupSelection(context.labelForm, group);
      this.workflowSvc.updateContext({ labelForm: updatedForm });
      setLocalDisplayFromGroup();
    }
  }

  matchesGroupOption(group: any): boolean {
    if (!group || !this.labelForm) return false;
    const search = (this.groupSearch || '').toLowerCase();
    const slug = (group.disc_group || '').toLowerCase();
    const releaseSlug = (group.release_slug || '').toLowerCase();
    const name = (group.release_name || '').toLowerCase();
    const matchesSearch = !search || slug.includes(search) || releaseSlug.includes(search) || name.includes(search);
    const targetType = this.labelForm.group_type || null;
    const groupType = group.group_type || 'movie';
    const typeMatches = !targetType || groupType === targetType || (!group.group_type && targetType === 'movie');
    return matchesSearch && typeMatches;
  }

  filteredGroupOptions(): any[] {
    // Use service's filter method
    return this.metadataSvc.filterGroupOptions(this.groupSearch).slice(0, 50);
  }

  openCombobox(): void {
    this.groupOpen = !this.groupOpen;
    if (this.groupOpen) {
      this.groupSearch = '';
    } else {
      this.groupSearch = '';
    }
  }

  closeCombobox(): void {
    this.groupOpen = false;
    this.groupSearch = '';
    this._filteredGroupOptionsCache = null;
    this.cdr.markForCheck();
  }

  private formatYearLabel(original?: any, release?: any): string {
    const orig = original != null && `${original}`.trim() ? `${original}` : null;
    const rel = release != null && `${release}`.trim() ? `${release}` : null;
    if (orig && rel && orig !== rel) return `${orig} · Release ${rel}`;
    return orig || rel || '—';
  }

  yearLabelForSelection(): string {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return '—';
    const labelOrig = (context?.labelForm?.production_year as any) || null;
    const labelRel = (context?.labelForm?.release_year as any) || null;
    const lastOrig = this.lastReleaseDetails?.production_year || null;
    const lastRel = this.lastReleaseDetails?.release_year || null;
    const discOrig = (this.lastDiscInfo as any)?.production_year || null;
    const discRel = (this.lastDiscInfo as any)?.release_year || null;
    return this.formatYearLabel(labelOrig || lastOrig || discOrig, labelRel || lastRel || discRel);
  }

  optionYearLabel(group: any): string {
    if (!group) return '—';
    const matchesCurrent =
      !!this.labelForm &&
      ((group.release_id && this.labelForm.release_id === group.release_id) ||
        (group.release_slug && this.labelForm.release_slug === group.release_slug) ||
        (group.disc_group && this.labelForm.disc_group === group.disc_group));
    const baseOrig = group.production_year || null;
    const baseRel = group.release_year || null;
    const effectiveOrig = matchesCurrent && this.labelForm ? (this.labelForm.production_year as any) || baseOrig : baseOrig;
    const effectiveRel = matchesCurrent && this.labelForm ? (this.labelForm.release_year as any) || baseRel : baseRel;
    return this.formatYearLabel(effectiveOrig, effectiveRel);
  }

  coverImageForSelection(): string | null {
    if (!this.labelForm) return this.lastReleaseDetails?.cover_front_url || null;
    return this.labelForm.cover_front_url || this.lastReleaseDetails?.cover_front_url || null;
  }

  private backfillReleaseFields(): void {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    if (!context?.labelForm || !this.lastReleaseDetails) return;
    const formRid = context.labelForm.release_id ?? null;
    const lastRid = this.lastReleaseDetails.release_id ?? null;
    if (!formRid || !lastRid || String(formRid) !== String(lastRid)) {
      return;
    }

    // Build updates object for all fields that need backfilling
    const updates: any = {};
    if (!context.labelForm.release_name && this.lastReleaseDetails.release_name) {
      updates.release_name = this.lastReleaseDetails.release_name;
    }
    if (!context.labelForm.release_slug && this.lastReleaseDetails.release_slug) {
      updates.release_slug = this.lastReleaseDetails.release_slug;
    }
    if (!context.labelForm.disc_group && this.lastReleaseDetails.release_slug) {
      updates.disc_group = this.lastReleaseDetails.release_slug;
    }
    if (!context.labelForm.production_year && this.lastReleaseDetails.production_year) {
      updates.production_year = this.lastReleaseDetails.production_year;
    }
    if (!context.labelForm.release_year && this.lastReleaseDetails.release_year) {
      updates.release_year = this.lastReleaseDetails.release_year;
    }
    // Do not inject release TMDB when movie_id is set — movie_id is authoritative; stale tmdb_id breaks saves.
    if (!context.labelForm.tmdb_id && this.lastReleaseDetails.tmdb_id && !context.labelForm.movie_id) {
      updates.tmdb_id = this.lastReleaseDetails.tmdb_id;
    }
    if (!context.labelForm.upc && this.lastReleaseDetails.upc) {
      updates.upc = this.lastReleaseDetails.upc;
    }
    if (!context.labelForm.asin && this.lastReleaseDetails.asin) {
      updates.asin = this.lastReleaseDetails.asin;
    }
    if (!context.labelForm.cover_front_url && this.lastReleaseDetails.cover_front_url) {
      updates.cover_front_url = this.lastReleaseDetails.cover_front_url;
    }
    if (!context.labelForm.cover_back_url && this.lastReleaseDetails.cover_back_url) {
      updates.cover_back_url = this.lastReleaseDetails.cover_back_url;
    }
    
    // Update via WorkflowService if there are any updates
    if (Object.keys(updates).length > 0) {
      this.workflowSvc.updateContext({ 
        labelForm: { ...context.labelForm, ...updates }
      });
    }
  }

  clearReleaseSelection(): void {
    if (!this.labelForm) return;
    this.lastReleaseId = null;
    this.lastReleaseSlug = null;
    this.lastReleaseDetails = null;
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    // REMOVED: buildLabelForm - WorkflowService handles labelForm building
    // Just clear release_id from existing labelForm
    if (context?.labelForm) {
      this.workflowSvc.updateContext({ 
        labelForm: { ...context.labelForm, release_id: null }
      });
    }
    this.onLabelChange();
  }

  deleteReleaseOption(group: any): void {
    const id = group?.release_id || group?.release_slug || group?.disc_group || null;
    if (!id) return;
    this.metadataSvc.deleteRelease(id).subscribe({
      next: () => {
        this.toast.show('Release deleted', 'success');
        // Reload group options after deletion
        const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
        const movieId = context?.labelForm?.movie_id;
        this.metadataSvc.loadGroupOptions(movieId || undefined).subscribe({
          next: (options) => {
            // groupOptions now comes from MetadataService getter - no need to set local state
            this.cdr.markForCheck();
          },
        });
        if (this.labelForm && (this.labelForm.release_id === id || this.labelForm.release_slug === id || this.labelForm.disc_group === id)) {
          this.clearReleaseSelection();
        }
        this.cdr.markForCheck();
      },
      error: err => {
        const msg = err?.error?.detail || 'Failed to delete release';
        this.toast.show(msg, 'error');
      },
    });
  }

  private updateCurrentGroupOption(): void {
    if (!this.labelForm) return;
    // Use MetadataService to update current group option
    this.metadataSvc.updateCurrentGroupOption(this.labelForm);
    // groupOptions now comes from MetadataService getter - no need to set local state
  }

  heroTitle(info: DiscDetail | null): string {
    // Use movie_name and release_name from enriched discinfo payload
    const movieName = (info as any)?.movie_name || this.labelForm?.movie_name || this.lastMovieDetails?.name || null;
    const releaseName = (info as any)?.release_name || this.labelForm?.release_name || null;
    
    if (movieName) {
      let title = movieName;
      // Append release name with " - " separator if it exists, is not empty, and is different from movie name
      const trimmedReleaseName = releaseName?.trim();
      if (trimmedReleaseName && trimmedReleaseName !== movieName) {
        title = `${title} - ${trimmedReleaseName}`;
      }
      return title;
    }
    
    // Fallback to release name or disc info
    return (releaseName || info?.movie_name || '');
  }

  getMovieNameOnly(info: DiscDetail | null): string | null {
    // Prefer movie_name from disc info (most reliable source for current card)
    if ((info as any)?.movie_name) {
      return (info as any).movie_name;
    }
    
    // Only use labelForm.movie_name if labelForm belongs to the current card
    // Validate by checking if labelForm matches the selected card
    let labelFormMovieName: string | null = null;
    const selectedCard1 = this.workflowSvc.getSelectedCard();
    if (this.labelForm?.movie_name && selectedCard1) {
      // For job cards, check if labelForm.job_id matches selected card id
      if (selectedCard1.type === 'job') {
        const labelFormJobId = (this.labelForm as any)?.job_id;
        if (labelFormJobId === selectedCard1.id || !labelFormJobId) {
          // labelForm belongs to current card or doesn't have job_id (will be set)
          labelFormMovieName = this.labelForm.movie_name;
        }
      } else if (selectedCard1.type === 'drive') {
        // For drive cards, check if labelForm.disc_id or mount_point matches
        const labelFormDiscId = (this.labelForm as any)?.disc_id;
        const labelFormMountPoint = (this.labelForm as any)?.mount_point;
        if (labelFormDiscId || labelFormMountPoint === selectedCard1.id || !labelFormMountPoint) {
          // labelForm belongs to current card or doesn't have mount_point (will be set)
          labelFormMovieName = this.labelForm.movie_name;
        }
      }
    }
    
    if (labelFormMovieName) {
      return labelFormMovieName;
    }
    
    // Only use lastMovieDetails if it's not stale (no labelForm means we're between cards)
    // If we have a selected card but no labelForm yet, don't use lastMovieDetails
    if (this.lastMovieDetails?.name && (!selectedCard1 || this.labelForm)) {
      return this.lastMovieDetails.name;
    }
    
    // Fallback to selected movie from options (only if labelForm belongs to current card)
    if (this.labelForm?.movie_id && selectedCard1) {
      // Validate labelForm belongs to current card before using movie_id
      let canUseLabelForm = false;
      if (selectedCard1.type === 'job') {
        const labelFormJobId = (this.labelForm as any)?.job_id;
        canUseLabelForm = labelFormJobId === selectedCard1.id || !labelFormJobId;
      } else if (selectedCard1.type === 'drive') {
        const labelFormMountPoint = (this.labelForm as any)?.mount_point;
        canUseLabelForm = labelFormMountPoint === selectedCard1.id || !labelFormMountPoint;
      }
      
      if (canUseLabelForm) {
        const movieId = this.labelForm.movie_id;
        const movie = movieId ? this.movieOptions.find(m => m.id === movieId) : null;
        return movie?.name || null;
      }
    }
    
    return null;
  }
  
  heroTitleYear(info: DiscDetail | null): number | null {
    // Use movie_production_year or production_year from enriched discinfo payload
    const movieYear = (info as any)?.movie_production_year || (info as any)?.production_year;
    if (movieYear) return Number(movieYear);
    
    // Fallback to labelForm or lastMovieDetails
    const filmYear = this.labelForm?.production_year || this.lastMovieDetails?.production_year;
    if (filmYear) return filmYear;
    
    // Fallback to release year from info
    const rawYear = info ? (
      (info as any).release_year ??
      (info as any).year ??
      null
    ) : null;
    return rawYear ? Number(rawYear) : null;
  }
  
  // Film selection methods
  loadFilmOptions(): void {
    this.metadataSvc.getMovies().subscribe({
      next: (movies) => {
        // movieOptions now comes from MetadataService getter - no need to set local state
        this.cdr.markForCheck();
      },
      error: (err: any) => {
        this.logger.error('Failed to load films:', err);
      },
    });
  }
  
  get filteredMovieOptions(): MovieSummary[] {
    // Use service's filter method
    return this.metadataSvc.filterMovies(this.movieSearch, 50);
  }
  
  // Invalidate caches when options change
  private invalidateFilterCaches(): void {
    this._filteredBoxsetOptionsCache = null;
    this._filteredMovieOptionsCache = null;
    this._filteredGroupOptionsCache = null;
  }
  
  get selectedMovieName(): string {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return '';
    const movieId = context?.labelForm?.movie_id;
    if (!movieId) return '';
    const movie = this.metadataSvc.getMovieOptions().value.find(m => m.id === movieId);
    return movie?.name || '';
  }

  get selectedMovieYear(): string {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return '';
    const movieId = context?.labelForm?.movie_id;
    if (!movieId) return '';
    const movie = this.metadataSvc.getMovieOptions().value.find(m => m.id === movieId);
    return movie?.production_year ? String(movie.production_year) : '';
  }

  get selectedMovieCover(): string | null {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    const movieId = context?.labelForm?.movie_id;
    if (!movieId) return null;
    const movie = this.metadataSvc.getMovieOptions().value.find(m => m.id === movieId);
    return movie?.cover_url || movie?.cover_path || null;
  }
  
  onMovieSearchChange(search: string): void {
    this.movieSearch = search || '';
    this._filteredMovieOptionsCache = null;
    this.cdr.markForCheck();
  }
  
  // Stub methods for template compatibility (legacy section is hidden but still compiled)
  clearMovieSelection(): void {
    // Legacy method - functionality moved to WorkflowLabelingComponent
    const context = this.workflowSvc.getCurrentContext();
    if (!context || !context.labelForm) return;
    this.workflowSvc.applyMetadataSelectionToActiveContext({ movieId: null }).subscribe({
      error: (err) => this.logger.error('Failed to clear movie:', err)
    });
  }

  onSelectMovie(movie: any): void {
    // Legacy method - functionality moved to WorkflowLabelingComponent
    if (!movie?.id) return;
    this.workflowSvc.applyMetadataSelectionToActiveContext({ movieId: movie.id }).subscribe({
      error: (err) => this.logger.error('Failed to select movie:', err)
    });
  }

  onMovieLookup(): void {
    // Legacy method - functionality moved to WorkflowLabelingComponent
    if (!this.tmdbUrl?.trim()) return;
    this.workflowSvc.createAndLinkMovieToActiveContext({ tmdb_url: this.tmdbUrl.trim() }).subscribe({
      next: () => this.tmdbUrl = '',
      error: (err) => this.logger.error('Failed to lookup movie:', err)
    });
  }

  get workflowLabelForm$(): Observable<LabelForm | null> {
    // Legacy observable - use activeContext$ instead
    return this.activeContext$.pipe(map(context => context?.labelForm || null));
  }

  get postProcessFiles(): PostProcessFile[] {
    // Get from current context
    const context = this.workflowSvc.getCurrentContext();
    return context?.postProcessFiles || [];
  }

  heroCover(info: DiscDetail | null): string | null {
    // Prefer release front cover from labelForm
    if (this.labelForm?.cover_front_url) {
      return this.labelForm.cover_front_url;
    }
    // Use release_image or movie_cover_url from enriched discinfo payload
    if (info?.release_image) {
      return info.release_image;
    }
    const movieCover = (info as any)?.movie_cover_url || (info as any)?.movie_cover_path;
    if (movieCover) {
      return movieCover;
    }
    // Fallback to labelForm or lastMovieDetails for backwards compatibility
    return this.labelForm?.movie_cover_path || this.labelForm?.movie_cover_url || this.lastMovieDetails?.cover_path || this.lastMovieDetails?.cover_url || null;
  }

  heroResolution(info: DiscDetail | null): string | null {
    // Use release_resolution or resolution from enriched discinfo payload
    const releaseRes = (info as any)?.release_resolution;
    if (releaseRes) return releaseRes;
    
    const discRes = (info as any)?.resolution;
    if (discRes) return discRes;
    
    // Fallback: check lastReleaseDetails for backwards compatibility
    if (this.lastReleaseDetails?.resolution) return this.lastReleaseDetails.resolution;
    
    // Fallback: infer from disc_format
    const fmt = (info as any)?.disc_format || this.labelForm?.disc_format;
    if (fmt === 'UHD') return '2160p';
    if (fmt === 'Blu-Ray') return '1080p';
    if (fmt === 'DVD') return '480p';
    return null;
  }

  get discNumberLabel(): string | null {
    const num = this.labelForm?.disc_number;
    if (num == null) return '01';
    return num.toString().padStart(2, '0');
  }

  // mergeUserEdits removed - WorkflowService handles merging internally

  private releaseSlugFor(name: string, year?: any, discFormat?: string | null, resolution?: string | null): string {
    return '';
  }

  private hasRequiredReleaseFields(): boolean {
    if (!this.labelForm) return false;
    const prodYear = this.labelForm.production_year;
    const required = [
      prodYear,
      this.labelForm.release_year,
      this.labelForm.release_name,
      this.labelForm.release_slug,
    ];
    return required.every(v => v !== null && v !== undefined && `${v}`.trim().length > 0);
  }

  get labelCompletionPercent(): number {
    const progress = this.computeLabelProgress();
    this.labelProgress = progress;
    if (progress.total === 0) return 0;
    const result = Math.round((progress.filled / progress.total) * 100);
    return result;
  }

  private computeLabelProgress(): { filled: number; total: number; releaseFilled: number; releaseTotal: number; discFilled: number; discTotal: number; titleFilled: number; titleTotal: number } {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return { filled: 0, total: 0, releaseFilled: 0, releaseTotal: 0, discFilled: 0, discTotal: 0, titleFilled: 0, titleTotal: 0 };
    return this.workflowSvc.computeLabelProgress(context, this.tmdbUrl);
  }

  previewUrlForTitle(t: any): string | null {
    if (!t) return null;
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    const jobId = context?.jobStatus?.jobId || null;
    const previews = (context?.jobStatus as any)?.disc_payload?.previews;
    const key = this.previewTrackKey(t, previews);
    const manifest = key && previews?.tracks?.[key]?.manifest;
    // If status is completed, return URL
    if (jobId && manifest && previews?.tracks?.[key]?.status === 'completed') {
        // manifest already includes 'previews/' prefix, so use it directly
        const url = `${this.apiBase}/jobs/${encodeURIComponent(jobId)}/${manifest}`;
        return url;
    }
    
    // Fallback: if manifest exists but status is stale (queued/running), still return URL
    // This handles cases where status update failed but file exists
    if (jobId && manifest) {
        const trackStatus = previews?.tracks?.[key]?.status;
        // Only use fallback if status is queued/running (not failed/null)
        // This allows previews to show up even if status update is delayed
        if (trackStatus === 'queued' || trackStatus === 'running') {
          const url = `${this.apiBase}/jobs/${encodeURIComponent(jobId)}/${manifest}`;
          return url;
        }
    }
    return null;
  }

  titlePath(t: any): string | null {
    if (!t) return null;
    const preview = this.previewUrlForTitle(t);
    if (preview) return preview;
    const output = t.output_file || t.note || null;
    if (!output) return null;
    if (typeof output === 'string' && output.startsWith('http')) return output;
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    const base = (context?.jobStatus as any)?.job_dir ? `${(context?.jobStatus as any).job_dir}/raw` : null;
    if (typeof output === 'string' && (output.startsWith('/') || output.startsWith('\\'))) {
      return output;
    }
    if (base) {
      const cleanedBase = String(base).replace(/[\\/]+$/, '');
      const cleanedOut = String(output).replace(/^[/\\]+/, '');
      return `${cleanedBase}/${cleanedOut}`;
    }
    return output;
  }

  private stripBase(base: string, output: string): string {
    const normBase = String(base).replace(/[\\/]+$/, '');
    const normOut = String(output).replace(/^[/\\]+/, '');
    if (normOut.startsWith(normBase)) {
      return normOut.slice(normBase.length).replace(/^[/\\]+/, '');
    }
    return normOut;
  }

  titleKey(t: any): string | null {
    if (!t) return null;
    if (typeof t === 'string') return t;
    return t.title_id || null;
  }

  private titleFileKey(t: any): string | null {
    // Prefer explicit title id; fallback to output filename
    const id = this.titleKey(t);
    if (id) return String(id);
    const output = t?.output_file || t?.note || '';
    if (!output) return null;
    const parts = String(output).split(/[\\/]/);
    return parts[parts.length - 1] || null;
  }

  retryPreviewForTitle(t: any): void {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    const jobId = context?.jobStatus?.jobId;
    if (!jobId) return;
    const trackKey = this.titleKey(t);
    if (!trackKey) return;
    this.jobSvc.retryPreviewTrack(jobId, trackKey).subscribe({
      next: () => {
        // Preview will transition to queued state via next status update
      },
      error: (err: any) => {
        console.error('Failed to retry preview:', err);
      },
    });
  }

  titlePreviewState(t: any): { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null {
    const ctx10 = this.workflowSvc.getCurrentContext();
    if (!ctx10) return null;
    const previews = (ctx10?.jobStatus as any)?.disc_payload?.previews;
    if (!previews || !t) {
      return null;
    }
    const key = this.previewTrackKey(t, previews);
    if (!key || !previews.tracks || !previews.tracks[key]) {
      return null;
    }
    const info = previews.tracks[key];
    const jobId = ctx10?.jobStatus?.jobId || null;
    // If manifest exists but status is still queued/running, treat as completed for UI.
    const manifestReady = !!info?.manifest;
    const effectiveStatus =
      manifestReady && (info.status === 'queued' || info.status === 'running')
        ? 'completed'
        : info.status;
    // Use backend-provided error message, fallback to generic
    const error = info.status === 'failed' ? (info.error || 'Preview generation failed') : null;
    // Retryable flag from backend (default true for backward compat with old data)
    const retryable = info.status === 'failed' ? (info.retryable !== false) : undefined;
    // Build thumbnail URL if available
    let thumbnail: string | null = null;
    if (jobId && info.thumbnail) {
      thumbnail = `${this.apiBase}/jobs/${encodeURIComponent(jobId)}/${info.thumbnail}`;
    }
    return { status: effectiveStatus, error, retryable, thumbnail };
  }

  /**
   * Check if previews are still generating or have failed.
   * Returns: 'generating' | 'complete' | 'failed' | null (no previews)
   */
  private previewGenerationStatus(): 'generating' | 'complete' | 'failed' | null {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    const job = context?.jobStatus || null;
    if (!job) return null;
    const previews = (job as any)?.disc_payload?.previews;
    if (!previews || !previews.tracks) return null;
    
    const tracks = previews.tracks;
    const trackKeys = Object.keys(tracks);
    if (trackKeys.length === 0) return null;
    
    // Check overall preview status
    const overallStatus = previews.status;
    if (overallStatus === 'failed') return 'failed';
    if (overallStatus === 'completed') return 'complete';
    
    // Check individual track statuses
    let hasGenerating = false;
    let hasFailed = false;
    let hasComplete = false;
    
    for (const key of trackKeys) {
      const track = tracks[key];
      const status = track?.status;
      if (status === 'queued' || status === 'running') {
        hasGenerating = true;
      } else if (status === 'failed') {
        hasFailed = true;
      } else if (status === 'completed') {
        hasComplete = true;
      }
    }
    
    // If any are still generating, return generating
    if (hasGenerating) return 'generating';
    // If any failed and none are generating, return failed
    if (hasFailed && !hasGenerating) return 'failed';
    // If all are complete, return complete
    if (hasComplete && !hasGenerating && !hasFailed) return 'complete';
    
    // Default to generating if status is unclear
    return 'generating';
  }

  /**
   * Get preview error message if previews failed.
   * Backend now stores simplified error messages, so we just return the generic message.
   */
  private previewErrorMessage(): string | null {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    const job = context?.jobStatus || null;
    if (!job) return null;
    const previews = (job as any)?.disc_payload?.previews;
    if (!previews) return null;
    
    // Check overall error
    if (previews.status === 'failed') {
      return 'Failed to generate preview';
    }
    
    // Check for track errors
    const tracks = previews.tracks || {};
    for (const [key, track] of Object.entries(tracks)) {
      const trackInfo = track as any;
      if (trackInfo?.status === 'failed') {
        return 'Failed to generate preview';
      }
    }
    
    return null;
  }

  private previewTrackKey(t: any, previews: any): string | null {
    const titleId = t?.title_id ? String(t.title_id) : null;
    if (!titleId) return null;
    if (!previews || !previews.tracks) return titleId;
    const tracks = previews.tracks || {};
    if (tracks[titleId]) return titleId;

    const context = this.workflowSvc.getCurrentContext();
    const discPayload = (context?.jobStatus as any)?.disc_payload || {};
    const titleFilenameMap = discPayload?.title_filename_map || {};
    const titleOutputMap = discPayload?.title_output_map || {};

    const sourceCandidates = [
      t?.output_file,
      t?.source_file,
      t?.track_id,
      t?.file,
      t?.src,
      t?.note,
    ].filter(Boolean).map((val: any) => String(val));
    const normalize = (val: string) => val.split(/[\\/]/).pop() || val;
    const normalizedCandidates = sourceCandidates.map(normalize);
    for (const [key, entry] of Object.entries(tracks)) {
      const entryTitleId = (entry as any)?.title_id ? String((entry as any).title_id) : null;
      if (entryTitleId && entryTitleId === titleId) {
        return key;
      }
    }
    for (const [key, entry] of Object.entries(tracks)) {
      const entrySource = (entry as any)?.source_file || (entry as any)?.track_id;
      if (!entrySource) continue;
      const normalizedEntry = normalize(String(entrySource));
      if (sourceCandidates.includes(String(entrySource)) || normalizedCandidates.includes(normalizedEntry)) {
        if (!this.previewKeyMissLog.has(`${titleId}:${key}:source-file`)) {
          this.previewKeyMissLog.add(`${titleId}:${key}:source-file`);
        }
        return key;
      }
    }
    let candidateKey: string | null = null;
    for (const [key, entry] of Object.entries(tracks)) {
      const source = (entry as any)?.source ? String((entry as any).source) : null;
      const sourceFile = (entry as any)?.source_file ? String((entry as any).source_file) : null;
      const trackId = (entry as any)?.track_id ? String((entry as any).track_id) : null;
      if (!source) continue;
      if (
        sourceCandidates.includes(source) ||
        normalizedCandidates.includes(normalize(source)) ||
        (sourceFile && (sourceCandidates.includes(sourceFile) || normalizedCandidates.includes(normalize(sourceFile)))) ||
        (trackId && (sourceCandidates.includes(trackId) || normalizedCandidates.includes(normalize(trackId))))
      ) {
        candidateKey = key;
        break;
      }
    }

    if (!candidateKey && titleFilenameMap && typeof titleFilenameMap === 'object') {
      const filenameFromTitleId = titleFilenameMap[titleId] ? String(titleFilenameMap[titleId]) : null;
      // Prefer title_output_map if present (keys are track keys, values are rel paths)
      if (filenameFromTitleId && titleOutputMap && typeof titleOutputMap === 'object') {
        for (const [key, rel] of Object.entries(titleOutputMap)) {
          if (rel && normalize(String(rel)) === normalize(filenameFromTitleId)) {
            candidateKey = key;
            break;
          }
        }
      }
      // Fallback: match previews.tracks[].source to filename
      if (!candidateKey && filenameFromTitleId) {
        for (const [key, entry] of Object.entries(tracks)) {
          const source = (entry as any)?.source ? String((entry as any).source) : null;
          if (source && normalize(source) === normalize(filenameFromTitleId)) {
            candidateKey = key;
            break;
          }
        }
      }
      // If titleFilenameMap is keyed by trackKey -> filename, match by title source
      if (!candidateKey) {
        const sourceSet = new Set(sourceCandidates.map(normalize));
        for (const [key, filename] of Object.entries(titleFilenameMap)) {
          if (!filename) continue;
          if (sourceSet.has(normalize(String(filename)))) {
            candidateKey = String(key);
            break;
          }
        }
      }
      if (candidateKey) {
        const logKey = `${titleId}:${candidateKey}:filename`;
        if (!this.previewKeyMissLog.has(logKey)) {
          this.previewKeyMissLog.add(logKey);
        }
        return candidateKey;
      }
    }
    if (candidateKey) {
      const logKey = `${titleId}:${candidateKey}`;
      if (!this.previewKeyMissLog.has(logKey)) {
        this.previewKeyMissLog.add(logKey);
      }
    } else {
      const logKey = `${titleId}:none`;
      if (!this.previewKeyMissLog.has(logKey)) {
        this.previewKeyMissLog.add(logKey);
      }
    }

    return titleId;
  }

  hasLabelContent(): boolean {
    if (!this.labelForm) return false;
    const progress = this.computeLabelProgress();
    return progress.filled > 0;
  }

  get isCopyComplete(): boolean {
    return this.pipelineState('rip') === 'completed' && this.pipelineState('postprocess') === 'completed';
  }

  private guessDiscFormat(info: DiscDetail | null = null): 'Blu-Ray' | 'UHD' | 'DVD' {
    const fmt = (info as any)?.disc_format || (this.lastDiscInfo as any)?.disc_format;
    if (fmt === 'UHD' || fmt === 'Blu-Ray' || fmt === 'DVD') return fmt;
    const res = (info as any)?.resolution || this.lastDiscInfo?.resolution || '';
    if (typeof res === 'string' && res.includes('2160')) return 'UHD';
    if (typeof res === 'string' && res.includes('480')) return 'DVD';
    return 'Blu-Ray';
  }

  private updateLastReleaseDetailsFromOptions(): void {
    if (!this.lastReleaseSlug || !Array.isArray(this.groupOptions)) return;
    const match = this.groupOptions.find(g => g.disc_group === this.lastReleaseSlug);
    if (match) {
      this.lastReleaseDetails = match;
    }
  }

  get isSeriesLabel(): boolean {
    const mode = (this.labelForm?.group_type || this.labelForm?.mode || '').toLowerCase();
    return mode === 'series';
  }

  // applyLastReleaseDefaults removed - WorkflowService handles defaults internally

  private rememberManualReleaseFromForm(): void {
    if (!this.isDiscDbMissing || !this.labelForm) return;
    const slug = this.labelForm.release_slug || this.labelForm.disc_group || this.lastManualReleaseSlug || '';
    if (!slug) return;
    this.lastManualReleaseSlug = slug;
    this.lastManualReleaseDetails = {
      release_id: this.labelForm.release_id ?? null,
      disc_group: slug,
      group_type: this.labelForm.group_type || this.labelForm.mode || 'movie',
      release_name: this.labelForm.release_name || '',
      release_slug: slug,
      tmdb_id: this.labelForm.tmdb_id || '',
      upc: this.labelForm.upc || null,
      asin: this.labelForm.asin || null,
      cover_front_url: this.labelForm.cover_front_url || null,
      cover_back_url: this.labelForm.cover_back_url || null,
      release_year: this.labelForm.release_year ?? null,
      production_year: this.labelForm.production_year ?? null,
    };
  }

  private currentDiscHash(): string | null {
    const hash = (this.lastDiscInfo as any)?.disc_hash || (this.lastDiscInfo as any)?.content_hash || null;
    return hash;
  }

  private syncReleaseDetailsFromForm(): void {
    if (!this.labelForm) return;
    const slug = this.labelForm.release_slug || this.labelForm.disc_group || this.lastReleaseSlug || null;
    const name = this.labelForm.release_name || null; // Only use actual release name (edition), not slug
    const productionYearVal = this.labelForm.production_year ?? null;
    const releaseYear = this.labelForm.release_year ?? null;
    // Only set when we actually have a name/slug to display.
    if (!slug && !name) return;
    this.lastReleaseDetails = {
      release_id: this.labelForm.release_id ?? null,
      disc_group: slug || '',
      group_type: this.labelForm.group_type || this.labelForm.mode || 'movie',
      release_name: name || null, // Only use actual release name (edition), not slug
      release_slug: slug || '',
      tmdb_id: this.labelForm.tmdb_id || null,
      upc: this.labelForm.upc || null,
      asin: this.labelForm.asin || null,
      cover_front_url: this.labelForm.cover_front_url || null,
      cover_back_url: this.labelForm.cover_back_url || null,
      release_year: releaseYear,
      production_year: productionYearVal,
      resolution: this.labelForm.disc_format ? (this.labelForm.disc_format === 'UHD' ? '2160p' : this.labelForm.disc_format === 'Blu-Ray' ? '1080p' : this.labelForm.disc_format === 'DVD' ? '480p' : null) : null,
    };
  }

  private computeDiscDbState(info: DiscDetail | null, status: JobStatus | null): 'hit' | 'miss' | 'unknown' {
    return this.workflowSvc.computeDiscDbState(info, status);
  }

  private jobMatchesDisc(info: DiscDetail, status: JobStatus | null): boolean {
    return this.workflowSvc.jobMatchesDisc(info, status);
  }

  private initLabelFormForDisc(info: DiscDetail): void {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
    if (context?.labelForm) return;
    const sourceTitle = info?.movie_name || '';
    this.releaseNameHint = this.releaseNameHint || sourceTitle;
    this.releaseSlugHint = '';
    // If we have a previously labeled manual release, seed from it; otherwise keep names blank.
    const hasPriorRelease = !!(this.lastManualReleaseDetails || this.lastManualReleaseSlug);
    // Include movie_id from enriched discinfo if available to prevent TMDB URL from flashing
    const movieId = (info as any)?.movie_id || null;
    const movieName = (info as any)?.movie_name || null;
    const movieProductionYear = (info as any)?.movie_production_year || (info as any)?.production_year || null;
    // REMOVED: applyLastReleaseDefaults - WorkflowService handles defaults when building contexts
    // This method should trigger WorkflowService to build a new context from disc info
    // For now, create a minimal labelForm structure
    const newLabelForm: any = {
      mode: 'movie',
      tmdb_id: '',
      disc_format: null,
      release_name: '',
      release_slug: '',
      release_year: (info as any)?.release_year ?? null,
      production_year: movieProductionYear,
      movie_id: movieId,
      movie_name: movieName,
      upc: null,
      asin: null,
      cover_front_url: null,
      cover_back_url: null,
      disc_name: '',
      disc_slug: '',
      tracks: [],
      disc_number: null,
    };
    this.workflowSvc.updateContext({ labelForm: newLabelForm });
  }

  /** @deprecated - Removed: WorkflowService.fetchDiscWorkflowContext() handles hydration when building contexts */
  private loadDiscAndReleaseLabels(info: DiscDetail): void {
    // REMOVED: This method's functionality is now handled by WorkflowService.fetchDiscWorkflowContext()
    // which builds complete contexts including labelForm from backend data
    this.logger.debug('[Ripper] loadDiscAndReleaseLabels called - hydration handled by WorkflowService.fetchDiscWorkflowContext()');
    return;
  }

  private ensureLiveLabelForm(status: JobStatus | null): void {
    if (!status) return;
    const hasLabelPayload =
      !!(status.disc_payload?.label_payload || status.label_draft || status.disc_payload?.label_draft);
    // Skip loading labels for DiscDB hits
    const discInfoState = this.workflowSvc.getDiscInfoState();
    if (discInfoState.discDbState === 'hit') {
      return;
    }
    if (status.disc_id && !this.loadedLabelDiscIds.has(status.disc_id) && !hasLabelPayload) {
      this.loadedLabelDiscIds.add(status.disc_id);
      this.loadLabelFromRecords(status.disc_id, status.release_id || null);
    }
    // Prefer the latest saved label payload; hydrate it regardless of DiscDB state.
    const prefill =
      status.disc_payload?.label_payload ||
      status.label_draft ||
      status.disc_payload?.label_draft ||
      null;
    if (prefill) {
      const priorLocks = { discNameLocked: this.discNameLocked, discSlugLocked: this.discSlugLocked };
      const ctx1 = this.workflowSvc.getCurrentContext();
    if (!ctx1) return;
      const prevForm = ctx1?.labelForm || null;
      this.labelDraft = prefill;
      // REMOVED: buildLabelForm/mergeUserEdits - WorkflowService handles labelForm building when building contexts
      // The labelForm should come from WorkflowService when fetching context from backend
      // For now, just update the context with the prefill data directly (this is a temporary workaround)
      // TODO: Trigger WorkflowService to rebuild context from backend data
      if (prevForm) {
        // Merge manually for now - WorkflowService should handle this
        const merged = { ...prevForm, ...prefill };
        this.workflowSvc.updateContext({ labelForm: merged });
      } else {
        this.workflowSvc.updateContext({ labelForm: prefill as any });
      }
      const ctx2 = this.workflowSvc.getCurrentContext();
    if (!ctx2) return;
      this.logger.debug('[LabelHydrate] applying backend label payload', {
        disc_name: prefill.disc_name,
        disc_slug: prefill.disc_slug,
        labelFormDiscSlug: ctx2?.labelForm?.disc_slug,
      });
      this.discNameLocked = this.discNameLocked || !!prefill.disc_name || priorLocks.discNameLocked;
      this.discSlugLocked = this.discSlugLocked || !!prefill.disc_slug || priorLocks.discSlugLocked;
      this.lastReleaseId = ctx2?.labelForm?.release_id ?? this.lastReleaseId;
      this.backfillReleaseFields();
      this.validateLabelForm();
      return;
    }
    // Only auto-seed when DiscDB is missing and the user hasn't loaded/edited a form yet.
    if (!this.isDiscDbMissing) return;

    const perTitle = status.perTitleProgress || {};
    const keys = Object.keys(perTitle);
    const total = status.totalTitles || keys.length;
    if (!keys.length && !total) return;
    if (!this.prefillAllowed && this.prefillDecided) return;

    // Create a form if none exists yet.
    if (!this.labelForm) {
      const sourceTitle = this.lastDiscInfo?.movie_name || '';
      const formatGuess = (this.lastDiscInfo as any)?.disc_format || this.guessDiscFormat(this.lastDiscInfo);
      this.releaseNameHint = this.releaseNameHint || sourceTitle;
      this.releaseSlugHint = '';
      const context = this.workflowSvc.getCurrentContext();
    if (!context) return;
      // REMOVED: applyLastReleaseDefaults - WorkflowService handles defaults when building contexts
      // Create a minimal labelForm structure for now
      const newLabelForm: any = {
        mode: 'movie',
        tmdb_id: '',
        disc_format: formatGuess,
        release_name: '',
        release_slug: '',
        upc: null,
        asin: null,
        cover_front_url: null,
        cover_back_url: null,
        disc_name: '',
        disc_slug: '',
        tracks: [],
        disc_number: null,
      };
      this.workflowSvc.updateContext({ labelForm: newLabelForm });
    }

    const trackIds = keys.length
      ? keys
      : Array.from({ length: total }, (_v, i) => `title-${(i + 1).toString().padStart(2, '0')}`);
    if (!this.labelForm) return;
    const existingIds = new Set((this.labelForm.tracks || []).map((t: any) => this.titleKey(t)));
    for (const id of trackIds) {
      if (existingIds.has(id)) continue;
      this.labelForm.tracks.push({
        source_file: id,
        track_id: id,
        title_id: null,
        title: '',
        description: '',
        comment: null,
        season: null,
        episode: null,
        type: 'extra',
        duration: null,
        size: null,
        streams: null,
        content: true,
      });
    }
    // Removed - labelErrors doesn't exist in UIOrchestrationState
    // Label errors are managed in WorkflowContext
  }

  private loadLabelFromRecords(discId: string, releaseId: string | null = null): void {
    this.metadataSvc.getDiscRecord(discId).subscribe({
      next: disc => {
        // Update disc_number from backend if available
        if ((disc as any)?.disc_number !== undefined && (disc as any)?.disc_number !== null && this.labelForm) {
          this.labelForm.disc_number = (disc as any).disc_number;
        }
        const buildPayload = (rel: any | null): void => {
          const sourceTitles = (disc as any).titles && (disc as any).titles.length
            ? (disc as any).titles
            : [];
          const tracks = sourceTitles.map((t: any) => ({
            source_file: t.source_file || null,
            track_id: t.title_id ?? null,
            title_id: t.title_id ?? null,
            title: t.title || '',
            description: t.description || t.note || '',
            note: t.description || t.note || '',
            comment: t.comment ?? null,
            season: t.season ?? null,
            episode: t.episode ?? null,
            type: t.type || null,
            duration: t.duration || null,
            size: t.size || null,
            streams: t.streams || null,
            chapters: t.chapters ?? null,
            content: t.content !== false,
          }));
          const payload: any = {
            disc_id: disc.id,
            disc_slug: disc.disc_slug,
            disc_name: disc.disc_name,
            disc_number: disc.disc_number,
            disc_format: disc.format,
            info_title: (disc as any)?.info_title || (disc as any)?.info_label || null,
            titles: sourceTitles,
            tracks,
          };
          if (rel) {
            payload.release_id = (rel as any).id;
            payload.disc_group = rel.slug;
            payload.release_slug = rel.slug;
            // Don't autofill release_name - it's optional and user-entered
            // payload.release_name = rel.name || (rel as any)?.info_title || rel.slug;
            // Include movie_id from release so it's loaded into the form
            payload.movie_id = rel.movie_id || null;
            payload.tmdb_id = rel.tmdb_id;
            payload.upc = rel.upc;
            payload.asin = rel.asin;
            payload.cover_front_url = rel.cover_front_url;
            payload.cover_back_url = rel.cover_back_url;
            payload.release_year = (rel as any)?.release_year ?? null;
            payload.production_year = (rel as any)?.production_year ?? null;
            payload.production_year = (rel as any)?.production_year ?? payload.production_year ?? null;
            payload.info_title = payload.info_title || (rel as any)?.info_title || (rel as any)?.info_label || null;
            // Include boxset_id from release if linked to a boxset
            payload.boxset_id = (rel as any)?.boxset_id || null;
          }
          this.applyLabelPayload(payload, rel);
        };
        const relId = releaseId || (disc as any)?.release_id || null;
        if (relId) {
          this.metadataSvc.getReleaseRecord(relId).subscribe({
            next: rel => buildPayload(rel),
            error: () => buildPayload(null),
          });
        } else {
          buildPayload(null);
        }
      },
      error: () => {
        // best effort; ignore
      },
    });
  }

  private applyLabelPayload(payload: any, rel: any | null): void {
    if (!payload) return;
    // Don't create labelForm for DiscDB hits
    const discInfoState = this.workflowSvc.getDiscInfoState();
    if (discInfoState.discDbState === 'hit') {
      return;
    }
    const priorLocks = { discNameLocked: this.discNameLocked, discSlugLocked: this.discSlugLocked };
    const prevForm = this.labelForm;
    this.labelDraft = payload;
    // REMOVED: buildLabelForm/mergeUserEdits - WorkflowService handles labelForm building when building contexts
    // For now, merge manually (this is a temporary workaround)
    // TODO: Trigger WorkflowService to rebuild context from backend data
    const newLabelForm = prevForm ? { ...prevForm, ...payload } : payload;
    // Update WorkflowContext
    this.workflowSvc.updateContext({ labelForm: newLabelForm as any });
    // If boxset_id is set, load boxsets to ensure selectedBoxset is populated
    if (this.labelForm?.boxset_id) {
      this.loadBoxsets();
    }
    if (rel) {
      this.lastReleaseId = (rel as any)?.id || this.lastReleaseId;
      this.lastReleaseSlug = rel.slug || this.lastReleaseSlug;
      this.lastReleaseDetails = {
        release_id: (rel as any)?.id,
        disc_group: rel.slug,
        group_type: rel.type,
        release_name: rel.name || null, // Only use actual release name (edition), not slug
        release_slug: rel.slug,
        tmdb_id: rel.tmdb_id,
        upc: rel.upc,
        asin: rel.asin,
        cover_front_url: rel.cover_front_url,
        cover_back_url: rel.cover_back_url,
        release_year: (rel as any)?.release_year ?? null,
        production_year: (rel as any)?.production_year ?? null,
      };
      
      // Load movie details from release
      if (rel.movie) {
        const movie = rel.movie;
        this.lastMovieDetails = {
          id: movie.id,
          name: movie.name,
          production_year: movie.production_year,
          tmdb_id: movie.tmdb_id,
          tmdb_type: movie.tmdb_type,
          cover_url: movie.cover_url,
          cover_path: movie.cover_path,
        };
        // Set movie_id in labelForm so the movie is automatically selected
        if (this.labelForm) {
          this.labelForm.movie_id = movie.id;
          this.labelForm.movie_name = movie.name;
          this.labelForm.production_year = movie.production_year || this.labelForm.production_year;
          // Populate tmdbUrl from movie's tmdb_id if available
          if (movie.tmdb_id && movie.tmdb_type) {
            const tmdbType = movie.tmdb_type === 'tv' ? 'tv' : 'movie';
            this.tmdbUrl = `https://www.themoviedb.org/${tmdbType}/${movie.tmdb_id}`;
          }
        }
      } else if (rel.movie_id) {
        // Load movie by ID if not included in response
        this.lastMovieDetails = {
          id: rel.movie_id,
        };
        // Set movie_id in labelForm
        if (this.labelForm) {
          this.labelForm.movie_id = rel.movie_id;
          // Reload releases when movie_id changes to filter by the new movie
          this.loadGroupOptions();
        }
        // Optionally load full movie details
        const movie = this.metadataSvc.getMovieOptions().value.find(m => m.id === rel.movie_id);
        if (movie) {
          this.lastMovieDetails = {
            id: movie.id,
            name: movie.name,
            production_year: movie.production_year ?? null,
            tmdb_id: movie.tmdb_id,
            tmdb_type: movie.tmdb_type,
            cover_url: movie.cover_url,
            cover_path: movie.cover_path,
          };
          // Populate tmdbUrl from movie's tmdb_id if available
          if (movie.tmdb_id && movie.tmdb_type && this.labelForm) {
            const tmdbType = movie.tmdb_type === 'tv' ? 'tv' : 'movie';
            this.tmdbUrl = `https://www.themoviedb.org/${tmdbType}/${movie.tmdb_id}`;
          }
          if (this.labelForm) {
            this.labelForm.movie_name = movie.name;
            this.labelForm.production_year = movie.production_year ?? this.labelForm.production_year;
          }
          this.cdr.markForCheck();
        }
      }
    }
    // Do not set labelForm.release_id from lastReleaseId here: lastReleaseId may be from a
    // different movie (e.g. another job/disc). Only context or explicit user selection should
    // set release_id; otherwise we would send the wrong release to the backend and link the disc.
    this.backfillReleaseFields();
    this.validateLabelForm();
  }

  private validateLabelForm(): boolean {
    // No validation needed if there's no labelForm (e.g., DiscDB hits)
    if (!this.labelForm) {
      // Removed - labelErrors doesn't exist in UIOrchestrationState
    // Label errors are managed in WorkflowContext
      return true;
    }
    // No validation needed for DiscDB hits
    if (this.isDiscDbHit) {
      // Removed - labelErrors doesn't exist in UIOrchestrationState
    // Label errors are managed in WorkflowContext
      return true;
    }
    const errs: string[] = [];
    const f = this.labelForm;
    if (!f.movie_id) errs.push('Movie ID is required (lookup from TMDB URL)');
    if (!f.mode) errs.push('Mode is required');
    if (!f.disc_format) errs.push('Disc format is required');
    // Release name is optional (edition name)
    if (!f.release_slug) errs.push('Release slug is required');
    if (!f.disc_name) errs.push('Disc name is required');
    // disc_slug optional: backend slugifies from disc_name on save when left blank
    if (!f.disc_group) errs.push('Group slug is required');
    if (!f.group_type) errs.push('Group type is required');
    // Removed - labelErrors property - errors are managed in WorkflowContext
    // Store errors locally for validation display
    const labelErrors = errs;
    return errs.length === 0;
  }

  titleStatus(track: any): string {
    const id = this.titleKey(track);
    if (!id || !this.jobMatchesCurrentDisc) return 'pending';
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return 'pending';
    
    const jobStatus = context?.jobStatus;
    const jobStatusValue = jobStatus?.job_status;
    const ripProgress = jobStatus?.rip_progress ?? 0;
    const ripState = jobStatus?.rip_state;
    
    // If job is completed and rip is done, treat all titles as completed
    // This handles cases where perTitleProgress might be missing or incomplete
    if (jobStatusValue === 'completed' && (ripProgress >= 100 || ripState === 'completed')) {
      return 'completed';
    }
    
    // Derive status from progress values and currentTitleId
    const progressMap = jobStatus?.perTitleProgress || {};
    const currentTitleId = jobStatus?.currentTitleId;
    const progress = progressMap[id];
    
    // Debug logging for title ID matching issues
    if (progressMap && Object.keys(progressMap).length > 0 && progress === undefined) {
      this.logger.debug(`[RipperPage] Title ID mismatch: titleKey returned "${id}", available keys in perTitleProgress:`, Object.keys(progressMap));
    }
    
    // Check if completed (progress >= 100 or in completedTitleIds)
    if (this.completedTitleIds.has(id) || (typeof progress === 'number' && progress >= 100)) {
      return 'completed';
    }
    
    // Check if currently ripping
    if (currentTitleId === id && typeof progress === 'number' && progress > 0 && progress < 100) {
      return 'running';
    }
    
    // Default to pending
    return 'pending';
  }

  get showTitleStatus(): boolean {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return false;
    return this.jobMatchesCurrentDisc && context?.jobStatus?.job_status === 'running';
  }

  titleIsActive(track: any): boolean {
    const id = this.titleKey(track);
    if (!id || !this.jobMatchesCurrentDisc) return false;
    const active = this.currentJobStatus?.currentTitleId;
    return !!active && active === id;
  }

  titleProgressValue(track: any): number {
    const id = this.titleKey(track);
    if (!id || !this.jobMatchesCurrentDisc) return 0;
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return 0;
    const map = (context?.jobStatus as any)?.perTitleProgress || {};
    const pct = map[id];
    return typeof pct === 'number' ? pct : 0;
  }

  get stageSteps(): Array<{ key: StageKey; label: string }> {
    const steps: Array<{ key: StageKey; label: string }> = [
      { key: 'rip', label: this.discMode === 'rip' ? 'Archive' : 'Copy' },
    ];
    // Hide label stage for DiscDB hits
    if (!this.isDiscDbHit) {
      steps.push({ key: 'label', label: 'Label' });
    }
    steps.push({ key: 'postprocess', label: 'Post-Process' });
    if (this.showUploadStep) {
      steps.push({ key: 'upload', label: 'Upload' });
    }
    steps.push({ key: 'transfer', label: 'Transfer' });
    return steps;
  }

  private jobProfile(status: JobStatus | null): 'miss' | 'hit' {
    if (status?.stage_profile === 'hit') return 'hit';
    if (status?.stage_profile === 'miss') return 'miss';
    if (status?.pipeline?.['label'] === 'skipped' || status?.pipeline?.['finalize_release'] === 'skipped') return 'hit';
    if (status?.label_required === false) return 'hit';
    if (status?.label_required === true) return 'miss';
    return this.isDiscDbMissing ? 'miss' : 'hit';
  }

  pipelineState(key: StageKey): string {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return 'pending';
    const status = context?.jobStatus || null;
    const profile = this.jobProfile(status);
    if (key === 'label' && profile === 'hit') return 'completed';
    if (!status) return 'pending';
    const pipe = status?.pipeline;
    const fromStages = (k: StageKey): string | null => {
      if (k === 'rip') return status?.rip_state || status?.job_status || null;
      if (k === 'postprocess') return status?.post_state || null;
      if (k === 'label') {
        return status?.label_state || (status?.disc_payload as any)?.label_state || null;
      }
      if (k === 'transfer' || k === 'upload') return status?.transfer_state || null;
      return null;
    };
    let val = (pipe && pipe[key]) || fromStages(key);
    if (key === 'label' && profile === 'miss' && this.labelCompletionPercent >= 100) {
      val = 'completed';
    }
    if (!val && key === 'transfer' && status?.job_status === 'completed') return 'completed';
    // If postprocess state is missing but we're past the postprocess phase, it must be completed
    if (!val && key === 'postprocess') {
      const ripState = status?.rip_state || status?.job_status || null;
      // If rip is completed and we're in transfer phase or later, postprocess must be completed
      if (ripState === 'completed' && (status?.phase === 'transfer' || status?.phase === 'complete' || status?.transfer_state || status?.job_status === 'completed')) {
        return 'completed';
      }
    }
    const result = val || 'pending';
    return result;
  }

  dotColor(key: StageKey): string {
    const state = this.pipelineState(key);
    if (state === 'completed') return '#10b981';
    if (state === 'running') return '#3b82f6';
    if (state === 'ready') return '#f59e0b';
    if (state === 'failed') return '#ef4444';
    return '#9ca3af';
  }

  get isValidating(): boolean {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return false;
    return context?.jobStatus?.job_status === 'validating';
  }

  get isPostProcessing(): boolean {
    const post = this.pipelineState('postprocess');
    const rip = this.pipelineState('rip');
    return post === 'running' && rip === 'completed';
  }

  get activeStage(): StageKey | null {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    const transferRunning = this.pipelineState('transfer') === 'running' || context?.jobStatus?.transfer_state === 'running';
    if (transferRunning || context?.jobStatus?.transfer_state === 'running') return 'transfer';
    // Check for post-processing: either validating, post_state is running, or we have post_progress
    // BUT exclude failed state - if post-processing failed, don't treat it as still processing
    const postState = this.pipelineState('postprocess');
    const ripState = this.pipelineState('rip');
    const ripDone = ripState === 'completed';
    const hasPostProgress = typeof context?.jobStatus?.post_progress === 'number' && context.jobStatus.post_progress >= 0;
    const isPostProcessing = postState !== 'failed' && (
                             this.isValidating || 
                             this.isPostProcessing || 
                             (postState === 'running' && ripDone) ||
                             (hasPostProgress && ripDone && (postState === 'running' || this.currentJobStatus?.job_status === 'running')));
    if (isPostProcessing) return 'postprocess';
    const labelState = this.pipelineState('label');
    const ripRunning =
      ripState === 'running' ||
      (!ripDone && (this.currentJobStatus?.job_status === 'running' || this.currentJobStatus?.job_status === 'pending'));
    const labelVisible = this.isDiscDbMissing && ripDone;
    if (labelVisible && (labelState === 'running' || labelState === 'pending')) return 'label';
    if (ripRunning) return 'rip';
    return null;
  }

  private stageLabel(stage: StageKey): string {
    if (stage === 'rip') return this.discMode === 'rip' ? 'Archiving…' : 'Copying…';
    if (stage === 'label') return 'Labeling…';
    if (stage === 'postprocess') return 'Post-processing…';
    return 'Transferring…';
  }

  private stageProgress(stage: StageKey): number | null {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    const status = context?.jobStatus || null;
    const clamp = (v: number) => Math.max(0, Math.min(100, v));
    const roundPct = (v: number) => Math.round((v + Number.EPSILON) * 100) / 100;
    if (stage === 'rip') {
      const ripState = this.pipelineState('rip');
      if (ripState === 'completed') return 100;
      // If rip failed, clear the progress bar
      if (ripState === 'failed') return null;
      const context = this.workflowSvc.getCurrentContext();
    if (!context) return 0;
      const ripPct = Math.max(context?.jobStatus?.rip_progress || 0, status?.rip_progress || 0);
      if (ripPct > 0) return clamp(roundPct(ripPct));
      return 0;
    }
    if (stage === 'label') {
      const state = this.pipelineState('label');
      const pct = this.labelCompletionPercent;
      // CRITICAL: Only return 100% if BOTH state is completed AND progress is actually 100%
      // This prevents showing 100% when backend incorrectly sets label_state='completed' 
      // but titles aren't actually filled (database is source of truth for progress)
      if (state === 'completed' && pct >= 100) return 100;
      // Always use actual progress calculation (database is source of truth)
      return clamp(roundPct(pct));
    }
    if (stage === 'postprocess') {
      if (this.isDiscDbMissing && this.pipelineState('label') !== 'completed') {
        return 0;
      }
      const state = this.pipelineState('postprocess');
      // Only show 100% when post-processing is completed
      if (state === 'completed') {
        return 100;
      }
      
      // When ready (not started yet), show 0%
      if (state === 'ready') {
        return 0;
      }
      
      // If post-processing failed, don't show 100% progress - return null or 0 to indicate failure
      if (state === 'failed') {
        return null;
      }
      // Only show progress when post-processing is actually active (running or validating)
      // Don't show progress when state is pending/ready - this prevents flashing
      const isPostProcessingActive = state === 'running' || status?.job_status === 'validating' || status?.phase === 'postprocess';
      if (!isPostProcessingActive) {
        return null;
      }
      // Use post_progress from job status (primary source)
      // If post_progress is 0 and we have a cached value, use the cache to prevent flashing
      // This handles cases where status updates arrive with post_progress=0 (default) but post-processing is still active
      if (typeof status?.post_progress === 'number' && status.post_progress >= 0) {
        // If post_progress is 0 and we have a cached value > 0, use the cache
        // This prevents flashing to 0% when status updates arrive without progress data
        if (status.post_progress === 0 && this.lastPostProgress !== null && this.lastPostProgress > 0) {
          return clamp(roundPct(this.lastPostProgress));
        }
        // Always return the progress value if we have it and post-processing is active
        return clamp(roundPct(status.post_progress));
      }
      // Fallback to disc_payload for backward compatibility
      // But don't show progress if post-processing failed
      if (state !== 'failed') {
        const postPayload: any = (status as any)?.disc_payload || {};
        const pctFromPayload =
          typeof postPayload?.post_progress === 'number'
            ? clamp(roundPct(postPayload.post_progress))
            : (typeof postPayload?.post_done === 'number' && typeof postPayload?.post_total === 'number' && postPayload.post_total > 0)
              ? clamp(roundPct((postPayload.post_done * 100) / postPayload.post_total))
              : null;
        if (pctFromPayload !== null) {
          if (pctFromPayload >= 100) return 100;
          // Return progress even if it's 0 (valid progress data)
          return pctFromPayload;
        }
      }
      // If we're in post-processing (running or validating), use cached progress if available
      // This prevents progress from bouncing when status updates arrive without progress data
      if (isPostProcessingActive) {
        // Use cached progress if current status doesn't have it
        if (this.lastPostProgress !== null) {
          return clamp(roundPct(this.lastPostProgress));
        }
        return 0;
      }
      return 0;
    }
    // transfer
    const transferState = this.pipelineState('transfer');
    if (transferState === 'completed') return 100;
    // If transfer failed, clear the progress bar
    if (transferState === 'failed') return null;
    const tp = status?.transfer_progress;
    if (tp != null && tp >= 0) return clamp(tp);
    return transferState === 'running' ? 0 : 0;
  }

  get ctaProgressLabel(): string | null {
    const stage = this.activeStage;
    if (!stage) return null;
    const pct = this.stageProgress(stage);
    const label = this.stageLabel(stage);
    return typeof pct === 'number' && pct > 0 ? `${label} ${pct}%` : label;
  }

  get activeStageLabel(): string {
    const stage = this.activeStage;
    return stage ? this.stageLabel(stage) : '';
  }

  get currentJobIdForDisplay(): string | null {
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return null;
    return context?.jobStatus?.jobId || null;
  }

  /** Get disc name for a drive card display */
  getDriveDiscName(drive: Drive): string {
    const cachedInfo = this.workflowSvc.getCachedDiscInfo(drive.disc_num);
    let discInfo: DiscDetail | null = cachedInfo || null;
    
    // If this is the currently selected drive, prefer lastDiscInfo for more up-to-date data
    // and also get labelForm and selectedBoxset for enriched information
    const selectedCard = this.workflowSvc.getSelectedCard();
    const discInfoState = this.workflowSvc.getDiscInfoState();
    if (selectedCard?.type === 'drive' && selectedCard.id === drive.mount_point && discInfoState.lastDiscInfo) {
      discInfo = discInfoState.lastDiscInfo;
      // Use labelForm and selectedBoxset if available for this drive
      return this.getDiscDisplayName(discInfo, this.labelForm, this.selectedBoxset);
    }
    
    if (!discInfo) {
      return 'No Disc';
    }
    
    // Use the standardized disc name helper (without labelForm/boxset for non-selected drives)
    return this.getDiscDisplayName(discInfo);
  }

  /** Get info_title for a drive card, or "Unknown Disc" if not available */
  getDriveInfoTitle(drive: Drive): string {
    const cachedInfo = this.workflowSvc.getCachedDiscInfo(drive.disc_num);
    let discInfo: DiscDetail | null = cachedInfo || null;
    
    // If this is the currently selected drive, prefer lastDiscInfo for more up-to-date data
    const selectedCard = this.workflowSvc.getSelectedCard();
    if ((selectedCard?.type === 'drive' && selectedCard.id === drive.mount_point) && this.lastDiscInfo) {
      discInfo = this.lastDiscInfo;
    }
    
    if (!discInfo) {
      return 'Unknown Disc';
    }
    
    const infoTitle = (discInfo as any)?.info_title || null;
    return infoTitle || 'Unknown Disc';
  }

  // New unified helpers for DiscMetadata
  // getDiscTitle and getDiscMeta moved to CardCarouselComponent

  // REMOVED: getDriveMeta() - replaced by getDiscMeta() in CardCarouselComponent for unified DiscMetadata

  /** Get standardized disc display name according to TODO.md priority:
   * 1. If movie|series name & boxset name present - <Movie Name> (<Production Year>) - <Boxset Name>
   * 2. If movie|series name present - <Movie Name> (<Production Year>)
   * 3. If disc info_title present - <Info_Title>
   * 4. "Unknown Disc" if nothing else available
   */
  getDiscDisplayName(discInfo: DiscDetail | null, labelForm?: any, selectedBoxset?: BoxsetSummary | null): string {
    if (!discInfo) {
      return 'Unknown Disc';
    }
    
    // Get movie/series name from discInfo or labelForm
    const movieName = discInfo.movie_name || 
                     (discInfo as any)?.release_name ||
                     labelForm?.movie_name ||
                     labelForm?.release_name ||
                     null;
    
    // Get production year from movie information (priority), then labelForm, then discInfo
    let productionYear: number | string | null = null;
    
    // Priority 1: Look up from movie options if we have a movie_id
    if (labelForm?.movie_id) {
      const movie = this.movieOptions.find(m => m.id === labelForm.movie_id);
      productionYear = movie?.production_year || null;
    }
    
    // Priority 2: Check if discInfo has movie_id and look it up
    if (!productionYear && (discInfo as any)?.movie_id) {
      const movie = this.movieOptions.find(m => m.id === (discInfo as any).movie_id);
      productionYear = movie?.production_year || null;
    }
    
    // Priority 3: Check labelForm production_year
    if (!productionYear && labelForm?.production_year) {
      productionYear = labelForm.production_year;
    }
    
    // Priority 4: Check discInfo for movie_production_year
    if (!productionYear && (discInfo as any)?.movie_production_year) {
      productionYear = (discInfo as any).movie_production_year;
    }
    
    // Priority 5: Check discInfo for production_year (if it exists)
    if (!productionYear && (discInfo as any)?.production_year) {
      productionYear = (discInfo as any).production_year;
    }
    
    // Get boxset name
    const boxsetName = selectedBoxset?.name || 
                      selectedBoxset?.title ||
                      (labelForm?.boxset_id ? this.selectedBoxset?.name || this.selectedBoxset?.title : null) ||
                      null;
    
    // Get info_title
    const infoTitle = (discInfo as any)?.info_title ||
                     labelForm?.info_title ||
                     null;
    
    // Priority 1: Movie/Series name + Boxset name
    if (movieName && boxsetName) {
      const yearPart = productionYear ? ` (${productionYear})` : '';
      return `${movieName}${yearPart} - ${boxsetName}`;
    }
    
    // Priority 2: Movie/Series name only
    if (movieName) {
      const yearPart = productionYear ? ` (${productionYear})` : '';
      return `${movieName}${yearPart}`;
    }
    
    // Priority 3: Info title
    if (infoTitle) {
      return infoTitle;
    }
    
    // Priority 4: Fallback
    return 'Unknown Disc';
  }

  /** Check if a drive is currently loading disc info */
  isDriveLoading(drive: Drive): boolean {
    return this.workflowSvc.getUIOrchestrationState().driveLoadingStates.get(drive.mount_point) || false;
  }


  /** Handle selection of an unfinished job (always fetches from HTTP) */
  onSelectUnfinishedJob(jobId: string): void {
    // Use setContextByCard to fetch context from HTTP (no caching)
    this.workflowSvc.setContextByCard({ type: 'job', id: jobId }).subscribe({
      next: (context) => {
        // Context loaded and set as active
        const cachedJob = this.workflowSvc.getCachedJobData(jobId);
        if (cachedJob) {
          this.loadWorkflowFromJob(cachedJob);
        }
      },
      error: (err: any) => {
        this.logger.error('[RipperPage] Failed to load unfinished job', err);
        this.workflowSvc.clearCardSelection(); // Clear selection on error
      }
    });
  }

  /** Close workflow modal / clear job selection */
  closeWorkflowModal(): void {
    // Clear job selection if a job is selected
    if (this.workflowSvc.getSelectedCard()?.type === 'job') {
      this.workflowSvc.clearCardSelection();
    }
    this.selectedTitleForModal = null;
  }

  /** Handle title click for mobile */
  onTitleClick(title: any): void {
    if (this.mobileService.isMobile) {
      this.selectedTitleForModal = title;
    }
  }

  /** Close title modal */
  closeTitleModal(): void {
    this.selectedTitleForModal = null;
  }

  /** Handle boxset creation from workflow */
  // REMOVED: createBoxsetFromWorkflow(), createReleaseFromWorkflow() - moved to WorkflowLabelingComponent
  // WorkflowLabelingComponent has its own implementations that use WorkflowService.createAndLink* methods

  /** Load unfinished jobs and pre-load full job data for instant display */
  // loadUnfinishedJobs() removed - now handled by WorkflowService

  // REMOVED: onWorkflowStepNavigate() - moved to WorkflowService.navigateToStep()
  // WorkflowService now handles workflow step navigation and persistence

  // updatePostProcessFiles() removed - use WorkflowContext.postProcessFiles


  get transferDestination(): any {
    // Get transfer destination configuration
    if (!this.settings) return null;
    
    return {
      path: this.settings.transferFolder,
      mode: this.settings.transferMode,
      // Add rsync config if available
      ...(this.settings.transferMode === 'rsync' ? {
        host: (this.settings as any).rsyncConfig?.host,
        user: (this.settings as any).rsyncConfig?.user,
        port: (this.settings as any).rsyncConfig?.port,
      } : {}),
    };
  }

  get releaseDiscs(): any[] {
    // First check if we have cached release discs from workflow context
    if ((this as any).cachedReleaseDiscs && Array.isArray((this as any).cachedReleaseDiscs)) {
      return (this as any).cachedReleaseDiscs;
    }
    
    // Fallback: Get all discs in current release
    const context = this.workflowSvc.getCurrentContext();
    if (!context) return [];
    if (!context?.jobStatus?.release_id) return [];
    
    // This would need to be fetched from the release service
    // For now, return empty array
    // TODO: Implement disc fetching from release service
    return [];
  }

  get boxsetMovies(): any[] {
    // First check if we have cached boxset movies from workflow context
    if ((this as any).cachedBoxsetMovies && Array.isArray((this as any).cachedBoxsetMovies)) {
      return (this as any).cachedBoxsetMovies;
    }
    
    // Fallback: Get all movies in current boxset
    if (!this.labelForm?.boxset_id) return [];
    
    // Find the boxset and return its movies
    const boxsetId = this.labelForm?.boxset_id;
    const boxset = boxsetId ? this.boxsetOptions.find(b => b.id === boxsetId) : null;
    if (!boxset) return [];
    
    // This would need to be fetched from the boxset service
    // For now, return empty array
    // TODO: Implement movie fetching from boxset service
    return [];
  }
  
  ngOnDestroy(): void {
    // Clean up manual subscriptions and intervals
    this.timeoutSub?.unsubscribe();
    this.timeoutSub = null;
    
    // WebSocket updates from WorkflowService handle job status updates
    
    // WebSocket updates from WorkflowService handle transfer status updates
    
    // Clear caches to free memory
    // Cache clearing handled by WorkflowService
    this.completedTitleIds.clear();
    this.workflowSvc.updateUIOrchestrationState({ driveLoadingStates: new Map<string, boolean>() });
  }
}
