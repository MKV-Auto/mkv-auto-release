import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { PillComponent } from '../pill/pill.component';
import { ProgressRingComponent } from '../progress-ring/progress-ring.component';

@Component({
  selector: 'ui-library-card',
  standalone: true,
  imports: [PillComponent, ProgressRingComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="ui-libcard"
      [class.ui-libcard--active]="active"
      (click)="activated.emit()"
    >
      <div class="ui-libcard__poster">
        @if (coverUrl) {
          <img class="ui-libcard__cover" [src]="coverUrl" [alt]="title" loading="lazy" />
        } @else {
          <div class="ui-libcard__cover ui-libcard__cover--placeholder" aria-hidden="true">
            {{ initials() }}
          </div>
        }
        @if (completion != null && completion >= 0) {
          <span class="ui-libcard__ring">
            <ui-progress-ring [value]="completion" [size]="34" tone="emerald"></ui-progress-ring>
          </span>
        }
        <div class="ui-libcard__chips">
          @if (year != null) {
            <ui-pill tone="slate">{{ year }}</ui-pill>
          }
          @if (resolution) {
            <ui-pill tone="indigo">{{ resolution }}</ui-pill>
          }
        </div>
      </div>
      <div class="ui-libcard__title">{{ title }}</div>
    </button>
  `,
  styles: [`
    .ui-libcard {
      all: unset;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 8px;
      width: 100%;
      box-sizing: border-box;
      transition: transform 200ms ease;
    }
    .ui-libcard:hover { transform: translateY(-2px); }
    .ui-libcard:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99,102,241,0.45);
      border-radius: var(--ui-card-radius, 14px);
    }
    .ui-libcard--active .ui-libcard__poster {
      box-shadow: var(--ui-accent-ring, 0 0 0 2px rgba(99,102,241,0.3));
    }

    .ui-libcard__poster {
      position: relative;
      aspect-ratio: 2 / 3;
      width: 100%;
      border-radius: var(--ui-card-radius, 14px);
      overflow: hidden;
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--ui-card-border, rgba(255,255,255,0.10));
      transition: box-shadow 150ms ease;
    }
    .ui-libcard__cover {
      width: 100%; height: 100%; object-fit: cover; display: block;
    }
    .ui-libcard__cover--placeholder {
      display: flex; align-items: center; justify-content: center;
      font-size: 28px; font-weight: 700; letter-spacing: 0.04em;
      color: rgba(255,255,255,0.35);
      background: linear-gradient(135deg, rgba(99,102,241,0.12), rgba(16,185,129,0.10));
    }
    .ui-libcard__ring {
      position: absolute;
      top: 8px;
      right: 8px;
      background: rgba(0,0,0,0.55);
      backdrop-filter: blur(6px);
      border-radius: 999px;
      padding: 2px;
      display: inline-flex;
    }
    .ui-libcard__chips {
      position: absolute;
      bottom: 8px;
      left: 8px;
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
    }
    .ui-libcard__title {
      font-size: 13px;
      font-weight: 600;
      color: #fff;
      line-height: 1.3;
      overflow: hidden;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      text-align: left;
    }
  `],
})
export class LibraryCardComponent {
  @Input({ required: true }) title!: string;
  @Input() coverUrl: string | null = null;
  @Input() year: number | string | null = null;
  @Input() resolution: string | null = null;
  @Input() completion: number | null = null;
  @Input() active = false;
  @Output() activated = new EventEmitter<void>();

  initials(): string {
    return this.title
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0]?.toUpperCase() ?? '')
      .join('');
  }
}
