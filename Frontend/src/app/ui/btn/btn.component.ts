import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { IconComponent } from '../icon/icon.component';

export type BtnVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'emerald';

@Component({
  selector: 'ui-btn',
  standalone: true,
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="ui-btn"
      [attr.data-variant]="variant"
      [attr.aria-busy]="loading ? 'true' : null"
      [class.ui-btn--full]="fullWidth"
      [class.ui-btn--loading]="loading"
      [disabled]="disabled || loading"
    >
      @if (loading) {
        <span class="ui-btn__spin"><ui-icon name="spinner" [size]="14"></ui-icon></span>
      } @else {
        <ng-content select="[uiBtnIcon]"></ng-content>
      }
      <ng-content></ng-content>
    </button>
  `,
  styles: [`
    .ui-btn {
      all: unset;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 10px 16px;
      border-radius: 10px;
      font-size: 13px;
      font-weight: 600;
      border: 1px solid transparent;
      transition: transform 150ms ease, background 150ms ease, border-color 150ms ease;
      white-space: nowrap;
      box-sizing: border-box;
    }
    .ui-btn:disabled { cursor: not-allowed; opacity: 0.5; }
    .ui-btn--full { display: flex; width: 100%; }
    .ui-btn:not(:disabled):hover { transform: translateY(-1px); }
    .ui-btn:not(:disabled):active { transform: translateY(0); }
    .ui-btn:focus-visible { box-shadow: 0 0 0 2px rgba(99,102,241,0.45); outline: none; }

    .ui-btn[data-variant="primary"]   { background: linear-gradient(135deg,#6366f1,#4f46e5); color: #fff; border-color: rgba(99,102,241,0.5); }
    .ui-btn[data-variant="secondary"] { background: rgba(255,255,255,0.06); color: #fff; border-color: rgba(255,255,255,0.14); }
    .ui-btn[data-variant="ghost"]     { background: transparent; color: #fff; border-color: rgba(255,255,255,0.10); }
    .ui-btn[data-variant="danger"]    { background: rgba(239,68,68,0.12); color: #fca5a5; border-color: rgba(239,68,68,0.35); }
    .ui-btn[data-variant="emerald"]   { background: rgba(16,185,129,0.15); color: #6ee7b7; border-color: rgba(16,185,129,0.35); }

    .ui-btn[data-variant="secondary"]:not(:disabled):hover { background: rgba(255,255,255,0.1); }
    .ui-btn[data-variant="ghost"]:not(:disabled):hover     { background: rgba(255,255,255,0.06); }
    .ui-btn[data-variant="danger"]:not(:disabled):hover    { background: rgba(239,68,68,0.18); }
    .ui-btn[data-variant="emerald"]:not(:disabled):hover   { background: rgba(16,185,129,0.22); }

    .ui-btn__spin {
      display: inline-flex;
      animation: ui-btn-spin 1s linear infinite;
    }
    @keyframes ui-btn-spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
  `],
})
export class BtnComponent {
  @Input() variant: BtnVariant = 'secondary';
  @Input() disabled = false;
  @Input() fullWidth = false;
  @Input() loading = false;
}
