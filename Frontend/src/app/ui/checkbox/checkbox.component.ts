import { ChangeDetectionStrategy, ChangeDetectorRef, Component, EventEmitter, Input, Output, forwardRef } from '@angular/core';
import { ControlValueAccessor, NG_VALUE_ACCESSOR } from '@angular/forms';

/**
 * Styled checkbox primitive — dark-theme by default, replaces the
 * unstyled native browser checkbox that mismatched the rest of the
 * Settings panel (#595).
 *
 * API:
 * - `[checked]` / `(checkedChange)` for `[(checked)]` two-way binding.
 * - Also implements `ControlValueAccessor`, so the existing
 *   `[(ngModel)]="..."` callsites only need the tag swapped.
 *
 * Renders a real `<input type="checkbox">` (visually hidden) for
 * keyboard, form-submission, and assistive-tech semantics, with a
 * styled visual box layered on top.
 */
@Component({
  selector: 'ui-checkbox',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [
    {
      provide: NG_VALUE_ACCESSOR,
      useExisting: forwardRef(() => CheckboxComponent),
      multi: true,
    },
  ],
  template: `
    <label class="ui-checkbox" [class.ui-checkbox--disabled]="disabled">
      <input
        type="checkbox"
        class="ui-checkbox__input"
        [checked]="checked"
        [disabled]="disabled"
        [attr.aria-label]="ariaLabel ?? null"
        (change)="onInputChange($event)"
        (blur)="onTouched()"
      />
      <span class="ui-checkbox__box" aria-hidden="true">
        <svg class="ui-checkbox__check" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="20 6 9 17 4 12"></polyline>
        </svg>
      </span>
      <ng-content></ng-content>
    </label>
  `,
  styles: [`
    .ui-checkbox {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      font-size: 13px;
      color: rgba(255, 255, 255, 0.85);
      user-select: none;
    }
    .ui-checkbox--disabled {
      cursor: not-allowed;
      opacity: 0.5;
    }
    .ui-checkbox__input {
      /* Visually hidden but reachable by AT + keyboard. */
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .ui-checkbox__box {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 16px;
      height: 16px;
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.2);
      color: transparent;
      transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease;
      flex-shrink: 0;
    }
    .ui-checkbox:hover .ui-checkbox__box {
      border-color: rgba(255, 255, 255, 0.35);
    }
    .ui-checkbox__input:focus-visible + .ui-checkbox__box {
      outline: 2px solid rgba(99, 102, 241, 0.7);
      outline-offset: 2px;
    }
    .ui-checkbox__input:checked + .ui-checkbox__box {
      background: #6366f1;
      border-color: #6366f1;
      color: #ffffff;
    }
    .ui-checkbox--disabled .ui-checkbox__box {
      background: rgba(255, 255, 255, 0.04);
      border-color: rgba(255, 255, 255, 0.1);
    }
    .ui-checkbox__check {
      opacity: 0;
      transition: opacity 80ms ease;
    }
    .ui-checkbox__input:checked + .ui-checkbox__box .ui-checkbox__check {
      opacity: 1;
    }
  `],
})
export class CheckboxComponent implements ControlValueAccessor {
  @Input() checked = false;
  @Input() disabled = false;
  @Input() ariaLabel?: string;
  @Output() checkedChange = new EventEmitter<boolean>();

  private onChange: (v: boolean) => void = () => {};
  onTouched: () => void = () => {};

  constructor(private cdr: ChangeDetectorRef) {}

  onInputChange(event: Event): void {
    const v = (event.target as HTMLInputElement).checked;
    this.checked = v;
    this.checkedChange.emit(v);
    this.onChange(v);
  }

  // ControlValueAccessor — lets existing [(ngModel)] callsites work
  // unchanged when the only edit is swapping the tag.
  writeValue(value: boolean): void {
    this.checked = !!value;
    this.cdr.markForCheck();
  }
  registerOnChange(fn: (v: boolean) => void): void {
    this.onChange = fn;
  }
  registerOnTouched(fn: () => void): void {
    this.onTouched = fn;
  }
  setDisabledState(isDisabled: boolean): void {
    this.disabled = isDisabled;
    this.cdr.markForCheck();
  }
}
