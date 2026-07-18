import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

export type PillTone =
  | 'emerald'
  | 'slate'
  | 'amber'
  | 'red'
  | 'blue'
  | 'cyan'
  | 'indigo'
  | 'purple';

@Component({
  selector: 'ui-pill',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span class="ui-pill" [attr.data-tone]="tone">
      <ng-content select="[uiPillIcon]"></ng-content>
      <ng-content></ng-content>
    </span>
  `,
  styles: [`
    .ui-pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 11px;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 999px;
      line-height: 1.3;
      border: 1px solid transparent;
      white-space: nowrap;
    }
    .ui-pill[data-tone="emerald"] { color: #6ee7b7; background: rgba(16,185,129,0.15); border-color: rgba(16,185,129,0.35); }
    .ui-pill[data-tone="slate"]   { color: #cbd5e1; background: rgba(148,163,184,0.12); border-color: rgba(148,163,184,0.25); }
    .ui-pill[data-tone="amber"]   { color: #fcd34d; background: rgba(251,191,36,0.18); border-color: rgba(251,191,36,0.35); }
    .ui-pill[data-tone="red"]     { color: #fca5a5; background: rgba(239,68,68,0.18); border-color: rgba(239,68,68,0.35); }
    .ui-pill[data-tone="blue"]    { color: #93c5fd; background: rgba(96,165,250,0.15); border-color: rgba(96,165,250,0.35); }
    .ui-pill[data-tone="cyan"]    { color: #67e8f9; background: rgba(34,211,238,0.12); border-color: rgba(34,211,238,0.30); }
    .ui-pill[data-tone="indigo"]  { color: #a5b4fc; background: rgba(99,102,241,0.18); border-color: rgba(99,102,241,0.35); }
    .ui-pill[data-tone="purple"]  { color: #d8b4fe; background: rgba(168,85,247,0.18); border-color: rgba(168,85,247,0.35); }
  `],
})
export class PillComponent {
  @Input() tone: PillTone = 'slate';
}
