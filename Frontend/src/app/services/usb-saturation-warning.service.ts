/**
 * UsbSaturationWarningService — opens a confirmation modal when the backend
 * refuses a rip start with ``409 code='usb_bus_saturation_risk'`` (#578).
 *
 * Same Shell-renders-modal pattern as :class:`RipSizeWarningService`, but
 * the user's confirm action invokes a caller-supplied callback rather than
 * the Shell knowing how to retry — the originating component
 * (workflow-actions) holds the retry path because it already has the
 * context, jobSvc, and error-handling plumbing.
 */
import { Injectable } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';


/**
 * Payload returned by ``POST /jobs/rip`` (and ``/jobs/rip-with-segment-reorder``)
 * when the USB bus the target drive sits on is saturated. Comes from
 * ``core.usb_bus_saturation_policy.SaturationDecision.to_409_payload()``.
 */
export interface UsbSaturationWarningPayload {
  bus: number;
  speed_mbps: number;
  /** Other mount_points with active rips on the same bus. */
  competing_mount_points: string[];
  /** Human-readable warning copy authored on the backend so the wording
   * stays in one place. */
  message: string;
  /** The JobCreate field the frontend should set to ``true`` on retry. */
  override_field: string;
}


export interface UsbSaturationModalState {
  payload: UsbSaturationWarningPayload;
}


@Injectable({ providedIn: 'root' })
export class UsbSaturationWarningService {
  private readonly _state$ = new BehaviorSubject<UsbSaturationModalState | null>(null);
  readonly state$: Observable<UsbSaturationModalState | null> = this._state$.asObservable();

  /** Callback invoked when the user clicks "Proceed anyway". Cleared
   * after firing OR on dismiss so a re-open doesn't accidentally invoke
   * a stale callback. */
  private pendingConfirm: (() => void) | null = null;

  /** Open the modal with the backend payload and a confirm callback.
   * The callback should re-fire the rip-start request with the
   * ``force_concurrent_on_saturated_bus: true`` flag. */
  open(payload: UsbSaturationWarningPayload, onConfirm: () => void): void {
    this.pendingConfirm = onConfirm;
    this._state$.next({ payload });
  }

  /** User clicked "Proceed anyway". */
  confirm(): void {
    const cb = this.pendingConfirm;
    this.pendingConfirm = null;
    this._state$.next(null);
    if (cb) cb();
  }

  /** User clicked Cancel or backdrop dismiss. */
  dismiss(): void {
    this.pendingConfirm = null;
    this._state$.next(null);
  }

  /** Current state snapshot (used by Shell template ngIf). */
  current(): UsbSaturationModalState | null {
    return this._state$.value;
  }
}
