import { Injectable } from '@angular/core';
import { Subject } from 'rxjs';

/**
 * Payload that drives the rip-size-warning modal. Built from the
 * `409 needs_user_choice` body returned by `POST /jobs/rip` on a
 * Midway-class disc (duplicate sorted-segment-map groups present
 * AND projected rip > MKVAUTO_RIP_REVIEW_THRESHOLD_GB).
 */
export interface RipSizeWarningPayload {
  /** Mount point for the disc (re-passed to whichever endpoint the user chooses). */
  mountPoint: string;
  /** Disc number from drive manager, optional but preferred. */
  discNum?: string;
  /** Disc id when the disc has a DB record, optional. */
  discId?: string;
  /** How many GB the rip would write if we did `mkv DEV all OUT` today. */
  projectedRipBytes: number | null;
  /** Available disk space in bytes; null when backend couldn't compute it. */
  availableDiskBytes: number | null;
  /** Threshold (GB) the projected size exceeded — useful for UI explainer copy. */
  thresholdGb: number;
  /** Number of duplicate-segment-map groups detected on the disc. */
  duplicateGroupCount: number;
  /** Diagnostic-only: list of duplicate-group members detected on the disc.
   * The backend auto-picks within the largest group; the user does NOT
   * choose between them in the modal. Surfaced for "we found N similar
   * playlists" copy. */
  candidates: Array<{
    title_index: number;
    duplicate_group_size: number;
    sorted_segment_key: string;
  }>;
}

/**
 * Same pattern as SetupModalService: service emits an open event;
 * Shell subscribes and renders <app-rip-size-warning-modal>.
 */
@Injectable({ providedIn: 'root' })
export class RipSizeWarningService {
  private readonly openSubject = new Subject<RipSizeWarningPayload>();
  readonly open$ = this.openSubject.asObservable();

  open(payload: RipSizeWarningPayload): void {
    this.openSubject.next(payload);
  }
}
