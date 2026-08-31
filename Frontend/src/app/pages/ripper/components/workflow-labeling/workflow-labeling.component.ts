import { Component, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { trigger, transition, style, animate } from '@angular/animations';
import { combineLatest, Observable, Subscription, of, timer } from 'rxjs';
import { map, switchMap, takeUntil, filter, distinctUntilChanged, withLatestFrom, take, debounceTime, tap, catchError } from 'rxjs/operators';
import { merge } from 'rxjs';
import { Subject } from 'rxjs';
import {
  WorkflowService,
  WorkflowContext,
  TitlePatchRequest,
  RenamePreviewEntry,
  RenameSummary,
  renameSummary,
} from '../../../../services/workflow.service';
// RipperStateService removed - using WorkflowService
import { MetadataService, BoxsetSummary, MovieRecord, MovieSummary, TmdbSearchCandidate, MovieCreate } from '../../../../services/metadata.service';
import { TmdbSuggestionInfo } from '../../../../services/drive.service';
import { LoggerService } from '../../../../services/logger.service';
import { MovieSelectorComponent } from '../../../../components/movie-selector/movie-selector.component';
import { BoxsetSelectorComponent } from '../../../../components/boxset-selector/boxset-selector.component';
import { ReleaseSelectorComponent } from '../../../../components/release-selector/release-selector.component';
import { DiscLabelComponent } from '../../../../components/disc-label/disc-label.component';
import { TitleLabelComponent } from '../../../../components/title-label/title-label.component';
import { WorkflowBreadcrumbComponent } from '../../../../components/workflow-breadcrumb/workflow-breadcrumb.component';
import { PathAWorkspaceComponent } from '../path-a-workspace/path-a-workspace.component';
import { WorkflowStep } from '../../../../services/workflow.service';
import { getStepOrderForContext } from '../../../../services/workflow-step-order.util';
import { MobileService } from '../../../../services/mobile.service';
import { ReleaseSummary } from '../../../../services/metadata.service';
import { JobService, PostProcessFile, PostProcessStatus } from '../../../../services/job.service';
import { ToastService, formatHttpErrorDetail } from '../../../../services/toast.service';
import { isReleaseSufficientlyComplete } from '../../services/label-form.service';
import {
  areLabelTitlesComplete,
  computeTitleLabelStats,
  sortTitleStatsTypeEntries,
  type TitleLabelStats,
} from '../../../../utils/title-label-stats.util';

@Component({
  selector: 'app-workflow-labeling',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MovieSelectorComponent,
    BoxsetSelectorComponent,
    ReleaseSelectorComponent,
    DiscLabelComponent,
    TitleLabelComponent,
    WorkflowBreadcrumbComponent,
    PathAWorkspaceComponent,
  ],
  templateUrl: './workflow-labeling.component.html',
  styleUrls: ['./workflow-labeling.component.scss'],
  animations: [
    trigger('slideAnimation', [
      transition('* => *', [
        style({ transform: 'translateX(100%)', opacity: 0 }),
        animate('300ms ease-in-out', style({ transform: 'translateX(0)', opacity: 1 }))
      ])
    ])
  ]
})
export class WorkflowLabelingComponent implements OnInit, OnDestroy {
  private subscriptions = new Subscription();
  private destroy$ = new Subject<void>();
  /** #695: debounced latest-wins label-save channel (wired in ngOnInit). */
  private labelSaveQueue$ = new Subject<void>();
  private static readonly LABEL_SAVE_DEBOUNCE_MS = 400;
  /** Triggers refetch of release options after creating a release so the new release appears without refresh. */
  private refreshReleaseOptions$ = new Subject<void>();
  private titleLengthByKey = new Map<string, number>();
  private autoAdvanceOnSelection = false;

  // State from services (initialized in constructor)
  context$!: Observable<WorkflowContext | null>;
  movieOptions$!: Observable<any[]>;
  boxsetOptions$!: Observable<any[]>;
  releaseOptions$!: Observable<ReleaseSummary[]>;
  groupOptions$!: Observable<any[]>;
  isWorkflowReady$!: Observable<boolean>;
  buttonSpinnerOverride$!: Observable<boolean>;
  /** Precomputed from context to avoid calling getOrderedReleaseDiscs on every CD. */
  orderedReleaseDiscs$!: Observable<any[]>;
  /** Precomputed from context to avoid calling getFilesFromTree(getCurrentDiscFolderTree()) on every CD. */
  filesFromCurrentDiscTree$!: Observable<Array<{ file: PostProcessFile | null; indent: number; folderPath: string; isFolder: boolean; folderName?: string }>>;

  // Step management
  // #365 Phase 2 § 6.4 — 'postprocess' removed (collapsed into transfer).
  steps: WorkflowStep[] = ['film', 'exploratory_rip', 'boxset', 'disc', 'titles', 'summary', 'transfer'];
  currentStep: WorkflowStep = 'film';
  currentStepIndex: number = 0;
  isMobile: boolean = false;

  // Re-rename state (#329 + #325): preview + apply renames on transfer step
  renamePreview: RenamePreviewEntry[] | null = null;
  renameLoading = false;
  renameError: string | null = null;
  renameExecuted = false;

  /** Summary header for the rename preview/result table (#325). */
  get renameSummary(): RenameSummary {
    return renameSummary(this.renamePreview);
  }

  // Local UI state for dropdowns/search
  movieSearch: string = '';
  boxsetSearch: string = '';
  releaseSearch: string = '';
  movieComboOpen: boolean = false;
  tmdbDropdownOpen: boolean = false;
  boxsetOpen: boolean = false;
  tmdbUrl: string = '';
  filmLookupLoading: boolean = false;
  filmLookupError: string | null = null;
  /** True while applying movie/boxset/release selection or create+link to context. */
  metadataSaving: boolean = false;

  // TMDB suggestion + override-search UI state (#389).
  // tmdbSuggestionMode toggles between showing the auto-suggestion card and
  // showing the override controls (URL paste + free-text search box). The
  // card defaults to 'suggestion'; the user clicks Change to flip to
  // 'override'. The mode is reset to 'suggestion' when the user dismisses
  // the override or when the active context changes.
  tmdbSuggestionMode: 'suggestion' | 'override' = 'suggestion';
  // The search query string itself is now owned by <app-movie-selector>'s
  // internal searchTerm — the selector emits (tmdbSearchRequested) with the
  // final string when the user clicks the "Search TMDB for ‹q›" CTA.
  tmdbSearchResults: TmdbSearchCandidate[] | null = null;
  tmdbSearchLoading: boolean = false;
  tmdbSearchError: string | null = null;
  /** Score above which the suggestion card omits the low-confidence warning.
   *  Kept in sync with the backend TMDB_LABEL_DRAFT_SEED_THRESHOLD (0.75) plus
   *  a bit of headroom — below 0.85 we warn the user to verify. */
  readonly TMDB_HIGH_CONFIDENCE_THRESHOLD = 0.85;

  // Boxset creation modal state
  showBoxsetCreateModal: boolean = false;
  newBoxset: { name: string; year: number | null; upc?: string; asin?: string; cover_front_url?: string; cover_back_url?: string } = { name: '', year: null };
  
  // Release creation modal state
  showReleaseCreateModal: boolean = false;
  /** #685: outcome of the last create-release call, consumed by the selector so
   *  the create form survives failures with the user's input intact. */
  releaseCreateResult: { ok: boolean; error?: string; token: number } | null = null;
  private releaseCreateToken = 0;
  newRelease: { name: string; release_year: number | null; upc?: string; asin?: string; cover_front_url?: string; cover_back_url?: string } = { name: '', release_year: null };
  releaseValidationErrors: { [key: string]: boolean } = {};
  
  // Post-process UI state
  expandedPostprocessDiscId: string | null = null;
  // Transfer step UI state (expandable job row)
  expandedTransferDiscId: string | null = null;

  // Preview regeneration state
  regeneratingPreviews = false;
  rescanningTitles = false;

  // ── Cached stable references for title-label inputs ──
  // Avoids creating new function/object refs on every change detection cycle,
  // which would defeat OnPush in TitleLabelComponent and cause excessive re-renders.
  private readonly defaultTitleStatusFn: (id: string | null | undefined) => string = () => 'pending';
  private readonly defaultTitleProgressValueFn: (id: string | null | undefined) => number = () => 0;
  private readonly defaultTitleActiveFn: (id: string | null | undefined) => boolean = () => false;
  private readonly defaultPreviewUrlFn: (t: any) => string | null = () => null;
  private readonly defaultPreviewStateFn: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null = () => null;
  private readonly defaultTitlePathFn: (t: any) => string | null = () => null;

  cachedTitleStatusFn: (id: string | null | undefined) => string = this.defaultTitleStatusFn;
  cachedTitleProgressValueFn: (id: string | null | undefined) => number = this.defaultTitleProgressValueFn;
  cachedTitleActiveFn: (id: string | null | undefined) => boolean = this.defaultTitleActiveFn;
  cachedPreviewUrlFn: (t: any) => string | null = this.defaultPreviewUrlFn;
  cachedPreviewStateFn: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null = this.defaultPreviewStateFn;
  cachedRetryPreviewFn: (t: any) => void = () => {};
  cachedTitlePathFn: (t: any) => string | null = this.defaultTitlePathFn;
  cachedTitleProgress: Record<string, number> = {};

  constructor(
    public workflowService: WorkflowService,
    // ripperStateService removed - using workflowService
    private metadataService: MetadataService,
    private mobileService: MobileService,
    private jobService: JobService,
    private logger: LoggerService,
    private toastService: ToastService
  ) {
    // Initialize observables after services are injected
    this.context$ = this.workflowService.getActiveContext();
    this.movieOptions$ = this.metadataService.getMovieOptions().asObservable();
    this.boxsetOptions$ = this.metadataService.getBoxsetOptions().asObservable().pipe(
      map((boxsets: BoxsetSummary[]) => boxsets || [])
    );
    // Load releases based on selected movie; refetch when movie_id changes or after creating a release
    const movieIdForReleases$ = combineLatest([
      this.workflowService.getActiveContext(),
      this.metadataService.getMovieOptions().asObservable()
    ]).pipe(
      map(([context]) => context?.labelForm?.movie_id || null),
      distinctUntilChanged()
    );
    this.releaseOptions$ = merge(
      movieIdForReleases$,
      this.refreshReleaseOptions$.pipe(
        withLatestFrom(movieIdForReleases$),
        map(([, movieId]) => movieId)
      )
    ).pipe(
      switchMap((movieId) => {
        if (movieId) {
          return this.metadataService.listReleases({ movie_id: movieId }).pipe(
            map((releases: ReleaseSummary[]) => (releases || []).filter(r => !r.boxset_id))
          );
        }
        return of([]);
      })
    );
    this.groupOptions$ = this.metadataService.getGroupOptions().asObservable();
    this.isWorkflowReady$ = this.workflowService.isWorkflowReady$;
    this.buttonSpinnerOverride$ = this.workflowService.getActiveContext().pipe(
      map((context: WorkflowContext | null) => context?.jobStatus?.job_status === 'running' || context?.jobStatus?.job_status === 'pending' || false)
    );
    this.orderedReleaseDiscs$ = this.context$.pipe(
      map(ctx => this.getOrderedReleaseDiscs(ctx))
    );
    this.filesFromCurrentDiscTree$ = this.context$.pipe(
      map(ctx => this.getFilesFromTree(this.getCurrentDiscFolderTree(ctx)))
    );
  }

  ngOnInit(): void {
    // #695: single debounced label-save channel. debounceTime collapses
    // per-keystroke edits into one trailing save; switchMap cancels any older
    // in-flight save's echo handling, so a stale response can never land last
    // and overwrite newer typing.
    this.labelSaveQueue$
      .pipe(
        debounceTime(WorkflowLabelingComponent.LABEL_SAVE_DEBOUNCE_MS),
        switchMap(() => this.performLabelSave$()),
        takeUntil(this.destroy$)
      )
      .subscribe();

    // Subscribe to mobile service
    this.mobileService.isMobile$
      .pipe(takeUntil(this.destroy$))
      .subscribe(isMobile => {
        this.isMobile = isMobile;
      });

    // Subscribe to context to update current step and cached title-label inputs
    this.context$
      .pipe(takeUntil(this.destroy$))
      .subscribe(context => {
        if (context?.labelForm) {
          this.updateCurrentStep(context);
        } else if (context?.discdbHit) {
          this.updateCurrentStep(context);
        }
        // Update cached function references for title-label inputs (once per context change, not per CD cycle)
        this.updateCachedTitleFns(context);
      });

    // Subscribe to post-process status when on post-process step
    combineLatest([
      this.context$,
      this.context$.pipe(map(ctx => ctx?.workflowStep ?? ctx?.labelForm?.workflow_step))
    ])
      .pipe(
        takeUntil(this.destroy$),
        filter(([context, step]) => step === 'postprocess' && !!context?.jobStatus),
        switchMap(([context]) => {
          // Update immediately (timer 0), then poll every 2 seconds
          return timer(0, 2000).pipe(
            map(() => context),
            takeUntil(this.destroy$)
          );
        })
      )
      .subscribe(context => {
        if (context?.jobStatus) {
          this.updatePostProcessStatus(context);
        }
      });

    // Subscribe to detect post-process failure
    this.context$.pipe(
      map(ctx => ctx?.jobStatus?.post_state || ctx?.jobStatus?.pipeline?.['postprocess']),
      distinctUntilChanged(),
      filter(state => state === 'failed'),
      takeUntil(this.destroy$)
    ).subscribe(() => {
      this.toastService.show(
        'Post-processing failed. Please check the logs and try again.',
        'error',
        5000
      );
    });
  }

  ngOnDestroy(): void {
    this.subscriptions.unsubscribe();
    this.destroy$.next();
    this.destroy$.complete();
  }

  // Step management (Phase 1: Use workflowStep from context)
  private updateCurrentStep(context: WorkflowContext): void {
    // Use workflowStep from context (migrated from labelForm.workflow_step)
    let workflowStep: WorkflowStep | null = context.workflowStep || null;
    const furthestStep = this.workflowService.computeFurthestStep(context);
    
    // If workflowStep is not set, determine it using WorkflowService
    if (!workflowStep) {
      workflowStep = this.workflowService.determineWorkflowStep(context, {
        respectUserNavigation: true,
        considerJobStates: true,
      });
    } else {
      // Validate stored workflowStep is accessible
      const steps: WorkflowStep[] = getStepOrderForContext(context);
      const storedStepIndex = steps.indexOf(workflowStep);
      const furthestStepIndex = steps.indexOf(furthestStep);
      
      if (storedStepIndex > furthestStepIndex) {
        // Don't override when step was set by backend (e.g. start-rip response) or user navigation
        if (context.stepNavigationSource !== 'user') {
          // Stored step is beyond furthest accessible step - reset to furthest
          workflowStep = furthestStep;
          // Only emit if actually changing the step — avoids no-op updateContext that
          // re-triggers subscriptions and can cause a loop with syncStepWithStage.
          if (context.workflowStep !== furthestStep) {
            this.workflowService.setWorkflowStep(workflowStep);
          }
        }
      }
    }
    
    const discdbHit = context.discdbHit;
    
    // For DiscDB hits, use summary step instead of disc
    if (discdbHit) {
      if (!workflowStep || workflowStep === 'film' || workflowStep === 'boxset') {
        this.currentStep = 'summary';
      } else if (this.steps.includes(workflowStep)) {
        // Only use workflowStep if it's in our steps array
        this.currentStep = workflowStep;
      } else {
        this.currentStep = 'summary';
      }
    } else {
      // Normal flow: use workflowStep or default to 'film'
      // Only use workflowStep if it's in our steps array
      if (workflowStep && this.steps.includes(workflowStep)) {
        this.currentStep = workflowStep;
      } else {
        this.currentStep = 'film';
      }
    }
    if (this.currentStep === 'disc') {
    }
    
    // Ensure postprocess and transfer are included in steps array for index calculation
    this.currentStepIndex = this.steps.indexOf(this.currentStep);
    if (this.currentStepIndex === -1) {
      // If step not found, try to determine based on job status
      if (context.jobStatus) {
        const postState = context.jobStatus.post_state || context.jobStatus.pipeline?.['postprocess'];
        const transferState = context.jobStatus.transfer_state ?? context.jobStatus.pipeline?.['transfer'];
        if (transferState === 'running' || transferState === 'completed') {
          this.currentStep = 'transfer';
        } else if (postState === 'running' || postState === 'completed') {
          // #365 Phase 2 § 6.4 — postprocess collapsed into transfer.
          this.currentStep = 'transfer';
        } else {
          this.currentStep = 'film';
        }
        this.currentStepIndex = this.steps.indexOf(this.currentStep);
      }
      if (this.currentStepIndex === -1) {
        this.currentStepIndex = 0;
      }
    }
  }

  // Breadcrumb methods
  getBreadcrumbSteps(): WorkflowStep[] {
    const currentContext = this.workflowService.getCurrentContext();

    // Delegate to the central step-order util so Path A's `exploratory_rip`
    // pill renders here too (used to be filtered out by a local validSteps
    // allowlist; the unified workflow shell now hosts the exploratory_rip
    // step alongside the others, so the breadcrumb must surface it).
    // #365 Phase 2 § 6.4 — 'postprocess' was already removed from the
    // step order returned here when the WorkflowStep type was purged of
    // it; the breadcrumb just renders what getStepOrderForContext gives.
    let steps = getStepOrderForContext(currentContext);

    // Hit profile keeps the short summary→transfer list as-is.
    if (currentContext?.discdbHit) {
      return steps;
    }

    // Miss profile: drop transfer until a job exists AND hasn't failed
    // (matches the pre-unification gating).
    const jobStatus = currentContext?.jobStatus;
    const jobStatusValue = jobStatus?.job_status;
    const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'];
    const isFailed = jobStatusValue === 'failed' || ripState === 'failed';
    if (!jobStatus || isFailed) {
      steps = steps.filter(s => s !== 'transfer');
    }
    return steps;
  }

  canNavigateToStep(step: WorkflowStep): boolean {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext) return false;
    
    // Use WorkflowService validation (Phase 1)
    const validation = this.workflowService.canNavigateToStep(currentContext, step);
    return validation.allowed;
  }

  getStepLabel(step: WorkflowStep): string {
    const currentContext = this.workflowService.getCurrentContext();
    const isSeries = currentContext?.labelForm?.group_type === 'series';
    const isBoxset = !!currentContext?.labelForm?.boxset_id;
    
    const labels: Partial<Record<WorkflowStep, string>> = {
      'film': isSeries ? 'Series' : 'Movie',
      'exploratory_rip': 'Exploratory Rip',
      'boxset': isBoxset ? 'Boxset' : 'Release',
      'disc': 'Disc',
      'titles': 'Titles',
      'summary': 'Summary',
      // #365 Phase 2 § 6.4 — 'postprocess' label removed (collapsed
      // into transfer).
      'transfer': 'Transfer'
    };
    return labels[step] || step;
  }

  onStepNavigate(step: WorkflowStep): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext) return;
    // Use WorkflowService validation (Phase 1)
    const validation = this.workflowService.canNavigateToStep(currentContext, step);
    if (validation.allowed) {
      this.workflowService.navigateToStep(step);
    } else {
      this.logger.warn('Cannot navigate to step:', validation.reason);
    }
  }

  // Label form changes
  /**
   * #695: value-carrying edit event from disc-label. Apply the value to the
   * CURRENT context immutably (never trust the template-bound object — under
   * change-detection churn it can be a stale detached copy), then enqueue the
   * debounced save.
   */
  onLabelChange(evt?: { field: 'disc_slug' | 'disc_format'; value: string }): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (currentContext && evt && typeof evt === 'object' && 'field' in evt) {
      this.workflowService.updateContext({
        labelForm: { ...(currentContext.labelForm || {}), [evt.field]: evt.value },
      });
    }
    this.queueLabelSave();
  }

  onTitlesChanged(titles: any[]): void {
    const currentContext = this.workflowService.getCurrentContext();
    const titlesToUse = titles || currentContext?.titles || [];
    for (const title of titlesToUse || []) {
      const key = this.getTitleKey(title);
      if (!key) continue;
      const length = (title?.title ?? '').toString().length;
      const prevLength = this.titleLengthByKey.get(key);
      if (prevLength !== undefined && length === 0 && prevLength > 0) {
      }
      if (prevLength !== undefined && length < prevLength) {
      }
      this.titleLengthByKey.set(key, length);
    }
    if (currentContext) {
      this.workflowService.updateContext({ titles: [...titlesToUse] });
    }
  }

  onTitlePatch(patch: TitlePatchRequest): void {
    const discId =
      this.workflowService.getDiscInfoState().currentDiscId ||
      (this.workflowService.getCurrentContext()?.discInfo as any)?.disc_id ||
      null;
    if (!discId) return;
    this.workflowService.patchDiscTitle(discId, patch).subscribe({
      error: (err) => {
        this.logger.error('[WorkflowLabelingComponent] Title patch failed', err);
      }
    });
  }

  onTitleBatchPatch(patches: TitlePatchRequest[]): void {
    if (!patches?.length) return;
    if (patches.length === 1) {
      this.onTitlePatch(patches[0]);
      return;
    }
    const discId =
      this.workflowService.getDiscInfoState().currentDiscId ||
      (this.workflowService.getCurrentContext()?.discInfo as any)?.disc_id ||
      null;
    if (!discId) return;
    this.workflowService.patchDiscTitlesBatch(discId, patches).subscribe({
      error: (err) => {
        this.logger.error('[WorkflowLabelingComponent] Title batch patch failed', err);
      }
    });
  }

  onPrimaryChanged(event: { discId: string; titleId: string }): void {
    const discId =
      event.discId ||
      this.workflowService.getDiscInfoState().currentDiscId ||
      (this.workflowService.getCurrentContext()?.discInfo as any)?.disc_id ||
      null;
    if (!discId) return;
    this.workflowService.setPrimary(discId, event.titleId).subscribe({
      error: (err) => {
        this.logger.error('[WorkflowLabelingComponent] Set primary failed', err);
      }
    });
  }
  
  /** Ungroup/Re-group changed the disc's dedupe-group shape server-side.
   *
   * Unlike set-primary — whose response carries the affected rows, so the
   * service can merge them — this recomputes which titles are grouped at all,
   * and `dedupeGroups` is what the left rail collapses on. Pull just that
   * field back rather than reimplementing the grouping rules client-side.
   */
  onUngrouped(event: { discId: string; titleId: string }): void {
    const discId =
      event?.discId ||
      this.workflowService.getDiscInfoState().currentDiscId ||
      (this.workflowService.getCurrentContext()?.discInfo as any)?.disc_id ||
      null;
    if (!discId) return;
    this.workflowService.refreshDedupeGroups(discId);
  }

  /**
   * Save labelForm with titles to backend
   */
  /** #695: enqueue a debounced, latest-wins label save (channel wired in ngOnInit). */
  private queueLabelSave(): void {
    this.labelSaveQueue$.next();
  }

  /**
   * Perform one label save from a fresh snapshot of the current context.
   * Runs inside the queue's switchMap: if another edit is enqueued while this
   * request is in flight, this observable is unsubscribed and its echo is
   * never applied (#695 — stale echoes were reverting newer keystrokes).
   */
  private performLabelSave$(): Observable<unknown> {
    const context = this.workflowService.getCurrentContext();
    if (!context || !context.id || !context.type || !context.labelForm) return of(null);
    // Strip nothing here: payload mirrors the old saveLabelForm shape
    const payload = { ...context.labelForm, tracks: context.labelForm?.tracks || [] };
    let save$: Observable<WorkflowContext | null>;
    if (context.type === 'job') {
      save$ = this.workflowService.saveJobWorkflowContext(context.id, payload, false);
    } else {
      // For drive context, need to determine if using discId or mount_point
      const discInfoState = this.workflowService.getDiscInfoState();
      const useDiscId = !!discInfoState.currentDiscId;
      const identifier = useDiscId ? discInfoState.currentDiscId! : context.id;
      save$ = this.workflowService.saveDiscWorkflowContext(identifier, payload, false, useDiscId);
    }
    return save$.pipe(
      tap((updatedContext) => this.applyEchoGuarded(updatedContext)),
      catchError((err) => {
        this.logger.error('Failed to save labelForm with titles:', err);
        return of(null);
      })
    );
  }

  /**
   * Apply a save-response echo without clobbering live edits (#695).
   * The echo reflects the form as of save time — anything typed during the
   * round-trip must win. Titles merge preserves user edits and scan metadata;
   * labelForm merge is current-wins for user-editable scalars; function refs
   * and UI state survive from the current context.
   */
  private applyEchoGuarded(updatedContext: WorkflowContext | null | undefined): void {
    if (!updatedContext || !this.workflowService.contextMatchesSelection(updatedContext)) return;
    const currentContext = this.workflowService.getCurrentContext();
    const mergedTitles = this.mergeTitlesWithBackend(
      currentContext?.titles || [],
      updatedContext.titles || []
    );
    const mergedLabelForm = this.mergeLabelFormWithBackend(
      currentContext?.labelForm,
      updatedContext.labelForm
    );
    const mergedContext: any = {
      ...currentContext,
      ...updatedContext,
      labelForm: mergedLabelForm,
      // Use merged titles (preserves user edits)
      titles: mergedTitles,
      // Preserve workflow step navigation source to prevent step reset
      workflowStep: currentContext?.workflowStep || updatedContext.workflowStep,
      stepNavigationSource: currentContext?.stepNavigationSource || updatedContext.stepNavigationSource || 'user',
      // Preserve function references and UI state that aren't in backend response
      titleStatusFn: currentContext?.titleStatusFn || updatedContext.titleStatusFn,
      titleProgressValueFn: currentContext?.titleProgressValueFn || updatedContext.titleProgressValueFn,
      titleActiveFn: currentContext?.titleActiveFn || updatedContext.titleActiveFn,
      previewUrlFn: currentContext?.previewUrlFn || updatedContext.previewUrlFn,
      previewStateFn: currentContext?.previewStateFn || updatedContext.previewStateFn,
      titlePathFn: currentContext?.titlePathFn || updatedContext.titlePathFn,
      stageProgressFn: currentContext?.stageProgressFn || updatedContext.stageProgressFn,
      isStageCompletedFn: currentContext?.isStageCompletedFn || updatedContext.isStageCompletedFn,
      stageTimeline: currentContext?.stageTimeline || updatedContext.stageTimeline,
      activeStage: currentContext?.activeStage || updatedContext.activeStage,
      progressUpdateTrigger: currentContext?.progressUpdateTrigger || updatedContext.progressUpdateTrigger,
    };
    this.workflowService.updateContext(mergedContext);
  }

  private getTitleKey(title: any): string | null {
    if (!title) return null;
    return title.title_id || null;
  }

  /** Normalize disc text fields for comparison (same idea as title merge). */
  private discFieldNorm(v: unknown): string {
    if (v == null) return '';
    return String(v).trim();
  }

  /**
   * After PATCH workflow-context: keep user-visible disc fields when they differ from the response
   * (stale/out-of-order saves); always take disc_number from backend when present.
   */
  /**
   * Fields the backend owns: a save echo may always update these, even when
   * the local value differs (they are computed server-side, never typed).
   */
  private static readonly ECHO_AUTHORITATIVE_KEYS = new Set<string>([
    'disc_number',
    'movie_cover_path',
    'workflow_step',
  ]);

  /**
   * #695: current-wins merge for a save-response echo. The old version guarded
   * only disc_name/disc_slug/disc_format — every other labelForm field took
   * the echo wholesale, reverting anything typed during the round-trip. Now
   * every user-editable scalar the current form holds survives when it
   * differs from the echo; the echo contributes new keys and server-owned
   * fields (ECHO_AUTHORITATIVE_KEYS).
   */
  private mergeLabelFormWithBackend(current: any | null | undefined, backend: any | null | undefined): any {
    if (!backend) return current || {};
    const merged = { ...backend };
    const cur = current || {};
    for (const key of Object.keys(cur)) {
      if (WorkflowLabelingComponent.ECHO_AUTHORITATIVE_KEYS.has(key)) continue;
      const cv = cur[key];
      // null/undefined locally = "unset here" — let the server value stand.
      if (cv === null || cv === undefined) continue;
      const t = typeof cv;
      // Objects/arrays (tracks, nested structures) keep echo semantics.
      if (t !== 'string' && t !== 'number' && t !== 'boolean') continue;
      if (this.discFieldNorm(cv) !== this.discFieldNorm(merged[key])) {
        merged[key] = cv;
      }
    }
    // User cleared the slug → adopt the server-generated one.
    const curSlug = this.discFieldNorm(cur.disc_slug);
    if (curSlug === '' && this.discFieldNorm(backend.disc_slug) !== '') {
      merged.disc_slug = backend.disc_slug;
    }
    return merged;
  }

  private mergeTitlesWithBackend(currentTitles: any[], backendTitles: any[]): any[] {
    const backendTitlesMap = new Map<string, any>();
    backendTitles.forEach((t: any) => {
      const key = this.getTitleKey(t);
      if (key) backendTitlesMap.set(key, t);
    });

    let currentEmptyTitleCount = 0;
    let backendEmptyTitleCount = 0;
    let backendMissingTitleForCurrent = 0;
    let sampleMissingTitleKey: string | null = null;
    currentTitles.forEach((currentTitle: any) => {
      const currentTitleText = (currentTitle?.title || '').toString();
      if (!currentTitleText) currentEmptyTitleCount += 1;
      const key = this.getTitleKey(currentTitle);
      const backendTitle = key ? backendTitlesMap.get(key) : null;
      if (backendTitle) {
        const backendTitleText = (backendTitle?.title || '').toString();
        if (!backendTitleText) backendEmptyTitleCount += 1;
        if (currentTitleText && !backendTitleText) {
          backendMissingTitleForCurrent += 1;
          if (!sampleMissingTitleKey) sampleMissingTitleKey = key;
        }
      }
    });


    const preserveIfMissingFields = [
      'chapters',
      'streams',
      'duration',
      'duration_seconds',
      'size',
      'file',
      'src',
      'source_file',
      'track_id',
      'title_id',
      'title_seq',
      'output_file',
      'output_path',
      'playlist',
      'playlist_index',
      'segment_map',
      'segmentMap',
      'name',
      'index',
      'order_index',
      'detection_warning',
      'detection_flags',
      'detection_confidence',
    ];

    return currentTitles.map((currentTitle: any) => {
      const key = this.getTitleKey(currentTitle);
      const backendTitle = key ? backendTitlesMap.get(key) : null;
      if (!backendTitle) {
        return currentTitle;
      }

      const titleDiffers = currentTitle.title !== backendTitle.title;
      const typeDiffers = currentTitle.type !== backendTitle.type;
      const descriptionDiffers =
        (currentTitle.description || currentTitle.note) !== (backendTitle.description || backendTitle.note);
      const seasonDiffers = currentTitle.season !== backendTitle.season;
      const episodeDiffers = currentTitle.episode !== backendTitle.episode;
      const hasUserEdits = titleDiffers || typeDiffers || descriptionDiffers || seasonDiffers || episodeDiffers;

      const mergedTitle = { ...backendTitle };

      if (hasUserEdits) {
        // Preserve all user-editable fields
        mergedTitle.title = currentTitle.title;
        mergedTitle.type = currentTitle.type;
        mergedTitle.description = currentTitle.description;
        mergedTitle.note = currentTitle.note;
        mergedTitle.season = currentTitle.season;
        mergedTitle.episode = currentTitle.episode;
      }

      // Preserve scan metadata if backend omits it
      for (const field of preserveIfMissingFields) {
        const backendValue = backendTitle[field];
        const currentValue = currentTitle[field];
        if ((backendValue === undefined || backendValue === null) && currentValue !== undefined) {
          mergedTitle[field] = currentValue;
        }
      }

      // Pre-mark as ignore when padding/junk detection flags the title and type is unset
      if (backendTitle.detection_warning && (!mergedTitle.type || mergedTitle.type === '')) {
        mergedTitle.type = 'ignore';
      }

      return mergedTitle;
    });
  }

  /**
   * #695: disc-name keystrokes carry the typed value. Previously this copied
   * the context's labelForm object — but ngModel writes into whatever object
   * was bound last CD cycle, so the copy could miss the newest keystroke
   * (the reported one-character loss). The value parameter is authoritative.
   */
  onNameChange(value?: string): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext?.labelForm) return;
    this.workflowService.updateContext({
      labelForm: typeof value === 'string'
        ? { ...currentContext.labelForm, disc_name: value }
        : { ...currentContext.labelForm },
    });
    this.queueLabelSave();
  }

  onNameBlur(): void {
    // #695: updateContext never persisted (the old comment claiming it does was
    // wrong) — blur now enqueues a real save so name edits reach the backend.
    this.queueLabelSave();
  }

  onSlugEdited(): void {
    // The value is already updated in the context via ngModel binding
    // Just trigger a context update to persist the changes
    const currentContext = this.workflowService.getCurrentContext();
    if (currentContext && currentContext.labelForm) {
      this.workflowService.updateContext({
        labelForm: { ...currentContext.labelForm }
      });
    }
  }

  // Movie selection: show selection optimistically, then save to backend (primary button disabled + spinner during save)
  onSelectMovie(movie: any): void {
    const groupType = (movie.tmdb_type === 'tv' ? 'series' : 'movie') as 'movie' | 'series';
    this.metadataSaving = true;
    const currentContext = this.workflowService.getCurrentContext();
    this.markTmdbSuggestionHandled(currentContext?.discInfo?.disc_id);
    if (currentContext?.labelForm) {
      this.workflowService.updateContext({
        labelForm: {
          ...currentContext.labelForm,
          movie_id: movie.id,
          tmdb_id: movie.tmdb_id ?? null,
          group_type: groupType,
          mode: groupType,
        },
      });
    }
    this.workflowService.applyMetadataSelectionToActiveContext({
      movieId: movie.id,
      tmdbId: movie.tmdb_id ?? null,
      groupType,
      releaseId: null,
      releaseSlug: null,
      releaseName: null,
      releaseYear: null,
      boxsetId: null,
      boxsetSlug: null,
    }).subscribe({
      next: () => {
        this.metadataSaving = false;
        this.movieComboOpen = false;
      },
      error: (err) => {
        this.metadataSaving = false;
        this.logger.error('Failed to select movie:', err);
      }
    });
  }

  // ────────────────────────────────────────────────────────────────────────
  // TMDB suggestion handlers (#389)
  // ────────────────────────────────────────────────────────────────────────

  /** Extract the persisted TMDB suggestion (#388) from the active context.
   *  Returns null when no suggestion is present (no key / TMDB miss / no
   *  disc info loaded yet). Template uses ``getTmdbSuggestion(state.context)
   *  as suggestion`` so the card binds straight to the typed object. */
  getTmdbSuggestion(context: WorkflowContext | null | undefined): TmdbSuggestionInfo | null {
    return context?.discInfo?.tmdb_suggestion ?? null;
  }

  /** True when the suggestion card should render in the film step.
   *
   *  The card is one-shot: once the user has handled the suggestion for a
   *  disc — by clicking Use this, clicking Change, picking a search /
   *  dropdown result, or pasting a URL — we mark the disc handled in
   *  ``localStorage`` and never re-render the card for that disc again,
   *  even after a page reload + Change-from-selected-chip cycle. The user
   *  would otherwise be re-prompted to confirm a TMDB match every time
   *  they cleared the movie, which feels like the app forgot what they
   *  just told it. */
  showTmdbSuggestionCard(context: WorkflowContext | null | undefined): boolean {
    if (!context) return false;
    if (context.labelForm?.movie_id) return false;
    if (this.tmdbSuggestionMode === 'override') return false;
    if (this.isTmdbSuggestionHandled(context.discInfo?.disc_id)) return false;
    return !!this.getTmdbSuggestion(context);
  }

  /** localStorage key for the per-disc "TMDB suggestion handled" flag. */
  private _tmdbSuggestionHandledKey(discId: string): string {
    return `mkv-auto.tmdb-suggestion-handled.${discId}`;
  }

  /** Mark the suggestion as handled for the given disc — called from any
   *  flow where the user has effectively made a decision about the film:
   *  Use this, Change, a successful URL paste, picking from the dropdown,
   *  or picking a TMDB candidate. After this, ``showTmdbSuggestionCard``
   *  stops returning true for the disc until storage is cleared. */
  private markTmdbSuggestionHandled(discId: string | null | undefined): void {
    if (!discId) return;
    try {
      localStorage.setItem(this._tmdbSuggestionHandledKey(discId), '1');
    } catch {
      /* localStorage unavailable (private browsing, quota); accept the
         session-only fallback — the card simply re-appears on reload. */
    }
  }

  /** Read the per-disc "handled" flag set by ``markTmdbSuggestionHandled``. */
  private isTmdbSuggestionHandled(discId: string | null | undefined): boolean {
    if (!discId) return false;
    try {
      return localStorage.getItem(this._tmdbSuggestionHandledKey(discId)) === '1';
    } catch {
      return false;
    }
  }

  /** True when the movie-selector combobox should render. After the user
   *  picks a movie (`movie_id` set) the combobox stays visible because it
   *  doubles as the "selected film" chip with a Change button. The only
   *  time the selector hides is when the auto-suggestion card is rendering
   *  in its place. */
  showTmdbOverrideControls(context: WorkflowContext | null | undefined): boolean {
    return !this.showTmdbSuggestionCard(context);
  }

  /** Accept the auto-suggestion as the chosen movie. Goes through
   *  WorkflowService.createAndLinkMovieToActiveContext (mandatory per
   *  CLAUDE.md — never call MetadataService.create* directly), then applies
   *  the metadata selection so movie_id flows into labelForm and the rest
   *  of the workflow advances. */
  onAcceptTmdbSuggestion(suggestion: TmdbSuggestionInfo): void {
    const candidate: TmdbSearchCandidate = {
      tmdb_id: suggestion.tmdb_id,
      tmdb_type: suggestion.tmdb_type,
      title: suggestion.title,
      year: suggestion.year,
      cover_url: suggestion.cover_url,
      score: suggestion.score,
    };
    this.onTmdbCandidatePicked(candidate);
  }

  /** Shared codepath for "Use this" (accept suggestion) and "pick a search
   *  result" — both pivot on a TMDB candidate and create+link a Movie.
   *  Delegates to ``_linkNewlyCreatedMovieToActiveContext`` so the behavior
   *  is byte-identical to the URL-paste flow ``onMovieLookup``. */
  onTmdbCandidatePicked(candidate: TmdbSearchCandidate): void {
    const movieData: MovieCreate = {
      name: candidate.title,
      production_year: candidate.year,
      tmdb_id: candidate.tmdb_id,
      tmdb_type: candidate.tmdb_type,
      cover_url: candidate.cover_url,
    };
    const ctxAtStart = this.workflowService.getCurrentContext();
    if (!ctxAtStart) {
      this.logger.warn('onTmdbCandidatePicked: no active workflow context');
      return;
    }
    const discId = ctxAtStart.discInfo?.disc_id ?? null;
    const mountPoint = ctxAtStart.type === 'drive' ? ctxAtStart.id : null;

    this.metadataSaving = true;
    this.metadataService.createMovieForDisc(discId, mountPoint, movieData).subscribe({
      next: (result) => {
        this.markTmdbSuggestionHandled(discId);
        this._linkNewlyCreatedMovieToActiveContext(result.movie, {
          onComplete: () => {
            this.metadataSaving = false;
            this.tmdbSearchResults = null;
            this.tmdbSuggestionMode = 'suggestion';
          },
          onError: (err) => {
            this.metadataSaving = false;
            this.logger.error('Failed to apply TMDB selection:', err);
          },
        });
      },
      error: (err) => {
        this.metadataSaving = false;
        this.logger.error('Failed to create+link movie from TMDB suggestion:', err);
        this.toastService.show(formatHttpErrorDetail(err) || 'Failed to select TMDB candidate', 'error');
      },
    });
  }

  /** Shared post-createMovieForDisc dance: wait for the tap-side-effect to
   *  propagate the new movie into MetadataService.getMovieOptions(), merge
   *  it into the active context's movieOptions array (so
   *  ``<app-movie-selector>`` can resolve selectedMovieId), then apply the
   *  metadata selection so ``labelForm.movie_id`` flows through. Both the
   *  URL-paste flow (``onMovieLookup``) and the TMDB-suggestion flow
   *  (``onTmdbCandidatePicked``) call this so they behave identically —
   *  including progression-logic hooks that watch for ``movie_id`` flips.
   *
   *  The setTimeout(0)/(50) waits and the in-list verification fallback
   *  mirror the URL-paste dance verbatim — they exist for race conditions
   *  between ``createMovieForDisc``'s synchronous tap and Angular's async
   *  CD propagation to the selector. */
  private _linkNewlyCreatedMovieToActiveContext(
    movie: any,
    callbacks: {
      onComplete: () => void;
      onError: (err: any) => void;
    },
  ): void {
    // setTimeout(0) — wait for next tick so:
    //   1. movieOptions$ observable has propagated to template
    //   2. movie-selector component has processed the new movieOptions input
    //   3. comboboxItems array has been updated
    setTimeout(() => {
      const ctx = this.workflowService.getCurrentContext();
      if (!ctx || !ctx.labelForm) {
        callbacks.onComplete();
        return;
      }

      const groupType: 'movie' | 'series' =
        (movie as any)?.tmdb_type === 'tv' ? 'series' : 'movie';

      // Verify the new movie made it into the service's authoritative list.
      const optionsInService = this.metadataService.getMovieOptions().value;
      const inServiceList = optionsInService.find((m) => m.id === movie.id);

      const applySelection = () => {
        this.workflowService.applyMetadataSelectionToActiveContext({
          movieId: movie.id,
          tmdbId: movie.tmdb_id ?? null,
          groupType,
          releaseId: null,
          releaseSlug: null,
          releaseName: null,
          releaseYear: null,
          boxsetId: null,
          boxsetSlug: null,
        }).subscribe({
          next: () => callbacks.onComplete(),
          error: (err) => callbacks.onError(err),
        });
      };

      if (inServiceList) {
        // Mirror the new movie into the active context's movieOptions array
        // (helper methods read off the context, not the service's BehaviorSubject).
        const contextOptions = ctx.movieOptions ?? [];
        const alreadyInContext = contextOptions.find((m) => m.id === movie.id);
        const updatedOptions = alreadyInContext
          ? contextOptions
          : [...contextOptions, movie];
        this.workflowService.updateContext({ ...ctx, movieOptions: updatedOptions });
        applySelection();
      } else {
        // Race fallback — give the synchronous tap one more tick to land,
        // then apply the selection regardless. The selector will catch up
        // once the BehaviorSubject emits.
        setTimeout(() => {
          const retryCtx = this.workflowService.getCurrentContext();
          if (retryCtx && retryCtx.labelForm) {
            applySelection();
          } else {
            callbacks.onComplete();
          }
        }, 50);
      }
    }, 0);
  }

  /** Switch from showing the suggestion card to revealing URL paste + search.
   *  Clicking Change is an explicit "I don't want this suggestion" signal,
   *  so mark the disc handled — even if the user then bails on the override
   *  flow without picking anything, the card stays hidden on subsequent
   *  visits to the film step. */
  enterTmdbOverrideMode(): void {
    this.tmdbSuggestionMode = 'override';
    const ctx = this.workflowService.getCurrentContext();
    this.markTmdbSuggestionHandled(ctx?.discInfo?.disc_id);
  }

  /** Cancel an override and return to the suggestion card (only meaningful
   *  when a suggestion exists). */
  dismissTmdbOverride(): void {
    this.tmdbSuggestionMode = 'suggestion';
    this.tmdbSearchResults = null;
    this.tmdbSearchError = null;
  }

  /** Run a free-text TMDB search via POST /movies/tmdb-search. Triggered by
   *  the "Search TMDB for ‹q›" CTA inside <app-movie-selector>'s empty-state
   *  (the selector emits (tmdbSearchRequested) with the user's search term). */
  onTmdbSearchRequested(query: string): void {
    const q = (query || '').trim();
    if (!q) return;
    const context = this.workflowService.getCurrentContext();
    const mediaType: 'movie' | 'tv' | null =
      context?.labelForm?.group_type === 'series' ? 'tv' :
      context?.labelForm?.group_type === 'movie' ? 'movie' : null;
    this.tmdbSearchLoading = true;
    this.tmdbSearchError = null;
    this.metadataService.searchTmdb(q, { media_type: mediaType, limit: 5 }).subscribe({
      next: (resp) => {
        this.tmdbSearchResults = resp.candidates;
        this.tmdbSearchLoading = false;
      },
      error: (err) => {
        this.tmdbSearchLoading = false;
        this.tmdbSearchResults = null;
        // Backend distinguishes "no key configured" from "network/API failure"
        // via detail.code so we can surface a more actionable message.
        const code = err?.error?.detail?.code;
        if (code === 'tmdb_unavailable') {
          this.tmdbSearchError = 'TMDB search not available — set a TMDB API key in Settings.';
        } else {
          this.tmdbSearchError = err?.error?.detail?.reason || err?.message || 'TMDB search failed';
        }
      },
    });
  }

  onMovieLookup(tmdbUrl?: string): void {
    const url = tmdbUrl || this.tmdbUrl;
    if (!url?.trim()) return;

    this.filmLookupLoading = true;
    this.filmLookupError = null;

    // First lookup movie data from TMDB
    this.metadataService.lookupMovie(url.trim()).subscribe({
      next: (movieData) => {
        const currentContext = this.workflowService.getCurrentContext();
        if (!currentContext) {
          this.filmLookupError = 'No active workflow context';
          this.filmLookupLoading = false;
          return;
        }

        const discId = currentContext.discInfo?.disc_id || null;
        const mountPoint = currentContext.type === 'drive' ? currentContext.id : null;

        // Backend creates movie, stores movie_id in label_draft, returns movie details. Keep loading until context updated.
        this.metadataService.createMovieForDisc(discId, mountPoint, movieData).subscribe({
          next: (result) => {
            // User just pasted a URL — that's an explicit pick, so mark the
            // suggestion handled. The card never returns for this disc.
            this.markTmdbSuggestionHandled(discId);
            // Shared post-create dance (#389 follow-up) — same helper used
            // by the TMDB suggestion "Use this" flow so both behave
            // identically (selected-chip render, progression hooks).
            this._linkNewlyCreatedMovieToActiveContext(result.movie, {
              onComplete: () => {
                this.tmdbUrl = '';
                this.tmdbDropdownOpen = false;
                this.filmLookupLoading = false;
              },
              onError: (err) => {
                this.logger.error('Failed to apply movie selection:', err);
                this.tmdbUrl = '';
                this.tmdbDropdownOpen = false;
                this.filmLookupLoading = false;
              },
            });
          },
          error: (err) => {
            this.filmLookupError = err.error?.detail || 'Failed to create movie';
            this.filmLookupLoading = false;
          }
        });
      },
      error: (err) => {
        this.filmLookupError = err.error?.detail || 'Failed to lookup movie';
        this.filmLookupLoading = false;
      }
    });
  }

  // Release/Boxset creation
  createReleaseFromWorkflow(data: any): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext) return;
    
    this.workflowService.createAndLinkReleaseToActiveContext(data)
      .subscribe({
        next: () => {
          // Release created and linked
        },
        error: (err) => {
          this.logger.error('Failed to create release:', err);
        }
      });
  }

  createBoxsetFromWorkflow(data: any): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext) return;
    
    this.workflowService.createAndLinkBoxsetToActiveContext(data)
      .subscribe({
        next: () => {
          // Boxset created and linked
        },
        error: (err) => {
          this.logger.error('Failed to create boxset:', err);
        }
      });
  }

  // Finalize label
  finalizeLabel(): void {
    this.workflowService.finalizeLabel()
      .subscribe({
        next: () => {
          // Label finalized
        },
        error: (err) => {
          this.logger.error('Failed to finalize label:', err);
        }
      });
  }

  // Workflow navigation (kept for backward compatibility)
  onWorkflowStepNavigate(step: string): void {
    this.onStepNavigate(step as WorkflowStep);
  }

  // Save disc mode
  saveDiscMode(mode: 'copy' | 'rip'): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (currentContext) {
      this.workflowService.updateContext({ discMode: mode });
    }
  }

  // Helper methods for template
  filteredMovieOptions(): Observable<any[]> {
    return this.movieOptions$.pipe(
      map(movies => {
        if (!this.movieSearch) return movies || [];
        const lowerQuery = this.movieSearch.toLowerCase();
        return (movies || []).filter(m => 
          m.name?.toLowerCase().includes(lowerQuery) ||
          (m.production_year && m.production_year.toString().includes(lowerQuery))
        );
      })
    );
  }

  filteredBoxsetOptions(): Observable<any[]> {
    return this.boxsetOptions$.pipe(
      map(boxsets => {
        if (!this.boxsetSearch) return boxsets || [];
        const lowerQuery = this.boxsetSearch.toLowerCase();
        return (boxsets || []).filter(b => 
          (b.name?.toLowerCase().includes(lowerQuery)) ||
          (b.title?.toLowerCase().includes(lowerQuery)) ||
          (b.year && b.year.toString().includes(lowerQuery))
        );
      })
    );
  }

  // Selected movie helpers
  selectedMovieName(context: any): string | null {
    return context?.labelForm?.movie_name || null;
  }

  selectedMovieYear(context: any): string | null {
    return context?.labelForm?.movie_production_year?.toString() || null;
  }

  selectedMovieCover(context: any): string | null {
    return context?.labelForm?.movie_cover_url || context?.labelForm?.movie_cover_path || null;
  }

  /**
   * Set Movie vs Series content type. Persists to backend for jobs.
   * When type changes, clears movie selection so the toggle matches the list filter (template behavior).
   */
  setContentType(type: 'movie' | 'series'): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext || !currentContext.labelForm) return;

    const currentType = currentContext.labelForm.group_type === 'series' ? 'series' : 'movie';
    const updatedLabelForm = {
      ...currentContext.labelForm,
      group_type: type,
      mode: type,
      // When switching type, clear movie (and related) so selection matches the new type
      ...(currentType !== type
        ? {
            movie_id: null,
            movie_name: null,
            movie_cover_url: null,
            movie_cover_path: null,
            movie_production_year: null,
            tmdb_id: null,
          }
        : {}),
    };
    this.workflowService.updateContext({ labelForm: updatedLabelForm });

    if (currentContext.type === 'job' && currentContext.id) {
      this.workflowService.saveJobWorkflowContext(currentContext.id, updatedLabelForm, false).subscribe({
        next: (updatedContext) => {
          this.applyEchoGuarded(updatedContext); // #695: echo must not clobber live edits
        },
        error: (err) => this.logger.error('[WorkflowLabelingComponent] Failed to save group_type', err),
      });
    } else if (currentContext.type === 'drive' && currentContext.id) {
      // Persist group_type on label_draft for drive (sets ignore window so context_changed refetch doesn't show loading)
      const discInfoState = this.workflowService.getDiscInfoState();
      const useDiscId = !!discInfoState.currentDiscId;
      const identifier = useDiscId ? discInfoState.currentDiscId! : currentContext.id;
      this.workflowService.saveDiscWorkflowContext(identifier, updatedLabelForm, false, useDiscId).subscribe({
        next: (updatedContext) => {
          this.applyEchoGuarded(updatedContext); // #695: echo must not clobber live edits
        },
        error: (err) => this.logger.error('[WorkflowLabelingComponent] Failed to save group_type', err),
      });
    }
  }

  /** Active when type is movie or when type is missing (default to movie like template). */
  isMovieActive(context: any): boolean {
    const type = context?.labelForm?.group_type ?? context?.labelForm?.mode ?? 'movie';
    return type !== 'series';
  }

  /** Active only when type is explicitly series. */
  isSeriesActive(context: any): boolean {
    const type = context?.labelForm?.group_type ?? context?.labelForm?.mode;
    return type === 'series';
  }

  /** True when boxset mode is active (boxset_id set, including __pending__). Template: "Release" vs "Boxset" toggle. */
  isBoxsetMode(context: any): boolean {
    return !!(context?.labelForm?.boxset_id);
  }

  /** Switch to release mode: clear boxset_id so user selects a standalone release. */
  setReleaseMode(): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext?.labelForm) return;
    const updatedLabelForm = { ...currentContext.labelForm, boxset_id: null };
    this.workflowService.updateContext({ labelForm: updatedLabelForm });
  }

  /** Switch to boxset mode: set boxset_id to __pending__, clear release_id (template behavior). */
  setBoxsetMode(): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext?.labelForm) return;
    const updatedLabelForm = {
      ...currentContext.labelForm,
      boxset_id: '__pending__',
      release_id: null,
    };
    this.workflowService.updateContext({ labelForm: updatedLabelForm });
  }

  // Boxset management
  toggleBoxset(): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext || !currentContext.labelForm) return;
    
    const labelForm = currentContext.labelForm;
    if (labelForm.boxset_id) {
      // Toggle OFF: Unlink from boxset
      const updatedLabelForm = { ...labelForm, boxset_id: null };
      this.workflowService.updateContext({ labelForm: updatedLabelForm });
    } else {
      // Toggle ON: Enable boxset mode
      const updatedLabelForm = { ...labelForm, boxset_id: '__pending__' };
      this.workflowService.updateContext({ labelForm: updatedLabelForm });
    }
  }

  toggleBoxsetCombo(): void {
    this.boxsetOpen = !this.boxsetOpen;
  }

  closeBoxsetCombo(): void {
    this.boxsetOpen = false;
  }

  onBoxsetSelected(boxset: any): void {
    if (!boxset?.id) {
      return;
    }
    const currentContext = this.workflowService.getCurrentContext();
    const movieId = currentContext?.labelForm?.movie_id;
    if (!movieId) {
      this.toastService.show('Select a movie before choosing a boxset.', 'error', 3500);
      return;
    }
    this.metadataSaving = true;
    this.findOrCreateReleaseForMovieBoxset(String(movieId), boxset);
  }

  /**
   * Ensure a release exists for (movie, boxset), link disc via POST or metadata selection.
   * Mirrors ripper-page findOrCreateReleaseForMovieBoxset; uses createReleaseForDisc when none exists.
   */
  private findOrCreateReleaseForMovieBoxset(movieId: string, boxset: BoxsetSummary): void {
    const boxsetId = boxset.id!;
    const boxsetSlug = boxset.slug ?? '';

    this.metadataService.findReleaseByMovieBoxset(movieId, boxsetId).subscribe({
      next: (release) => {
        if (release?.id) {
          const releaseMovieId = (release as { movie_id?: string }).movie_id;
          if (releaseMovieId && releaseMovieId !== movieId) {
            this.logger.warn(
              `[WorkflowLabeling] Release ${release.id} has movie_id ${releaseMovieId}, expected ${movieId} — ignoring`
            );
            this.metadataSaving = false;
            return;
          }
          const boxsetSlugFromRel = (release as { boxset_slug?: string }).boxset_slug ?? undefined;
          const selection = {
            releaseId: release.id,
            releaseSlug: release.slug,
            // Carry the release's own metadata into the form. Without these the
            // form kept its previous values — release_name="" when nothing had
            // been assigned yet — and the autosave wrote that blank over the
            // release's real name (see applyBoxsetLinkToForm).
            ...(release.name ? { releaseName: release.name } : {}),
            ...(release.release_year != null ? { releaseYear: release.release_year } : {}),
            ...(release.cover_front_url ? { coverFrontUrl: release.cover_front_url } : {}),
            ...(release.boxset_id && release.boxset_id === boxsetId
              ? { boxsetId: release.boxset_id, boxsetSlug: boxsetSlugFromRel ?? undefined }
              : { boxsetId, boxsetSlug }),
          };
          this.workflowService.applyMetadataSelectionToActiveContext(selection).subscribe({
            next: () => this.afterBoxsetSelectionSaved(),
            error: (err) => {
              this.metadataSaving = false;
              this.logger.error('Failed to link release for boxset:', err);
            },
          });
          return;
        }

        const ctx = this.workflowService.getCurrentContext();
        if (!ctx) {
          this.metadataSaving = false;
          return;
        }
        const discId = ctx.discInfo?.disc_id ?? null;
        const mountPoint = ctx.type === 'drive' ? ctx.id : null;
        this.metadataService
          .createReleaseForDisc(discId, mountPoint, { movie_id: movieId, boxset_id: boxsetId })
          .subscribe({
            next: (result) => {
              const rel = result.release;
              if (rel?.id != null && rel.slug != null) {
                this.workflowService
                  .linkBoxsetToContext(boxsetId, boxsetSlug, ctx.id, ctx.type, {
                    id: String(rel.id),
                    slug: String(rel.slug),
                  })
                  .subscribe({
                    next: () => {
                      this.refreshReleaseOptions$.next();
                      this.afterBoxsetSelectionSaved();
                    },
                    error: (err) => {
                      this.metadataSaving = false;
                      this.logger.error('Failed to sync boxset/release to context after create:', err);
                    },
                  });
              } else {
                this.metadataSaving = false;
                this.toastService.show('Release was not returned after create', 'error', 4000);
              }
            },
            error: (err) => {
              this.metadataSaving = false;
              this.logger.error('Failed to create release for boxset:', err);
              this.toastService.show(formatHttpErrorDetail(err), 'error', 5000);
            },
          });
      },
      error: (err) => {
        this.metadataSaving = false;
        this.logger.error('Failed to find release by movie+boxset:', err);
        this.toastService.show('Failed to look up release for this boxset', 'error', 4000);
      },
    });
  }

  private afterBoxsetSelectionSaved(): void {
    this.metadataSaving = false;
    this.boxsetOpen = false;
    const currentContext = this.workflowService.getCurrentContext();
    if (currentContext) {
      const currentStep =
        currentContext.workflowStep || this.workflowService.determineWorkflowStep(currentContext);
      if (currentStep === 'boxset' && this.autoAdvanceOnSelection) {
        this.workflowService.updateContext({
          workflowStep: 'disc',
          stepNavigationSource: 'automatic',
        });
      }
    }
  }

  onReleaseSelected(release: ReleaseSummary): void {
    if (release?.id) {
      this.metadataSaving = true;
      this.workflowService.applyMetadataSelectionToActiveContext({
        releaseId: release.id,
        releaseSlug: release.slug,
        releaseName: release.name ?? null,
        releaseYear: release.release_year ?? null,
        coverFrontUrl: release.cover_front_url ?? null,
      }).subscribe({
        next: () => {
          this.metadataSaving = false;
          this.refreshReleaseOptions$.next();
          // Transition to disc step after release is selected (Phase 1: Use workflowStep)
          const currentContext = this.workflowService.getCurrentContext();
          if (currentContext) {
            const currentStep = currentContext.workflowStep || this.workflowService.determineWorkflowStep(currentContext);
            if (currentStep === 'boxset' && this.autoAdvanceOnSelection) {
              this.workflowService.updateContext({
                workflowStep: 'disc',
                stepNavigationSource: 'automatic'
              });
            }
          }
        },
        error: (err) => {
          this.metadataSaving = false;
          this.logger.error('Failed to select release:', err);
        }
      });
    }
  }

  onReleaseCleared(): void {
    this.metadataSaving = true;
    this.workflowService.applyMetadataSelectionToActiveContext({
      releaseId: null,
      releaseSlug: null,
      releaseName: null,
      releaseYear: null,
      coverFrontUrl: null,
    }).subscribe({
      next: () => {
        this.metadataSaving = false;
        // Keep user on boxset step so they can re-select (prevent reset to film)
        this.workflowService.updateContext({ workflowStep: 'boxset', stepNavigationSource: 'user' });
      },
      error: (err) => {
        this.metadataSaving = false;
        this.logger.error('Failed to clear release:', err);
      }
    });
  }

  onBoxsetCleared(): void {
    this.metadataSaving = true;
    this.workflowService.applyMetadataSelectionToActiveContext({
      boxsetId: null,
      boxsetSlug: null
    }).subscribe({
      next: () => {
        this.metadataSaving = false;
        // Keep user on boxset step so they can re-select (prevent reset to film)
        this.workflowService.updateContext({ workflowStep: 'boxset', stepNavigationSource: 'user' });
      },
      error: (err) => {
        this.metadataSaving = false;
        this.logger.error('Failed to clear boxset:', err);
      }
    });
  }

  onReleaseMetadataPatched(r: ReleaseSummary): void {
    this.refreshReleaseOptions$.next();
    const ctx = this.workflowService.getCurrentContext();
    if (!ctx?.labelForm || ctx.labelForm.release_id !== r.id) return;
    this.workflowService
      .applyMetadataSelectionToActiveContext({
        releaseId: r.id,
        releaseSlug: r.slug,
        releaseName: r.name ?? null,
        releaseYear: r.release_year ?? null,
        coverFrontUrl: r.cover_front_url ?? null,
      })
      .subscribe({
        error: (e) => this.logger.error('[WorkflowLabeling] sync release after PATCH', e),
      });
  }

  onReleaseDeleted(deletedReleaseId: string): void {
    this.refreshReleaseOptions$.next();
    const ctx = this.workflowService.getCurrentContext();
    if (ctx?.labelForm?.release_id === deletedReleaseId) {
      this.onReleaseCleared();
    }
  }

  onBoxsetMetadataPatched(b: BoxsetSummary): void {
    this.metadataService.refreshBoxsetOptions();
    this.refreshReleaseOptions$.next();
    const ctx = this.workflowService.getCurrentContext();
    if (!ctx?.labelForm || ctx.labelForm.boxset_id !== b.id) return;
    this.workflowService
      .applyMetadataSelectionToActiveContext({
        boxsetId: b.id,
        boxsetSlug: b.slug,
      })
      .subscribe({
        error: (e) => this.logger.error('[WorkflowLabeling] sync boxset after PATCH', e),
      });
  }

  onBoxsetDeleted(deletedBoxsetId: string): void {
    this.metadataService.refreshBoxsetOptions();
    this.refreshReleaseOptions$.next();
    const ctx = this.workflowService.getCurrentContext();
    if (ctx?.labelForm?.boxset_id !== deletedBoxsetId) return;
    this.metadataSaving = true;
    this.workflowService
      .applyMetadataSelectionToActiveContext({
        boxsetId: null,
        boxsetSlug: null,
        releaseId: null,
        releaseSlug: null,
        releaseName: null,
        releaseYear: null,
        coverFrontUrl: null,
      })
      .subscribe({
        next: () => {
          this.metadataSaving = false;
          this.workflowService.updateContext({ workflowStep: 'boxset', stepNavigationSource: 'user' });
        },
        error: (err) => {
          this.metadataSaving = false;
          this.logger.error('[WorkflowLabeling] clear context after boxset delete', err);
        },
      });
  }

  onMovieMetadataPatched(updated: MovieRecord): void {
    this.metadataService.refreshMovieOptions();
    const ctx = this.workflowService.getCurrentContext();
    if (!ctx?.labelForm || ctx.labelForm.movie_id !== updated.id) return;

    const lf = {
      ...ctx.labelForm,
      movie_name: updated.name ?? ctx.labelForm.movie_name ?? null,
      movie_production_year: updated.production_year ?? ctx.labelForm.movie_production_year ?? null,
      movie_cover_url: updated.cover_url ?? ctx.labelForm.movie_cover_url ?? null,
    };
    this.workflowService.updateContext({ labelForm: lf });

    if (ctx.type === 'job' && ctx.id) {
      this.workflowService.saveJobWorkflowContext(ctx.id, lf, false).subscribe({
        next: (uc) => {
          this.applyEchoGuarded(uc); // #695: echo must not clobber live edits
        },
        error: (e) => this.logger.error('[WorkflowLabeling] persist movie metadata', e),
      });
      return;
    }
    if (ctx.type === 'drive' && ctx.id) {
      const discInfoState = this.workflowService.getDiscInfoState();
      const useDiscId = !!discInfoState.currentDiscId;
      const identifier = useDiscId ? discInfoState.currentDiscId! : ctx.id;
      this.workflowService.saveDiscWorkflowContext(identifier, lf, false, useDiscId).subscribe({
        next: (uc) => {
          this.applyEchoGuarded(uc); // #695: echo must not clobber live edits
        },
        error: (e) => this.logger.error('[WorkflowLabeling] persist movie metadata (drive)', e),
      });
    }
  }

  onReleaseCreated(releaseData?: any): void {
    // When release selector emits (user submitted the create form in drawer/panel), create the release on both mobile and desktop.
    const nameTrimmed = (releaseData?.name ?? '').toString().trim();
    if (nameTrimmed) {
      this.createReleaseFromData(releaseData);
      return;
    }
    // Only show workflow modal when explicitly opened (e.g. openReleaseCreateModal()); never when selector emitted (object with empty name = bad payload, don't open modal).
    if (this.isMobile && (releaseData === undefined || releaseData === null)) {
      this.showReleaseCreateModal = true;
      this.newRelease = { name: '', release_year: null };
    }
  }

  private createReleaseFromData(releaseData: any): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext) {
      this.logger.error('No active workflow context');
      this.releaseCreateResult = { ok: false, error: 'No active workflow context — reload and try again', token: ++this.releaseCreateToken };
      return;
    }

    const discId = currentContext.discInfo?.disc_id ?? null;
    const mountPoint = currentContext.type === 'drive' ? currentContext.id : null;

    const yearRaw = releaseData.release_year;
    const releaseYear =
      yearRaw != null && yearRaw !== ''
        ? (Number(yearRaw) || null)
        : null;
    if (releaseYear == null || releaseYear < 1000 || releaseYear > 9999) {
      this.toastService.show('Release year is required (1000–9999)', 'error', 3000);
      this.releaseCreateResult = { ok: false, error: 'Release year is required (1000–9999)', token: ++this.releaseCreateToken };
      return;
    }
    const payload: Record<string, unknown> = {
      release_name: (releaseData.name ?? '').toString().trim() || null,
      release_year: releaseYear,
      upc: releaseData.upc ?? null,
      asin: releaseData.asin ?? null,
      cover_front_url: releaseData.cover_front_url ?? null,
      cover_back_url: releaseData.cover_back_url ?? null,
      // The user filled in the create form, so this must not resolve to a
      // release the show already has. Without it a second season silently
      // returned the first one (#821). Selecting an existing release is a
      // different path entirely (releaseSelected -> linkReleaseToContext) and
      // never reaches here.
      create_new: true,
    };
    const movieId = currentContext.labelForm?.movie_id;
    if (movieId) {
      payload['movie_id'] = movieId;
    }

    this.metadataService.createReleaseForDisc(discId, mountPoint, payload).subscribe({
      next: (result) => {
        this.releaseCreateResult = { ok: true, token: ++this.releaseCreateToken };
        this.refreshReleaseOptions$.next();
        this.toastService.show('Release created', 'success', 2000);
        const rel = result.release;
        if (rel?.id != null && rel.slug != null) {
          const displayName = (rel as any).name ?? (rel as any).title ?? (rel as any).release_name ?? null;
          this.workflowService
            .linkReleaseToContext(String(rel.id), String(rel.slug), currentContext.id, currentContext.type, displayName)
            .subscribe({
              error: (e) => this.logger.error('Failed to link new release to workflow context', e),
            });
        }
      },
      error: (err) => {
        this.logger.error('Failed to create release:', err);
        this.releaseCreateResult = { ok: false, error: formatHttpErrorDetail(err), token: ++this.releaseCreateToken };
        this.toastService.show(formatHttpErrorDetail(err), 'error', 5000);
      },
    });
  }

  openReleaseCreateModal(): void {
    this.showReleaseCreateModal = true;
    this.newRelease = { name: '', release_year: null };
  }

  closeReleaseCreateModal(): void {
    this.showReleaseCreateModal = false;
    this.newRelease = { name: '', release_year: null };
    this.releaseValidationErrors = {};
  }

  // Validation functions for release
  _validateReleaseYear(year: number | null): boolean {
    if (year === null || year === undefined) return false;
    return Number.isInteger(year) && year >= 1000 && year <= 9999;
  }

  /** Accepts GTIN-8 (EAN-8), GTIN-12 (UPC-A), GTIN-13 (EAN-13), GTIN-14. */
  _validateUPC(upc: string | undefined): boolean {
    if (!upc) return false;
    const s = String(upc).trim();
    if (!/^\d+$/.test(s)) return false;
    const len = s.length;
    return len === 8 || len === 12 || len === 13 || len === 14;
  }

  _validateCoverURL(url: string | undefined): boolean {
    if (!url) return false;
    const trimmed = url.trim();
    return trimmed.startsWith('http://') || trimmed.startsWith('https://');
  }

  // Check if release field is invalid
  isReleaseFieldInvalid(fieldName: string): boolean {
    return this.releaseValidationErrors[fieldName] || false;
  }

  // Validate release field on blur/change
  validateReleaseField(fieldName: string): void {
    let isValid = true;
    
    switch (fieldName) {
      case 'name':
        isValid = !!(this.newRelease.name && this.newRelease.name.trim());
        break;
      case 'release_year':
        isValid = this._validateReleaseYear(this.newRelease.release_year);
        break;
      case 'upc':
        isValid = this._validateUPC(this.newRelease.upc);
        break;
      case 'cover_front_url':
        isValid = this._validateCoverURL(this.newRelease.cover_front_url);
        break;
    }
    
    this.releaseValidationErrors[fieldName] = !isValid;
  }

  // Check if release form is valid
  isReleaseFormValid(): boolean {
    const nameValid = !!(this.newRelease.name && this.newRelease.name.trim());
    const yearValid = this._validateReleaseYear(this.newRelease.release_year);
    const upcValid = this._validateUPC(this.newRelease.upc);
    const coverValid = this._validateCoverURL(this.newRelease.cover_front_url);
    
    return nameValid && yearValid && upcValid && coverValid;
  }

  createRelease(): void {
    // Validate all fields before creating
    this.validateReleaseField('name');
    this.validateReleaseField('release_year');
    this.validateReleaseField('upc');
    this.validateReleaseField('cover_front_url');
    
    if (!this.isReleaseFormValid()) {
      // Don't create if validation fails
      return;
    }
    
    this.createReleaseFromData(this.newRelease);
    this.closeReleaseCreateModal();
  }
  
  selectBoxset(boxset: any): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext || !currentContext.labelForm) return;
    
    const updatedLabelForm = {
      ...currentContext.labelForm,
      boxset_id: boxset.id || null
    };
    this.workflowService.updateContext({ labelForm: updatedLabelForm });
    this.boxsetOpen = false;
  }

  selectedBoxset(context: any): any | null {
    if (!context?.labelForm?.boxset_id) return null;
    
    // This will be computed in template via async pipe
    // For now, return a synchronous helper that template can use
    // Template will need to use async pipe with boxsetOptions$
    return null; // Will be computed in template
  }

  // Movie selection helpers
  onMovieCleared(): void {
    // User explicitly cleared the selected-film chip (the Change button on
    // the selector) — that's a decision about the suggestion too. Without
    // this mark, the auto-suggestion card would pop back up the moment
    // movie_id flipped to null, asking them to re-confirm a match they
    // just rejected.
    const ctx = this.workflowService.getCurrentContext();
    this.markTmdbSuggestionHandled(ctx?.discInfo?.disc_id);
    this.workflowService.applyMetadataSelectionToActiveContext(
      { movieId: null }
    ).subscribe({
      error: (err) => this.logger.error('Failed to clear movie:', err)
    });
  }
  
  clearMovieSelection(): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext || !currentContext.labelForm) return;
    
    const updatedLabelForm = {
      ...currentContext.labelForm,
      movie_id: null,
      movie_name: null,
      movie_production_year: null,
      movie_cover_url: null,
      movie_cover_path: null
    };
    this.workflowService.updateContext({ labelForm: updatedLabelForm });
    this.movieComboOpen = false;
  }

  onMovieSearchChange(search: string): void {
    this.movieSearch = search || '';
  }

  // Boxset modals
  onBoxsetCreated(boxsetData?: any): void {
    // When boxset selector emits (user submitted the create form in drawer/panel), create the boxset on both mobile and desktop.
    const hasValidData = boxsetData &&
      (boxsetData.name ?? '').toString().trim() &&
      boxsetData.year != null &&
      Number.isInteger(boxsetData.year) &&
      boxsetData.year >= 1000 &&
      boxsetData.year <= 9999;
    if (hasValidData) {
      this.createBoxsetFromData(boxsetData);
      return;
    }
    if (this.isMobile) {
      // Fallback: no valid data passed — show modal to create (e.g. from another entry point)
      this.showBoxsetCreateModal = true;
      this.newBoxset = boxsetData || { name: '', year: null };
      this.boxsetOpen = false;
    }
  }

  /** Put a freshly created/linked boxset release into the active form.
   *
   * Routes through applyMetadataSelectionToActiveContext — the same call the
   * manual boxset selection uses — so the form, the persisted draft and the
   * step-completion state all move together. Name, year and cover are
   * included on purpose: a later autosave sends the whole form, and a form
   * still holding release_name="" would blank the release's real name. */
  applyBoxsetLinkToForm(release: any, boxset: any): void {
    if (!release?.id) return;
    this.workflowService.applyMetadataSelectionToActiveContext({
      releaseId: release.id,
      releaseSlug: release.slug,
      ...(release.name ? { releaseName: release.name } : {}),
      ...(release.release_year != null ? { releaseYear: release.release_year } : {}),
      ...(release.cover_front_url ? { coverFrontUrl: release.cover_front_url } : {}),
      boxsetId: release.boxset_id ?? boxset?.id ?? null,
      boxsetSlug: boxset?.slug ?? (release as any).boxset_slug ?? null,
    }).subscribe({
      next: () => this.afterBoxsetSelectionSaved(),
      error: (err) => {
        this.metadataSaving = false;
        this.logger.error('Failed to apply created boxset to the form:', err);
      },
    });
  }

  private createBoxsetFromData(boxsetData: any): void {
    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext) {
      this.logger.error('No active workflow context');
      return;
    }

    const movieId = currentContext.labelForm?.movie_id;
    if (!movieId) {
      this.logger.error('Movie must be selected before creating a boxset');
      return;
    }

    const discId = currentContext.discInfo?.disc_id || null;
    const mountPoint = currentContext.type === 'drive' ? currentContext.id : null;

    this.metadataService.createBoxsetForDisc(
      discId,
      mountPoint,
      {
        name: boxsetData.name,
        year: boxsetData.year,
        upc: boxsetData.upc,
        asin: boxsetData.asin,
        cover_front_url: boxsetData.cover_front_url,
        cover_back_url: boxsetData.cover_back_url,
      },
      movieId
    ).subscribe({
      next: (result) => {
        // Apply the response to the form. The backend has already created the
        // boxset, a release named after it, and linked this disc — but the
        // WebSocket patch this used to rely on deliberately carries none of
        // those fields for a job context (it protects in-flight edits), so the
        // form kept saying "no boxset" while the server said otherwise. The
        // user then assigned it by hand, and that autosave carried the form's
        // stale release_name="" back to the server, blanking the name the
        // create had just set and disabling Continue on the boxset step.
        this.applyBoxsetLinkToForm(result?.release, result?.boxset);
        this.toastService.show('Boxset created', 'success', 2000);
      },
      error: (err) => {
        this.logger.error('Failed to create boxset:', err);
        this.toastService.show(err?.error?.detail || 'Failed to create boxset', 'error', 3000);
      }
    });
  }
  
  openBoxsetCreateModal(): void {
    this.showBoxsetCreateModal = true;
    this.newBoxset = { name: '', year: null };
  }

  closeBoxsetCreateModal(): void {
    this.showBoxsetCreateModal = false;
    this.newBoxset = { name: '', year: null };
  }

  createBoxset(): void {
    if (!this.newBoxset.name || !this.newBoxset.year) {
      this.logger.error('Boxset name and year are required');
      return;
    }

    const currentContext = this.workflowService.getCurrentContext();
    if (!currentContext) {
      this.logger.error('No active workflow context');
      return;
    }

    const movieId = currentContext.labelForm?.movie_id;
    if (!movieId) {
      this.logger.error('Movie must be selected before creating a boxset');
      return;
    }

    const discId = currentContext.discInfo?.disc_id || null;
    const mountPoint = currentContext.type === 'drive' ? currentContext.id : null;

    // Call MetadataService - it handles creation and list updates
    // Backend will emit workflow_context_updated via WebSocket
    // WorkflowService will automatically update context via handleWorkflowMessage()
    this.metadataService.createBoxsetForDisc(
      discId,
      mountPoint,
      {
        name: this.newBoxset.name,
        year: this.newBoxset.year,
        upc: this.newBoxset.upc,
        asin: this.newBoxset.asin,
        cover_front_url: this.newBoxset.cover_front_url,
        cover_back_url: this.newBoxset.cover_back_url,
      },
      movieId
    ).subscribe({
      next: () => {
        // MetadataService has already updated its lists
        // Backend has emitted workflow_context_updated
        // WorkflowService will automatically update context via WebSocket
        // Just close modal and handle step transition
        this.closeBoxsetCreateModal();
      },
      error: (err) => {
        this.logger.error('Failed to create boxset:', err);
      }
    });
  }

  openBoxsetEditModal(boxset: any, event: Event): void {
    event.stopPropagation();
    // TODO: Implement boxset edit modal
  }

  // TMDB URL visibility
  shouldShowTmdbUrl(context: any): boolean {
    if (!context) return false;
    
    // Never show for DiscDB hits
    if (context.discdbHit) return false;
    
    // Don't show if DiscDB result is still unknown
    if (context.discInfo?.discdb_hit === null || context.discInfo?.discdb_hit === undefined) {
      return false;
    }
    
    // Show if no movie is selected
    return !context.labelForm?.movie_id;
  }

  // Breadcrumb cover: movieCover (TMDB) → releaseCover → fallback. DiscDB hits have no TMDB data so they show release cover.
  getMovieCover(context: WorkflowContext | null): string | null {
    // 1. Movie cover (TMDB): labelForm or movieOptions
    const movieCoverUrl = context?.labelForm?.movie_cover_url || context?.labelForm?.movie_cover_path;
    if (movieCoverUrl) {
      return movieCoverUrl;
    }
    const movieId = context?.labelForm?.movie_id;
    if (movieId && context?.movieOptions) {
      const movie = context.movieOptions.find(m => m.id === movieId);
      if (movie) {
        const url = movie.cover_url || movie.cover_path || null;
        if (url) return url;
      }
    }
    // 2. Release cover (DiscDB / coordinator or labelForm) – only when a movie/release is selected (otherwise breadcrumb would show disc image after "Change")
    const releaseCover = context?.discInfo?.release_image || context?.labelForm?.cover_front_url;
    const hasMovieOrRelease = !!(context?.labelForm?.movie_id || context?.labelForm?.release_id);
    if (hasMovieOrRelease && releaseCover) {
      return releaseCover;
    }
    // 3. Fallback: coordinator merged value or getMovieSummary
    if (context?.movieCover) {
      return context.movieCover;
    }
    const summary = this.getMovieSummary(context);
    return summary?.cover_url || summary?.cover_path || null;
  }

  getMovieName(context: WorkflowContext | null): string | null {
    if (context?.discdbHit && context.movieName) {
      return context.movieName;
    }
    if (context?.labelForm?.movie_name) {
      return context.labelForm.movie_name;
    }
    const movieId = context?.labelForm?.movie_id;
    if (movieId && context?.movieOptions) {
      const movie = context.movieOptions.find(m => m.id === movieId);
      if (movie?.name) {
        return movie.name;
      }
    }
    const infoTitle = (context?.discInfo as any)?.info_title;
    if (infoTitle) {
      return infoTitle;
    }
    return null;
  }

  getProductionYear(context: WorkflowContext | null): number | null {
    if (context?.discdbHit && context.productionYear != null) {
      return context.productionYear;
    }
    if (context?.labelForm?.movie_production_year) {
      return context.labelForm.movie_production_year;
    }
    const movieId = context?.labelForm?.movie_id;
    if (movieId && context?.movieOptions) {
      const movie = context.movieOptions.find(m => m.id === movieId);
      if (movie?.production_year) {
        return movie.production_year;
      }
    }
    return null;
  }

  /** Number of titles for postprocess summary (titleOrder or titles length). */
  getPostprocessTitleCount(context: WorkflowContext | null): number {
    if (!context) return 0;
    const order = context.titleOrder;
    const titles = context.titles;
    if (order && order.length) return order.length;
    if (titles && titles.length) return titles.length;
    return 0;
  }

  /** One-line template for where postprocess will put files (display-only). */
  getPostprocessStructureTemplate(context: WorkflowContext | null): string {
    if (!context) return '';
    const name = context.movieName ?? this.getMovieName(context) ?? '…';
    const year = context.productionYear ?? this.getProductionYear(context);
    const yearPart = year ? ` (${year})` : '';
    if (context.isSeries) {
      return `Series / ${name}${yearPart} / Season NN / …`;
    }
    return `Movies / ${name}${yearPart} /`;
  }

  // ── Cached title-label input helpers (called once per context change, not per CD cycle) ──

  /** Update all cached function references for title-label inputs. Called from context$ subscription. */
  private updateCachedTitleFns(context: WorkflowContext | null): void {
    this.cachedTitleStatusFn = context?.titleStatusFn || this.defaultTitleStatusFn;
    this.cachedTitleProgressValueFn = context?.titleProgressValueFn || this.defaultTitleProgressValueFn;
    this.cachedTitleActiveFn = context?.titleActiveFn || this.defaultTitleActiveFn;
    this.cachedPreviewUrlFn = context?.previewUrlFn || this.defaultPreviewUrlFn;
    this.cachedPreviewStateFn = context?.previewStateFn || this.defaultPreviewStateFn;
    this.cachedTitlePathFn = context?.titlePathFn || this.defaultTitlePathFn;
    this.cachedRetryPreviewFn = this.buildRetryPreviewFn(context);
    this.cachedTitleProgress = this.computeTitleProgress(context);
  }

  /** Build retry preview closure once per context (not per CD cycle). */
  private buildRetryPreviewFn(context: WorkflowContext | null): (t: any) => void {
    if (!context) return () => {};
    return (t: any) => {
      const jobId = context?.jobStatus?.jobId;
      if (!jobId) return;
      const trackKey = t?.title_id;
      if (!trackKey) return;
      this.jobService.retryPreviewTrack(jobId, trackKey).subscribe({
        next: () => {
          // Preview will transition to queued state via next WebSocket status update
        },
        error: (err: any) => {
          console.error('Failed to retry preview:', err);
        },
      });
    };
  }

  /** Compute title progress object once per context change (not per CD cycle). */
  private computeTitleProgress(context: WorkflowContext | null): Record<string, number> {
    if (!context?.titleProgressValueFn || !context?.titles) return {};
    const progress: Record<string, number> = {};
    for (const title of context.titles) {
      const titleId = title.title_id;
      if (titleId) {
        progress[titleId] = context.titleProgressValueFn(titleId) || 0;
      }
    }
    return progress;
  }

  // ── Legacy title helpers (kept for any external callers; template now uses cached properties) ──

  getTitleProgress(context: WorkflowContext | null): Record<string, number> {
    return this.computeTitleProgress(context);
  }

  getTitleStatusFn(context: WorkflowContext | null): (id: string | null | undefined) => string {
    return context?.titleStatusFn || this.defaultTitleStatusFn;
  }

  getTitleProgressValueFn(context: WorkflowContext | null): (id: string | null | undefined) => number {
    return context?.titleProgressValueFn || this.defaultTitleProgressValueFn;
  }

  getTitleActiveFn(context: WorkflowContext | null): (id: string | null | undefined) => boolean {
    return context?.titleActiveFn || this.defaultTitleActiveFn;
  }

  getPreviewUrlFn(context: WorkflowContext | null): (t: any) => string | null {
    return context?.previewUrlFn || this.defaultPreviewUrlFn;
  }

  getPreviewStateFn(context: WorkflowContext | null): (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null {
    return context?.previewStateFn || this.defaultPreviewStateFn;
  }

  getRetryPreviewFn(context: WorkflowContext | null): (t: any) => void {
    return this.buildRetryPreviewFn(context);
  }

  getTitlePathFn(context: WorkflowContext | null): (t: any) => string | null {
    return context?.titlePathFn || this.defaultTitlePathFn;
  }

  // ── Preview regeneration helpers ──

  /** Entity-based label progress (duplicate groups count once). */
  getTitlesLabelStats(context: WorkflowContext | null): TitleLabelStats {
    return computeTitleLabelStats(context?.titles);
  }

  /** For template: sort type breakdown given stats snapshot from `as tls`. */
  sortedTitleTypeRows(byType: Record<string, number>): { type: string; count: number }[] {
    return sortTitleStatsTypeEntries(byType || {});
  }

  /** Count of failed previews for the current context (for "Regenerate Previews (N)" button). */
  getFailedPreviewCount(context: WorkflowContext | null): number {
    const previews = (context?.jobStatus as any)?.disc_payload?.previews;
    if (!previews?.tracks || typeof previews.tracks !== 'object') return 0;
    return Object.values(previews.tracks).filter(
      (t: any) => t?.status === 'failed'
    ).length;
  }

  /** Batch regenerate all failed previews for the current job. */
  regenerateAllPreviews(context: WorkflowContext | null): void {
    const jobId = context?.jobStatus?.jobId;
    if (!jobId || this.regeneratingPreviews) return;
    this.regeneratingPreviews = true;
    this.jobService.regenerateAllPreviews(jobId).subscribe({
      next: () => {
        this.regeneratingPreviews = false;
        this.toastService.show('Preview regeneration started', 'success', 2000);
        // Preview status will update via WebSocket progress/context_changed events
      },
      error: (err) => {
        this.regeneratingPreviews = false;
        this.logger.error('[WorkflowLabelingComponent] Batch preview regeneration failed', err);
        this.toastService.show(err?.error?.detail || 'Failed to regenerate previews', 'error', 3000);
      }
    });
  }

  /** Queue ffprobe + padding detection for all ripped titles (force; same idea as per-title regenerate). */
  rescanTitlesDetection(context: WorkflowContext | null): void {
    const jobId = context?.jobStatus?.jobId;
    if (!jobId || this.rescanningTitles) return;
    this.rescanningTitles = true;
    this.jobService.regenerateJobDetection(jobId, { force: true }).subscribe({
      next: (res) => {
        this.rescanningTitles = false;
        const n = typeof res?.count === 'number' ? res.count : res?.titles?.length ?? 0;
        this.toastService.show(
          n > 0 ? `Title rescan queued (${n} title${n === 1 ? '' : 's'})` : 'Title rescan queued',
          'success',
          2500
        );
      },
      error: (err) => {
        this.rescanningTitles = false;
        this.logger.error('[WorkflowLabelingComponent] Title detection rescan failed', err);
        this.toastService.show(err?.error?.detail || 'Failed to queue title rescan', 'error', 3500);
      }
    });
  }

  // Step validation methods
  canContinue(context: WorkflowContext | null): boolean {
    if (!context) return false;
    const step = context.labelForm?.workflow_step || 'film';
    
    switch (step) {
      case 'film':
        return !!context.labelForm?.movie_id;
      case 'boxset':
        return this.isReleaseComplete(context);
      case 'disc':
        return this.isDiscComplete(context);
      case 'titles':
        return this.areTitlesComplete(context);
      case 'postprocess':
        // Can continue if post-process is completed
        const postState = context.jobStatus?.post_state || context.jobStatus?.pipeline?.['postprocess'];
        return postState === 'completed';
      case 'transfer':
        return false; // Can't continue past transfer
      default:
        return true;
    }
  }

  isDiscComplete(context: WorkflowContext | null): boolean {
    if (!context?.labelForm) return false;
    const form = context.labelForm;
    return !!(form.disc_name && form.disc_format);
  }

  areTitlesComplete(context: WorkflowContext | null): boolean {
    return areLabelTitlesComplete(context?.titles);
  }

  isReleaseComplete(context: WorkflowContext | null): boolean {
    // #580: a linked release is not enough — the required user-fillable
    // fields (release_name, release_slug, release_year) must also be
    // populated before Continue is enabled. The canonical predicate lives
    // in label-form.service.ts so this component, getStepCompletionState,
    // and validateStepCompletion all share one definition.
    return isReleaseSufficientlyComplete(context?.labelForm ?? null);
  }

  // Presentation helper methods
  getMovieSummary(context: WorkflowContext | null): MovieSummary | null {
    if (!context) return null;
    const movieId = context.labelForm?.movie_id;
    if (movieId && context.movieOptions?.length) {
      const found = context.movieOptions.find(m => m.id === movieId);
      if (found) return found;
    }
    if (context.discdbHit && (context.movieName || context.movieCover)) {
      return {
        id: '',
        name: context.movieName ?? '—',
        production_year: context.productionYear ?? undefined,
        cover_url: context.movieCover ?? undefined,
        cover_path: undefined,
      } as MovieSummary;
    }
    return null;
  }

  getReleaseSummary(context: WorkflowContext | null): any {
    if (!context?.labelForm) return null;
    const form = context.labelForm;
    return {
      name: form.release_name,
      slug: form.release_slug,
      year: form.release_year,
      upc: form.upc,
      asin: form.asin,
      cover_front_url: form.cover_front_url,
      cover_back_url: form.cover_back_url,
    };
  }

  formatBytes(bytes: number): string {
    if (!bytes) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
  }

  formatSpeed(mbps: number): string {
    if (mbps == null || mbps <= 0) return '—';
    return `${mbps.toFixed(1)} MB/s`;
  }

  /** Estimate remaining time from bytes transferred, total bytes, and speed (MB/s). Returns human-readable string. */
  estimateTransferEta(bytesTransferred: number, totalBytes: number, speedMbps: number): string {
    if (!speedMbps || speedMbps <= 0 || totalBytes == null || totalBytes <= 0 || bytesTransferred >= totalBytes) return '—';
    const remainingBytes = totalBytes - bytesTransferred;
    const remainingMb = remainingBytes / (1024 * 1024);
    const seconds = Math.max(0, Math.ceil(remainingMb / speedMbps));
    if (seconds < 60) return `${seconds}s`;
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    if (mins < 60) return secs ? `${mins}m ${secs}s` : `${mins}m`;
    const hours = Math.floor(mins / 60);
    const m = mins % 60;
    return m ? `${hours}h ${m}m` : `${hours}h`;
  }

  // Post-process helper methods
  private updatePostProcessStatus(context: WorkflowContext): void {
    const postProcessStatus = this.jobService.getPostProcessStatus(context.jobStatus, context.titles || []);
    const allFiles = [
      ...postProcessStatus.pending,
      ...postProcessStatus.inProgress,
      ...postProcessStatus.completed,
    ];
    
    // Update context with post-process files
    this.workflowService.updateContext({
      postProcessFiles: allFiles
    });
  }

  getCurrentDiscFolderTree(context: WorkflowContext | null): FolderTree {
    if (!context?.postProcessFiles || context.postProcessFiles.length === 0) {
      return {};
    }
    return this.buildFolderTree(context.postProcessFiles);
  }

  buildFolderTree(files: PostProcessFile[]): FolderTree {
    // Filter out ignored titles first
    const nonIgnoredFiles = files.filter(f => !f.isIgnored);
    
    const tree: FolderTree = {};
    
    for (const file of nonIgnoredFiles) {
      // Use folderPath if available, otherwise extract from relativePath
      const folderPath = file.folderPath || (file.relativePath ? file.relativePath.split('/').slice(0, -1).join('/') : '');
      
      if (!folderPath) {
        // File at root level
        if (!tree['']) {
          tree[''] = { files: [], subfolders: {} };
        }
        tree[''].files.push(file);
      } else {
        // File in a folder - build nested structure
        const parts = folderPath.split('/').filter(p => p);
        if (parts.length === 0) {
          // Empty folder path means root
          if (!tree['']) {
            tree[''] = { files: [], subfolders: {} };
          }
          tree[''].files.push(file);
        } else {
          // Build nested structure
          let current = tree;
          for (let i = 0; i < parts.length; i++) {
            const part = parts[i];
            const pathSoFar = parts.slice(0, i + 1).join('/');
            
            if (!current[pathSoFar]) {
              current[pathSoFar] = { files: [], subfolders: {} };
            }
            
            if (i === parts.length - 1) {
              // Last part - add file here
              current[pathSoFar].files.push(file);
            } else {
              // Intermediate folder - navigate deeper
              current = current[pathSoFar].subfolders;
            }
          }
        }
      }
    }
    
    return tree;
  }

  getFilesFromTree(tree: FolderTree): Array<{ file: PostProcessFile | null; indent: number; folderPath: string; isFolder: boolean; folderName?: string }> {
    const result: Array<{ file: PostProcessFile | null; indent: number; folderPath: string; isFolder: boolean; folderName?: string }> = [];
    
    const traverse = (subtree: FolderTree, indent: number, parentPath: string) => {
      // Sort folders for consistent display
      const sortedFolders = Object.keys(subtree).sort();
      
      for (const folderPath of sortedFolders) {
        const node = subtree[folderPath];
        const fullPath = parentPath ? `${parentPath}/${folderPath}` : folderPath;
        const folderName = folderPath.split('/').pop() || folderPath;
        
        // Add folder header if it's not root and has files or subfolders
        if (folderPath && (node.files.length > 0 || Object.keys(node.subfolders).length > 0)) {
          result.push({ 
            file: null, 
            indent, 
            folderPath: fullPath, 
            isFolder: true,
            folderName 
          });
        }
        
        // Add files in this folder
        for (const file of node.files) {
          result.push({ 
            file, 
            indent: folderPath ? indent + 1 : indent, 
            folderPath: fullPath, 
            isFolder: false 
          });
        }
        
        // Recursively add subfolders
        if (Object.keys(node.subfolders).length > 0) {
          traverse(node.subfolders, folderPath ? indent + 1 : indent, fullPath);
        }
      }
    };
    
    traverse(tree, 0, '');
    return result;
  }

  getReleaseDiscPostState(disc: any): 'completed' | 'running' | 'pending' | 'failed' {
    const latest = disc?.latest_job_status;
    const postState = latest?.post_state || latest?.pipeline?.postprocess || latest?.pipeline?.['postprocess'];
    if (postState === 'completed') return 'completed';
    if (postState === 'running') return 'running';
    if (postState === 'failed') return 'failed';
    if (latest?.post_paths || disc?.artifacts?.post_paths) return 'completed';
    return 'pending';
  }

  getReleaseDiscPostPaths(disc: any): Record<string, string> | null {
    return disc?.latest_job_status?.post_paths || disc?.artifacts?.post_paths || null;
  }

  /** Transfer state for a disc (from job_status / latest_job_status). */
  getReleaseDiscTransferState(disc: any): 'completed' | 'running' | 'pending' | 'failed' {
    const js = disc?.job_status ?? disc?.latest_job_status;
    const t = js?.transfer_state ?? js?.pipeline?.transfer ?? js?.pipeline?.['transfer'];
    if (t === 'completed') return 'completed';
    if (t === 'running') return 'running';
    if (t === 'failed') return 'failed';
    return 'pending';
  }

  /** Aggregate transfer stats from releaseDiscs or single job. */
  getTransferStats(context: WorkflowContext | null): { total: number; completed: number; running: number; pending: number; failed: number } {
    const releaseDiscs = context?.releaseDiscs || [];
    if (!releaseDiscs.length) {
      const t = context?.jobStatus?.transfer_state ?? context?.jobStatus?.pipeline?.['transfer'];
      const completed = t === 'completed' ? 1 : 0;
      const running = t === 'running' ? 1 : 0;
      const failed = t === 'failed' ? 1 : 0;
      const pending = t ? 0 : (context?.jobStatus ? 1 : 0);
      return { total: context?.jobStatus ? 1 : 0, completed, running, pending, failed };
    }
    let completed = 0, running = 0, pending = 0, failed = 0;
    for (const disc of releaseDiscs) {
      const state = this.getReleaseDiscTransferState(disc);
      if (state === 'completed') completed += 1;
      else if (state === 'running') running += 1;
      else if (state === 'failed') failed += 1;
      else pending += 1;
    }
    return { total: releaseDiscs.length, completed, running, pending, failed };
  }

  getTransferOverallPercent(context: WorkflowContext | null): number {
    if (!context?.releaseDiscs?.length && context?.jobStatus?.transfer_progress != null) {
      return Math.round(Number(context.jobStatus.transfer_progress));
    }
    const stats = this.getTransferStats(context);
    if (!stats.total) return 0;
    return Math.round((stats.completed / stats.total) * 100);
  }

  getReleaseDiscFolderTree(disc: any): FolderTree | null {
    const postPaths = this.getReleaseDiscPostPaths(disc);
    if (!postPaths) return null;
    const files: PostProcessFile[] = Object.entries(postPaths).map(([titleId, path]) => ({
      name: titleId,
      path: path as string,
      relativePath: (path as string).replace(/^.*?transient[\/\\]/, '').replace(/^[\/\\]+/, ''),
      folderPath: (path as string).split(/[\/\\]/).slice(0, -1).join('/'),
      fileName: (path as string).split(/[\/\\]/).pop() || titleId,
      status: 'completed' as const,
      isIgnored: false,
    }));
    return this.buildFolderTree(files);
  }

  getReleaseDiscStats(context: WorkflowContext | null): { total: number; completed: number; running: number; pending: number; failed: number } {
    const releaseDiscs = context?.releaseDiscs || [];
    if (!releaseDiscs.length) {
      const postState = context?.jobStatus?.post_state || context?.jobStatus?.pipeline?.['postprocess'];
      const completed = postState === 'completed' ? 1 : 0;
      const running = postState === 'running' ? 1 : 0;
      const failed = postState === 'failed' ? 1 : 0;
      const pending = postState ? 0 : (context?.jobStatus ? 1 : 0);
      return { total: context?.jobStatus ? 1 : 0, completed, running, pending, failed };
    }

    let completed = 0;
    let running = 0;
    let pending = 0;
    let failed = 0;
    for (const disc of releaseDiscs) {
      const state = this.getReleaseDiscPostState(disc);
      if (state === 'completed') completed += 1;
      else if (state === 'running') running += 1;
      else if (state === 'failed') failed += 1;
      else pending += 1;
    }
    return { total: releaseDiscs.length, completed, running, pending, failed };
  }

  getReleaseOverallPercent(context: WorkflowContext | null): number {
    if (!context?.releaseDiscs?.length && context?.jobStatus?.post_progress != null) {
      return Math.round(context.jobStatus.post_progress);
    }
    const stats = this.getReleaseDiscStats(context);
    if (!stats.total) return 0;
    return Math.round((stats.completed / stats.total) * 100);
  }

  /** Post progress for the current disc (when showing in-row progress). */
  getReleaseDiscPostProgress(context: WorkflowContext | null): number {
    const p = context?.jobStatus?.post_progress;
    return p != null ? Math.round(p) : this.getReleaseOverallPercent(context);
  }

  isCurrentReleaseDisc(disc: any, context: WorkflowContext | null): boolean {
    const currentDiscId = context?.jobStatus?.disc_id || context?.discInfo?.disc_id || null;
    return !!currentDiscId && disc?.disc_id === currentDiscId;
  }

  isBoxsetContext(context: WorkflowContext | null): boolean {
    return !!(
      context?.lastReleaseDetails?.boxset_id ||
      context?.jobStatus?.boxset_id ||
      context?.labelForm?.boxset_id
    );
  }

  canExpandReleaseDisc(disc: any): boolean {
    const state = this.getReleaseDiscPostState(disc);
    return (state === 'completed' || state === 'running') && !!this.getReleaseDiscPostPaths(disc);
  }

  toggleReleaseDisc(discId: string, canExpand: boolean): void {
    if (!canExpand) return;
    this.expandedPostprocessDiscId = this.expandedPostprocessDiscId === discId ? null : discId;
  }

  isReleaseDiscExpanded(discId: string): boolean {
    return this.expandedPostprocessDiscId === discId;
  }

  toggleTransferDisc(discId: string, canExpand: boolean): void {
    if (!canExpand) return;
    this.expandedTransferDiscId = this.expandedTransferDiscId === discId ? null : discId;
  }

  /** True if this transfer row is expanded (user toggled). All discs use the same toggle state. */
  isTransferDiscRowExpanded(context: WorkflowContext | null, disc: any): boolean {
    return this.expandedTransferDiscId === disc?.disc_id;
  }

  canExpandTransferDisc(disc: any): boolean {
    const state = this.getReleaseDiscTransferState(disc);
    return state === 'completed' || state === 'running';
  }

  /** Transfer retry count for a disc (current job from context, or disc.job_status / latest_job_status). */
  getTransferRetryCount(disc: any, context: WorkflowContext | null): number {
    if (this.isCurrentReleaseDisc(disc, context) && context?.jobStatus?.transfer_retry_count != null) {
      return Number(context.jobStatus.transfer_retry_count);
    }
    const js = disc?.job_status ?? disc?.latest_job_status;
    return js?.transfer_retry_count != null ? Number(js.transfer_retry_count) : 0;
  }

  /** Transfer max retries for a disc. */
  getTransferMaxRetries(disc: any, context: WorkflowContext | null): number | null {
    if (this.isCurrentReleaseDisc(disc, context) && context?.jobStatus?.transfer_max_retries != null) {
      return Number(context.jobStatus.transfer_max_retries);
    }
    const js = disc?.job_status ?? disc?.latest_job_status;
    return js?.transfer_max_retries != null ? Number(js.transfer_max_retries) : null;
  }

  /**
   * Ordered list for the combined disc card: current disc first, then others by disc_number.
   * If there are no releaseDiscs but we have a current job (e.g. single-disc), returns one synthetic row.
   */
  getOrderedReleaseDiscs(context: WorkflowContext | null): any[] {
    const releaseDiscs = context?.releaseDiscs || [];
    const currentDiscId = context?.jobStatus?.disc_id || context?.discInfo?.disc_id || null;

    if (releaseDiscs.length > 0) {
      const current = currentDiscId ? releaseDiscs.find((d: any) => d.disc_id === currentDiscId) : null;
      const others = releaseDiscs
        .filter((d: any) => d.disc_id !== currentDiscId)
        .sort((a: any, b: any) => (a.disc_number ?? 0) - (b.disc_number ?? 0));
      return current ? [current, ...others] : others;
    }

    if (context?.jobStatus && currentDiscId) {
      const payload = context.jobStatus.disc_payload || {};
      return [
        {
          disc_id: currentDiscId,
          disc_number: context.labelForm?.disc_number ?? context.discInfo?.disc_number ?? payload.disc_number,
          disc_name: context.labelForm?.disc_name ?? context.discInfo?.disc_name ?? payload.disc_name ?? 'Untitled Disc',
          disc_format: payload.disc_format,
          latest_job_status: context.jobStatus,
        },
      ];
    }

    return [];
  }

  /** True if this row is expanded (user toggled). All discs use the same toggle state. */
  isCurrentDiscRowExpanded(context: WorkflowContext | null, disc: any): boolean {
    return this.expandedPostprocessDiscId === disc?.disc_id;
  }

  trackByDiscId(_index: number, disc: { disc_id?: string }): string {
    return disc?.disc_id ?? '';
  }
  trackByFileTreeItem(index: number): number {
    return index;
  }

  getCurrentDiscLabel(context: WorkflowContext | null): string {
    const discNumber =
      context?.labelForm?.disc_number ?? context?.discInfo?.disc_number ?? null;
    const discName =
      context?.labelForm?.disc_name ??
      context?.discInfo?.disc_name ??
      context?.jobStatus?.disc_payload?.disc_name ??
      null;
    if (discNumber != null || discName) {
      return `Disc ${discNumber != null ? discNumber : ''}${discName ? `: ${discName}` : ''}`.trim();
    }
    return 'Current disc';
  }

  /**
   * Non-blocking hint: release/boxset chosen but disc number not on labelForm or discInfo yet (backend may still normalize).
   */
  isDiscNumberAssigning(context: WorkflowContext | null): boolean {
    const lf = context?.labelForm;
    if (!lf) return false;
    const hasTarget = !!(lf.release_id || (lf.boxset_id && lf.boxset_id !== '__pending__'));
    if (!hasTarget) return false;
    const n = lf.disc_number ?? context?.discInfo?.disc_number;
    return n === null || n === undefined;
  }

  getCompletedDiscsInRelease(context: WorkflowContext | null): Array<{
    disc: any;
    postState: string;
    postPaths?: Record<string, string>;
    folderStructure?: FolderTree;
  }> {
    if (!context?.releaseDiscs || context.releaseDiscs.length === 0) {
      return [];
    }

    return context.releaseDiscs.map((disc: any) => {
      // Check if disc has latest_job_status that indicates post-processing completion
      const postState = disc.latest_job_status?.post_state || 
                       (disc.artifacts?.post_paths ? 'completed' : 'pending');
      
      // Use post_paths (title_id -> relative_path) instead of final_paths
      const postPaths = disc.artifacts?.post_paths || disc.latest_job_status?.post_paths;
      let folderStructure: FolderTree | undefined;
      
      if (postPaths) {
        // Convert post_paths to PostProcessFile format and build tree
        // post_paths keys are title_id (UUID), values are relative paths
        const files: PostProcessFile[] = Object.entries(postPaths).map(([titleId, path]) => ({
          name: titleId,  // Use title_id as the key/name
          path: path as string,
          relativePath: (path as string).replace(/^.*?transient[\/\\]/, '').replace(/^[\/\\]+/, ''),
          folderPath: (path as string).split(/[\/\\]/).slice(0, -1).join('/'),
          fileName: (path as string).split(/[\/\\]/).pop() || titleId,
          status: 'completed' as const,
          isIgnored: false,
        }));
        folderStructure = this.buildFolderTree(files);
      }
      
      return {
        disc,
        postState,
        postPaths,
        folderStructure,
      };
    }).filter(item => item.postState === 'completed');
  }

  getTotalFileCount(context: WorkflowContext | null): number {
    if (!context?.postProcessFiles) return 0;
    return context.postProcessFiles.filter(f => !f.isIgnored).length;
  }

  getCompletedFileCount(context: WorkflowContext | null): number {
    if (!context?.postProcessFiles) return 0;
    return context.postProcessFiles.filter(f => f.status === 'completed' && !f.isIgnored).length;
  }

  getInProgressFileCount(context: WorkflowContext | null): number {
    if (!context?.postProcessFiles) return 0;
    return context.postProcessFiles.filter(f => f.status === 'processing' && !f.isIgnored).length;
  }

  // #329: Re-rename files on transfer step when title metadata changed
  previewRenameOnTransfer(): void {
    this.renameLoading = true;
    this.renameError = null;
    this.renamePreview = null;
    this.renameExecuted = false;
    this.workflowService.getActiveContext().pipe(take(1)).subscribe(ctx => {
      const discId = ctx?.discInfo?.disc_id;
      if (!discId) { this.renameLoading = false; return; }
      this.workflowService.previewRename(discId).subscribe({
        next: (res: any) => {
          this.renamePreview = res.results || [];
          this.renameLoading = false;
        },
        error: (err: any) => {
          this.renameError = err?.error?.detail || err?.message || 'Failed to preview renames';
          this.renameLoading = false;
        },
      });
    });
  }

  executeRenameOnTransfer(): void {
    this.renameLoading = true;
    this.renameError = null;
    this.workflowService.getActiveContext().pipe(take(1)).subscribe(ctx => {
      const discId = ctx?.discInfo?.disc_id;
      if (!discId) { this.renameLoading = false; return; }
      this.workflowService.executeRename(discId).subscribe({
        next: (res: any) => {
          this.renamePreview = res.results || [];
          this.renameLoading = false;
          this.renameExecuted = true;
          this.toastService.show('Files renamed successfully', 'success');
        },
        error: (err: any) => {
          this.renameError = err?.error?.detail || err?.message || 'Failed to rename files';
          this.renameLoading = false;
        },
      });
    });
  }

  get renameChangedCount(): number {
    return this.renameSummary.changed;
  }

  /** Display-only basename for the rename preview rows. The backend ships
   * absolute paths; the row layout only has space for the filename, so
   * the rest is trimmed (the dry-run preview table doesn't gain from
   * showing the full transient path twice per row). */
  renameBasename(path: string | null | undefined): string {
    if (!path) return '?';
    const idx = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
    return idx >= 0 ? path.slice(idx + 1) : path;
  }

  /** [1..n] inclusive — options for the disc-season selects (moved here from
   * the breadcrumb; #830's Specials=0 option is appended in the template). */
  seasonsRange(n: number | null): number[] {
    if (!n || n < 1) return [];
    return Array.from({ length: n }, (_, i) => i + 1);
  }
}

// Folder tree interface for post-process file organization
interface FolderTree {
  [folderPath: string]: {
    files: PostProcessFile[];
    subfolders: FolderTree;
  };
}
