import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

@Component({
  selector: 'ui-empty-state',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="ui-empty">
      <div class="ui-empty__icon" aria-hidden="true">
        <ng-content select="[uiEmptyIcon]"></ng-content>
      </div>
      <div class="ui-empty__title">{{ title }}</div>
      @if (body) {
        <div class="ui-empty__body">{{ body }}</div>
      }
      <div class="ui-empty__actions">
        <ng-content></ng-content>
      </div>
    </div>
  `,
  styles: [`
    .ui-empty {
      padding: 48px 24px;
      text-align: center;
      background: rgba(255,255,255,0.02);
      border: 1px dashed rgba(255,255,255,0.10);
      border-radius: 14px;
    }
    .ui-empty__icon {
      width: 48px;
      height: 48px;
      border-radius: 12px;
      margin: 0 auto 14px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: rgba(99,102,241,0.12);
      color: #a5b4fc;
    }
    .ui-empty__title {
      color: #fff;
      font-weight: 600;
      font-size: 16px;
      margin-bottom: 6px;
    }
    .ui-empty__body {
      color: var(--text-muted, rgba(255,255,255,0.55));
      font-size: 13px;
      max-width: 420px;
      margin: 0 auto 14px;
    }
    .ui-empty__actions:empty { display: none; }
  `],
})
export class EmptyStateComponent {
  @Input({ required: true }) title!: string;
  @Input() body?: string;
}
