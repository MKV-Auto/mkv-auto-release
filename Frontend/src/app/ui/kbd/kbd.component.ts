import { ChangeDetectionStrategy, Component } from '@angular/core';

@Component({
  selector: 'ui-kbd',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<kbd class="ui-kbd"><ng-content></ng-content></kbd>`,
  styles: [`
    .ui-kbd {
      font-family: inherit;
      padding: 2px 6px;
      border-radius: 4px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.12);
      font-size: 11px;
      color: rgba(255,255,255,0.85);
    }
  `],
})
export class KbdComponent {}
