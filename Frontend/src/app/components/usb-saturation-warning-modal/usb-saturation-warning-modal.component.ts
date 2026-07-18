/**
 * UsbSaturationWarningModalComponent — confirmation modal for the
 * ``409 usb_bus_saturation_risk`` response from the rip-start endpoints
 * (#578). Shows the bus number + speed + competing mount_points, the
 * backend's warning copy, and lets the user choose Cancel or
 * "Proceed anyway".
 */
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';

import { UsbSaturationWarningPayload } from '../../services/usb-saturation-warning.service';
import { IconComponent } from '../../ui/icon/icon.component';

@Component({
  selector: 'app-usb-saturation-warning-modal',
  standalone: true,
  imports: [CommonModule, IconComponent],
  template: `
    <div class="usw-modal__backdrop" (click)="onDismiss()" *ngIf="payload">
      <div class="usw-modal" role="dialog" aria-modal="true"
           aria-labelledby="usw-modal-title"
           (click)="$event.stopPropagation()">
        <div class="usw-modal__header">
          <ui-icon name="alert" [size]="18"></ui-icon>
          <h3 id="usw-modal-title">USB bus saturation risk</h3>
        </div>

        <div class="usw-modal__body">
          <p class="usw-modal__lead">{{ payload.message }}</p>

          <dl class="usw-modal__facts">
            <div>
              <dt>Bus</dt>
              <dd>USB Bus {{ payload.bus }} ({{ payload.speed_mbps }} Mbps)</dd>
            </div>
            <div *ngIf="payload.competing_mount_points.length">
              <dt>Active rip(s) on this bus</dt>
              <dd>
                <code *ngFor="let mp of payload.competing_mount_points; let last = last">
                  {{ mp }}<ng-container *ngIf="!last">, </ng-container>
                </code>
              </dd>
            </div>
          </dl>

          <p class="usw-modal__hint">
            Concurrent rips on a USB 2.0 bus can saturate it and trigger
            controller resets that fail one or both rips mid-stream. If
            possible, move one drive to a USB 3.0 (SuperSpeed) port.
          </p>
        </div>

        <div class="usw-modal__actions">
          <button type="button" class="usw-btn usw-btn--ghost" (click)="onDismiss()">
            Cancel
          </button>
          <button type="button" class="usw-btn usw-btn--danger" (click)="onConfirm()">
            Proceed anyway
          </button>
        </div>
      </div>
    </div>
  `,
  styles: [`
    .usw-modal__backdrop {
      position: fixed; inset: 0;
      background: rgba(0, 0, 0, 0.6);
      backdrop-filter: blur(4px);
      display: flex; align-items: center; justify-content: center;
      z-index: 1000;
      animation: usw-fade-in 0.15s ease-out;
    }
    @keyframes usw-fade-in { from { opacity: 0; } to { opacity: 1; } }
    .usw-modal {
      width: min(520px, 92vw);
      background: rgb(20, 22, 30);
      border: 1px solid rgba(239, 68, 68, 0.4);
      border-radius: 10px;
      box-shadow: 0 25px 60px rgba(0, 0, 0, 0.5);
      overflow: hidden;
    }
    .usw-modal__header {
      display: flex; align-items: center; gap: 0.6rem;
      padding: 1rem 1.25rem;
      background: rgba(239, 68, 68, 0.08);
      border-bottom: 1px solid rgba(239, 68, 68, 0.25);
      color: rgb(252, 165, 165);
    }
    .usw-modal__header h3 { margin: 0; font-size: 1rem; font-weight: 600; }
    .usw-modal__body { padding: 1.25rem; display: flex; flex-direction: column; gap: 1rem; }
    .usw-modal__lead { margin: 0; line-height: 1.5; }
    .usw-modal__facts {
      display: flex; flex-direction: column; gap: 0.5rem;
      margin: 0;
      padding: 0.75rem;
      background: rgba(255, 255, 255, 0.03);
      border-radius: 6px;
      font-size: 0.85rem;
    }
    .usw-modal__facts > div { display: flex; gap: 0.75rem; align-items: baseline; }
    .usw-modal__facts dt { font-weight: 500; opacity: 0.7; min-width: 9rem; margin: 0; }
    .usw-modal__facts dd { margin: 0; }
    .usw-modal__facts code {
      background: rgba(255, 255, 255, 0.08);
      padding: 0.1rem 0.4rem;
      border-radius: 3px;
      font-size: 0.78rem;
    }
    .usw-modal__hint { margin: 0; font-size: 0.825rem; opacity: 0.75; line-height: 1.5; }
    .usw-modal__actions {
      display: flex; justify-content: flex-end; gap: 0.5rem;
      padding: 1rem 1.25rem;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      background: rgba(0, 0, 0, 0.2);
    }
    .usw-btn {
      padding: 0.55rem 1rem;
      border-radius: 6px;
      font-size: 0.875rem;
      font-weight: 500;
      cursor: pointer;
      border: 1px solid transparent;
      transition: background-color 0.1s ease;
    }
    .usw-btn--ghost {
      background: transparent;
      color: rgba(255, 255, 255, 0.8);
      border-color: rgba(255, 255, 255, 0.15);
    }
    .usw-btn--ghost:hover { background: rgba(255, 255, 255, 0.05); }
    .usw-btn--danger {
      background: rgba(239, 68, 68, 0.85);
      color: white;
    }
    .usw-btn--danger:hover { background: rgba(220, 38, 38, 1); }
  `],
})
export class UsbSaturationWarningModalComponent {
  @Input() payload: UsbSaturationWarningPayload | null = null;

  @Output() confirm = new EventEmitter<void>();
  @Output() dismiss = new EventEmitter<void>();

  onConfirm(): void {
    this.confirm.emit();
  }

  onDismiss(): void {
    this.dismiss.emit();
  }
}
