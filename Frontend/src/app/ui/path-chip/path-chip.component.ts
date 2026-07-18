import { ChangeDetectionStrategy, ChangeDetectorRef, Component, EventEmitter, Input, Output } from '@angular/core';
import { IconComponent } from '../icon/icon.component';

@Component({
  selector: 'ui-path-chip',
  standalone: true,
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span class="ui-pathchip" [class.ui-pathchip--full]="full" [attr.title]="path">
      <span class="ui-pathchip__text">{{ path }}</span>
      <button
        type="button"
        class="ui-pathchip__btn"
        [class.ui-pathchip__btn--copied]="copied"
        aria-label="Copy path"
        (click)="copy()"
      >
        <ui-icon [name]="copied ? 'check' : 'copy'" [size]="12"></ui-icon>
      </button>
    </span>
  `,
  styles: [`
    .ui-pathchip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 8px;
      background: rgba(0,0,0,0.28);
      border: 1px solid rgba(255,255,255,0.08);
      font-family: var(--ui-font-mono, ui-monospace, SFMono-Regular, Menlo, Monaco, monospace);
      font-size: 11.5px;
      color: rgba(255,255,255,0.85);
      max-width: 380px;
      min-width: 0;
      box-sizing: border-box;
    }
    .ui-pathchip--full { max-width: 100%; }
    .ui-pathchip__text {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      direction: rtl;
      text-align: left;
      min-width: 0;
      flex: 1;
    }
    .ui-pathchip__btn {
      all: unset;
      cursor: pointer;
      padding: 2px;
      border-radius: 4px;
      color: rgba(255,255,255,0.5);
      display: inline-flex;
      flex-shrink: 0;
      transition: color 150ms ease;
    }
    .ui-pathchip__btn:hover { color: rgba(255,255,255,0.85); }
    .ui-pathchip__btn--copied { color: #6ee7b7; }
    .ui-pathchip__btn:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99,102,241,0.45);
    }
  `],
})
export class PathChipComponent {
  @Input({ required: true }) path!: string;
  @Input() full = false;
  @Output() copied$ = new EventEmitter<string>();

  copied = false;

  constructor(private cdr: ChangeDetectorRef) {}

  async copy(): Promise<void> {
    try {
      await navigator.clipboard?.writeText(this.path);
    } catch {
      // Ignore clipboard errors — fall through to visual feedback.
    }
    this.copied = true;
    this.copied$.emit(this.path);
    this.cdr.markForCheck();
    setTimeout(() => {
      this.copied = false;
      this.cdr.markForCheck();
    }, 1400);
  }
}
