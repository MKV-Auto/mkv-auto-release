/**
 * UsbTopologyComponent — Settings page section that surfaces the live USB
 * bus topology + bandwidth contention warnings (#578).
 *
 * Renders one row per detected optical drive, grouped by USB bus number,
 * with a red banner above any bus that's flagged contended (≥2 drives on
 * a sub-SuperSpeed host). The remediation hint copy is fetched from the
 * backend warning so the source of truth stays in core/usb_topology.py.
 */
import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Observable } from 'rxjs';

import {
  UsbBusContentionWarning,
  UsbOpticalDrive,
  UsbTopology,
  UsbTopologyService,
} from '../../services/usb-topology.service';
import { IconComponent } from '../../ui/icon/icon.component';

interface BusGroup {
  bus: number;
  speed_mbps: number;
  drives: UsbOpticalDrive[];
  warning: UsbBusContentionWarning | null;
}

const SUPERSPEED_MBPS = 5000;

@Component({
  selector: 'app-usb-topology',
  standalone: true,
  imports: [CommonModule, IconComponent],
  template: `
    <div class="usb-topology">
      <div class="usb-topology__header">
        <div class="usb-topology__title">
          <ui-icon name="server" [size]="14"></ui-icon>
          <h4>USB Bus Topology</h4>
        </div>
        <button
          type="button"
          class="usb-topology__refresh"
          [disabled]="(loading$ | async) ?? false"
          (click)="refresh()"
          aria-label="Refresh USB topology">
          <ui-icon name="refresh" [size]="13"></ui-icon>
          <span>Refresh</span>
        </button>
      </div>

      <p class="usb-topology__subtitle">
        How your optical drives are wired to USB host controllers. Multiple drives
        on the same sub-SuperSpeed bus can saturate it during concurrent rips.
      </p>

      <div class="usb-topology__error" *ngIf="error$ | async as err">
        <ui-icon name="alert" [size]="14"></ui-icon>
        <span>{{ err }}</span>
      </div>

      <ng-container *ngIf="topology$ | async as topology">
        <div class="usb-topology__empty" *ngIf="topology.drives.length === 0">
          No optical drives detected.
        </div>

        <div
          class="usb-topology__bus"
          *ngFor="let group of groupByBus(topology)"
          [class.is-contended]="group.warning !== null">
          <div class="usb-topology__bus-header">
            <span class="usb-topology__bus-num">USB Bus {{ group.bus }}</span>
            <span class="usb-topology__bus-speed"
                  [class.is-superspeed]="group.speed_mbps >= 5000">
              {{ formatSpeed(group.speed_mbps) }}
            </span>
            <span class="usb-topology__bus-count">
              {{ group.drives.length }} drive{{ group.drives.length === 1 ? '' : 's' }}
            </span>
          </div>

          <div class="usb-topology__warning" *ngIf="group.warning as w">
            <ui-icon name="alert" [size]="14"></ui-icon>
            <span>{{ w.message }}</span>
          </div>

          <ul class="usb-topology__drive-list">
            <li *ngFor="let drive of group.drives" class="usb-topology__drive">
              <ui-icon name="disc" [size]="13"></ui-icon>
              <div class="usb-topology__drive-text">
                <span class="usb-topology__drive-name">{{ drive.product }}</span>
                <span class="usb-topology__drive-meta">
                  {{ drive.manufacturer || 'unknown' }}
                  <ng-container *ngIf="drive.serial"> · serial {{ drive.serial }}</ng-container>
                </span>
              </div>
            </li>
          </ul>
        </div>
      </ng-container>
    </div>
  `,
  styles: [`
    .usb-topology {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    .usb-topology__header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .usb-topology__title {
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .usb-topology__title h4 {
      margin: 0;
      font-size: 0.95rem;
      font-weight: 600;
    }
    .usb-topology__refresh {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.35rem 0.7rem;
      border: 1px solid rgba(255, 255, 255, 0.1);
      background: rgba(255, 255, 255, 0.04);
      color: inherit;
      border-radius: 6px;
      font-size: 0.8rem;
      cursor: pointer;
    }
    .usb-topology__refresh:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .usb-topology__subtitle {
      margin: 0;
      font-size: 0.8rem;
      opacity: 0.7;
    }
    .usb-topology__error,
    .usb-topology__warning {
      display: flex;
      gap: 0.5rem;
      padding: 0.6rem 0.75rem;
      border-radius: 6px;
      background: rgba(239, 68, 68, 0.1);
      border: 1px solid rgba(239, 68, 68, 0.3);
      color: rgb(252, 165, 165);
      font-size: 0.825rem;
      align-items: flex-start;
    }
    .usb-topology__empty {
      padding: 1rem;
      text-align: center;
      opacity: 0.6;
      font-size: 0.85rem;
    }
    .usb-topology__bus {
      padding: 0.75rem;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.02);
    }
    .usb-topology__bus.is-contended {
      border-color: rgba(239, 68, 68, 0.4);
      background: rgba(239, 68, 68, 0.05);
    }
    .usb-topology__bus-header {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 0.5rem;
      font-size: 0.85rem;
    }
    .usb-topology__bus-num {
      font-weight: 600;
    }
    .usb-topology__bus-speed {
      padding: 0.15rem 0.5rem;
      border-radius: 3px;
      background: rgba(239, 68, 68, 0.2);
      color: rgb(252, 165, 165);
      font-size: 0.75rem;
      font-weight: 500;
    }
    .usb-topology__bus-speed.is-superspeed {
      background: rgba(34, 197, 94, 0.2);
      color: rgb(134, 239, 172);
    }
    .usb-topology__bus-count {
      opacity: 0.7;
      font-size: 0.8rem;
    }
    .usb-topology__drive-list {
      list-style: none;
      padding: 0;
      margin: 0.5rem 0 0 0;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
    }
    .usb-topology__drive {
      display: flex;
      align-items: flex-start;
      gap: 0.5rem;
      padding: 0.4rem 0.5rem;
      background: rgba(255, 255, 255, 0.03);
      border-radius: 5px;
      font-size: 0.8rem;
    }
    .usb-topology__drive-text {
      display: flex;
      flex-direction: column;
      gap: 0.1rem;
    }
    .usb-topology__drive-name {
      font-weight: 500;
    }
    .usb-topology__drive-meta {
      opacity: 0.6;
      font-size: 0.72rem;
    }
  `],
})
export class UsbTopologyComponent implements OnInit {
  topology$: Observable<UsbTopology>;
  loading$: Observable<boolean>;
  error$: Observable<string | null>;

  constructor(private svc: UsbTopologyService) {
    this.topology$ = svc.topology$;
    this.loading$ = svc.loading$;
    this.error$ = svc.error$;
  }

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.svc.refresh().subscribe();
  }

  /**
   * Group drives by bus number, surfacing the bus speed (max across siblings)
   * and the contention warning if present. The backend's warning list is
   * keyed by bus, so a simple lookup is sufficient.
   */
  groupByBus(topology: UsbTopology): BusGroup[] {
    const warningsByBus = new Map<number, UsbBusContentionWarning>();
    for (const w of topology.warnings) {
      warningsByBus.set(w.bus, w);
    }
    const drivesByBus = new Map<number, UsbOpticalDrive[]>();
    for (const d of topology.drives) {
      const list = drivesByBus.get(d.bus) || [];
      list.push(d);
      drivesByBus.set(d.bus, list);
    }
    const groups: BusGroup[] = [];
    for (const [bus, drives] of drivesByBus) {
      const speed = Math.max(...drives.map(d => d.speed_mbps));
      groups.push({
        bus,
        speed_mbps: speed,
        drives,
        warning: warningsByBus.get(bus) ?? null,
      });
    }
    // Contended buses first, then by bus number.
    groups.sort((a, b) => {
      const aContended = a.warning ? 0 : 1;
      const bContended = b.warning ? 0 : 1;
      if (aContended !== bContended) return aContended - bContended;
      return a.bus - b.bus;
    });
    return groups;
  }

  formatSpeed(mbps: number): string {
    if (mbps >= SUPERSPEED_MBPS) {
      const gbps = mbps / 1000;
      return `${gbps} Gbps (USB ${gbps >= 10 ? '3.1+' : '3.0'} SuperSpeed)`;
    }
    if (mbps >= 480) return `${mbps} Mbps (USB 2.0)`;
    if (mbps >= 12) return `${mbps} Mbps (USB 1.1)`;
    return `${mbps} Mbps`;
  }
}
