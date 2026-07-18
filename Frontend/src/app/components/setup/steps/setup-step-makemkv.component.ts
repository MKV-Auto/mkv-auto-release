import { Component, ElementRef, EventEmitter, Input, OnChanges, OnInit, OnDestroy, Output, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { IconComponent } from '../../../ui/icon/icon.component';
import { BtnComponent } from '../../../ui/btn/btn.component';
import {
  DiscWorkflowBlockReason,
  MakeMKVDownloadState,
  MakeMKVHealth,
  SystemService,
} from '../../../services/system.service';
import { WorkflowService } from '../../../services/workflow.service';
import { Subscription } from 'rxjs';
import { filter } from 'rxjs/operators';
import { environment } from '../../../environments/environment';

export interface MakemkvStepData {
  key: string;
  valid: boolean;
  installed: boolean;
  disc_workflow_blocked?: boolean;
  disc_workflow_block_reason?: DiscWorkflowBlockReason;
}

@Component({
  selector: 'app-setup-step-makemkv',
  standalone: true,
  imports: [CommonModule, FormsModule, IconComponent, BtnComponent],
  template: `
    <!-- Phase 1: Checking if MakeMKV is installed -->
    <div *ngIf="checkingInstall" class="setup-step setup-step-center">
      <div class="setup-step-spinner-lg"></div>
      <h3 class="setup-step-title">Checking MakeMKV Installation</h3>
      <p class="setup-step-desc">Please wait while we verify your system...</p>
    </div>

    <!-- Phase 2: Not installed - show install UI -->
    <div *ngIf="!checkingInstall && !data.installed" class="setup-step">
      <div class="setup-step-header">
        <div class="setup-step-icon setup-step-icon-amber">
          <ui-icon name="download" [size]="24"></ui-icon>
        </div>
        <div class="setup-step-header-text">
          <h3 class="setup-step-title">Install MakeMKV</h3>
          <p class="setup-step-desc">MakeMKV is required to rip Blu-ray and DVD discs but is not currently installed. Click the button below to install the latest version automatically.</p>
        </div>
      </div>
      <div *ngIf="error" class="setup-step-message setup-step-message-error">
        <span>{{ error }}</span>
      </div>
      <div class="setup-step-install-options">
        <h5 class="setup-step-options-title">Installation Options</h5>
        <div class="setup-step-option-row">
          <input type="checkbox" id="include-advanced-features" [(ngModel)]="includeAdvancedFeatures" [disabled]="installing" class="setup-step-checkbox" />
          <label for="include-advanced-features" class="setup-step-option-label">
            Include advanced FFmpeg features (recommended)
            <span style="display: block; font-size: 0.75rem; color: rgba(255,255,255,0.6); margin-top: 0.25rem;">
              Enables non-free codecs (libfdk-aac) for improved AAC decoding quality. FFmpeg will be built regardless of this option.
            </span>
          </label>
        </div>
        <button type="button" class="setup-step-btn setup-step-btn-amber"
                [disabled]="installing || downloadState === 'downloading'"
                (click)="handleInstall()">
          <span *ngIf="installing || downloadState === 'downloading'" class="setup-step-spinner"></span>
          {{ installing
              ? 'Installing MakeMKV...'
              : (downloadState === 'downloading' ? 'Downloading MakeMKV...' : 'Install MakeMKV') }}
        </button>
        <p *ngIf="!installing && (downloadState === 'failed' || downloadState === 'missing')"
           class="setup-step-desc" style="margin: 0.25rem 0 0 0; font-size: 0.75rem; color: rgba(255,255,255,0.55);">
          Source pre-download unavailable — the installer will download inline (this may take longer).
        </p>
        <div *ngIf="installLogs.length > 0 || downloadStatusDisplay" class="setup-step-logs" #logsContainer>
          <div *ngFor="let log of installLogs" class="setup-step-log-line">{{ log }}</div>
          <div *ngIf="downloadStatusDisplay" class="setup-step-log-line setup-step-log-line-live">{{ downloadStatusDisplay }}</div>
        </div>
        <div *ngIf="disconnected && currentJobId && installing" class="setup-step-reconnect">
          <p class="setup-step-desc">Connection lost. Refresh the page to see latest status.</p>
          <button type="button" class="setup-step-btn setup-step-btn-reconnect" (click)="refreshPage()">
            Refresh page
          </button>
        </div>
      </div>
      <div class="setup-step-info">
        <p class="setup-step-info-title">About MakeMKV</p>
        <ul>
          <li>MakeMKV is the industry standard for ripping Blu-ray and DVD content</li>
          <li>This installer will download and configure MakeMKV automatically</li>
          <li>For MakeMKV licensing information please visit <a href="https://www.makemkv.com/" target="_blank" rel="noopener noreferrer" class="setup-step-link">makemkv.com</a></li>
        </ul>
        <p class="setup-step-info-title" style="margin-top: 1rem;">⚠️ License Terms</p>
        <ul>
          <li>MakeMKV is proprietary shareware (free during beta period)</li>
          <li>By clicking Install, you agree to MakeMKV's End User License Agreement</li>
          <li [ngSwitch]="downloadState">
            <ng-container *ngSwitchCase="'ready'">
              The latest version of MakeMKV has been downloaded, review the
              <a [href]="eulaUrl" target="_blank" rel="noopener noreferrer" class="setup-step-link">End User License Agreement</a>
              before installation.
            </ng-container>
            <ng-container *ngSwitchCase="'downloading'">
              Downloading MakeMKV sources
              <span class="setup-step-spinner" style="display: inline-block; vertical-align: middle; width: 0.75rem; height: 0.75rem; margin-left: 0.25rem;"></span>
              — the End User License Agreement will be linked here once the download completes.
            </ng-container>
            <ng-container *ngSwitchDefault>
              The End User License Agreement will be downloaded with MakeMKV when you click Install.
            </ng-container>
          </li>
          <li>Learn more at <a href="https://www.makemkv.com/" target="_blank" rel="noopener noreferrer" class="setup-step-link">makemkv.com</a></li>
        </ul>
      </div>
    </div>

    <!-- Phase 3: Installed - show registration UI -->
    <div *ngIf="!checkingInstall && data.installed" class="setup-step">
      <div class="setup-step-header">
        <div class="setup-step-icon setup-step-icon-blue">
          <ui-icon name="link" [size]="24"></ui-icon>
        </div>
        <div class="setup-step-header-text">
          <h3 class="setup-step-title">MakeMKV Registration Key</h3>
          <p class="setup-step-desc">MakeMKV is required to rip Blu-ray and DVD discs. Enter your registration key to get started. You can purchase a key from <a href="https://www.makemkv.com/buy/" target="_blank" rel="noopener noreferrer" class="setup-step-link" style="display: inline-flex; align-items: center; gap: 0.25rem;">makemkv.com<ui-icon name="external" [size]="12"></ui-icon></a> or use a beta key while in trial.</p>
        </div>
      </div>
      <div class="setup-step-body">
        <div
          *ngIf="data.disc_workflow_blocked && !data.valid"
          class="setup-step-message setup-step-message-error"
          style="display: flex; align-items: start; gap: 0.5rem; margin-bottom: 0.75rem;"
        >
          <ui-icon name="info" [size]="16" style="flex-shrink: 0; margin-top: 0.125rem;"></ui-icon>
          <span>{{ discWorkflowBlockHint }}</span>
        </div>
        <label class="setup-step-label">Registration Key <span class="text-red-400">*</span></label>
        <div class="setup-step-input-wrap">
          <input
            type="text"
            class="setup-step-input"
            [class.setup-step-input-error]="error"
            [class.setup-step-input-valid]="data.valid"
            [(ngModel)]="localKey"
            (ngModelChange)="onKeyChange($event)"
            placeholder="T-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            [disabled]="validating || data.valid"
          />
          <ui-icon *ngIf="data.valid" name="check-circle" [size]="20" class="setup-step-input-check" style="color: #22c55e;"></ui-icon>
        </div>
        <div *ngIf="error" class="setup-step-message setup-step-message-error" style="display: flex; align-items: start; gap: 0.5rem;">
          <ui-icon name="info" [size]="16" style="flex-shrink: 0; margin-top: 0.125rem;"></ui-icon>
          <span>{{ error }}</span>
        </div>
        <div *ngIf="data.valid" class="setup-step-message setup-step-message-success" style="display: flex; align-items: start; gap: 0.5rem;">
          <ui-icon name="check-circle" [size]="16" style="flex-shrink: 0; margin-top: 0.125rem;"></ui-icon>
          <span>Registration key validated successfully!</span>
        </div>
        <button
          *ngIf="!data.valid"
          type="button"
          class="setup-step-btn setup-step-btn-primary"
          [disabled]="!data.key || validating"
          (click)="validate()"
        >
          <ui-icon *ngIf="validating" name="spinner" [size]="16" style="animation: setup-spin 0.8s linear infinite; display: inline-flex;"></ui-icon>
          {{ validating ? 'Validating...' : 'Validate Key' }}
        </button>
        <div class="setup-step-info">
          <p class="setup-step-info-title">💡 Where to find your key</p>
          <ul>
            <li>Purchase a permanent key from MakeMKV's website</li>
            <li>Use a free beta key (updated monthly on the MakeMKV forum)</li>
            <li>Keys start with "T-" followed by a long string of characters</li>
          </ul>
        </div>
        <!-- DevMode: Quick test key option -->
        <div *ngIf="!environment.production" class="setup-step-info" style="background: rgba(245, 158, 11, 0.08); border-color: rgba(245, 158, 11, 0.2);">
          <p class="setup-step-info-title">🔧 Developer Mode</p>
          <button 
            type="button" 
            class="setup-step-btn setup-step-btn-primary" 
            (click)="useTestKey()"
            style="margin-top: 0.5rem;">
            Use DevMode Test Key
          </button>
        </div>
      </div>
    </div>
  `,
  styles: [`
    .setup-step { }
    .setup-step-center { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 4rem 0; gap: 1.5rem; text-align: center; }
    .setup-step-spinner-lg { width: 4rem; height: 4rem; border-radius: 50%; border: 3px solid rgba(59, 130, 246, 0.3); border-top-color: #3b82f6; animation: setup-spin 0.8s linear infinite; }
    .setup-step-header { display: flex; gap: 1rem; align-items: flex-start; margin-bottom: 1.5rem; }
    .setup-step-icon { width: 3rem; height: 3rem; border-radius: 0.75rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .setup-step-icon-blue { background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); box-shadow: 0 0 20px rgba(59, 130, 246, 0.3); color: #fff; }
    .setup-step-icon-amber { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); box-shadow: 0 0 20px rgba(245, 158, 11, 0.3); color: #fff; }
    .setup-step-header-text { flex: 1; }
    .setup-step-title { font-size: 1.125rem; font-weight: 700; color: #fff; margin: 0 0 0.5rem 0; }
    .setup-step-desc { font-size: 0.875rem; color: rgba(255,255,255,0.7); margin: 0; }
    .setup-step-link { color: #93c5fd; }
    .setup-step-link:hover { color: #bfdbfe; }
    .setup-step-body { display: flex; flex-direction: column; gap: 0.75rem; }
    .setup-step-install-options { padding: 1.5rem; border-radius: 0.75rem; background: rgba(245, 158, 11, 0.05); border: 1px solid rgba(245, 158, 11, 0.2); margin-bottom: 1rem; display: flex; flex-direction: column; gap: 1rem; }
    .setup-step-options-title { font-size: 0.875rem; font-weight: 700; color: #fff; margin: 0; }
    .setup-step-option-row { display: flex; align-items: flex-start; gap: 0.75rem; }
    .setup-step-checkbox { width: 1rem; height: 1rem; border-radius: 0.25rem; flex-shrink: 0; margin-top: 0.125rem; cursor: pointer; }
    .setup-step-option-label { font-size: 0.875rem; color: rgba(255,255,255,0.8); flex: 1; line-height: 1.5; cursor: pointer; }
    .setup-step-logs { padding: 0.75rem; border-radius: 0.5rem; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); max-height: 10rem; overflow-y: auto; margin-top: 1rem; }
    .setup-step-log-line { font-size: 0.75rem; font-family: monospace; color: #4ade80; margin-bottom: 0.25rem; }
    .setup-step-log-line-live { color: #93c5fd; }
    .setup-step-btn-amber { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); box-shadow: 0 0 20px rgba(245, 158, 11, 0.3); }
    .setup-step-btn-amber:disabled { opacity: 0.6; cursor: not-allowed; }
    .setup-step-reconnect { margin-top: 1rem; }
    .setup-step-btn-reconnect { background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); color: #fff; font-weight: 500; padding: 0.625rem 1rem; border-radius: 0.5rem; border: none; cursor: pointer; display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; }
    .setup-step-btn-reconnect:hover { transform: scale(1.02); }
    .setup-step-message { padding: 0.75rem 1rem; border-radius: 0.5rem; font-size: 0.875rem; }
    .setup-step-message-error { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); color: #f87171; margin-bottom: 1rem; }
    .setup-step-message-success { background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.3); color: #4ade80; }
    .setup-step-label { font-size: 0.875rem; font-weight: 500; color: rgba(255,255,255,0.8); }
    .setup-step-input-wrap { position: relative; }
    .setup-step-input { width: 100%; padding: 0.75rem 1rem; padding-right: 3rem; font-size: 0.875rem; font-family: monospace; color: #fff; border-radius: 0.5rem; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); }
    .setup-step-input:disabled { opacity: 0.6; cursor: not-allowed; }
    .setup-step-input-error { border-color: rgba(239, 68, 68, 0.5); }
    .setup-step-input-valid { border-color: rgba(34, 197, 94, 0.5); }
    .setup-step-input-check { position: absolute; right: 1rem; top: 50%; transform: translateY(-50%); color: #22c55e; font-weight: bold; }
    .setup-step-btn { width: 100%; padding: 0.75rem 1rem; border-radius: 0.5rem; font-size: 0.875rem; font-weight: 500; color: #fff; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 0.5rem; }
    .setup-step-btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .setup-step-btn-primary { background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); box-shadow: 0 0 15px rgba(59, 130, 246, 0.3); }
    .setup-step-btn-primary:not(:disabled) { background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); }
    .setup-step-spinner { width: 1rem; height: 1rem; border: 2px solid rgba(255,255,255,0.3); border-top-color: #fff; border-radius: 50%; animation: setup-spin 0.8s linear infinite; }
    @keyframes setup-spin { to { transform: rotate(360deg); } }
    .setup-step-info { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; background: rgba(59, 130, 246, 0.08); border: 1px solid rgba(59, 130, 246, 0.2); }
    .setup-step-info-title { color: #93c5fd; font-weight: 500; margin: 0 0 0.5rem 0; }
    .setup-step-info ul { margin: 0; padding-left: 1rem; color: rgba(191, 219, 254, 0.8); }
  `],
})
export class SetupStepMakemkvComponent implements OnChanges, OnInit, OnDestroy {
  @Input() data!: MakemkvStepData;
  @Output() dataChange = new EventEmitter<Partial<MakemkvStepData>>();
  @ViewChild('logsContainer') logsContainer?: ElementRef;

  // Expose environment for template access
  environment = environment;

  localKey = '';
  error: string | null = null;
  validating = false;
  checkingInstall = true;
  installing = false;
  installLogs: string[] = [];
  includeAdvancedFeatures = true;
  
  // WebSocket + HTTP polling state
  currentJobId: string | null = null;
  disconnected = false;
  private wsSubscription: Subscription | null = null;
  private connectionMonitorSubscription: Subscription | null = null;
  
  // Diagnostic tracking
  private lastMessageTime = Date.now();
  private messageCount = 0;
  private connectionStart = 0;

  /** Current download status line (with optional client-side countdown). */
  downloadStatusLine = '';
  /** Seconds remaining for download timeout; decremented every second for live countdown. */
  downloadRemainingSeconds: number | null = null;
  private countdownInterval: ReturnType<typeof setInterval> | null = null;

  /**
   * MakeMKV source pre-download state (#625) — drives the EULA link and gates
   * the Install button while the container startup task is still fetching.
   */
  downloadState: MakeMKVDownloadState = 'missing';
  eulaUrl = '';
  private predownloadPollHandle: ReturnType<typeof setInterval> | null = null;

  constructor(
    private system: SystemService,
    private workflowService: WorkflowService
  ) {
    this.eulaUrl = this.system.getMakeMKVEulaUrl();
  }

  ngOnInit(): void {
    this.checkMakeMKVInstall();
    this.checkAndReattachActiveJob();
  }

  ngOnChanges(): void {
    if (this.data) {
      this.localKey = this.data.key ?? '';
    }
  }

  ngOnDestroy(): void {
    this.cleanup();
  }

  /** User-facing hint when disc workflow is blocked but MakeMKV binary is installed. */
  get discWorkflowBlockHint(): string {
    const r = this.data?.disc_workflow_block_reason;
    if (r === 'makemkv_error') {
      return 'Disc detection had a problem after install. You can continue setup; check logs or retry after a restart if ripping fails.';
    }
    return 'Enter and validate your MakeMKV registration key to use disc ripping.';
  }

  private emitInstallStateFromHealth(health: MakeMKVHealth): void {
    const installed = health.valid && health.can_rip;
    this.dataChange.emit({
      installed,
      disc_workflow_blocked: health.disc_workflow_blocked ?? false,
      disc_workflow_block_reason: health.disc_workflow_block_reason ?? 'none',
    });
    this.updateDownloadStateFromHealth(health);
  }

  /**
   * Reflect the backend pre-download state (#625) into the component and
   * schedule polling while the container startup task is mid-download.
   */
  private updateDownloadStateFromHealth(health: MakeMKVHealth): void {
    const next = health.download?.state ?? 'missing';
    this.downloadState = next;
    if (next === 'downloading') {
      this.ensurePredownloadPoll();
    } else {
      this.stopPredownloadPoll();
    }
  }

  private ensurePredownloadPoll(): void {
    if (this.predownloadPollHandle) return;
    this.predownloadPollHandle = setInterval(() => {
      this.system.getMakeMKVHealth().subscribe({
        next: (h) => this.updateDownloadStateFromHealth(h),
        // If the poll fails transiently, keep the interval running.
      });
    }, 3000);
  }

  private stopPredownloadPoll(): void {
    if (this.predownloadPollHandle) {
      clearInterval(this.predownloadPollHandle);
      this.predownloadPollHandle = null;
    }
  }

  checkMakeMKVInstall(): void {
    this.checkingInstall = true;
    this.system.getMakeMKVHealth().subscribe({
      next: (health) => {
        this.emitInstallStateFromHealth(health);
        this.checkingInstall = false;
      },
      error: () => {
        this.dataChange.emit({ installed: false });
        this.checkingInstall = false;
      },
    });
  }

  handleInstall(): void {
    // Always POST to start; backend returns existing job_id if an install is already running.
    this.startNewInstall();
  }

  private startNewInstall(): void {
    this.installing = true;
    this.error = null;
    this.installLogs = [];
    this.disconnected = false;

    this.system.startMakeMKVUpdate({ ffmpeg_advanced_features: this.includeAdvancedFeatures }).subscribe({
      next: (res) => {
        this.currentJobId = res.jobId;
        // Preload existing logs when backend returned an already-running job
        this.system.getMakeMKVUpdateJob(res.jobId).subscribe({
          next: (status) => {
            this.installLogs = status.logs ? [...status.logs] : [];
            if (status.status === 'completed') {
              this.handleInstallComplete();
              return;
            }
            if (status.status === 'failed') {
              this.error = status.error ?? 'Installation failed';
              this.installing = false;
              this.cleanup();
              return;
            }
            this.subscribeToUpdates(res.jobId);
          },
          error: () => this.subscribeToUpdates(res.jobId),
        });
      },
      error: (err) => {
        console.error('[MakeMKV] Failed to start installation:', err);
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to start installation';
        this.installing = false;
        this.currentJobId = null;
      },
    });
  }

  /** Reattach to an in-progress install (after refresh or when install already running). */
  private reattachToJob(data: { jobId?: string | null; status?: string | null; logs?: string[] | null; error?: string | null }): void {
    if (!data.jobId) return;
    this.currentJobId = data.jobId;
    this.installing = true;
    this.error = null;
    this.installLogs = data.logs ? [...data.logs] : [];
    this.disconnected = false;
    if (data.status === 'failed' && data.error) {
      this.error = data.error;
      this.installing = false;
      this.cleanup();
      return;
    }
    if (data.status === 'completed') {
      this.handleInstallComplete();
      return;
    }
    this.subscribeToUpdates(data.jobId);
  }

  /** On load: if an install is already running, show its status (no user action). */
  private checkAndReattachActiveJob(): void {
    this.system.getMakeMKVUpdateActive().subscribe({
      next: (active) => {
        if (!active.active || !active.jobId) return;
        if (active.status === 'running' || active.status === 'pending') {
          this.reattachToJob(active);
        }
      },
      error: () => {},
    });
  }

  private subscribeToUpdates(jobId: string): void {
    this.connectionStart = Date.now();
    this.lastMessageTime = Date.now();
    this.messageCount = 0;
    
    // Subscribe to WebSocket messages
    this.wsSubscription = this.workflowService.makemkvUpdateMessages$
      .pipe(
        filter(msg => msg.job_id === jobId)
      )
      .subscribe(msg => {
        const now = Date.now();
        const sinceLastMessage = now - this.lastMessageTime;
        const connectionAge = (now - this.connectionStart) / 1000;
        this.lastMessageTime = now;
        this.messageCount++;
        
        if (msg.type === 'makemkv_update_log') {
          if (msg.line) {
            this.installLogs = [...this.installLogs, msg.line];
            this.scrollToBottom();
            this.updateDownloadCountdown(msg.line);
          }
        } else if (msg.type === 'makemkv_update_status') {
          if (msg.status === 'completed') {
            this.handleInstallComplete();
          } else if (msg.status === 'failed') {
            this.error = msg.error || 'Installation failed';
            this.installing = false;
            this.cleanup();
          }
        }
      });

    // Show "Connection lost" when WebSocket disconnects; no HTTP polling - user refreshes to see latest.
    this.connectionMonitorSubscription = this.workflowService.coordinatorConnected$
      .subscribe(connected => {
        if (!connected && this.installing) {
          this.disconnected = true;
        } else if (connected) {
          this.disconnected = false;
        }
      });
  }

  private handleInstallComplete(): void {
    // Verify installation with backend health check before marking as complete
    this.system.getMakeMKVHealth().subscribe({
      next: (health) => {
        if (health.valid && health.can_rip) {
          this.emitInstallStateFromHealth(health);
          this.installing = false;
          this.cleanup();
        } else {
          // Installation reported complete but health check failed
          this.error = health.error || 'Installation completed but MakeMKV is not ready. Please try again.';
          this.installing = false;
          this.cleanup();
        }
      },
      error: (err) => {
        console.error('[MakeMKV] Failed to verify installation:', err);
        this.error = 'Failed to verify installation. Please try again.';
        this.installing = false;
        this.cleanup();
      }
    });
  }

  private cleanup(): void {
    if (this.wsSubscription) {
      this.wsSubscription.unsubscribe();
      this.wsSubscription = null;
    }
    if (this.connectionMonitorSubscription) {
      this.connectionMonitorSubscription.unsubscribe();
      this.connectionMonitorSubscription = null;
    }
    this.stopDownloadCountdown();
    this.stopPredownloadPoll();
    this.downloadStatusLine = '';
    this.downloadRemainingSeconds = null;
    this.currentJobId = null;
    this.disconnected = false;
  }

  refreshPage(): void {
    window.location.reload();
  }

  onKeyChange(key: string): void {
    this.localKey = key;
    this.error = null;
    this.dataChange.emit({ key: this.localKey, valid: false });
  }

  useTestKey(): void {
    if (!environment.production) {
      this.localKey = 'T-MKVAUTO-DEVMODE-TEST-KEY-BYPASS';
      this.onKeyChange(this.localKey);
    }
  }

  validate(): void {
    const key = this.localKey?.trim() ?? '';
    if (!key) return;
    this.dataChange.emit({ key });
    this.validating = true;
    this.error = null;
    this.system.registerKey(key).subscribe({
      next: (res) => {
        this.validating = false;
        if (res && !res.expired) {
          this.system.getMakeMKVHealth().subscribe({
            next: (health) => {
              this.emitInstallStateFromHealth(health);
              this.dataChange.emit({ valid: true });
            },
            error: () => {
              this.dataChange.emit({ valid: true });
            },
          });
        } else {
          this.error = "MakeMKV didn't accept that key. Double-check for typos and paste the full key, then try again.";
        }
      },
      error: () => {
        this.validating = false;
        this.error = "MakeMKV didn't accept that key. Double-check for typos and paste the full key, then try again.";
      },
    });
  }

  private scrollToBottom(): void {
    if (this.logsContainer?.nativeElement) {
      setTimeout(() => {
        const element = this.logsContainer!.nativeElement;
        element.scrollTop = element.scrollHeight;
      }, 0);
    }
  }

  /** Parse download line for "M:SS remaining" and start or update client-side countdown. */
  private updateDownloadCountdown(line: string): void {
    const match = line.match(/(\d+):(\d{2})\s+remaining/);
    if (!match) {
      this.stopDownloadCountdown();
      this.downloadStatusLine = '';
      this.downloadRemainingSeconds = null;
      return;
    }
    const minutes = parseInt(match[1], 10);
    const seconds = parseInt(match[2], 10);
    const totalSeconds = minutes * 60 + seconds;
    const base = line.replace(/\s*[(\u2014-]\s*\d+:\d{2}\s+remaining\s*\)?\s*$/i, '').trim();
    this.downloadStatusLine = base;
    this.downloadRemainingSeconds = totalSeconds;
    this.stopDownloadCountdown();
    this.countdownInterval = setInterval(() => {
      if (this.downloadRemainingSeconds === null || this.downloadRemainingSeconds <= 0) {
        this.stopDownloadCountdown();
        this.downloadStatusLine = '';
        this.downloadRemainingSeconds = null;
        return;
      }
      this.downloadRemainingSeconds--;
    }, 1000);
  }

  private stopDownloadCountdown(): void {
    if (this.countdownInterval) {
      clearInterval(this.countdownInterval);
      this.countdownInterval = null;
    }
  }

  /** Display line for current download status with live countdown. */
  get downloadStatusDisplay(): string {
    if (!this.downloadStatusLine) return '';
    const sec = this.downloadRemainingSeconds;
    if (sec === null) return this.downloadStatusLine;
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${this.downloadStatusLine} — ${m}:${s.toString().padStart(2, '0')} remaining`;
  }
}
