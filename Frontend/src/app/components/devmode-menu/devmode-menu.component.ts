// src/app/components/devmode-menu/devmode-menu.component.ts
import { Component, Input, OnInit, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { JobService, JobStatus } from '../../services/job.service';
import { MetadataService } from '../../services/metadata.service';
import { SystemService } from '../../services/system.service';
import { ToastService } from '../../services/toast.service';
import { WorkflowService } from '../../services/workflow.service';
import { SetupModalService } from '../../services/setup-modal.service';

interface RevertOption {
  label: string;
  stage: 'finalize' | 'postprocess' | 'transfer';
  enabled: boolean;
  action: () => void;
}

@Component({
  selector: 'app-devmode-menu',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="p-0">
      <button
        type="button"
        class="devmode-menu-item w-full text-left px-4 py-2.5 text-sm text-white/80 hover:bg-white/5 flex items-center border-b border-white/10"
        (click)="openSetup()">
        Setup
      </button>
      <ng-container *ngIf="revertOptions.length > 0">
        <button
          *ngFor="let option of revertOptions"
          type="button"
          class="devmode-menu-item w-full text-left px-4 py-2.5 text-sm text-white/80 hover:bg-white/5 flex items-center disabled:opacity-50 disabled:cursor-not-allowed"
          [disabled]="!option.enabled || reverting"
          (click)="option.action()">
          <span *ngIf="reverting === option.stage" class="inline-block w-3.5 h-3.5 rounded-full border-2 border-white/35 border-t-white animate-spin align-middle mr-1.5"></span>
          {{ option.label }}
        </button>
      </ng-container>
      <div *ngIf="revertOptions.length === 0" class="devmode-no-revert w-full text-left px-4 py-2.5 text-sm text-white/60">No revert options available</div>
      <div class="devmode-options-section px-4 py-3 border-b border-white/10">
        <div class="text-sm font-medium text-white mb-2">Options</div>
        <div class="flex items-center justify-between mb-2">
          <span class="text-xs text-white/60">Disable DiscDB</span>
          <button
            type="button"
            class="devmode-toggle relative w-11 h-6 rounded-full transition-colors cursor-pointer"
            [ngClass]="discdbDisabled ? 'bg-green-500' : 'bg-white/20'"
            [attr.aria-pressed]="discdbDisabled"
            (click)="toggleDiscdbDisabled()">
            <span class="absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform" [class.translate-x-5]="discdbDisabled"></span>
          </button>
        </div>
        <div class="flex items-center justify-between mb-2">
          <span class="text-xs text-white/60">Quick Post-Process Tests</span>
          <button
            type="button"
            class="devmode-toggle relative w-11 h-6 rounded-full transition-colors cursor-pointer"
            [ngClass]="quickPostProcessTestsEnabled ? 'bg-green-500' : 'bg-white/20'"
            [attr.aria-pressed]="quickPostProcessTestsEnabled"
            (click)="toggleQuickPostProcessTests()">
            <span class="absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform" [class.translate-x-5]="quickPostProcessTestsEnabled"></span>
          </button>
        </div>
        <div class="flex items-center justify-between">
          <span class="text-xs text-white/60">Disable FFmpeg Detection</span>
          <button
            type="button"
            class="devmode-toggle relative w-11 h-6 rounded-full transition-colors cursor-pointer"
            [ngClass]="!ffmpegDetectionEnabled ? 'bg-green-500' : 'bg-white/20'"
            [attr.aria-pressed]="!ffmpegDetectionEnabled"
            (click)="toggleFfmpegDetection()">
            <span class="absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform" [class.translate-x-5]="!ffmpegDetectionEnabled"></span>
          </button>
        </div>
      </div>
      <button
        type="button"
        class="devmode-menu-item w-full text-left px-4 py-2.5 text-sm text-white/80 hover:bg-white/5 flex items-center disabled:opacity-50 disabled:cursor-not-allowed border-b border-white/10"
        [disabled]="!discId || relookingUpDiscdb"
        (click)="relookupDiscdb()">
        <span *ngIf="relookingUpDiscdb" class="inline-block w-3.5 h-3.5 rounded-full border-2 border-white/35 border-t-white animate-spin align-middle mr-1.5"></span>
        Re-lookup DiscDB
      </button>
      <button
        type="button"
        class="devmode-menu-item w-full text-left px-4 py-2.5 text-sm text-white/80 hover:bg-white/5 last:rounded-b-[0.5rem]"
        (click)="testNotifications()">
        Test Notifications
      </button>
    </div>
  `,
})
export class DevmodeMenuComponent implements OnInit {
  @Input() jobStatus: JobStatus | null = null;
  @Input() jobId: string | null = null;
  @Input() discId: string | null = null;
  @Input() skipNavigation: boolean = false; // If true, don't navigate to ripper page after revert
  @Output() reverted = new EventEmitter<void>(); // Emit when a revert is completed

  revertOptions: RevertOption[] = [];
  reverting: 'finalize' | 'postprocess' | 'transfer' | null = null;
  discFinalized: boolean | null = null; // Cache disc finalized status
  quickPostProcessTestsEnabled = false;
  ffmpegDetectionEnabled = true;
  discdbDisabled = false;
  relookingUpDiscdb = false;

  constructor(
    private jobSvc: JobService,
    private workflowService: WorkflowService,
    private metadataSvc: MetadataService,
    private systemSvc: SystemService,
    private toast: ToastService,
    private router: Router,
    private setupModalSvc: SetupModalService,
  ) {}

  ngOnInit(): void {
    // If jobId is provided, fetch the job status
    if (this.jobId && !this.jobStatus) {
      this.jobSvc.getJobStatus(this.jobId).subscribe(status => {
        this.jobStatus = status;
        if (status?.disc_id && !this.discId) {
          this.discId = status.disc_id;
          this.fetchDiscFinalizedStatus();
        }
        this.updateRevertOptions();
      });
    }
    
    // Subscribe to job status updates if on ripper page (no explicit jobStatus or jobId provided)
    if (!this.jobStatus && !this.jobId) {
      this.workflowService.getJobStatus$().subscribe((status: JobStatus | null) => {
        this.jobStatus = status;
        if (status?.disc_id && !this.discId) {
          this.discId = status.disc_id;
          this.fetchDiscFinalizedStatus();
        }
        this.updateRevertOptions();
      });
    }
    
    // Fetch disc finalized status if discId is already provided
    if (this.discId) {
      this.fetchDiscFinalizedStatus();
    }
    
    this.updateRevertOptions();
    this.loadQuickPostProcessTestsState();
    this.loadFfmpegDetectionState();
    this.loadDiscdbDisabledState();
  }

  private loadDiscdbDisabledState(): void {
    this.systemSvc.getDiscdbDisabled().subscribe({
      next: (r) => {
        this.discdbDisabled = r.disabled;
        // When DiscDB is disabled, force the miss workflow regardless of
        // any cached `discdb_result='hit'` on existing jobs. Passing
        // false (the override value) means "discdbHit = false" so the
        // UI renders the full labeling flow.
        this.workflowService.setWorkflowModeOverride(r.disabled ? false : null);
      },
      error: () => {
        this.discdbDisabled = false;
        this.workflowService.setWorkflowModeOverride(null);
      },
    });
  }

  toggleDiscdbDisabled(): void {
    const next = !this.discdbDisabled;
    this.systemSvc.setDiscdbDisabled(next).subscribe({
      next: (r) => {
        this.discdbDisabled = r.disabled;
        this.workflowService.setWorkflowModeOverride(r.disabled ? false : null);
        // Only force-update the active context when we're forcing miss;
        // disabling the override (toggle off) leaves backend hit/miss
        // intact and lets the next context fetch settle the value.
        if (r.disabled) {
          this.workflowService.updateContext({ discdbHit: false });
        }
      },
      error: () => { /* keep current */ },
    });
  }

  private loadQuickPostProcessTestsState(): void {
    this.systemSvc.getQuickPostProcessTestsEnabled().subscribe({
      next: (r) => { this.quickPostProcessTestsEnabled = r.enabled; },
      error: () => { this.quickPostProcessTestsEnabled = false; },
    });
  }

  private loadFfmpegDetectionState(): void {
    this.systemSvc.getFfmpegDetectionEnabled().subscribe({
      next: (r) => { this.ffmpegDetectionEnabled = r.enabled; },
      error: () => { this.ffmpegDetectionEnabled = true; },
    });
  }

  toggleQuickPostProcessTests(): void {
    const next = !this.quickPostProcessTestsEnabled;
    this.systemSvc.setQuickPostProcessTestsEnabled(next).subscribe({
      next: (r) => { this.quickPostProcessTestsEnabled = r.enabled; },
      error: () => { /* keep current */ },
    });
  }

  toggleFfmpegDetection(): void {
    const next = !this.ffmpegDetectionEnabled;
    this.systemSvc.setFfmpegDetectionEnabled(next).subscribe({
      next: (r) => { this.ffmpegDetectionEnabled = r.enabled; },
      error: () => { /* keep current */ },
    });
  }

  relookupDiscdb(): void {
    if (!this.discId || this.relookingUpDiscdb) return;
    this.relookingUpDiscdb = true;
    this.systemSvc.relookupDiscdb(this.discId).subscribe({
      next: (r) => {
        this.relookingUpDiscdb = false;
        if (r.result === 'hit') {
          this.toast.show('DiscDB hit — titles refreshed', 'success', 2500);
        } else {
          this.toast.show('DiscDB miss — no metadata found', 'info', 2500);
        }
      },
      error: (err) => {
        this.relookingUpDiscdb = false;
        this.toast.show(err?.error?.detail || 'Re-lookup failed', 'error', 3000);
      },
    });
  }

  openSetup(): void {
    this.setupModalSvc.open();
  }

  testNotifications(): void {
    this.toast.show('Info notification test', 'info', 2000);
    setTimeout(() => {
      this.toast.show('Success notification test', 'success', 2000);
    }, 500);
    setTimeout(() => {
      this.toast.show('Warning notification test', 'warning', 2000);
    }, 1000);
    setTimeout(() => {
      this.toast.show('Error notification test', 'error', 2000);
    }, 1500);
  }

  private fetchDiscFinalizedStatus(): void {
    if (!this.discId) return;
    
    this.metadataSvc.getDiscById(this.discId).subscribe({
      next: (result) => {
        this.discFinalized = result?.disc?.finalized ?? false;
        this.updateRevertOptions();
      },
      error: () => {
        // If fetch fails, set to null to allow backend validation
        this.discFinalized = null;
      }
    });
  }

  private updateRevertOptions(): void {
    const options: RevertOption[] = [];
    
    // Determine which stages are completed
    const status = this.jobStatus;
    
    // Need at least a job status or jobId to show options
    if (!status && !this.jobId) {
      this.revertOptions = [];
      return;
    }

    // Determine which stage is currently being reverted (for precedence logic)
    const revertingStage = this.reverting;
    const stageOrder = { finalize: 0, postprocess: 1, transfer: 2 };
    const revertingStageOrder = revertingStage ? stageOrder[revertingStage] : -1;

    // For finalize, check if disc is finalized (requires discId)
    if (this.discId || status?.disc_id) {
      const discId = this.discId || status?.disc_id;
      
      // Show finalize revert if disc is finalized (check cached value or allow backend validation)
      // Only show if no earlier stage revert is in progress
      const shouldShowFinalize = (this.discFinalized === true) || (this.discFinalized === null && discId);
      const canRevertFinalize = !revertingStage || revertingStageOrder >= stageOrder.finalize;
      
      if (shouldShowFinalize && discId && canRevertFinalize) {
        this.discId = discId;
        options.push({
          label: 'Revert Finalize',
          stage: 'finalize',
          enabled: !revertingStage, // Disable if any revert is in progress
          action: () => this.revertFinalize(),
        });
      }
    }

    // For postprocess, check if post_state is completed
    // Only show if finalize is not being reverted (finalize takes precedence)
    const postState = status?.pipeline?.['postprocess'] || status?.post_state;
    const canRevertPostprocess = !revertingStage || revertingStageOrder >= stageOrder.postprocess;
    
    if ((postState === 'completed' || postState === 'failed') && canRevertPostprocess) {
      options.push({
        label: 'Revert Post-Process',
        stage: 'postprocess',
        enabled: !revertingStage, // Disable if any revert is in progress
        action: () => this.revertPostprocess(),
      });
    }

    // For transfer, check if transfer_state is completed
    // Only show if finalize/postprocess are not being reverted (they take precedence)
    const transferState = status?.pipeline?.['transfer'] || status?.transfer_state;
    const canRevertTransfer = !revertingStage || revertingStageOrder >= stageOrder.transfer;
    
    if (transferState === 'completed' && canRevertTransfer) {
      options.push({
        label: 'Revert Transfer',
        stage: 'transfer',
        enabled: !revertingStage, // Disable if any revert is in progress
        action: () => this.revertTransfer(),
      });
    }

    // Sort options by stage order (finalize, postprocess, transfer)
    options.sort((a, b) => stageOrder[a.stage] - stageOrder[b.stage]);
    
    this.revertOptions = options;
  }

  private getJobId(): string | null {
    if (this.jobId) return this.jobId;
    if (this.jobStatus?.jobId) return this.jobStatus.jobId;
    return null;
  }

  private revertFinalize(): void {
    // Prevent concurrent reverts
    if (this.reverting) {
      return;
    }

    const discId = this.discId || this.jobStatus?.disc_id;
    if (!discId) {
      this.toast.show('Disc ID not available', 'error');
      return;
    }

    if (!confirm('Are you sure you want to revert finalization? This will restore the backup and reset the finalized state. Dependent stages (post-process, transfer) will also be reverted if they were completed.')) {
      return;
    }

    this.reverting = 'finalize';
    this.updateRevertOptions(); // Update to disable other options
    
    this.metadataSvc.revertDiscFinalization(discId).subscribe({
      next: () => {
        this.toast.show('Finalization reverted successfully', 'success');
        this.reverting = null;
        this.discFinalized = false; // Update cached status
        // Refresh job status if available
        const jobId = this.getJobId();
        if (jobId) {
          this.jobSvc.refreshJobStatus(jobId).subscribe(status => {
            this.jobStatus = status;
            if (status?.disc_id) {
              this.fetchDiscFinalizedStatus(); // Refresh disc status
            }
            this.updateRevertOptions();
          });
        }
        // Emit event for parent component to handle refresh
        this.reverted.emit();
        // Navigate to ripper page if not already there and navigation is not skipped
        if (!this.skipNavigation && this.router.url !== '/activity') {
          this.router.navigate(['/activity']);
        }
      },
      error: (err) => {
        this.toast.show(err?.error?.detail || 'Failed to revert finalization', 'error');
        this.reverting = null;
        this.updateRevertOptions(); // Re-enable options
      },
    });
  }

  private revertPostprocess(): void {
    // Prevent concurrent reverts
    if (this.reverting) {
      return;
    }

    const jobId = this.getJobId();
    if (!jobId) {
      this.toast.show('Job ID not available', 'error');
      return;
    }

    if (!confirm('Are you sure you want to revert post-processing? This will restore the backup and reset the post-process state. Dependent stages (transfer) will also be reverted if they were completed.')) {
      return;
    }

    this.reverting = 'postprocess';
    this.updateRevertOptions(); // Update to disable other options
    
    this.jobSvc.restorePostprocess(jobId).subscribe({
      next: (result) => {
        this.toast.show('Post-process reverted successfully', 'success');
        this.reverting = null;
        // Refresh job status if available
        this.jobSvc.refreshJobStatus(jobId).subscribe(status => {
          this.jobStatus = status;
          this.updateRevertOptions();
        });
        // Emit event for parent component to handle refresh
        this.reverted.emit();
        // Navigate to ripper page if not already there and navigation is not skipped
        if (!this.skipNavigation && this.router.url !== '/activity') {
          this.router.navigate(['/activity']);
        }
      },
      error: (err) => {
        this.toast.show(err?.error?.detail || 'Failed to revert post-process', 'error');
        this.reverting = null;
        this.updateRevertOptions(); // Re-enable options
      },
    });
  }

  private revertTransfer(): void {
    // Prevent concurrent reverts
    if (this.reverting) {
      return;
    }

    const jobId = this.getJobId();
    if (!jobId) {
      this.toast.show('Job ID not available', 'error');
      return;
    }

    if (!confirm('Are you sure you want to revert transfer? This will restore the backup and reset the transfer state.')) {
      return;
    }

    this.reverting = 'transfer';
    this.updateRevertOptions(); // Update to disable other options
    
    this.jobSvc.revertTransfer(jobId).subscribe({
      next: () => {
        this.toast.show('Transfer reverted successfully', 'success');
        this.reverting = null;
        // Refresh job status if available
        this.jobSvc.refreshJobStatus(jobId).subscribe(status => {
          this.jobStatus = status;
          this.updateRevertOptions();
        });
        // Emit event for parent component to handle refresh
        this.reverted.emit();
        // Navigate to ripper page if not already there and navigation is not skipped
        if (!this.skipNavigation && this.router.url !== '/activity') {
          this.router.navigate(['/activity']);
        }
      },
      error: (err) => {
        this.toast.show(err?.error?.detail || 'Failed to revert transfer', 'error');
        this.reverting = null;
        this.updateRevertOptions(); // Re-enable options
      },
    });
  }
}

