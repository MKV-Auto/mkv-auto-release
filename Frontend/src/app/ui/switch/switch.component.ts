import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';

/**
 * On/off switch primitive: the same control the Settings page uses for its
 * toggles (`.settings-toggle`), shared so workflow surfaces match it.
 *
 * API: `[checked]` / `(checkedChange)` for `[(checked)]`, `[disabled]`,
 * `[ariaLabel]` when there's no visible label. A `<button role="switch">`,
 * so it's keyboard-operable and announced as a switch, and the global
 * `input` styling never reaches it.
 */
@Component({
  selector: 'ui-switch',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      role="switch"
      class="ui-switch"
      [class.is-on]="checked"
      [disabled]="disabled"
      [attr.aria-checked]="checked ? 'true' : 'false'"
      [attr.aria-label]="ariaLabel ?? null"
      (click)="toggle()">
      <span class="ui-switch__knob" aria-hidden="true"></span>
    </button>
  `,
  styles: [`
    :host { display: inline-flex; }
    .ui-switch {
      all: unset;
      cursor: pointer;
      position: relative;
      width: 36px;
      height: 20px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.18);
      transition: background 200ms ease;
      flex-shrink: 0;
    }
    .ui-switch.is-on { background: #22c55e; }
    .ui-switch:focus-visible { outline: none; box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.45); }
    .ui-switch:disabled { cursor: default; opacity: 0.5; }
    .ui-switch__knob {
      position: absolute;
      top: 2px;
      left: 2px;
      width: 16px;
      height: 16px;
      border-radius: 999px;
      background: #fff;
      transition: transform 200ms ease;
    }
    .ui-switch.is-on .ui-switch__knob { transform: translateX(16px); }
  `],
})
export class SwitchComponent {
  @Input() checked = false;
  @Input() disabled = false;
  @Input() ariaLabel?: string;
  @Output() checkedChange = new EventEmitter<boolean>();

  toggle(): void {
    if (this.disabled) return;
    this.checked = !this.checked;
    this.checkedChange.emit(this.checked);
  }
}
