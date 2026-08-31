import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { PillComponent } from '../../ui/pill/pill.component';
import { titleTypeDisplayLabel } from '../../constants/title-type-options';

export type TitleRowStatus = 'complete' | 'running' | 'failed' | 'pending' | 'ignored' | 'duplicate';

/**
 * Compact summary row for a single title in the labeling step (approved
 * cleanup mock). A fixed-width TYPE CHIP leads the row — it carries labeled
 * state, its source (user vs automation), and ignored state in one object —
 * followed by name over source · duration, then rip progress %, then the
 * parent's suffix slot (badges + the always-visible preview/ignore buttons).
 *
 * This component is intentionally read-only — all editable fields live in
 * `TitleEditor`. Keeping the row compact lets the user scan a long disc
 * without scrolling through form noise.
 */
@Component({
  selector: 'app-title-row',
  standalone: true,
  imports: [CommonModule, PillComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="title-row"
      [class.is-selected]="selected"
      [class.is-ignored]="status === 'ignored'"
      [class.is-unlabeled]="chipState() === 'todo'"
      [attr.aria-pressed]="selected ? 'true' : 'false'"
      (click)="selected$.emit()">
      <!-- Type chip leads the row: amber = needs you, green = you labeled
           it, indigo = automation labeled it, grey = ignored. Replaces the
           decorative gradient thumb, the standalone green check, and the
           right-side source chips — one status object per row. -->
      <span class="title-row__chip" [attr.data-chip]="chipState()">{{ chipLabel() }}</span>

      <span class="title-row__body">
        <span class="title-row__name" [class.title-row__name--placeholder]="!title">
          {{ title || 'Untitled' }}
        </span>
        <span class="title-row__meta">
          <span class="title-row__source" *ngIf="sourceFile">{{ sourceFile }}</span>
          <span aria-hidden="true" *ngIf="sourceFile && duration"> · </span>
          <span class="title-row__duration" *ngIf="duration">{{ duration }}</span>
          <span aria-hidden="true" *ngIf="(sourceFile || duration) && chipState() === 'auto' && discdbHit"> · </span>
          <span class="title-row__discdb" *ngIf="chipState() === 'auto' && discdbHit">DiscDB</span>
        </span>
      </span>

      <span class="title-row__status">
        <!-- Rip progress is a plain number — the % IS the information.
             Failures keep a red pill; they need attention. -->
        <span class="title-row__progress" *ngIf="status === 'running' && progress != null && progress > 0">{{ progress }}%</span>
        <ui-pill *ngIf="status === 'failed'" tone="red">Failed</ui-pill>
        <ng-content select="[uiRowSuffix]"></ng-content>
      </span>
    </button>
  `,
  styles: [`
    .title-row {
      all: unset;
      cursor: pointer;
      display: grid;
      grid-template-columns: 86px 1fr auto;
      gap: 9px;
      align-items: center;
      padding: 7px 9px;
      border-radius: 9px;
      background: transparent;
      border: 1px solid transparent;
      transition: background 150ms ease, border-color 150ms ease;
      box-sizing: border-box;
      width: 100%;
    }
    .title-row:hover { background: rgba(255, 255, 255, 0.03); }
    .title-row.is-unlabeled { border-color: rgba(251, 191, 36, 0.28); }
    .title-row.is-selected {
      background: rgba(99, 102, 241, 0.10);
      border-color: rgba(99, 102, 241, 0.45);
    }
    .title-row.is-ignored { opacity: 0.55; }
    .title-row:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.45);
    }

    .title-row__chip {
      width: 86px;
      box-sizing: border-box;
      text-align: center;
      font-size: 10.5px;
      font-weight: 600;
      border-radius: 999px;
      padding: 2.5px 6px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      flex-shrink: 0;
    }
    .title-row__chip[data-chip='todo'] { background: rgba(251, 191, 36, 0.12); color: #fbbf24; }
    .title-row__chip[data-chip='done'] { background: rgba(74, 222, 128, 0.10); color: #4ade80; }
    .title-row__chip[data-chip='auto'] { background: rgba(99, 102, 241, 0.14); color: #818cf8; }
    .title-row__chip[data-chip='off']  { background: rgba(255, 255, 255, 0.06); color: rgba(255, 255, 255, 0.55); }

    .title-row__body {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .title-row__name {
      font-size: 13px;
      font-weight: 600;
      color: #fff;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .title-row__name--placeholder {
      color: var(--text-muted, rgba(255, 255, 255, 0.55));
      font-style: italic;
      font-weight: 500;
    }
    .title-row__meta {
      font-size: 11px;
      color: var(--text-muted, rgba(255, 255, 255, 0.55));
      display: flex;
      align-items: center;
      gap: 4px;
      flex-wrap: wrap;
      min-width: 0;
    }
    .title-row__source {
      font-family: var(--ui-font-mono, ui-monospace, SFMono-Regular, Menlo, Monaco, monospace);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .title-row__discdb {
      color: #818cf8;
      font-weight: 600;
    }
    .title-row__progress {
      color: #60a5fa;
      font-weight: 600;
      font-size: 10.5px;
      font-variant-numeric: tabular-nums;
    }

    .title-row__status {
      display: flex;
      align-items: center;
      gap: 6px;
    }
  `],
})
export class TitleRowComponent {
  /** Title string shown as the row's primary label. Blank → "Untitled". */
  @Input() title: string | null = null;
  /** Mono-text source filename (e.g. `00539.mpls`). */
  @Input() sourceFile: string | null = null;
  /** Pre-formatted duration string (e.g. `2h 18m`). */
  @Input() duration: string | null = null;
  /** Display thumbnail image, when one exists (frame from disc previews). */
  @Input() previewUrl: string | null = null;
  /** Inline gradient background for the thumb when `previewUrl` is null —
   *  defaults to the prototype's neutral indigo→navy fade. */
  @Input() thumbBackground = 'linear-gradient(135deg, rgba(99,102,241,0.25), rgba(15,23,42,0.6))';
  /** Per-title status — drives the right-hand pill tone + label. */
  @Input() status: TitleRowStatus = 'pending';
  /** Optional progress percentage (0-100), shown inline when status is `running`. */
  @Input() progress: number | null = null;
  /** Highlights the row with the indigo accent border + background. */
  @Input() selected = false;

  /** Source-split for the chip system (PR 1's auto/user_type columns).
   * When the status is non-runtime ('pending' / 'ignored' / 'duplicate'),
   * the row's right-side chip(s) come from <app-title-chips> driven by
   * these inputs instead of the legacy single-status pill. */
  @Input() userType: string | null | undefined = null;
  @Input() autoType: string | null | undefined = null;
  /** Whether the parent disc had a successful DiscDB lookup. Determines
   * whether `auto_type` renders as "DiscDB" or "Pending Review". */
  @Input() discdbHit: boolean = false;

  @Output() selected$ = new EventEmitter<void>();

  /** One status object per row (approved cleanup mock):
   *  'off'  — ignored;
   *  'done' — the USER labeled the type (green: your decision);
   *  'auto' — automation labeled it and the user hasn't confirmed (indigo;
   *           the sub-line says "DiscDB" when that's the source);
   *  'todo' — no effective type yet (amber "Type?", row outlined). */
  chipState(): 'todo' | 'done' | 'auto' | 'off' {
    const user = (this.userType ?? '').toString().trim();
    const auto = (this.autoType ?? '').toString().trim();
    const effective = user || auto;
    if (effective.toLowerCase() === 'ignore' || this.status === 'ignored') return 'off';
    if (user) return 'done';
    if (auto) return 'auto';
    return 'todo';
  }

  chipLabel(): string {
    const state = this.chipState();
    if (state === 'off') return 'Ignored';
    if (state === 'todo') return 'Type?';
    const value = state === 'done' ? this.userType : this.autoType;
    return titleTypeDisplayLabel(value);
  }
}
