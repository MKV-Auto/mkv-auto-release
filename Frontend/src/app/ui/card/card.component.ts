import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

@Component({
  selector: 'ui-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="ui-card"
      [class.ui-card--active]="active"
      [class.ui-card--interactive]="interactive"
      [attr.role]="interactive ? 'button' : null"
      [attr.tabindex]="interactive ? 0 : null"
    >
      <ng-content></ng-content>
    </div>
  `,
  styles: [`
    .ui-card {
      background: var(--ui-card-bg, rgba(255,255,255,0.04));
      border: 1px solid var(--ui-card-border, rgba(255,255,255,0.10));
      border-radius: var(--ui-card-radius, 14px);
      backdrop-filter: blur(var(--ui-card-blur, 12px));
      -webkit-backdrop-filter: blur(var(--ui-card-blur, 12px));
      transition: transform 150ms ease, background 150ms ease, border-color 150ms ease, box-shadow 150ms ease;
      box-sizing: border-box;
    }
    .ui-card--active {
      background: rgba(255,255,255,0.08);
      border-color: var(--ui-accent, #6366f1);
      box-shadow: var(--ui-accent-ring, 0 0 0 2px rgba(99,102,241,0.3));
    }
    .ui-card--interactive { cursor: pointer; }
    .ui-card--interactive:hover { transform: translateY(-2px); background: rgba(255,255,255,0.07); }
    .ui-card--interactive:focus-visible {
      outline: none;
      box-shadow: var(--ui-accent-ring, 0 0 0 2px rgba(99,102,241,0.3));
    }
  `],
})
export class CardComponent {
  @Input() active = false;
  @Input() interactive = false;
}
