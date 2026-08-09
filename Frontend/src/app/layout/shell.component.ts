import { Component, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { NavigationEnd, Router, RouterModule } from '@angular/router';
import { ToastService } from '../services/toast.service';
import { ToastContainerComponent } from '../ui/toast.component';
import { DriveService } from '../services/drive.service';
import { Subscription } from 'rxjs';
import { filter } from 'rxjs/operators';
import { JobService } from '../services/job.service';
import { SystemService } from '../services/system.service';
import { WorkflowService } from '../services/workflow.service';
import { SetupModalService, SetupModalConfig } from '../services/setup-modal.service';
import { RipSizeWarningService, RipSizeWarningPayload } from '../services/rip-size-warning.service';
import { RipSizeWarningPendingAction } from '../components/rip-size-warning-modal/rip-size-warning-modal.component';
import {
  UsbSaturationWarningPayload,
  UsbSaturationWarningService,
} from '../services/usb-saturation-warning.service';
import { UsbSaturationWarningModalComponent } from '../components/usb-saturation-warning-modal/usb-saturation-warning-modal.component';
import { LoggerService } from '../services/logger.service';
import { DevmodeHostComponent } from '../components/devmode-host/devmode-host.component';
import { SetupModalComponent } from '../components/setup/setup-modal.component';
import { PlatformGuideComponent } from '../components/setup/platform-guide.component';
import { RipSizeWarningModalComponent } from '../components/rip-size-warning-modal/rip-size-warning-modal.component';
import { NotificationHistoryService, StoredNotification } from '../services/notification-history.service';
import { UpdateBannerComponent } from '../components/update-banner/update-banner.component';
import { BrowserNotificationService } from '../services/browser-notification.service';
import { formatBackendNotificationToastText } from '../utils/backend-notification-display.util';

@Component({
  selector: 'app-shell',
  standalone: true,
  imports: [
    CommonModule,
    RouterModule,
    ToastContainerComponent,
    DevmodeHostComponent,
    SetupModalComponent,
    PlatformGuideComponent,
    RipSizeWarningModalComponent,
    UsbSaturationWarningModalComponent,
    UpdateBannerComponent,
  ],
  templateUrl: './shell.component.html',
})
export class ShellComponent implements OnInit, OnDestroy {
  /** #718: running app version shown by the header wordmark (e.g. "v1.0.3"; "dev" locally). */
  appVersion: string | null = null;
  navOpen = false;
  devMode = false;
  devMenuOpen = false;
  /** When true, notification panel dropdown is visible. */
  notifPanelOpen = false;
  /** Backend says this install has earned the support prompt (enough completed
   * rips, not dismissed or snoozed). Eligibility lives server-side so a
   * dismissal carries across every browser and device on this install. */
  private supportPromptEligible = false;
  /** When true, Setup wizard modal is open. */
  setupModalOpen = false;
  /** Configuration for setup modal (e.g. target step, close on complete) */
  setupModalConfig: SetupModalConfig | undefined;
  /** When true, Platform Guide overlay is open (e.g. after "Take a Quick Tour"). */
  platformGuideOpen = false;
  /** Active rip-size-warning payload; non-null while the threshold modal is open. */
  ripSizeWarningPayload: RipSizeWarningPayload | null = null;
  /** #578: active USB-bus-saturation payload; non-null while the saturation
   * confirmation modal is open. */
  usbSaturationPayload: UsbSaturationWarningPayload | null = null;
  /** Which threshold-modal action is currently in flight, drives the inline
   * spinner + disables the rest of the modal so the user can't double-fire
   * the underlying request. Cleared on response (success or error). */
  ripSizeWarningPending: RipSizeWarningPendingAction = null;
  /** When true, main is full-width so history/settings page styling matches template (no page-container). */
  isFullWidthRoute = false;
  private lastDriveError: string | null = null;
  private subs = new Subscription();
  private initialLoadGracePeriod = 5000; // 5 seconds grace period
  private initialLoadStartTime = Date.now();
  private lastModalCloseTime: number | null = null;
  private modalCloseGracePeriod = 3000; // 3 seconds grace period after modal closes

  constructor(
    public toast: ToastService,
    private driveSvc: DriveService,
    private jobSvc: JobService,
    private systemSvc: SystemService,
    private workflowService: WorkflowService,
    private setupModalSvc: SetupModalService,
    private ripSizeWarningSvc: RipSizeWarningService,
    private usbSaturationSvc: UsbSaturationWarningService,
    private logger: LoggerService,
    private router: Router,
    public notifHistory: NotificationHistoryService,
    public browserNotif: BrowserNotificationService,
  ) {}

  ngOnInit(): void {
    document.addEventListener('visibilitychange', this._onVisibilityChange);
    this.updateHistoryRoute(this.router.url);

    // #718: show the running version by the header wordmark. Semver renders as
    // "v1.0.3"; a dev build stays "dev". Failures leave it hidden (*ngIf).
    this.subs.add(
      this.systemSvc.getAppVersion().subscribe({
        next: (v) => {
          const s = (v || '').trim();
          this.appVersion = s ? (/^\d+\.\d+\.\d+/.test(s) ? `v${s}` : s) : null;
        },
        error: () => {},
      })
    );
    this.refreshSupportPrompt();

    this.subs.add(
      this.router.events.pipe(filter((e): e is NavigationEnd => e instanceof NavigationEnd)).subscribe(() => {
        this.updateHistoryRoute(this.router.url);
        
        // Check if navigation state indicates platform guide should open
        const state = history.state as any;
        if (state?.openPlatformGuide === true) {
          // Small delay to ensure navigation completes
          setTimeout(() => {
            this.platformGuideOpen = true;
          }, 100);
        }
      })
    );
    this.subs.add(
      this.driveSvc.error$.subscribe(err => {
        const timeSinceLoad = Date.now() - this.initialLoadStartTime;
        const isInGracePeriod = timeSinceLoad < this.initialLoadGracePeriod;
        
        // Don't show errors during initial load grace period
        // Also skip "Drive event stream error" - it's shown in the UI card instead
        if (err && err !== this.lastDriveError && !isInGracePeriod && err !== 'Drive event stream error') {
          this.toast.show(err, 'error', 5000);
          this.lastDriveError = err;
        }
        if (!err) {
          this.lastDriveError = null;
        }
      })
    );

    // Check MakeMKV health on startup
    this.checkMakeMKVHealth();

    // Check transfer destination on startup (auto-open setup if not configured, similar to MakeMKV check)
    this.checkTransferDestination();

    // Detect dev mode from history snapshot or live job updates
    this.jobSvc.listJobs(5).subscribe({
      next: items => {
        if (items?.some(i => i.dev_mode)) {
          this.devMode = true;
          this.exposeToastService();
        }
      },
      error: () => {},
    });
    this.subs.add(
      this.workflowService.getJobStatus$().subscribe((status: any) => {
        if (status?.dev_mode) {
          this.devMode = true;
          this.exposeToastService();
        }
        // Segment-reorder UI is now embedded inline in the ripper's
        // path-a-workspace (no separate route navigation needed). The
        // standalone /segment-reorder/:jobId route still works as a
        // direct deep-link fallback but the workflow no longer punts
        // the user there — keeping them inside the breadcrumbed shell
        // where the disc-presence guard + workflow-actions are visible.
      })
    );
    this.subs.add(
      this.systemSvc.getDevMode().subscribe({
        next: res => {
          if (res?.enabled) {
            this.devMode = true;
            this.exposeToastService();
          }
        },
        error: () => {},
      })
    );
    // Backend-emitted notifications: display as toast + store in history (#319)
    this.subs.add(
      this.workflowService.notifications$.pipe(
        filter((n) => n != null && n.message != null)
      ).subscribe((n) => {
        // Store in notification history for the bell dropdown
        this.notifHistory.add(n);

        const text = formatBackendNotificationToastText(n);
        const isDiscReadError = n.level === 'error_disc_read';
        const timeout = isDiscReadError ? 8000 : 3500;
        this.toast.show(text, n.kind, timeout);
        // Optional: open setup modal when backend asks (e.g. action_required no_transfer_destination)
        if (n.action_type === 'open_transfer_setup') {
          this.setupModalSvc.open({ targetStep: 2 });  // 2 = Transfer step
        }
      })
    );
    // Open setup modal when requested (e.g. from dev menu "Setup" button, or MakeMKV health check)
    // Do not open the shell overlay when already on /setup — the setup page shows the wizard there; avoid two modals.
    this.subs.add(
      this.setupModalSvc.open$.subscribe((config) => {
        if (this.isOnSetupRoute()) {
          return;
        }
        this.devMenuOpen = false;
        this.setupModalConfig = config;
        this.setupModalOpen = true;
      })
    );

    // Open rip-size-warning modal when /jobs/rip returns 409 needs_user_choice.
    this.subs.add(
      this.ripSizeWarningSvc.open$.subscribe((payload) => {
        this.ripSizeWarningPayload = payload;
      })
    );

    // #578: open USB-bus-saturation modal when /jobs/rip returns 409
    // usb_bus_saturation_risk. The service stores the confirm callback so
    // we don't need to wire the retry path through the Shell.
    this.subs.add(
      this.usbSaturationSvc.state$.subscribe((state) => {
        this.usbSaturationPayload = state?.payload ?? null;
      })
    );
  }

  onUsbSaturationConfirm(): void {
    this.usbSaturationSvc.confirm();
  }

  onUsbSaturationDismiss(): void {
    this.usbSaturationSvc.dismiss();
  }

  /** True when the current route is the full-page setup wizard. */
  private isOnSetupRoute(): boolean {
    const url = this.router.url;
    return url === '/setup' || url.startsWith('/setup?');
  }

  /** When tab regains focus, check if setup completed in another tab (#306). */
  private _onVisibilityChange = () => {
    if (document.visibilityState !== 'visible' || !this.setupModalOpen) return;
    this.systemSvc.getSetupStatus().subscribe({
      next: (status) => {
        if (status.first_time_setup_complete && this.setupModalOpen) {
          this.setupModalOpen = false;
          this.logger.debug('[Shell] Setup completed in another tab — closing modal');
        }
      },
      error: () => {},  // Ignore — transient API failure
    });
  };

  ngOnDestroy(): void {
    document.removeEventListener('visibilitychange', this._onVisibilityChange);
    this.subs.unsubscribe();
    // Clean up window testToast function
    if ((window as any).testToast) {
      delete (window as any).testToast;
    }
  }

  private exposeToastService(): void {
    if (this.devMode && !(window as any).testToast) {
      (window as any).testToast = (message: string, kind: 'info' | 'success' | 'warning' | 'error' = 'info') => {
        this.toast.show(message, kind);
      };
    }
  }

  toggleNav(): void {
    this.navOpen = !this.navOpen;
  }

  toggleDevMenu(): void {
    this.devMenuOpen = !this.devMenuOpen;
  }

  closeDevMenu(): void {
    this.devMenuOpen = false;
  }

  toggleNotifPanel(): void {
    this.notifPanelOpen = !this.notifPanelOpen;
    if (this.notifPanelOpen) {
      this.notifHistory.markAllRead();
      this.browserNotif.refreshPromptVisibility();
      this.refreshSupportPrompt();
    }
  }

  onApproveOsNotifPrompt(event: Event): void {
    event.stopPropagation();
    void this.browserNotif.approveOsNotifPrompt();
  }

  onDismissOsNotifPrompt(event: Event): void {
    event.stopPropagation();
    this.browserNotif.dismissOsNotifPrompt();
  }

  /** Show the support prompt once the backend says this install has earned it.
   *
   * Deliberately not gated on job state. An earlier version hid the prompt
   * whenever a job was non-terminal, but `job_status` stays `running` right
   * through labeling and transfer — long after the rip finishes — so on a
   * working install the prompt almost never appeared. Worse, it depended on
   * whether the workflow context had loaded yet, so the prompt would render on
   * first paint and then vanish. The panel is opened deliberately and can't
   * interrupt anything, which is what the gate was guarding against anyway. */
  get supportPromptVisible(): boolean {
    return this.supportPromptEligible;
  }

  /** "Maybe later" snoozes; "Don't show again" silences permanently. Both hide
   * it immediately rather than waiting on the response — the user has answered,
   * and a slow request shouldn't leave the prompt sitting there. */
  onDismissSupportPrompt(event: Event, forever: boolean): void {
    event.stopPropagation();
    this.supportPromptEligible = false;
    this.subs.add(
      this.systemSvc.dismissSupportPrompt(forever).subscribe({ error: () => {} })
    );
  }

  private refreshSupportPrompt(): void {
    this.subs.add(
      this.systemSvc.getSupportPromptStatus().subscribe({
        next: (s) => {
          this.supportPromptEligible = !!s?.should_show;
        },
        error: () => {
          this.supportPromptEligible = false;
        },
      })
    );
  }

  closeNotifPanel(): void {
    this.notifPanelOpen = false;
  }

  openSetupModal(): void {
    this.setupModalOpen = true;
  }

  closeSetupModal(): void {
    this.setupModalOpen = false;
    this.lastModalCloseTime = Date.now();
  }

  onSetupComplete(showGuide: boolean): void {
    this.setupModalOpen = false;
    if (showGuide) {
      this.platformGuideOpen = true;
    }
  }

  openPlatformGuide(): void {
    this.platformGuideOpen = true;
  }

  closePlatformGuide(): void {
    this.platformGuideOpen = false;
  }

  private updateHistoryRoute(url: string): void {
    // /history now redirects to /library (#500). Recognise both so the
    // full-width layout still applies during the brief redirect frame.
    this.isFullWidthRoute = url === '/library' || url.startsWith('/library?') || url.startsWith('/library/') ||
                            url === '/history' || url.startsWith('/history?') || url.startsWith('/history/') ||
                            url === '/settings' || url.startsWith('/settings?') || url.startsWith('/settings/') ||
                            url === '/search' || url.startsWith('/search?') || url.startsWith('/search/');
  }

  private checkMakeMKVHealth(): void {
    this.systemSvc.getMakeMKVHealth().subscribe({
      next: (health) => {
        if (!health.valid || !health.can_rip) {
          // Check if we're in the grace period after modal close
          const timeSinceModalClose = this.lastModalCloseTime 
            ? Date.now() - this.lastModalCloseTime 
            : Infinity;
          
          if (timeSinceModalClose < this.modalCloseGracePeriod) {
            // Skip showing install prompt during grace period
            return;
          }
          // Already on setup page — do not open shell overlay (would stack two modals)
          if (this.isOnSetupRoute()) {
            return;
          }
          // MakeMKV installation is invalid - show notification and open setup modal to MakeMKV step
          const message = health.error || 'MakeMKV is not properly installed';
          this.toast.show(
            `${message}. Click to reinstall.`,
            'error',
            10000  // 10 second timeout
          );
          // Open setup modal at MakeMKV step (step 1) and close after completion
          setTimeout(() => {
            if (this.isOnSetupRoute()) {
              return;
            }
            this.setupModalSvc.open({ targetStep: 1, closeOnComplete: true });
          }, 1500);
        }
      },
      error: (err) => {
        // If health check fails, log but don't block the app
        this.logger.warn('Failed to check MakeMKV health', err);
      }
    });
  }

  /** Auto-open setup modal at Transfer step if no transfer destination is configured (similar to MakeMKV check). */
  private checkTransferDestination(): void {
    this.systemSvc.getTransferConfigs().subscribe({
      next: (configs) => {
        const active = configs.find((c) => c.is_active);
        const hasDestination =
          active &&
          (active.mode !== 'local' || (active.transfer_dir != null && String(active.transfer_dir).trim() !== ''));
        if (hasDestination) {
          return;
        }
        const timeSinceModalClose = this.lastModalCloseTime
          ? Date.now() - this.lastModalCloseTime
          : Infinity;
        if (timeSinceModalClose < this.modalCloseGracePeriod) {
          return;
        }
        if (this.isOnSetupRoute()) {
          return;
        }
        this.toast.show(
          'Transfer destination is not configured. Click to set up.',
          'warning',
          8000
        );
        setTimeout(() => {
          if (this.isOnSetupRoute()) {
            return;
          }
          this.setupModalSvc.open({ targetStep: 2, closeOnComplete: true });
        }, 1500);
      },
      error: (err) => {
        this.logger.warn('Failed to check transfer destination', err);
      }
    });
  }

  // ── Rip-size-warning modal handlers ──────────────────────────────────────

  onRipSizeWarningDismiss(): void {
    if (this.ripSizeWarningPending) return;
    this.ripSizeWarningPayload = null;
  }

  onRipSizeWarningChooseFindCanonical(): void {
    const payload = this.ripSizeWarningPayload;
    if (!payload || this.ripSizeWarningPending) return;
    this.ripSizeWarningPending = 'findCanonical';
    // No exploratory_title_index — backend auto-picks via DiscDB > MakeMKV
    // flag-clear > first member of the largest duplicate group.
    this.jobSvc.startRipWithSegmentReorder({
      mount_point: payload.mountPoint,
      disc_id: payload.discId,
      disc_num: payload.discNum,
    }).subscribe({
      next: () => {
        this.ripSizeWarningPending = null;
        this.ripSizeWarningPayload = null;
        this.toast.show('Exploratory rip started — we\'ll ping you when previews are ready', 'success');
      },
      error: (err) => {
        this.ripSizeWarningPending = null;
        this.logger.error('Failed to start segment-reorder rip', err);
        this.toast.show('Failed to start exploratory rip. See logs.', 'error', 6000);
      },
    });
  }

  onRipSizeWarningChooseRipWhole(): void {
    const payload = this.ripSizeWarningPayload;
    if (!payload || this.ripSizeWarningPending) return;
    this.ripSizeWarningPending = 'ripWhole';
    this.jobSvc.startRip({
      mount_point: payload.mountPoint,
      disc_id: payload.discId,
      disc_num: payload.discNum,
      mode: 'copy',
      force_full_rip: true,
    }).subscribe({
      next: () => {
        this.ripSizeWarningPending = null;
        this.ripSizeWarningPayload = null;
        this.toast.show('Ripping whole disc — this may take a while', 'info');
      },
      error: (err) => {
        this.ripSizeWarningPending = null;
        this.logger.error('Failed to start full-disc rip after threshold modal', err);
        this.toast.show('Failed to start rip. See logs.', 'error', 6000);
      },
    });
  }
}
