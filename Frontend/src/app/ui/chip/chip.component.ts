import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';

@Component({
  selector: 'ui-chip',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="ui-chip"
      [class.ui-chip--active]="active"
      [attr.aria-pressed]="active ? 'true' : 'false'"
      (click)="toggled.emit(!active)"
    >
      <ng-content select="[uiChipIcon]"></ng-content>
      <ng-content></ng-content>
    </button>
  `,
  styles: [`
    .ui-chip {
      all: unset;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 500;
      color: rgba(255,255,255,0.7);
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.10);
      transition: background 150ms ease, color 150ms ease, border-color 150ms ease;
      box-sizing: border-box;
    }
    .ui-chip:hover { background: rgba(255,255,255,0.08); color: #fff; }
    .ui-chip--active {
      color: #fff;
      background: rgba(99,102,241,0.18);
      border-color: rgba(99,102,241,0.45);
    }
    .ui-chip:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99,102,241,0.45);
    }
  `],
})
export class ChipComponent {
  @Input() active = false;
  @Output() toggled = new EventEmitter<boolean>();
}
