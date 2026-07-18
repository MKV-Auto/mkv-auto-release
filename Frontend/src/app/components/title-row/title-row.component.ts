import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { IconComponent } from '../../ui/icon/icon.component';
import { PillComponent, PillTone } from '../../ui/pill/pill.component';
import { TitleChipsComponent } from '../title-chips/title-chips.component';

export type TitleRowStatus = 'complete' | 'running' | 'failed' | 'pending' | 'ignored' | 'duplicate';

const STATUS_TONE: Record<TitleRowStatus, PillTone> = {
  complete: 'emerald',
  running: 'blue',
  failed: 'red',
  pending: 'slate',
  ignored: 'slate',
  duplicate: 'purple',
};

const STATUS_LABEL: Record<TitleRowStatus, string> = {
  complete: 'Complete',
  running: 'Running',
  failed: 'Failed',
  pending: 'Pending',
  ignored: 'Ignored',
  duplicate: 'Duplicate',
};

/** Statuses where the runtime pill wins over the source chips. Only
 * `running` and `failed` need attention — `complete` is the expected
 * end state during the labeling step and just duplicates the global
 * stage progress bar. Letting the source chip (DiscDB / Canonical /
 * Ignored) win on completed rows surfaces *where the label came from*,
 * which is what the labeling step is actually about. */
const RUNTIME_STATUSES = new Set<TitleRowStatus>(['running', 'failed']);

/**
 * Compact summary row for a single title in the labeling step. Adapted from the
 * prototype's `TitleListRow` (research/MKV Auto UI/labeling.jsx). Renders a
 * 40px gradient thumbnail (or actual preview image when provided), the title /
 * source / duration, and a status pill on the right. Click selects the title
 * for editing in the side-panel `TitleEditor`.
 *
 * This component is intentionally read-only — all editable fields live in
 * `TitleEditor`. Keeping the row compact lets the user scan a long disc
 * without scrolling through form noise.
 */
@Component({
  selector: 'app-title-row',
  standalone: true,
  imports: [CommonModule, IconComponent, PillComponent, TitleChipsComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="title-row"
      [class.is-selected]="selected"
      [class.is-ignored]="status === 'ignored'"
      [attr.aria-pressed]="selected ? 'true' : 'false'"
      (click)="selected$.emit()">
      <span class="title-row__thumb" [style.background]="thumbBackground">
        <img *ngIf="previewUrl" [src]="previewUrl" [alt]="''" class="title-row__thumb-img" loading="lazy" />
        <span class="title-row__thumb-icon" *ngIf="!previewUrl" aria-hidden="true">
          <ui-icon name="play" [size]="11"></ui-icon>
        </span>
      </span>

      <span class="title-row__body">
        <span class="title-row__name" [class.title-row__name--placeholder]="!title">
          {{ title || 'Untitled' }}
        </span>
        <span class="title-row__meta">
          <span class="title-row__source" *ngIf="sourceFile">{{ sourceFile }}</span>
          <span aria-hidden="true" *ngIf="sourceFile && duration"> · </span>
          <span class="title-row__duration" *ngIf="duration">{{ duration }}</span>
          <span aria-hidden="true" *ngIf="(sourceFile || duration) && progress != null && progress > 0 && status === 'running'"> · </span>
          <span class="title-row__progress" *ngIf="progress != null && progress > 0 && status === 'running'">{{ progress }}%</span>
        </span>
      </span>

      <span class="title-row__status">
        <!-- Source attribution chips render FIRST so they line up with the
             Canonical / Likely decoy pills that come from the parent's
             uiRowSuffix slot (those are pills too). Keeping every pill on
             the left of the labeling-complete check icon gives a
             consistent "[source] [status]" reading order regardless of
             whether the pill came from this component or the slot. -->
        <app-title-chips
          *ngIf="!showRuntimePill"
          [userType]="userType"
          [autoType]="autoType"
          [discdbHit]="discdbHit">
        </app-title-chips>
        <!-- Runtime statuses (running / failed) own a pill — those are
             real-time progress signals worth highlighting on the row. -->
        <ui-pill *ngIf="showRuntimePill" [tone]="pillTone()">{{ pillLabel() }}</ui-pill>
        <ng-content select="[uiRowSuffix]"></ng-content>
      </span>
    </button>
  `,
  styles: [`
    .title-row {
      all: unset;
      cursor: pointer;
      display: grid;
      grid-template-columns: 40px 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 10px;
      border-radius: 8px;
      background: transparent;
      border: 1px solid transparent;
      transition: background 150ms ease, border-color 150ms ease;
      box-sizing: border-box;
      width: 100%;
    }
    .title-row:hover { background: rgba(255, 255, 255, 0.03); }
    .title-row.is-selected {
      background: rgba(99, 102, 241, 0.10);
      border-color: rgba(99, 102, 241, 0.45);
    }
    .title-row.is-ignored { opacity: 0.65; }
    .title-row:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.45);
    }

    .title-row__thumb {
      width: 40px;
      height: 40px;
      border-radius: 6px;
      border: 1px solid rgba(255, 255, 255, 0.06);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
      flex-shrink: 0;
      position: relative;
    }
    .title-row__thumb-img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .title-row__thumb-icon {
      color: rgba(255, 255, 255, 0.6);
      display: inline-flex;
    }

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
    .title-row__progress {
      color: #93c5fd;
      font-weight: 600;
    }

    .title-row__status {
      display: flex;
      align-items: center;
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

  get showRuntimePill(): boolean {
    return RUNTIME_STATUSES.has(this.status);
  }

  pillTone(): PillTone {
    return STATUS_TONE[this.status] ?? 'slate';
  }
  pillLabel(): string {
    return STATUS_LABEL[this.status] ?? 'Pending';
  }
}
