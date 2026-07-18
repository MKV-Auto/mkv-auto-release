/**
 * UsbTopologyService — fetches the OS-level USB bus topology + bandwidth
 * contention warnings exposed by ``GET /drives/usb-topology`` (#578/#579).
 *
 * Used by the Settings > Disc handling section to show users which drives
 * share a bus and whether the bus is bandwidth-constrained. Not polled
 * by default — sysfs is the source of truth and rarely changes between
 * settings views; callers refresh on demand via :func:`refresh`.
 */
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject, Observable, of } from 'rxjs';
import { catchError, tap } from 'rxjs/operators';

import { environment } from '../environments/environment';

const API_BASE = environment.apiBase ?? '';


export interface UsbOpticalDrive {
  bus: number;
  speed_mbps: number;
  product: string;
  manufacturer: string;
  serial: string;
  sysfs_path: string;
}


export interface UsbBusContentionWarning {
  bus: number;
  speed_mbps: number;
  drive_count: number;
  drives: string[];
  message: string;
}


export interface UsbTopology {
  drives: UsbOpticalDrive[];
  warnings: UsbBusContentionWarning[];
}


const EMPTY: UsbTopology = { drives: [], warnings: [] };


@Injectable({ providedIn: 'root' })
export class UsbTopologyService {
  private readonly http = inject(HttpClient);
  private readonly _topology$ = new BehaviorSubject<UsbTopology>(EMPTY);
  private readonly _loading$ = new BehaviorSubject<boolean>(false);
  private readonly _error$ = new BehaviorSubject<string | null>(null);

  readonly topology$: Observable<UsbTopology> = this._topology$.asObservable();
  readonly loading$: Observable<boolean> = this._loading$.asObservable();
  readonly error$: Observable<string | null> = this._error$.asObservable();

  current(): UsbTopology {
    return this._topology$.value;
  }

  /** Fetch the latest topology snapshot. Fails soft on HTTP error — the
   * observable carries the empty topology + an error message rather than
   * throwing, so callers don't need a separate error subscription. */
  refresh(): Observable<UsbTopology> {
    this._loading$.next(true);
    this._error$.next(null);
    return this.http
      .get<UsbTopology>(`${API_BASE}/drives/usb-topology`)
      .pipe(
        tap(topology => {
          this._topology$.next(topology ?? EMPTY);
          this._loading$.next(false);
        }),
        catchError(err => {
          this._error$.next(
            err?.error?.detail || err?.message || 'Failed to load USB topology',
          );
          this._loading$.next(false);
          this._topology$.next(EMPTY);
          return of(EMPTY);
        }),
      );
  }
}
