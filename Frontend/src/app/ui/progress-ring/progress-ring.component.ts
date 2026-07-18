import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

export type ProgressRingTone = 'blue' | 'emerald' | 'amber' | 'red' | 'indigo';

const TONE_COLORS: Record<ProgressRingTone, string> = {
  blue: '#60a5fa',
  emerald: '#10b981',
  amber: '#facc15',
  red: '#ef4444',
  indigo: '#6366f1',
};

@Component({
  selector: 'ui-progress-ring',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="ui-ring" [style.width.px]="size" [style.height.px]="size">
      <svg [attr.width]="size" [attr.height]="size" [attr.viewBox]="'0 0 ' + size + ' ' + size">
        <circle [attr.cx]="size / 2" [attr.cy]="size / 2" [attr.r]="radius"
                fill="none" stroke="rgba(255,255,255,0.1)" stroke-width="2" />
        <circle [attr.cx]="size / 2" [attr.cy]="size / 2" [attr.r]="radius"
                fill="none" [attr.stroke]="strokeColor" stroke-width="2" stroke-linecap="round"
                [attr.stroke-dasharray]="circumference"
                [attr.stroke-dashoffset]="dashOffset"
                [attr.transform]="'rotate(-90 ' + (size / 2) + ' ' + (size / 2) + ')'"
                style="transition: stroke-dashoffset .4s ease" />
      </svg>
      <span class="ui-ring__value">{{ displayValue }}</span>
    </div>
  `,
  styles: [`
    .ui-ring { position: relative; display: inline-block; }
    .ui-ring__value {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 7px;
      font-weight: 700;
      color: rgba(255,255,255,0.8);
      line-height: 1;
    }
  `],
})
export class ProgressRingComponent {
  @Input() value = 0;
  @Input() size = 22;
  @Input() tone: ProgressRingTone = 'blue';

  get displayValue(): number {
    return Math.max(0, Math.min(100, Math.round(this.value)));
  }
  get radius(): number {
    return (this.size - 4) / 2;
  }
  get circumference(): number {
    return 2 * Math.PI * this.radius;
  }
  get dashOffset(): number {
    return this.circumference * (1 - this.displayValue / 100);
  }
  get strokeColor(): string {
    return TONE_COLORS[this.tone] ?? TONE_COLORS.blue;
  }
}
