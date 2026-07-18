import { Injectable, OnDestroy } from '@angular/core';
import { Router } from '@angular/router';
import { BehaviorSubject, Subscription } from 'rxjs';
import { filter } from 'rxjs/operators';
import { BackendNotification, WorkflowService } from './workflow.service';
import { SetupModalService } from './setup-modal.service';
import { LoggerService } from './logger.service';
import { formatBackendNotificationToastText } from '../utils/backend-notification-display.util';

/** User dismissed the OS-notification banner for this browser tab session. */
const SESSION_BANNER_DISMISS_KEY = 'mkvauto_os_notif_banner_dismissed';

/**
 * Extends backend in-app notifications to the Web Notifications API when the tab is hidden.
 * Permission: optional thin banner in the notifications dropdown (Approve / Dismiss); Approve calls
 * `requestPermission()` on a user gesture. Delivery follows the same WebSocket stream as toasts.
 */
@Injectable({ providedIn: 'root' })
export class BrowserNotificationService implements OnDestroy {
  private sub = new Subscription();

  /** Whether the dropdown banner inviting OS notifications should show. */
  private readonly _osNotifPromptVisible = new BehaviorSubject(false);
  readonly osNotifPromptVisible$ = this._osNotifPromptVisible.asObservable();

  constructor(
    private workflowService: WorkflowService,
    private setupModalSvc: SetupModalService,
    private logger: LoggerService,
    private router: Router,
  ) {
    this.sub.add(
      this.workflowService.notifications$
        .pipe(filter((n) => n != null && n.message != null))
        .subscribe((n) => this.maybeShowOsNotification(n)),
    );
    this.refreshPromptVisibility();
  }

  ngOnDestroy(): void {
    this.sub.unsubscribe();
  }

  /** Recompute banner visibility (e.g. when opening the notifications panel or after Approve). */
  refreshPromptVisibility(): void {
    const show =
      this.isNotificationApiAvailable() &&
      Notification.permission === 'default' &&
      !this.isBannerDismissedInSession();
    this._osNotifPromptVisible.next(show);
  }

  /** User chose not to enable OS notifications this session (banner only; toasts unchanged). */
  dismissOsNotifPrompt(): void {
    try {
      if (typeof sessionStorage !== 'undefined') {
        sessionStorage.setItem(SESSION_BANNER_DISMISS_KEY, '1');
      }
    } catch {
      // ignore
    }
    this._osNotifPromptVisible.next(false);
  }

  /**
   * User approved: show the native permission dialog (must run from a click handler).
   * @returns resolved permission string when the promise settles.
   */
  async approveOsNotifPrompt(): Promise<NotificationPermission> {
    if (!this.isNotificationApiAvailable()) {
      return 'denied';
    }
    let result: NotificationPermission = Notification.permission;
    try {
      result = await Notification.requestPermission();
    } catch (err) {
      this.logger.debug('[BrowserNotification] requestPermission failed', err);
    }
    this.refreshPromptVisibility();
    return result;
  }

  private isNotificationApiAvailable(): boolean {
    if (typeof globalThis === 'undefined' || typeof globalThis.Notification === 'undefined') {
      return false;
    }
    try {
      if (typeof globalThis.isSecureContext === 'boolean' && !globalThis.isSecureContext) {
        return false;
      }
    } catch {
      return false;
    }
    return true;
  }

  private isBannerDismissedInSession(): boolean {
    try {
      return typeof sessionStorage !== 'undefined' && sessionStorage.getItem(SESSION_BANNER_DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  }

  private maybeShowOsNotification(n: BackendNotification): void {
    if (!this.isNotificationApiAvailable()) {
      return;
    }
    if (Notification.permission !== 'granted') {
      return;
    }
    if (typeof document !== 'undefined' && document.visibilityState !== 'hidden') {
      return;
    }
    const { title, body } = this.osTitleAndBody(n);
    const options: NotificationOptions = { body };
    if (n.id) {
      options.tag = n.id;
    }
    try {
      const notification = new Notification(title, options);
      notification.onclick = () => {
        try {
          globalThis.focus?.();
        } catch {
          // ignore
        }
        notification.close();
        if (n.action_type === 'open_transfer_setup') {
          this.setupModalSvc.open({ targetStep: 2 });
        } else if (n.action_type === 'open_ripper_drive') {
          const mount = n.action_payload?.['mount_point'];
          if (typeof mount === 'string' && mount.length > 0) {
            this.workflowService.selectDrive(mount);
            void this.router.navigate(['/activity']).then(() => {
              this.workflowService.setContextByCard({ type: 'drive', id: mount }).subscribe();
            });
          }
        }
      };
    } catch (e) {
      this.logger.warn('[BrowserNotification] Failed to show notification', e);
    }
  }

  private osTitleAndBody(n: BackendNotification): { title: string; body: string } {
    const fallbackTitle = 'MKV-Auto';
    if (n.level === 'error_disc_read') {
      return {
        title: n.title ?? fallbackTitle,
        body: formatBackendNotificationToastText(n),
      };
    }
    if (n.title) {
      return { title: n.title, body: n.message ?? '' };
    }
    return { title: fallbackTitle, body: n.message ?? '' };
  }
}
