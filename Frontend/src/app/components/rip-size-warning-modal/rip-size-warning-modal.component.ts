import { Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RipSizeWarningPayload } from '../../services/rip-size-warning.service';
import { BtnComponent } from '../../ui/btn/btn.component';
import { CardComponent } from '../../ui/card/card.component';
import { IconComponent } from '../../ui/icon/icon.component';
import { PillComponent } from '../../ui/pill/pill.component';

/**
 * Threshold modal (Path A trigger). Displayed when `POST /jobs/rip` returns
 * 409 `needs_user_choice` (Midway-class obfuscated disc + projected rip >
 * threshold). Two actions: "Find canonical" (segment-reorder) or "Rip
 * whole disc anyway" (disabled when disk doesn't fit).
 *
 * Re-skinned against the design system: outer ui-card supplies the dialog
 * shell; ui-pill replaces the bespoke pill variants; ui-btn provides the
 * cancel button. Action buttons stay as native `<button>` elements so they
 * retain disabled semantics + keyboard activation.
 */
export type RipSizeWarningPendingAction = 'findCanonical' | 'ripWhole' | null;

@Component({
  selector: 'app-rip-size-warning-modal',
  standalone: true,
  imports: [CommonModule, CardComponent, PillComponent, BtnComponent, IconComponent],
  templateUrl: './rip-size-warning-modal.component.html',
  styleUrls: ['./rip-size-warning-modal.component.scss'],
})
export class RipSizeWarningModalComponent {
  @Input() payload!: RipSizeWarningPayload;

  /** Which action is currently in flight. Drives the inline button spinner
   * and disables the rest of the modal so the user can't double-fire the
   * underlying request before the backend responds. */
  @Input() pendingAction: RipSizeWarningPendingAction = null;

  /** User picked "Find canonical". The backend auto-picks the exploratory
   * playlist within the duplicate-segment-map group — within a group every
   * member references the same physical segments, so it doesn't matter
   * which one feeds the previews. */
  @Output() chooseFindCanonical = new EventEmitter<void>();

  /** User picked "Rip whole disc anyway". */
  @Output() chooseRipWhole = new EventEmitter<void>();

  /** User dismissed the modal without choosing. */
  @Output() dismiss = new EventEmitter<void>();

  get isPending(): boolean {
    return this.pendingAction !== null;
  }

  get fits(): boolean {
    const projected = this.payload?.projectedRipBytes ?? null;
    const available = this.payload?.availableDiskBytes ?? null;
    if (projected === null || available === null) {
      return true; // Unknown — let backend's space pre-flight decide.
    }
    return projected <= available;
  }

  get projectedGb(): number | null {
    const v = this.payload?.projectedRipBytes;
    return v ? Math.round(v / (1024 ** 3)) : null;
  }

  get availableGb(): number | null {
    const v = this.payload?.availableDiskBytes;
    return v ? Math.round(v / (1024 ** 3)) : null;
  }

  onFindCanonical(): void {
    if (this.isPending) return;
    this.chooseFindCanonical.emit();
  }

  onRipWhole(): void {
    if (this.isPending || !this.fits) return;
    this.chooseRipWhole.emit();
  }

  onBackdropClick(event: MouseEvent): void {
    if (this.isPending) return;
    // Only dismiss on click outside the dialog content.
    if ((event.target as HTMLElement).classList.contains('rsw-backdrop')) {
      this.dismiss.emit();
    }
  }
}
