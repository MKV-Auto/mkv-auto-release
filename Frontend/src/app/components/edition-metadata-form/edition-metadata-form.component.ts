// Shared release/boxset edition fields: create vs complete-metadata use the same UI; parent handles API on submit.
import {
  Component,
  Input,
  Output,
  EventEmitter,
  OnChanges,
  SimpleChanges,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

export interface EditionFormValue {
  name: string;
  year: number | null;
  upc: string;
  asin: string;
  cover_front_url: string;
  cover_back_url: string;
}

@Component({
  selector: 'app-edition-metadata-form',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './edition-metadata-form.component.html',
  styleUrls: ['./edition-metadata-form.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EditionMetadataFormComponent implements OnChanges {
  /** Visual theme (focus ring + primary button tint). */
  @Input() theme: 'release' | 'boxset' | 'movie' = 'release';
  @Input() heading = '';
  /** Optional plus icon row (create flow). */
  @Input() showHeaderIcon = false;
  @Input() missingHints: string[] | null = null;
  /** Server / async errors (e.g. PATCH still incomplete). */
  @Input() externalErrors: string[] = [];
  @Input() loading = false;
  @Input() nameLabel = 'Name *';
  @Input() yearLabel = 'Release Year *';
  @Input() upcLabel = 'UPC / GTIN *';
  @Input() namePlaceholder = '';
  @Input() yearPlaceholder = '';
  @Input() asinPlaceholder = 'B08ABCD123';
  @Input() frontCoverLabel = 'Cover Image URL (Front) *';
  @Input() backCoverLabel = 'Cover Image URL (Back)';
  @Input() cancelLabel = 'Cancel';
  @Input() submitLabel = 'Save';
  /** Apply horizontal padding (e.g. mobile drawer). */
  @Input() padded = false;
  /** Prefill when opening create vs complete-metadata. */
  @Input() prefill: Partial<EditionFormValue> | null = null;
  /** Increment whenever the parent opens the form to re-apply `prefill`. */
  @Input() resetVersion = 0;

  @Output() cancelled = new EventEmitter<void>();
  @Output() submitted = new EventEmitter<EditionFormValue>();
  /** Every keystroke, so a parent can keep a draft.
   *
   * `submitted` only fires once validation passes, and this component is
   * destroyed by the *ngIf around it whenever the panel closes — so without
   * this a parent has no way to know what the user typed, and closing the
   * dropdown threw the work away. */
  @Output() modelChange = new EventEmitter<EditionFormValue>();

  model: EditionFormValue = {
    name: '',
    year: null,
    upc: '',
    asin: '',
    cover_front_url: '',
    cover_back_url: '',
  };
  fieldErrors: string[] = [];

  constructor(private cdr: ChangeDetectorRef) {}

  ngOnChanges(changes: SimpleChanges): void {
    const pf = changes['prefill'];
    const rv = changes['resetVersion'];
    // Re-apply when resetVersion bumps, or when prefill **reference** changes — not on every CD
    // (parents must pass a stable prefill object while the user edits).
    const prefillRefChanged = !!(pf && pf.previousValue !== pf.currentValue);
    if (rv || prefillRefChanged) {
      this.applyPrefill();
    }
    if (changes['externalErrors']) {
      this.cdr.markForCheck();
    }
  }

  applyPrefill(): void {
    const p = this.prefill || {};
    this.model = {
      name: (p.name ?? '').toString(),
      year: p.year ?? null,
      upc: (p.upc ?? '').toString(),
      asin: (p.asin ?? '').toString(),
      cover_front_url: (p.cover_front_url ?? '').toString(),
      cover_back_url: (p.cover_back_url ?? '').toString(),
    };
    this.fieldErrors = [];
    this.cdr.markForCheck();
  }

  /** Bound to every field's (ngModelChange) so a parent can hold a draft.
   * Emits a copy — the parent must not alias `model`, or feeding it back as
   * `prefill` would mutate what it is trying to restore. */
  onModelChanged(): void {
    this.modelChange.emit({ ...this.model });
  }

  get allErrors(): string[] {
    const ext = this.externalErrors || [];
    return [...this.fieldErrors, ...ext];
  }

  _validateUPC(upc: string | undefined): boolean {
    if (!upc) return false;
    const s = String(upc).trim();
    if (!/^\d+$/.test(s)) return false;
    if (/^0+$/.test(s)) return false;
    const len = s.length;
    return len === 8 || len === 12 || len === 13 || len === 14;
  }

  _validateCoverURL(url: string | undefined): boolean {
    if (!url) return false;
    const s = String(url).trim();
    return s.startsWith('http://') || s.startsWith('https://');
  }

  submit(): void {
    this.fieldErrors = [];
    const isMovie = this.theme === 'movie';
    const nameOk = !!(this.model.name && this.model.name.trim());
    const y = this.model.year;
    const yearOk =
      y != null && y !== ('' as unknown as number) && Number.isInteger(Number(y)) && Number(y) >= 1000 && Number(y) <= 9999;
    const upcOk = isMovie ? true : this._validateUPC(this.model.upc);
    const coverTrim = (this.model.cover_front_url || '').trim();
    const coverOk = isMovie
      ? !coverTrim || this._validateCoverURL(this.model.cover_front_url)
      : this._validateCoverURL(this.model.cover_front_url);
    const backRaw = this.model.cover_back_url?.trim();
    const backOk = !backRaw || this._validateCoverURL(backRaw);
    if (!nameOk) this.fieldErrors.push('Name is required');
    if (!yearOk) this.fieldErrors.push(isMovie ? 'Year must be 1000–9999' : 'Release year must be 1000–9999');
    if (!isMovie && !upcOk) this.fieldErrors.push('UPC/GTIN must be 8, 12, 13, or 14 digits');
    if (!coverOk) this.fieldErrors.push('Front cover URL must be http:// or https://');
    if (!backOk) this.fieldErrors.push('Back cover URL must be http:// or https:// when provided');
    if (this.fieldErrors.length) {
      this.cdr.markForCheck();
      return;
    }
    this.submitted.emit({
      name: this.model.name.trim(),
      year: Number(this.model.year),
      upc: this.model.upc.trim(),
      asin: (this.model.asin || '').trim(),
      cover_front_url: coverTrim,
      cover_back_url: backRaw || '',
    });
  }

  cancel(): void {
    this.cancelled.emit();
  }
}
