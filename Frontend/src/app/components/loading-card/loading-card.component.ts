import { ChangeDetectionStrategy, Component, Input, ViewEncapsulation } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-loading-card',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './loading-card.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
})
export class LoadingCardComponent {
  @Input() message = 'Loading...';
}
