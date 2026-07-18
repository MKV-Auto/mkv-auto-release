import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { IconComponent } from '../icon/icon.component';

export type StageStatus = 'pending' | 'active' | 'done' | 'error' | 'skipped';

@Component({
  selector: 'ui-stage-chip',
  standalone: true,
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="ui-stage" [attr.data-status]="status">
      <div class="ui-stage__head">
        @switch (status) {
          @case ('active') {
            <span class="ui-stage__icon ui-stage__icon--spin"><ui-icon name="spinner" [size]="11"></ui-icon></span>
          }
          @case ('done') {
            <span class="ui-stage__icon"><ui-icon name="check" [size]="11"></ui-icon></span>
          }
          @case ('error') {
            <span class="ui-stage__icon"><ui-icon name="alert" [size]="11"></ui-icon></span>
          }
          @default {
            <span class="ui-stage__dot" aria-hidden="true"></span>
          }
        }
        <span class="ui-stage__label">{{ label }}</span>
      </div>
      @if (sub) {
        <div class="ui-stage__sub">{{ sub }}</div>
      }
    </div>
  `,
  styles: [`
    .ui-stage {
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(255,255,255,0.02);
      border: 1px solid rgba(255,255,255,0.10);
      min-width: 0;
    }
    .ui-stage__head { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
    .ui-stage__label {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .ui-stage__sub {
      font-size: 12px;
      color: rgba(255,255,255,0.75);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .ui-stage__icon { display: inline-flex; }
    .ui-stage__icon--spin { animation: ui-stage-spin 1s linear infinite; }
    @keyframes ui-stage-spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    .ui-stage__dot { width: 7px; height: 7px; border-radius: 999px; }

    .ui-stage[data-status="pending"] { border-color: rgba(255,255,255,0.10); }
    .ui-stage[data-status="pending"] .ui-stage__label { color: rgba(255,255,255,0.55); }
    .ui-stage[data-status="pending"] .ui-stage__dot { background: rgba(255,255,255,0.25); }

    .ui-stage[data-status="active"] { border-color: rgba(96,165,250,0.35); }
    .ui-stage[data-status="active"] .ui-stage__label,
    .ui-stage[data-status="active"] .ui-stage__icon { color: #60a5fa; }

    .ui-stage[data-status="done"] { border-color: rgba(16,185,129,0.35); }
    .ui-stage[data-status="done"] .ui-stage__label,
    .ui-stage[data-status="done"] .ui-stage__icon { color: #6ee7b7; }

    .ui-stage[data-status="error"] { border-color: rgba(239,68,68,0.35); }
    .ui-stage[data-status="error"] .ui-stage__label,
    .ui-stage[data-status="error"] .ui-stage__icon { color: #fca5a5; }

    .ui-stage[data-status="skipped"] { border-color: rgba(255,255,255,0.10); }
    .ui-stage[data-status="skipped"] .ui-stage__label { color: rgba(255,255,255,0.55); }
    .ui-stage[data-status="skipped"] .ui-stage__dot { background: rgba(255,255,255,0.30); }
  `],
})
export class StageChipComponent {
  @Input({ required: true }) status!: StageStatus;
  @Input({ required: true }) label!: string;
  @Input() sub?: string;
}
