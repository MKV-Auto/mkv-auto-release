import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, ViewEncapsulation } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { slugifyDiscName } from '../../utils/disc-slug.util';
import { BtnComponent, SwitchComponent } from '../../ui';

@Component({
  selector: 'app-disc-label',
  standalone: true,
  imports: [CommonModule, FormsModule, BtnComponent, SwitchComponent],
  templateUrl: './disc-label.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
})
export class DiscLabelComponent {
  @Input() labelForm: any;
  /** Shown in step title when set; falls back to labelForm.disc_number (e.g. merge discInfo when labelForm lags after WS). */
  @Input() displayDiscNumber: number | null | undefined;
  @Input() labelSaving = false;
  @Input() lastAutosaveOk = true;
  /** When false, hide title/icon/subtitle (e.g. when parent provides the step header). Status line still shown when compact. */
  @Input() showHeading = true;

  /**
   * #695: events carry the edited field + value. Handlers must apply the value
   * to the CURRENT context (immutably) rather than trusting the template-bound
   * labelForm object — ngModel writes into whatever object was bound last
   * change-detection cycle, so under churn the bound object can be stale and
   * identity-dependent reads drop keystrokes.
   */
  @Output() labelChanged = new EventEmitter<
    { field: 'disc_slug' | 'disc_format'; value: string } | { field: 'auto_naming'; value: boolean } | undefined
  >();
  @Output() nameChanged = new EventEmitter<string>();
  /** #873: the user set this disc's number by hand. */
  @Output() discNumberChanged = new EventEmitter<number>();
  /** #873: put the release's discs back in scan order. */
  @Output() resetDiscNumbers = new EventEmitter<void>();
  @Output() fieldBlur = new EventEmitter<void>();

  private focusDepth = 0;
  isActive = false;

  get showSpinner(): boolean {
    return this.labelSaving || this.isActive;
  }

  get headingDiscNumber(): number | null | undefined {
    return this.displayDiscNumber ?? this.labelForm?.disc_number;
  }

  private isEmpty(val: any): boolean {
    return val === null || val === undefined || `${val}`.trim() === '';
  }

  get missingDiscFormat(): boolean {
    return this.isEmpty(this.labelForm?.disc_format);
  }

  get missingDiscName(): boolean {
    return this.isEmpty(this.labelForm?.disc_name?.trim?.());
  }

  /** Auto-generated slug for placeholder (ui-lab: "Auto-generated from name") */
  commitDiscNumber(raw: string): void {
    const n = Number(raw);
    if (!Number.isInteger(n) || n < 1 || n === this.headingDiscNumber) return;
    this.discNumberChanged.emit(n);
  }

  /** #915: on unless the server says the user chose their own name. */
  get autoNaming(): boolean {
    return this.labelForm?.auto_naming !== false;
  }

  setAutoNaming(on: boolean): void {
    this.labelChanged.emit({ field: 'auto_naming', value: on });
  }

  get autoSlug(): string {
    const name = this.labelForm?.disc_name;
    if (!name || `${name}`.trim() === '') return '';
    return slugifyDiscName(name);
  }

  /** Format options for button group (value, label) – API values preserved */
  readonly discFormats = [
    { value: 'Blu-Ray', label: 'Blu-ray' },
    { value: 'UHD', label: '4K UHD' },
    { value: 'DVD', label: 'DVD' },
  ] as const;

  setDiscFormat(value: string): void {
    if (this.labelForm) {
      this.labelForm.disc_format = value;
      this.labelChanged.emit({ field: 'disc_format', value });
      this.fieldBlur.emit();
    }
  }

  onDiscFormatChange(): void {
    this.labelChanged.emit({ field: 'disc_format', value: this.labelForm?.disc_format ?? '' });
  }

  onFocusIn(): void {
    this.focusDepth += 1;
    this.isActive = true;
  }

  onFocusOut(): void {
    this.focusDepth = Math.max(0, this.focusDepth - 1);
    this.isActive = this.focusDepth > 0;
  }

}
