import { Injectable, InjectionToken, OnDestroy, inject } from '@angular/core';
import { Subscription, interval } from 'rxjs';
import { SystemService } from './system.service';
import { ToastService } from './toast.service';

/** Indirection for `window.location.reload()` so tests can substitute a spy.
 * Karma's Chrome Headless locks down `window.location.reload` so a direct
 * Object.defineProperty stub raises "Cannot redefine property". */
export const RELOAD_PAGE = new InjectionToken<() => void>('RELOAD_PAGE', {
  providedIn: 'root',
  factory: () => () => window.location.reload(),
});

/**
 * Polls `/system/frontend-version` and reacts when the served
 * `index.html` hash changes — i.e. someone ran `npm run build` and
 * the new bundle is on disk but this tab is still running the old
 * JS chunks.
 *
 * In dev mode (`is_dev_mode = true` from `/system/devmode`) the tab
 * auto-reloads on detection so the developer iterating on the UI
 * doesn't have to hard-refresh after every rebuild — the long-standing
 * "stale site cache when using the docker container in development"
 * pain. In production a single info toast prompts the user to reload;
 * we don't force-reload because that could throw away in-progress
 * label edits or a running rip's confirmation modal.
 *
 * Cheap: one GET every 30s, returns ~30 bytes. The polling stops if
 * the initial fetch fails so we don't generate noise when the API is
 * unreachable.
 */
@Injectable({ providedIn: 'root' })
export class FrontendVersionService implements OnDestroy {
  private static readonly POLL_INTERVAL_MS = 30_000;
  private initialVersion: string | null = null;
  private devMode = false;
  private reloadAnnounced = false;
  private sub = new Subscription();

  private reloadPage = inject(RELOAD_PAGE);

  constructor(
    private system: SystemService,
    private toast: ToastService,
  ) {}

  /** Called once from APP_INITIALIZER. Resolves the dev-mode flag and the
   * initial version, then starts the poll. Safe to call again — the
   * subsequent call is a no-op (the first version pin sticks). */
  start(): void {
    if (this.initialVersion !== null) return;
    this.sub.add(
      this.system.getDevMode().subscribe({
        next: (status) => {
          this.devMode = !!status?.enabled;
        },
        error: () => { this.devMode = false; },
      }),
    );
    this.sub.add(
      this.system.getFrontendVersion().subscribe({
        next: (r) => {
          this.initialVersion = r?.version ?? '';
          if (!this.initialVersion) return;  // backend can't read index.html — don't poll
          this.sub.add(
            interval(FrontendVersionService.POLL_INTERVAL_MS).subscribe(() => this.check()),
          );
        },
        error: () => {
          // Don't poll if we can't get the initial baseline — avoids burning
          // a request every 30s during backend outage.
          this.initialVersion = '';
        },
      }),
    );
  }

  private check(): void {
    this.system.getFrontendVersion().subscribe({
      next: (r) => {
        const next = r?.version ?? '';
        if (!next || !this.initialVersion) return;
        if (next === this.initialVersion) return;
        this.handleNewVersion();
      },
      error: () => { /* transient — try again next interval */ },
    });
  }

  private handleNewVersion(): void {
    if (this.reloadAnnounced) return;
    this.reloadAnnounced = true;
    if (this.devMode) {
      // Dev: silently reload. The developer just ran `npm run build`;
      // surprising them with a reload is the entire point.
      this.reloadPage();
      return;
    }
    // Prod: nudge but don't force — reloading mid-rip could lose work.
    // Timeout is long (12s) so the user has time to notice.
    this.toast.show(
      'A new version of MKV-Auto is available. Reload the page to update.',
      'info',
      12_000,
    );
  }

  ngOnDestroy(): void {
    this.sub.unsubscribe();
  }
}
