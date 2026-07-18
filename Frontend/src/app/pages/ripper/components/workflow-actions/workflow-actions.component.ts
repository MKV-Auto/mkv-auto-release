import { Component, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { combineLatest, Observable, Subscription, asyncScheduler, BehaviorSubject } from 'rxjs';
import { map, distinctUntilChanged, take, observeOn } from 'rxjs/operators';
import {
  DrivePresenceState,
  computeDrivePresenceState,
} from '../../../../utils/drive-presence-state.util';
import { DriveSnapshotService } from '../../../../services/drive-snapshot.service';
import {
  DiscMetadata,
  WorkflowService,
  WorkflowContext,
  WorkflowStep,
  StageProgressValues,
  StageCompletionValues,
} from '../../../../services/workflow.service';
import { getStepOrderForContext } from '../../../../services/workflow-step-order.util';
// RipperStateService removed - using WorkflowService
import { WorkflowActionBarComponent, StageTimelineItem, StageKey } from '../../../../components/workflow-action-bar/workflow-action-bar.component';
import { LoggerService } from '../../../../services/logger.service';
import { ToastService, formatHttpErrorDetail } from '../../../../services/toast.service';
import { Router } from '@angular/router';
import { SetupModalService } from '../../../../services/setup-modal.service';
import { RipSizeWarningService } from '../../../../services/rip-size-warning.service';
import {
  UsbSaturationWarningPayload,
  UsbSaturationWarningService,
} from '../../../../services/usb-saturation-warning.service';
import { isNoActiveTransferConfigError, isTransferConfigOrPathError } from '../../../../services/job.service';
import {
  isAmbiguousStartRipTransportError,
  START_RIP_AMBIGUOUS_RESPONSE_COPY,
  startRipFailureVerb,
} from '../../../../utils/start-rip-error.util';

@Component({
  selector: 'app-workflow-actions',
  standalone: true,
  imports: [CommonModule, WorkflowActionBarComponent],
  templateUrl: './workflow-actions.component.html',
})
export class WorkflowActionsComponent implements OnInit, OnDestroy {
  private subscriptions = new Subscription();
  /** Exposed as observable so canContinue$ and buttonSpinner$ can react to in-flight Continue actions. */
  private _continueInProgress$ = new BehaviorSubject<boolean>(false);
  private get _continueInProgress(): boolean { return this._continueInProgress$.value; }
  private set _continueInProgress(v: boolean) { this._continueInProgress$.next(v); }

  // State from services (initialized in constructor)
  context$!: Observable<WorkflowContext | null>;
  stageTimeline$!: Observable<StageTimelineItem[]>;
  activeStage$!: Observable<StageKey | null>;
  canContinue$!: Observable<boolean>;
  /** Human-readable reason the Continue button is disabled (or null when
   * enabled). Surfaced as a tooltip on the disabled button so the user
   * knows why they can't progress (e.g. "Waiting for copy to finish before
   * titles can be labeled"). Mirrors the gates in ``canContinue$``. */
  disabledReason$!: Observable<string | null>;
  canGoBack$!: Observable<boolean>;
  buttonText$!: Observable<string>;
  buttonSpinner$!: Observable<boolean>;
  /** True at `awaiting_segment_order` (and any future stages where an
   * embedded step component owns the primary CTA). When true, the
   * action-bar suppresses its Continue button so the user isn't
   * pulled toward a disabled "Order segments" CTA instead of the
   * embedded Submit. */
  hideContinue$!: Observable<boolean>;
  stageProgress$!: Observable<StageProgressValues>;
  isStageCompleted$!: Observable<StageCompletionValues>;

  /** #571 — drive-presence tri-state for the CTA. Subscribed by both
   * canContinue$ and buttonText$ so the disabled-state and label stay in
   * lockstep. Emits 'available' until the first snapshot+discs pair lands.
   */
  private drivePresenceState$!: Observable<DrivePresenceState>;

  constructor(
    private workflowService: WorkflowService,
    // ripperStateService removed - using workflowService
    private logger: LoggerService,
    private toast: ToastService,
    private router: Router,
    private setupModalSvc: SetupModalService,
    private ripSizeWarningSvc: RipSizeWarningService,
    private driveSnapshotSvc: DriveSnapshotService,
    private usbSaturationSvc: UsbSaturationWarningService,
  ) {
    // #571 — start the snapshot poll on first action-bar mount. ``providedIn:
    // 'root'`` means restarting is a no-op for downstream subscribers.
    this.driveSnapshotSvc.startPolling();

    this.drivePresenceState$ = combineLatest([
      this.workflowService.getSelectedCard$(),
      this.workflowService.discs$,
      this.driveSnapshotSvc.drives$,
    ]).pipe(
      map(([selectedCard, discs, driveSnapshot]) =>
        computeDrivePresenceState({ selectedCard, discs, driveSnapshot }),
      ),
      distinctUntilChanged(),
    );

    // Initialize observables after services are injected
    // Note: canContinue$ uses getActiveContext() directly to avoid duplicate subscriptions
    this.context$ = this.workflowService.getActiveContext();
    
    this.stageTimeline$ = this.context$.pipe(
      map((context: WorkflowContext | null) => {
        if (!context) return [];
        
        // Build pipeline stages (not workflow steps)
        const stages: StageTimelineItem[] = [];
        const discdbHit = context.discdbHit;
        const discMode = context.discMode || 'copy';
        
        // Stage 1: Copy/Rip
        stages.push({
          key: 'rip',
          label: discMode === 'rip' ? 'Archive' : 'Copy'
        });
        
        // Stage 2: Label (only for DiscDB miss)
        if (!discdbHit) {
          stages.push({
            key: 'label',
            label: 'Label'
          });
        }

        // Stage 3: Transfer (collapsed — covers preparing → transferring → verifying
        // sub-phases via stage-progress-bar's transferPhaseLabel). #365 Phase 2 § 6.4:
        // the standalone Post-Process card was dropped here; the work still runs but
        // is shown as the Transfer step's "Preparing files…" sub-phase.
        stages.push({
          key: 'transfer',
          label: 'Transfer'
        });

        // Stage 4: Done (complete when transfer is complete)
        stages.push({
          key: 'done',
          label: 'Done'
        });
        
        return stages;
      })
    );

    this.activeStage$ = this.context$.pipe(
      map((context: WorkflowContext | null) => {
        // Default to 'rip' if no context (pre-rip state)
        if (!context) return 'rip';
        
        // Use activeStage from context if available (from ripper-page)
        if (context.activeStage) {
          return context.activeStage;
        }
        
        // Fallback: Determine active stage from job status
        const jobStatus = context.jobStatus;
        // If no job status yet, default to 'rip' (pre-rip state)
        if (!jobStatus) return 'rip';
        
        const transferState = jobStatus.transfer_state ?? jobStatus.pipeline?.['transfer'];
        const postState = jobStatus.post_state || jobStatus.pipeline?.['postprocess'];
        const ripState = jobStatus.rip_state || jobStatus.pipeline?.['rip'] || jobStatus.job_status;
        const labelState = jobStatus.label_state || jobStatus.pipeline?.['label'];
        
        // #365 Phase 2 § 6.4 — postprocess collapsed into transfer's preparing
        // sub-phase. activeStage='postprocess' would point at a stage card that
        // no longer exists in stageTimeline$; route those states to 'transfer'
        // so the (now-collapsed) stage card stays highlighted while the
        // transferPhaseLabel renders "Preparing files…".
        if (transferState === 'running' || transferState === 'completed') return 'transfer';
        if (transferState === 'failed') return 'transfer';
        if (postState === 'running' || postState === 'completed') return 'transfer';
        if (postState === 'failed') return 'transfer';
        if (ripState === 'completed' && (labelState === 'completed' || context.discdbHit)) return 'transfer';
        if (ripState === 'completed' && labelState !== 'completed' && !context.discdbHit) return 'label';
        if (ripState === 'running' || ripState === 'pending') return 'rip';
        if (ripState === 'failed') return 'rip';
        if (!context.discdbHit && labelState !== 'completed') return 'label';
        
        return 'rip';
      })
    );

    // Use a single subscription to getActiveContext to avoid duplicate subscriptions and timing issues
    const activeContext$ = this.workflowService.getActiveContext();
    this.canContinue$ = combineLatest([
      activeContext$,
      activeContext$.pipe(
        map((context: WorkflowContext | null) => !(context?.jobStatus?.job_status === 'running' || context?.jobStatus?.job_status === 'pending'))
      ),
      this.workflowService.getStartRipInProgress$(),
      this.workflowService.getFilmStepSaveInProgress$(),
      this._continueInProgress$,
      this.drivePresenceState$,
    ]).pipe(
      map(([context, canStartRip, startRipInProgress, filmStepSaveInProgress, continueInProgress, presence]: [WorkflowContext | null, boolean, boolean, boolean, boolean, DrivePresenceState]) => {
        // Disable button if any Continue/advance action is in progress
        if (continueInProgress) {
          return false;
        }
        // Disable button if startRip is in progress
        if (startRipInProgress) {
          return false;
        }

        // #571 — disable when the selected card refers to a disc that isn't
        // currently loaded, EXCEPT while a rip is actively running (so
        // "Copying NN%" stays disabled-with-progress rather than swapping
        // to "Insert Disc"). The label change is handled in buttonText$.
        if (presence !== 'available') {
          const isRipping =
            context?.jobStatus?.job_status === 'running' ||
            context?.jobStatus?.job_status === 'pending';
          if (!isRipping) {
            return false;
          }
        }

        if (!context) return false;

        const step = this.workflowService.getEffectiveWorkflowStep(context) || 'film';
        const discdbHit = context.discdbHit;
        const jobStatus = context.jobStatus;

        // Film / boxset: disable primary action while saving label workflow context (movie or release/boxset PATCH)
        if ((step === 'film' || step === 'boxset') && !discdbHit && filmStepSaveInProgress) {
          return false;
        }
        
        // Transfer: allow Start Transfer (pre) or Finish (completed); disallow while transferring
        if (step === 'transfer') {
          const transferState = jobStatus?.transfer_state ?? jobStatus?.pipeline?.['transfer'];
          const isTransferRunning = transferState === 'running';
          if (isTransferRunning) return false;
          return true; // Start Transfer (pre) or Finish (completed)
        }

        // Check prior steps are complete
        // Use precomputed stepCompletionState from context (computed reactively in updateContext)
        // Fallback to getStepCompletionState if not available (for backwards compatibility)
        const completionState = context.stepCompletionState || this.workflowService.getStepCompletionState(context);
        
        // For titles step, use stepCompletionState.titles as the source of truth (it's updated reactively via WebSocket)
        // validateStepCompletion checks labelForm.tracks which may be stale, so we skip it for titles step
        if (step === 'titles') {
          // Skip validateStepCompletion for titles - use stepCompletionState.titles instead
          // This is the authoritative source that's updated reactively when titles are labeled
        } else {
          // Validate current step is complete (for other steps)
          const validation = this.workflowService.validateStepCompletion(step, context.labelForm);
          if (!validation.valid) {
            // DEBUG: Log why validation failed
            if (step === 'boxset') {
              this.logger.debug('[DEBUG] Continue button disabled on boxset step: validation failed', {
                step,
                validation,
                completionState,
                labelForm: context.labelForm,
                movie_id: context.labelForm?.movie_id,
                release_id: context.labelForm?.release_id,
                release_name: context.labelForm?.release_name,
                release_slug: context.labelForm?.release_slug,
                boxset_id: context.labelForm?.boxset_id,
              });
            }
            return false; // Current step is not complete
          }
        }
        
        // Exploratory Rip step (Path A only): enabled when the segment-reorder
        // workflow has reached canonical_complete — that's when the user can
        // hand off to the standard labeling flow (boxset/disc/titles/...).
        // Mid-rip the button stays disabled; the workspace card has its own
        // "Cancel and pick manually" affordance for bailing out.
        if (step === 'exploratory_rip') {
          const srStage = jobStatus?.segment_reorder_state?.stage;
          return srStage === 'canonical_complete';
        }

        // Film step: require movie selected (filmStepSaveInProgress already handled above)
        if (step === 'film' && !discdbHit) {
          const jobStatusValue = jobStatus?.job_status;
          const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'];
          if (jobStatusValue === 'failed') {
            return completionState.film;
          }
          // Rip running/completed: still require movie selected to enable Continue
          if (jobStatus && (ripState === 'running' || ripState === 'completed')) {
            return completionState.film;
          }
          return completionState.film;
        }
        
        // Summary step (DiscDB hit): enable Start Copy when no job yet, or when copy running/completed
        if (step === 'summary' && discdbHit) {
          if (!jobStatus) return true; // No job yet - user can click Start Copy
          const ripState = jobStatus.rip_state || jobStatus.pipeline?.['rip'] || jobStatus.job_status;
          const isRipRunning = jobStatus.job_status === 'running' || jobStatus.job_status === 'pending' || ripState === 'running';
          const isRipCompleted = ripState === 'completed';
          return isRipRunning || isRipCompleted;
        }
        
        // Boxset step: require film complete
        if (step === 'boxset' && !discdbHit) {
          if (!completionState.film) {
            // DEBUG: Log why button is disabled
            this.logger.debug('[DEBUG] Continue button disabled on boxset step: film not complete', {
              completionState,
              labelForm: context.labelForm,
              movie_id: context.labelForm?.movie_id,
              release_id: context.labelForm?.release_id,
              release_name: context.labelForm?.release_name,
              boxset_id: context.labelForm?.boxset_id,
            });
            return false;
          }
        }
        
        // Disc step: require prior steps complete
        if (step === 'disc') {
          if (!discdbHit) {
            if (!completionState.film || !completionState.boxset) return false;
          }
        }
        
        // Titles step: require prior steps complete AND rip completed (if rip is running, disable until it completes)
        if (step === 'titles') {
          if (!discdbHit) {
            if (!completionState.film || !completionState.boxset || !completionState.disc) {
              return false;
            }
          } else {
            if (!completionState.disc) {
              return false;
            }
          }

          const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status;
          const isRipRunning = jobStatus?.job_status === 'running' || jobStatus?.job_status === 'pending' || ripState === 'running';
          const isRipCompleted = ripState === 'completed';
          if (isRipRunning && !isRipCompleted) {
            return false;
          }

          if (!completionState.titles) {
            return false;
          }

          // Do NOT gate on label_state === 'completed'. The backend can set label_state=completed
          // before workflow_step has moved to postprocess (sync/race), leaving the user on titles
          // with Continue disabled. workflow_step is the source of truth: on titles we allow Continue.

          return true;
        }
        
        // #365 Phase 2 § 6.4 — the 'postprocess' step branch was removed
        // once backend stopped emitting workflow_step="postprocess" and
        // 'postprocess' was dropped from the WorkflowStep type. The same
        // pre-transfer gates now live in the 'transfer' branch (one
        // step earlier in the collapsed flow); transferPhaseLabel
        // renders the prep sub-phase on the Transfer card.


        // Check if we can navigate to the next step
        const steps: WorkflowStep[] = getStepOrderForContext(context);
        const currentIndex = steps.indexOf(step);
        if (currentIndex < steps.length - 1) {
          const nextStep = steps[currentIndex + 1];
          const nextStepValidation = this.workflowService.canNavigateToStep(context, nextStep);

          // DEBUG: Log navigation validation for boxset step
          if (step === 'boxset' && !nextStepValidation.allowed) {
            this.logger.debug('[DEBUG] Continue button disabled on boxset step: cannot navigate to next step', {
              step,
              nextStep,
              nextStepValidation,
              completionState,
              rip_state: jobStatus?.rip_state,
              pipeline_rip: jobStatus?.pipeline?.['rip'],
              job_status: jobStatus?.job_status,
              labelForm: context.labelForm,
            });
          }

          return nextStepValidation.allowed;
        }
        return false;
      }),
      distinctUntilChanged(),
      // Schedule emissions in the next microtask to ensure Angular change detection runs
      // This fixes the race condition where distinctUntilChanged filters duplicates
      // but Angular change detection hasn't run yet
      observeOn(asyncScheduler)
    );

    // Tooltip reason mirroring canContinue$'s gates. Returns null when
    // enabled (no tooltip) or a short message naming the specific blocker
    // so the user doesn't have to guess.
    this.disabledReason$ = combineLatest([
      activeContext$,
      this.workflowService.getStartRipInProgress$(),
      this.workflowService.getFilmStepSaveInProgress$(),
      this._continueInProgress$,
      this.canContinue$,
    ]).pipe(
      map(([context, startRipInProgress, filmStepSaveInProgress, continueInProgress, canContinue]: [WorkflowContext | null, boolean, boolean, boolean, boolean]) => {
        if (canContinue) return null;
        if (continueInProgress) return null;
        if (startRipInProgress) return 'Starting copy…';
        if (!context) return null;

        const step = this.workflowService.getEffectiveWorkflowStep(context) || 'film';
        const discdbHit = context.discdbHit;
        const jobStatus = context.jobStatus;
        const completionState = context.stepCompletionState || this.workflowService.getStepCompletionState(context);
        const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status;
        const isRipRunning = jobStatus?.job_status === 'running' || jobStatus?.job_status === 'pending' || ripState === 'running';
        const isRipCompleted = ripState === 'completed';

        if ((step === 'film' || step === 'boxset') && !discdbHit && filmStepSaveInProgress) {
          return 'Saving…';
        }

        if (step === 'transfer') {
          const transferState = jobStatus?.transfer_state ?? jobStatus?.pipeline?.['transfer'];
          if (transferState === 'running') return 'Transfer in progress…';
          return null;
        }

        if (step === 'exploratory_rip') {
          const srStage = jobStatus?.segment_reorder_state?.stage;
          if (srStage !== 'canonical_complete') {
            return 'Waiting for the exploratory rip to finish…';
          }
          return null;
        }

        if (step === 'film' && !discdbHit) {
          if (!completionState.film) return 'Select a movie or series to continue';
          return null;
        }

        if (step === 'summary' && discdbHit) {
          if (isRipRunning && !isRipCompleted) return 'Waiting for copy to finish…';
          return null;
        }

        if (step === 'boxset' && !discdbHit) {
          if (!completionState.film) return 'Select a movie or series first';
          if (!completionState.boxset) return 'Select or create a release to continue';
          return null;
        }

        if (step === 'disc') {
          if (!discdbHit && (!completionState.film || !completionState.boxset)) {
            return 'Complete the previous steps first';
          }
          if (!completionState.disc) return 'Fill in disc info (name + format) to continue';
          return null;
        }

        if (step === 'titles') {
          if (!discdbHit && (!completionState.film || !completionState.boxset || !completionState.disc)) {
            return 'Complete the previous steps first';
          }
          if (discdbHit && !completionState.disc) {
            return 'Complete the previous steps first';
          }
          if (isRipRunning && !isRipCompleted) {
            return 'Waiting for copy to finish — titles can be labeled once the rip completes';
          }
          if (!completionState.titles) {
            return 'Label or ignore every title to continue';
          }
          return null;
        }

        // #365 Phase 2 § 6.4 — 'postprocess' step removed; prep messages
        // fold into the 'transfer' step branch above (transferPhaseLabel
        // shows "Preparing files…" on the Transfer card).

        return null;
      }),
      distinctUntilChanged(),
      observeOn(asyncScheduler),
    );

    this.canGoBack$ = this.context$.pipe(
      map((context: WorkflowContext | null) => {
        if (!context) return false;
        // Phase 1: Use workflowStep from context; respect DiscDB profile for step order
        const step = this.workflowService.getEffectiveWorkflowStep(context) || (context.discdbHit ? 'summary' : 'film');
        const steps: WorkflowStep[] = getStepOrderForContext(context);
        const stepIndex = steps.indexOf(step);
        // Hide back button on first step (summary for hit, film for miss)
        if (stepIndex <= 0) return false;
        // #363 H1: hide back when the previous step is a labeling step and
        // labels are already locked (post-processing consumed them).
        return this.workflowService.canNavigateToStep(context, steps[stepIndex - 1]).allowed;
      })
    );

    this.buttonText$ = combineLatest([
      this.context$,
      this.canContinue$,
      this.drivePresenceState$,
    ]).pipe(
      map(([context, canContinue, presence]: [WorkflowContext | null, boolean, DrivePresenceState]) => {
        // #571 — drive-presence label overrides the per-step labels. Skip
        // while a rip is actively running so "Copying NN%" wins.
        const isRipping =
          context?.jobStatus?.job_status === 'running' ||
          context?.jobStatus?.job_status === 'pending';
        if (!isRipping && presence !== 'available') {
          return presence === 'drive-empty' ? 'Insert Disc' : 'Drive Not Connected';
        }

        if (!context) return 'Continue';

        const step = this.workflowService.getEffectiveWorkflowStep(context) || 'film';
        const jobStatus = context.jobStatus;
        const discdbHit = context.discdbHit;
        
        // #365 Phase 2 § 6.4 — the 'postprocess' button-text branch was
        // removed once 'postprocess' was dropped from the WorkflowStep
        // type. The "Prepare Transfer" label that used to fire here when
        // labeling was done but rip hadn't finished is no longer
        // reachable: backend now writes workflow_step="transfer"
        // directly, so the user lands on the Transfer step with the
        // sub-phase label "Waiting for copy to finish…" handled by
        // disabledReason$ instead.


        // Exploratory Rip step (Path A only)
        if (step === 'exploratory_rip') {
          const srStage = jobStatus?.segment_reorder_state?.stage;
          if (srStage === 'canonical_complete') return 'Continue';
          if (srStage === 'awaiting_segment_order') return 'Order segments';
          return 'Ripping…';
        }

        // Film step (Movie step) - Copy stage
        if (step === 'film' && !discdbHit) {
          const jobStatusValue = jobStatus?.job_status;
          const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'];
          
          // If job_status is failed, ignore it (treat as no job)
          if (jobStatusValue === 'failed') {
            return 'Start Copy';
          }
          
          // If we have a job_status (available), check rip_state
          if (jobStatus && ripState) {
            // rip_state pending/ready → "Start Copy"
            if (ripState === 'pending' || ripState === 'ready') {
              return 'Start Copy';
            }
            // rip_state running → "Continue"
            if (ripState === 'running') {
              return 'Continue';
            }
            // rip_state completed → "Continue"
            if (ripState === 'completed') {
              return 'Continue';
            }
          }
          
          // No rip_state yet - show "Start Copy"
          return 'Start Copy';
        }
        
        // Summary step (DiscDB hit)
        if (step === 'summary' && discdbHit) {
          const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status;
          const isRipRunning = jobStatus?.job_status === 'running' || jobStatus?.job_status === 'pending' || ripState === 'running';
          const isRipCompleted = ripState === 'completed';
          
          if (isRipCompleted) return 'Continue';
          if (isRipRunning) return 'Copying...';
          return 'Start Copy';
        }
        
        // Titles step
        if (step === 'titles') {
          const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status;
          const isRipRunning = jobStatus?.job_status === 'running' || jobStatus?.job_status === 'pending' || ripState === 'running';
          const isRipCompleted = ripState === 'completed';
          
          // If rip is running or completed, show "Continue"
          // If rip hasn't started, show "Start Rip"
          if (isRipRunning || isRipCompleted) {
            return 'Continue';
          }
          return 'Start Rip';
        }
        
        // #365 Phase 2 § 6.4 — 'postprocess' button-text branch removed;
        // 'postprocess' is no longer a WorkflowStep. The Transfer branch
        // below covers the same UX (button label "Start Transfer" before
        // the click; "Transferring" while running) and the stage card's
        // transferPhaseLabel surfaces the "Preparing files…" sub-phase
        // during prep.

        // Transfer step (#365 Phase 2 § 6.4 — absorbs the prep sub-phase
        // that used to live on the postprocess step). Button text branches
        // on post_state for prep states (pre-transfer) then on
        // transfer_state for the actual copy.
        if (step === 'transfer') {
          const postState = jobStatus?.post_state || jobStatus?.pipeline?.['postprocess'];
          const transferState = jobStatus?.transfer_state ?? jobStatus?.pipeline?.['transfer'];
          const isTransferRunning = transferState === 'running';
          const isTransferCompleted = transferState === 'completed' || context.isStageCompletedFn?.('transfer');
          const isTransferFailed = transferState === 'failed';

          if (isTransferCompleted) return 'Finish';
          if (isTransferRunning) return 'Transferring';
          if (isTransferFailed) return 'Retry Transfer';
          // Prep sub-phase signals (post_state) — the click would
          // retry / continue the prep work, not the actual copy.
          if (postState === 'failed') return 'Retry Preparing';
          if (postState === 'running') return 'Preparing…';
          return 'Start Transfer';
        }
        
        // Default
        return 'Continue';
      })
    );

    this.buttonSpinner$ = combineLatest([
      this.context$,
      this.canContinue$,
      this.workflowService.getStartRipInProgress$(),
      this.workflowService.getFilmStepSaveInProgress$(),
      this.workflowService.getDiscStepContinueInProgress$(),
      this._continueInProgress$,
    ]).pipe(
      map(([context, canContinue, startRipInProgress, filmStepSaveInProgress, discStepContinueInProgress, continueInProgress]: [WorkflowContext | null, boolean, boolean, boolean, boolean, boolean]) => {
        // Show spinner if any Continue/advance action is in progress
        if (continueInProgress) {
          return true;
        }
        // Show spinner if startRip is in progress
        if (startRipInProgress) {
          return true;
        }

        if (!context) return false;

        const step = this.workflowService.getEffectiveWorkflowStep(context) || 'film';
        const jobStatus = context.jobStatus;
        const discdbHit = context.discdbHit;

        // Film / boxset: show spinner while saving label workflow context (movie or release/boxset PATCH)
        if ((step === 'film' || step === 'boxset') && !discdbHit && filmStepSaveInProgress) {
          return true;
        }

        if (step === 'disc' && !discdbHit && discStepContinueInProgress) {
          return true;
        }
        
        // #365 Phase 2 § 6.4 — 'postprocess' button-spinner branch
        // removed; the prep work runs under the 'transfer' step now,
        // and the Transfer branch below handles the spinner when prep
        // or copy is running.

        // Transfer step: spinner when transferring
        if (step === 'transfer') {
          const transferState = jobStatus?.transfer_state ?? jobStatus?.pipeline?.['transfer'];
          if (transferState === 'running') return true;
        }

        // Summary step: show spinner if copy is starting
        if (step === 'summary' && discdbHit) {
          const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status;
          const isRipRunning = jobStatus?.job_status === 'running' || jobStatus?.job_status === 'pending' || ripState === 'running';
          const isRipCompleted = ripState === 'completed';
          
          // Show spinner if copy is running but not completed
          if (isRipRunning && !isRipCompleted) {
            return true;
          }
        }
        
        // Film step: show spinner when rip_state is running
        if (step === 'film' && !discdbHit) {
          const jobStatusValue = jobStatus?.job_status;
          const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'];
          
          // If job_status is failed, no spinner
          if (jobStatusValue === 'failed') {
            return false;
          }
          
          // Show spinner when rip_state is running
          if (jobStatus && ripState === 'running') {
            return true;
          }
          
          return false;
        }
        
        return false;
      }),
      distinctUntilChanged()
    );

    // Progress calculation - use service observables
    this.stageProgress$ = this.workflowService.getStageProgress$();
    this.isStageCompleted$ = this.workflowService.getStageCompletion$();

    // Hide the workflow-actions Continue button at stages where an
    // embedded step component owns the primary CTA. Currently:
    //   awaiting_segment_order — segment-reorder page's Submit handles
    //   user submission inline; the workflow-actions "Order segments"
    //   button is intentionally disabled (advance gates on
    //   canonical_complete) but the label looked actionable, drawing
    //   user clicks away from the real Submit button.
    this.hideContinue$ = this.context$.pipe(
      map((context: WorkflowContext | null) => {
        if (!context) return false;
        const step = this.workflowService.getEffectiveWorkflowStep(context);
        if (step !== 'exploratory_rip') return false;
        const srStage = context.jobStatus?.segment_reorder_state?.stage;
        return srStage === 'awaiting_segment_order';
      })
    );
  }

  ngOnInit(): void {
    // Component initialized
  }

  ngOnDestroy(): void {
    this.subscriptions.unsubscribe();
  }

  // Actions
  onContinue(): void {
    // Prevent duplicate calls - set flag immediately at function start
    if (this._continueInProgress) {
      this.logger.warn('[WorkflowActions] onContinue called while already in progress, ignoring duplicate call');
      return;
    }
    this._continueInProgress = true;
    
    combineLatest([this.context$, this.activeStage$])
      .pipe(take(1))
      .subscribe(([context, activeStage]: [WorkflowContext | null, StageKey | null]) => {
      if (!context) {
        this._continueInProgress = false;
        return;
      }
      
      const workflowStep = this.workflowService.getEffectiveWorkflowStep(context) || 'film';
      const jobStatus = context.jobStatus;
      const discdbHit = context.discdbHit;
      
      // Validate current step is complete before proceeding
      const validation = this.workflowService.validateStepCompletion(workflowStep, context.labelForm);

      // Film step (Movie step) - DiscDB miss only
      if (workflowStep === 'film' && !discdbHit) {
        // Require movie selected
        if (!validation.valid) {
          this.logger.warn('Cannot continue: movie must be selected');
          this._continueInProgress = false;
          return;
        }
        
        // Button state is based on rip_state, not job_status
        const jobStatusValue = jobStatus?.job_status;
        const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'];
        
        // If job_status is failed, ignore it (treat as no job)
        // If no rip_state or rip_state is pending/ready, start the rip
        const shouldStartRip = !jobStatus || 
                                jobStatusValue === 'failed' || 
                                !ripState || 
                                ripState === 'pending' ||
                                ripState === 'ready';

        const jobId = context.jobStatus?.jobId ?? (context.type === 'job' ? context.id : null) ?? context.id;
        // When we have a job context (e.g. job already ripped, user changed movie), prefer advancing step.
        // After "change movie" jobStatus can be missing/stale so ripState is undefined → we'd wrongly call startRip.
        const hasJobId = context.type === 'job' && !!context.id;
        const shouldAdvanceStep = ripState === 'running' || ripState === 'completed' || (hasJobId && !jobStatus);

        if (shouldAdvanceStep && jobId) {
          // Rip is running/completed, or we have a job id but no jobStatus (e.g. after change movie) → advance step
          this.workflowService.advanceStepTo('boxset', jobId).subscribe({
            next: () => { this._continueInProgress = false; },
            error: (err: any) => {
              this.logger.warn('[ContinueDebug] advanceStepTo error', err?.status, err?.error?.detail || err?.message || err);
              this._continueInProgress = false;
            }
          });
        } else if (shouldStartRip) {
          // Start the copy job (backend returns JobStatus with workflow_step; applied in startRip tap)
          this.workflowService.startRip().subscribe({
            next: () => { this._continueInProgress = false; },
            error: (err: any) => {
              this.handleStartRipError(err, context);
            }
          });
        }
        return;
      }
      
      // Exploratory Rip step (Path A miss only). canContinue$ already gates
      // this branch on segment_reorder_state.stage === 'canonical_complete',
      // so by the time we land here the canonical rip is finished and the
      // backend has bumped workflow_step off exploratory_rip — but keep the
      // explicit advanceStepTo('boxset') in case the backend broadcast lags
      // the user click.
      if (workflowStep === 'exploratory_rip') {
        const jobId = context.jobStatus?.jobId ?? (context.type === 'job' ? context.id : null) ?? context.id;
        if (jobId) {
          this.workflowService.advanceStepTo('boxset', jobId).subscribe({
            next: () => { this._continueInProgress = false; },
            error: (err: any) => {
              this.logger.warn('[ContinueDebug] advanceStepTo from exploratory_rip error', err?.status, err?.error?.detail || err?.message || err);
              this._continueInProgress = false;
            }
          });
        } else {
          this._continueInProgress = false;
        }
        return;
      }

      // Summary step (DiscDB hit only)
      if (workflowStep === 'summary' && discdbHit) {
        // Check rip state - if jobStatus is null, treat it as "no rip started yet"
        // The backend guard will prevent duplicate rips if there's an active job
        const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status;
        const isRipRunning = jobStatus?.job_status === 'running' || jobStatus?.job_status === 'pending' || ripState === 'running';
        const isRipCompleted = ripState === 'completed';
        if (!isRipRunning && !isRipCompleted) {
          // Start the copy job (backend guard will prevent duplicates)
          this.workflowService.startRip().subscribe({
            next: () => {
              // Will auto-progress to postprocess when copy completes (handled by determineWorkflowStep)
              this._continueInProgress = false;
            },
            error: (err: any) => {
              this.handleStartRipError(err, context);
            }
          });
        } else if (isRipCompleted) {
          // Copy completed - advance straight to transfer via POST
          // /workflow/step/complete. #365 Phase 2 § 6.4 — was 'postprocess'
          // before the step was collapsed.
          const jobIdSummary = context.jobStatus?.jobId ?? (context.type === 'job' ? context.id : null) ?? context.id;
          this.workflowService.advanceStepTo('transfer', jobIdSummary ?? undefined).subscribe({
            next: () => { this._continueInProgress = false; },
            error: () => { this._continueInProgress = false; }
          });
        } else {
          this._continueInProgress = false;
        }
        return;
      }
      
      // Boxset step - DiscDB miss only
      if (workflowStep === 'boxset' && !discdbHit) {
        // Require release/boxset selected + movie selected
        if (!validation.valid) {
          this.logger.warn('Cannot continue: release or boxset must be selected');
          return;
        }
        
        // Check prior step (film) is complete
        const completionState = this.workflowService.getStepCompletionState(context);
        if (!completionState.film) {
          this.logger.warn('Cannot continue: movie must be selected first');
          return;
        }
        
        const obs = this.workflowService.continueToNextStep();
        if (obs != null && typeof (obs as any)?.subscribe === 'function') {
          (obs as Observable<unknown>).subscribe({ next: () => { this._continueInProgress = false; }, error: () => { this._continueInProgress = false; } });
        } else {
          this._continueInProgress = false;
        }
        return;
      }
      
      // Disc step
      if (workflowStep === 'disc') {
        // Require disc labels completed
        if (!validation.valid) {
          this.logger.warn('Cannot continue: disc information must be completed');
          this._continueInProgress = false;
          return;
        }
        
        // Check prior steps are complete
        const completionState = this.workflowService.getStepCompletionState(context);
        if (!discdbHit) {
          if (!completionState.film || !completionState.boxset) {
            this.logger.warn('Cannot continue: prior steps must be completed first');
            this._continueInProgress = false;
            return;
          }
        }

        // Job: save labelForm then advance (spinner via getDiscStepContinueInProgress$)
        if (context.type === 'job' && context.jobStatus?.jobId && context.labelForm) {
          this.workflowService.saveDiscStepAndContinueToNext().subscribe({
            next: () => {
              this._continueInProgress = false;
            },
            error: (err: unknown) => {
              this.logger.warn('[WorkflowActions] disc step save+continue failed', err);
              this.toast.show(formatHttpErrorDetail(err), 'error', 5000);
              this._continueInProgress = false;
            },
          });
          return;
        }

        const obsDisc = this.workflowService.continueToNextStep();
        if (obsDisc != null && typeof (obsDisc as any)?.subscribe === 'function') {
          (obsDisc as Observable<unknown>).subscribe({ next: () => { this._continueInProgress = false; }, error: () => { this._continueInProgress = false; } });
        } else {
          this._continueInProgress = false;
        }
        return;
      }
      
      // Titles step — same source of truth as canContinue$: stepCompletionState / getStepCompletionState.
      // validateStepCompletion('titles', labelForm) can disagree (stale labelForm.tracks vs WebSocket titles).
      if (workflowStep === 'titles') {
        const completionState =
          context.stepCompletionState || this.workflowService.getStepCompletionState(context);
        if (!completionState.titles) {
          this.logger.warn('Cannot continue: all titles must be labeled or ignored');
          this._continueInProgress = false;
          return;
        }

        // Check prior steps are complete
        if (!discdbHit) {
          if (!completionState.film || !completionState.boxset || !completionState.disc) {
            this.logger.warn('Cannot continue: prior steps must be completed first');
            this._continueInProgress = false;
            return;
          }
        } else {
          if (!completionState.disc) {
            this.logger.warn('Cannot continue: disc information must be completed first');
            this._continueInProgress = false;
            return;
          }
        }
        
        const obsTitles = this.workflowService.continueToNextStep();
        if (obsTitles != null && typeof (obsTitles as any)?.subscribe === 'function') {
          (obsTitles as Observable<unknown>).subscribe({ next: () => { this._continueInProgress = false; }, error: () => { this._continueInProgress = false; } });
        } else {
          this._continueInProgress = false;
        }
        return;
      }
      
      // Transfer step (#365 Phase 2 § 6.4 — absorbs the old postprocess
      // step). The Transfer card stays active across both the prep
      // sub-phase (transfer_phase="preparing") and the actual file copy
      // (transfer_phase="transferring"/"verifying"). The click dispatch
      // branches on post_state so the right backend call fires for the
      // current sub-state.
      if (workflowStep === 'transfer') {
        const ripState = jobStatus?.rip_state || jobStatus?.pipeline?.['rip'] || jobStatus?.job_status;
        const isRipCompleted = ripState === 'completed';

        // Labeling prerequisites (miss profile only).
        //
        // The client-side ``getStepCompletionState`` counter tracks per-title
        // *user-confirmed* actions in Angular component state and can drift
        // out of sync with the backend when labels are set through any
        // non-UI path (API, tests, DB backfill, disc reprocess after prefill
        // updates disc_titles.type). The backend-owned ``label_state`` is the
        // authoritative signal — if it reads ``completed``, labeling is done
        // regardless of the client-side counter. Discovered by #632 (v1.0.0
        // smoke QA cell 1: TV × HIT × prefill=ON, where backend flips
        // label_state=completed via POST /label/complete but the client-side
        // counter stayed at "48 REMAINING" and blocked Start Transfer).
        if (!discdbHit) {
          const labelState = jobStatus?.label_state || jobStatus?.pipeline?.['label'];
          const backendLabelDone = labelState === 'completed' || labelState === 'skipped';
          if (!backendLabelDone) {
            const completionState = this.workflowService.getStepCompletionState(context);
            const allLabelingComplete = (completionState.film && completionState.boxset && completionState.disc && completionState.titles);
            if (!allLabelingComplete) {
              this.logger.warn('Cannot continue: all labeling steps must be completed first');
              this._continueInProgress = false;
              return;
            }
          }
        }

        // Rip must finish before any transfer-stage work can begin.
        if (!isRipCompleted) {
          this._continueInProgress = false;
          return;
        }

        const postState = jobStatus?.post_state || jobStatus?.pipeline?.['postprocess'];
        const transferState = jobStatus?.transfer_state ?? jobStatus?.pipeline?.['transfer'];
        const isTransferCompleted = transferState === 'completed' || context.isStageCompletedFn?.('transfer');
        const isTransferRunning = transferState === 'running';

        // Terminal: Finish dismisses the card.
        if (isTransferCompleted) {
          const jobId = context.jobStatus?.jobId;
          if (jobId) {
            this.workflowService.finishJob(jobId).subscribe({
              next: () => {
                this.workflowService.setSelectedCard(null);
                this._continueInProgress = false;
              },
              error: (err: unknown) => {
                this.logger.error('Failed to finish job:', err);
                this.toast.show('Failed to dismiss job. Please try again.', 'error');
                this._continueInProgress = false;
              }
            });
          } else {
            this.workflowService.setSelectedCard(null);
            this._continueInProgress = false;
          }
          return;
        }

        // In-flight (prep or copy): button is in spinner state via
        // buttonSpinner$ and click is a no-op.
        if (isTransferRunning || postState === 'running') {
          this._continueInProgress = false;
          return;
        }

        // Failure recovery: prep failure → retry prep. Transfer-stage
        // failure (transfer_state=failed) drops through to startTransfer
        // below to retry the copy.
        if (postState === 'failed') {
          this.workflowService.resumeJob().subscribe({
            next: () => { this._continueInProgress = false; },
            error: (err: any) => {
              this.logger.error('Failed to resume prep:', err);
              // #204: surface the failure to the user — silent no-op looks like a broken button.
              this.toast.show(`Failed to resume prep: ${formatHttpErrorDetail(err)}`, 'error', 6000);
              this._continueInProgress = false;
            }
          });
          return;
        }

        // Pre-prep: post_state is 'ready' or 'pending' — the user click
        // starts the prep work. Remote modes auto-dispatch the actual
        // transfer after prep (PR #473); local mode too (PR #478).
        if (postState === 'ready' || postState === 'pending') {
          this.workflowService.startPostProcess().subscribe({
            next: () => { this._continueInProgress = false; },
            error: (err: any) => {
              this.logger.error('Failed to start prep:', err);
              // #204: surface the failure — silent no-op is confusing.
              this.toast.show(`Failed to start prep: ${formatHttpErrorDetail(err)}`, 'error', 6000);
              this._continueInProgress = false;
            }
          });
          return;
        }

        // Post-prep (post_state=completed, transfer_state=ready/pending/
        // failed). Manual click to (re)trigger the actual file copy.
        this.workflowService.startTransfer().subscribe({
          next: () => { this._continueInProgress = false; },
          error: (err: any) => {
            this.logger.error('Failed to start transfer:', err);
            this._continueInProgress = false;
            if (isNoActiveTransferConfigError(err)) {
              this.setupModalSvc.open({ targetStep: 2, closeOnComplete: true });
            } else if (isTransferConfigOrPathError(err)) {
              this.router.navigate(['/settings']);
            } else {
              // #204: surface unexpected errors — the two branches above handle
              // the recognised recoverable cases (config-missing → setup modal,
              // path/config invalid → Settings). Anything else was a silent no-op.
              this.toast.show(`Failed to start transfer: ${formatHttpErrorDetail(err)}`, 'error', 6000);
            }
          }
        });
        return;
      }
      
      // Other steps: advance (validation already passed)
      const obsOther = this.workflowService.continueToNextStep();
      if (obsOther != null && typeof (obsOther as any)?.subscribe === 'function') {
        (obsOther as Observable<unknown>).subscribe({ next: () => { this._continueInProgress = false; }, error: () => { this._continueInProgress = false; } });
      } else {
        this._continueInProgress = false;
      }
    });
  }

  onBack(): void {
    this.workflowService.navigateToPreviousStep();
  }


  // Helper methods
  private getStepLabel(step: string): string {
    const labels: Record<string, string> = {
      film: 'Film',
      boxset: 'Boxset/Release',
      disc: 'Disc',
      titles: 'Titles',
      // #365 Phase 2 § 6.4 — postprocess collapsed into transfer.
      transfer: 'Transfer'
    };
    return labels[step] || step;
  }

  private isStepComplete(context: WorkflowContext | null, step: WorkflowStep): boolean {
    if (!context || !context.labelForm) return false;
    
    const form = context.labelForm;
    
    switch (step) {
      case 'summary':
        // Summary step is always complete (it's just a display step)
        return true;
      case 'film':
        return !!(form.movie_id);
      case 'boxset':
        return !!(form.release_id || form.release_name || form.release_slug ||
          (form.boxset_id && form.boxset_id !== '__pending__'));
      case 'disc':
        return !!(form.disc_name && form.disc_format);
      case 'titles':
        // At least one title should be labeled
        if (!context.titles || context.titles.length === 0) return false;
        return context.titles.some((t: any) => t.title && t.type && t.type !== 'ignore');
      // #365 Phase 2 § 6.4 — 'postprocess' case removed (collapsed
      // into transfer).
      case 'transfer':
        return context.isStageCompletedFn?.('transfer') || false;
      default:
        return true;
    }
  }

  private stageCompletion(key: StageKey, context: WorkflowContext | null): boolean {
    // Map StageKey to WorkflowStep for completion check
    if (key === 'label') {
      const workflowStep = context?.workflowStep || (context ? this.workflowService.determineWorkflowStep(context) : null);
      if (workflowStep === 'titles') {
        return this.isStepComplete(context, 'titles');
      }
      // For other labeling steps, check if we've moved past them
      return workflowStep !== 'film' && workflowStep !== 'boxset' && workflowStep !== 'disc';
    }
    // For postprocess and transfer, use isStageCompletedFn
    return context?.isStageCompletedFn?.(key) || false;
  }

  getTitleProgress(titleId: string | null | undefined): Observable<number | null> {
    return this.workflowService.getActiveContext().pipe(
      map((context: WorkflowContext | null) => {
        if (!titleId || !context?.titleProgressValueFn) return null;
        return context.titleProgressValueFn(titleId) || null;
      })
    );
  }

  /** Shared handler for WorkflowService.startRip() failures (film + summary steps). */
  private handleStartRipError(err: unknown, context: WorkflowContext): void {
    this.logger.error('Failed to start rip:', err);
    this._continueInProgress = false;

    // Path A trigger: backend says this disc has duplicate-segment-map groups
    // and the projected rip exceeds threshold. Open the threshold modal
    // instead of toasting an error.
    const errObj = err as {
      status?: number;
      error?: {
        detail?: { code?: string; [k: string]: any };
        code?: string;
      };
    };
    const detailCode = errObj?.error?.detail?.code ?? errObj?.error?.code;

    // Multi-drive policy rejection (#540 / #550): the rip target drive's
    // identity comes from the by-path / sysfs fallback (or is unresolvable
    // entirely), and the gatekeeper refuses to start while other drives are
    // attached. The backend has already emitted a unified notification via
    // core.notifications.emit_notification_sync, which lands in both Discord
    // and the WebUI ToastService through the existing WS channel — so we
    // just early-return here to suppress the generic "Failed to start rip"
    // fallback toast and avoid duplicate UI signal.
    if (
      errObj?.status === 409 &&
      (detailCode === 'drive_unsafe_with_others' ||
        detailCode === 'drive_unidentifiable')
    ) {
      return;
    }

    // #578: USB bus saturation. Backend refused because another rip is
    // already running on the same sub-SuperSpeed bus. Open the
    // confirmation modal; on confirm, retry the rip with
    // ``forceConcurrentOnSaturatedBus: true``. Cancel just clears state.
    if (errObj?.status === 409 && detailCode === 'usb_bus_saturation_risk') {
      const detail = (errObj.error?.detail as any) || (errObj.error as any) || {};
      const payload: UsbSaturationWarningPayload = {
        bus: detail.bus ?? 0,
        speed_mbps: detail.speed_mbps ?? 0,
        competing_mount_points: detail.competing_mount_points ?? [],
        message: detail.message ?? 'USB bus saturation risk.',
        override_field: detail.override_field ?? 'force_concurrent_on_saturated_bus',
      };
      this.usbSaturationSvc.open(payload, () => {
        // Retry once, with the override flag. If the retry surfaces a
        // different error code, the same handler picks it up — no
        // recursion on saturation because the backend's gate honours
        // the flag and won't return this code again.
        this.workflowService
          .startRip({ forceConcurrentOnSaturatedBus: true })
          .subscribe({
            error: (retryErr) => this.handleStartRipError(retryErr, context),
          });
      });
      return;
    }

    if (errObj?.status === 409 && detailCode === 'needs_user_choice') {
      const detail = (errObj.error?.detail as any) || (errObj.error as any) || {};
      const candidates = (detail as any).candidates || [];
      const mountPoint =
        (context.discInfo as any)?.mount_point ||
        (context.discInfo as any)?.mountPoint ||
        '';
      const discId =
        (context.discInfo as any)?.disc_id ||
        (context.discInfo as any)?.discId ||
        undefined;
      this.ripSizeWarningSvc.open({
        mountPoint,
        discId,
        discNum: context.discNum || undefined,
        projectedRipBytes: detail.projected_rip_bytes ?? null,
        availableDiskBytes: detail.available_disk_bytes ?? null,
        thresholdGb: detail.threshold_gb ?? 200,
        duplicateGroupCount: detail.duplicate_group_count ?? candidates.length,
        candidates: candidates,
      });
      return;
    }

    if (
      typeof err === 'object' &&
      err !== null &&
      (err as { status?: number }).status === 503 &&
      (err as { error?: { action_required?: string } }).error?.action_required === 'reinstall_makemkv'
    ) {
      const e = err as { error?: { error?: string } };
      this.toast.show(
        e.error?.error || 'MakeMKV is not properly installed. Please reinstall.',
        'error',
        10000
      );
      this.setupModalSvc.open({ targetStep: 1, closeOnComplete: true });
      return;
    }

    if (isAmbiguousStartRipTransportError(err)) {
      this.workflowService.tryRecoverStartRipAfterAmbiguousError().subscribe((recovered) => {
        if (!recovered) {
          this.toast.show(START_RIP_AMBIGUOUS_RESPONSE_COPY, 'warning', 7000);
        }
      });
      return;
    }

    const detail = formatHttpErrorDetail(err);
    const verb = startRipFailureVerb(context.discMode);
    this.toast.show(`Failed to start ${verb}: ${detail}`, 'error', 8000);
  }
}