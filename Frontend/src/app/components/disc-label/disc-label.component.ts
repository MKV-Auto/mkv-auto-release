import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, ViewEncapsulation } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { slugifyDiscName } from '../../utils/disc-slug.util';

@Component({
  selector: 'app-disc-label',
  standalone: true,
  imports: [CommonModule, FormsModule],
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

  @Output() labelChanged = new EventEmitter<void>();
  @Output() nameChanged = new EventEmitter<void>();
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
      this.labelChanged.emit();
      this.fieldBlur.emit();
    }
  }

  onDiscFormatChange(): void {
    this.labelChanged.emit();
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
