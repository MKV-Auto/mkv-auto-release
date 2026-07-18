import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { PillComponent, PillTone } from '../../ui/pill/pill.component';

/**
 * Source-aware chip set for a title row in the labeling step.
 *
 * Replaces the flat "Pending" pill with one or two chips that tell the
 * user *who* labeled the title:
 *   - **User selected** (emerald) — `user_type` is set to a non-ignore.
 *   - **DiscDB** (cyan) — `auto_type` is set to a non-ignore.
 *   - **Pending Review** (amber) — neither source has touched the title.
 *   - **Ignored** (slate) — `user_type === 'ignore'`.
 *   - *(blank)* — `user_type` NULL AND `auto_type === 'ignore'`; the
 *     row stays visible chip-less so the user can review the
 *     automated guess and confirm/override (see PR 4 for the
 *     "Confirm ignore" affordance).
 *
 * Both chips render when the user's pick matches what DiscDB had,
 * giving the user a glanceable "we both agree" signal.
 */

export interface TitleChip {
  tone: PillTone;
  label: string;
  /** Tooltip on hover; surfaced when the user overrode DiscDB's pick
   * so the original auto value isn't lost from the UI. */
  tooltip?: string | null;
}

@Component({
  selector: 'app-title-chips',
  standalone: true,
  imports: [CommonModule, PillComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span class="title-chips" [attr.aria-label]="ariaLabel || null">
      <ui-pill
        *ngFor="let chip of chips; trackBy: trackByLabel"
        [tone]="chip.tone"
        [title]="chip.tooltip || null">
        {{ chip.label }}
      </ui-pill>
    </span>
  `,
  styles: [`
    .title-chips {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      flex-wrap: wrap;
    }
  `],
})
export class TitleChipsComponent {
  /** From `disc_titles.user_type` — user's direct input. */
  @Input() userType: string | null | undefined = null;
  /** From `disc_titles.auto_type` — automated detection (DiscDB,
   * scan-time, Path A sibling-ignore, subsumption, etc). */
  @Input() autoType: string | null | undefined = null;
  /** Whether the parent disc had a successful DiscDB lookup. Drives
   * whether `auto_type` renders as "DiscDB" (true) or "Pending Review"
   * (false — i.e. auto came from scan defaults / dedupe consensus /
   * FFmpeg padding-detect / Path A sibling-ignore, not DiscDB). */
  @Input() discdbHit: boolean = false;

  /** Aggregate aria-label for screen readers; omitted on title-row
   * usage where the parent's button-label already names the title. */
  @Input() ariaLabel: string | null = null;

  get chips(): TitleChip[] {
    return computeTitleChips(this.userType, this.autoType, this.discdbHit);
  }

  trackByLabel(_: number, chip: TitleChip): string {
    return chip.label;
  }
}

/** Pure helper — exported so the title-row can compute chips when it
 * decides whether to render the runtime-status pill OR the source
 * chips (mutually exclusive; runtime status wins for running/failed).
 *
 * Quiet-by-default rules: chips fire ONLY for noteworthy states so the
 * left rail stays scannable when most rows are normally labeled.
 *
 * - `user_type='ignore'` → **Ignored** (slate).
 * - `user_type` set to anything else → silent (user labeled, no chip —
 *   the title text itself signals it's labeled).
 * - `auto_type` non-ignore + `discdbHit=true` + user hasn't picked →
 *   **DiscDB** (cyan).
 * - All other states (auto-from-scan / auto-ignore-awaiting-review /
 *   unlabeled / user-overrode-DiscDB) → silent. A separate label-
 *   complete check icon next to the chip area covers the
 *   "what's done vs not done" question without adding pills.
 *
 * Path A canonical / dup-group primary chip lives at the title-label
 * template level (driven by `matchedCanonicalIndex` / `active===true`),
 * not here — that's a per-row state the chip helper can't see.
 */
export function computeTitleChips(
  userType: string | null | undefined,
  autoType: string | null | undefined,
  discdbHit: boolean = false,
): TitleChip[] {
  const u = (userType || '').trim() || null;
  const a = (autoType || '').trim() || null;
  const uIgnore = u === 'ignore';

  // User explicitly ignored → "Ignored" (hidden behind Show-ignored).
  if (uIgnore) {
    return [{
      tone: 'slate',
      label: 'Ignored',
      tooltip:
        'You marked this title as ignored. Hidden by default — click "Show ignored" to reveal it.',
    }];
  }

  // User picked anything else → silent. They labeled it, no chip needed.
  if (u) {
    return [];
  }

  // User hasn't touched it, DiscDB confirmed it → DiscDB attribution.
  if (a && a !== 'ignore' && discdbHit) {
    return [{
      tone: 'cyan',
      label: 'DiscDB',
      tooltip:
        'Type set automatically from DiscDB metadata. You haven\'t reviewed it yet — ' +
        'confirm by picking a type, or override if DiscDB got it wrong.',
    }];
  }

  // Everything else stays silent — auto-from-scan, auto-ignored
  // awaiting review, truly unlabeled. The labeling-complete check
  // icon (rendered separately) covers attention-needed visibility.
  return [];
}
