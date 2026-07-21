import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject, fromEvent, timer } from 'rxjs';
import { switchMap, takeUntil, filter } from 'rxjs/operators';
import { SystemService, UpdateStatus } from '../../services/system.service';
import { LoggerService } from '../../services/logger.service';

/**
 * #699: persistent "update available" banner.
 *
 * Sits in the app shell between the header and the page content — visible on
 * every page, pushes content down instead of covering it, and stays until
 * dismissed. Dismissal is per-version (localStorage): dismissing v1.0.2
 * suppresses only v1.0.2; the banner returns when v1.0.3 ships.
 *
 * The check runs ~5s after bootstrap (never in the boot critical path — #652)
 * and again on tab-refocus once the last check is older than 6h (the backend
 * caches for 6h, so earlier refocus checks are answered from cache anyway).
 */
@Component({
  selector: 'app-update-banner',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './update-banner.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class UpdateBannerComponent implements OnInit, OnDestroy {
  static readonly DISMISSED_STORAGE_KEY = 'mkvauto.dismissed-update-version';
  private static readonly INITIAL_DELAY_MS = 5000;
  private static readonly RECHECK_AFTER_MS = 6 * 3600 * 1000;

  visible = false;
  status: UpdateStatus | null = null;

  private lastCheckedAt = 0;
  private destroy$ = new Subject<void>();

  constructor(
    private systemService: SystemService,
    private logger: LoggerService,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    timer(UpdateBannerComponent.INITIAL_DELAY_MS)
      .pipe(
        switchMap(() => this.systemService.getUpdateStatus()),
        takeUntil(this.destroy$)
      )
      .subscribe({
        next: (status) => this.evaluate(status),
        error: (err) => this.logger.warn('[UpdateBanner] update check failed', err),
      });

    // Re-check when the user returns to a long-lived tab (aligned with the
    // #696 focus-reconcile philosophy). Errors are swallowed per emission so
    // one failed check doesn't kill the stream.
    fromEvent(document, 'visibilitychange')
      .pipe(
        filter(() => document.visibilityState === 'visible'),
        filter(() => Date.now() - this.lastCheckedAt > UpdateBannerComponent.RECHECK_AFTER_MS),
        switchMap(() => this.systemService.getUpdateStatus()),
        takeUntil(this.destroy$)
      )
      .subscribe({
        next: (status) => this.evaluate(status),
        error: (err) => this.logger.warn('[UpdateBanner] refocus update check failed', err),
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  private evaluate(status: UpdateStatus): void {
    this.lastCheckedAt = Date.now();
    this.status = status;
    const dismissed = this.readDismissedVersion();
    this.visible = !!(
      status.update_available &&
      status.latest_version &&
      status.latest_version !== dismissed
    );
    this.cdr.markForCheck();
  }

  dismiss(): void {
    if (this.status?.latest_version) {
      this.writeDismissedVersion(this.status.latest_version);
    }
    this.visible = false;
    this.cdr.markForCheck();
  }

  // Storage access is isolated behind these two seams so tests can mock them at
  // the instance level. Spying the localStorage API directly is unreliable
  // across environments — CI headless Chrome no-ops writes and exposes setItem
  // as an own property (not on Storage.prototype), so neither the value nor the
  // call is observable through the platform API.
  private readDismissedVersion(): string | null {
    try {
      return localStorage.getItem(UpdateBannerComponent.DISMISSED_STORAGE_KEY);
    } catch {
      return null;
    }
  }

  private writeDismissedVersion(version: string): void {
    try {
      localStorage.setItem(UpdateBannerComponent.DISMISSED_STORAGE_KEY, version);
    } catch {
      // Storage unavailable (private mode etc.) — banner just reappears next load.
    }
  }
}
