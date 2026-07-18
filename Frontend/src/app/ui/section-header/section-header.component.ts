import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

@Component({
  selector: 'ui-section-header',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="ui-sec">
      <div class="ui-sec__main">
        <h3 class="ui-sec__title">
          <span class="ui-sec__icon"><ng-content select="[uiSecIcon]"></ng-content></span>
          {{ title }}
        </h3>
        @if (subtitle) {
          <p class="ui-sec__sub">{{ subtitle }}</p>
        }
      </div>
      <div class="ui-sec__actions">
        <ng-content></ng-content>
      </div>
    </div>
  `,
  styles: [`
    .ui-sec {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 14px;
    }
    .ui-sec__title {
      margin: 0;
      font-size: 15px;
      font-weight: 700;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .ui-sec__icon {
      color: #60a5fa;
      display: inline-flex;
    }
    .ui-sec__icon:empty { display: none; }
    .ui-sec__sub {
      margin: 4px 0 0;
      font-size: 13px;
      color: var(--text-muted, rgba(255,255,255,0.55));
    }
    .ui-sec__actions:empty { display: none; }
  `],
})
export class SectionHeaderComponent {
  @Input({ required: true }) title!: string;
  @Input() subtitle?: string;
}
