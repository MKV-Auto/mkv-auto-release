import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { IconComponent } from '../icon/icon.component';

@Component({
  selector: 'ui-stepper',
  standalone: true,
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <ol class="ui-stepper" role="list">
      @for (s of steps; track s; let i = $index, last = $last) {
        <li class="ui-stepper__item"
            [class.ui-stepper__item--active]="i === activeIndex"
            [class.ui-stepper__item--done]="i < activeIndex">
          <span class="ui-stepper__index">
            @if (i < activeIndex) {
              <ui-icon name="check" [size]="12"></ui-icon>
            } @else {
              {{ i + 1 }}
            }
          </span>
          <span class="ui-stepper__label">{{ s }}</span>
          @if (!last) {
            <span class="ui-stepper__connector" aria-hidden="true"></span>
          }
        </li>
      }
    </ol>
  `,
  styles: [`
    .ui-stepper {
      display: flex;
      align-items: center;
      gap: 0;
      flex-wrap: wrap;
      list-style: none;
      padding: 0;
      margin: 0;
    }
    .ui-stepper__item {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .ui-stepper__index {
      width: 24px;
      height: 24px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      font-weight: 700;
      background: rgba(255,255,255,0.06);
      color: var(--text-muted, rgba(255,255,255,0.55));
      border: 1px solid rgba(255,255,255,0.10);
      box-sizing: border-box;
    }
    .ui-stepper__label {
      font-size: 13px;
      font-weight: 500;
      color: var(--text-muted, rgba(255,255,255,0.55));
    }
    .ui-stepper__connector {
      flex: 1;
      min-width: 24px;
      height: 1px;
      margin: 0 14px;
      background: rgba(255,255,255,0.08);
    }

    .ui-stepper__item--active .ui-stepper__index {
      background: var(--primary, #6366f1);
      color: #fff;
      border-color: rgba(99,102,241,0.5);
    }
    .ui-stepper__item--active .ui-stepper__label { color: #fff; }

    .ui-stepper__item--done .ui-stepper__index {
      background: rgba(16,185,129,0.18);
      color: #6ee7b7;
      border-color: rgba(16,185,129,0.35);
    }
    .ui-stepper__item--done .ui-stepper__label { color: rgba(255,255,255,0.7); }
    .ui-stepper__item--done .ui-stepper__connector { background: rgba(16,185,129,0.30); }
  `],
})
export class StepperComponent {
  @Input({ required: true }) steps: string[] = [];
  @Input({ required: true }) activeIndex = 0;
}
