/**
 * DriveSnapshotService — poll-based exposure of the backend's OS-level
 * drive registry (``GET /drives/snapshot``) so the UI can tell apart:
 *
 *   1. drive present + this disc loaded → "Start Copy"
 *   2. drive present + different/no disc → "Insert Disc"
 *   3. no drives connected at all       → "Drive Not Connected"
 *
 * The existing ``WorkflowService.drives$`` derives from the coordinator's
 * in-drive discs, which only surfaces drives that already have media. The
 * snapshot endpoint also returns drives with ``loaded: false``, which is
 * what distinguishes state 2 from state 3.
 *
 * The CTA tri-state lives in ``ripper-page.component.ts`` (``ctaState``);
 * this service is the data source. See #571.
 */
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject, Observable, Subject, Subscription, of, timer } from 'rxjs';
import { catchError, switchMap, takeUntil } from 'rxjs/operators';

import { environment } from '../environments/environment';

const API_BASE = environment.apiBase ?? '';

export interface DriveSnapshotRow {
  mount_point: string;
  loaded: boolean;
  volume_label: string | null;
  media_kind: 'BD' | 'DVD' | 'CD' | 'unknown' | null;
  by_id_serial: string;
  identity_source: 'by-id' | 'by-path' | 'sysfs' | 'unknown';
  multi_drive_safe: boolean;
  vendor: string;
  model: string;
  bus: string;
}

const DEFAULT_POLL_MS = 5000;

@Injectable({ providedIn: 'root' })
export class DriveSnapshotService {
  private readonly http = inject(HttpClient);
  private readonly snapshot$ = new BehaviorSubject<DriveSnapshotRow[]>([]);
  private pollSub: Subscription | null = null;
  private readonly stop$ = new Subject<void>();

  /** Stream of the latest registry snapshot. Empty array until first poll lands. */
  readonly drives$: Observable<DriveSnapshotRow[]> = this.snapshot$.asObservable();

  /** Snapshot for callers that only need the current value (e.g. computed getters). */
  current(): DriveSnapshotRow[] {
    return this.snapshot$.value;
  }

  /** Subscribers in active components keep polling alive; idempotent. */
  startPolling(intervalMs = DEFAULT_POLL_MS): void {
    if (this.pollSub) return;
    this.pollSub = timer(0, intervalMs)
      .pipe(
        switchMap(() =>
          this.http
            .get<DriveSnapshotRow[]>(`${API_BASE}/drives/snapshot`)
            .pipe(catchError(() => of<DriveSnapshotRow[]>([]))),
        ),
        takeUntil(this.stop$),
      )
      .subscribe(rows => this.snapshot$.next(rows));
  }

  /** Drop the poll. Safe to call repeatedly. */
  stopPolling(): void {
    if (!this.pollSub) return;
    this.pollSub.unsubscribe();
    this.pollSub = null;
  }
}
