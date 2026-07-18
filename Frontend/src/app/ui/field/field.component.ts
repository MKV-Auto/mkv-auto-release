import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

@Component({
  selector: 'ui-field',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="ui-field" [class.ui-field--inline]="inline">
      <div class="ui-field__head">
        <div class="ui-field__label">{{ label }}</div>
        @if (hint) {
          <div class="ui-field__hint">{{ hint }}</div>
        }
        @if (!inline) {
          <div class="ui-field__control"><ng-content></ng-content></div>
        }
      </div>
      @if (inline) {
        <div class="ui-field__control"><ng-content select="[uiFieldInline]"></ng-content></div>
      }
    </div>
  `,
  styles: [`
    .ui-field {
      display: block;
      padding-bottom: 14px;
      margin-bottom: 14px;
      border-bottom: 1px solid rgba(255,255,255,0.06);
    }
    .ui-field--inline {
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      gap: 16px;
    }
    .ui-field__label { font-size: 14px; font-weight: 600; color: #fff; }
    .ui-field__hint { font-size: 12px; color: var(--text-muted, rgba(255,255,255,0.6)); margin-top: 2px; }
    .ui-field:not(.ui-field--inline) .ui-field__control { margin-top: 10px; }
  `],
})
export class FieldComponent {
  @Input({ required: true }) label!: string;
  @Input() hint?: string;
  @Input() inline = false;
}
