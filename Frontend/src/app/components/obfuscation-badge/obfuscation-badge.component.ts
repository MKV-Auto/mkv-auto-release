import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { IconComponent } from '../../ui/icon/icon.component';
import { IconName } from '../../ui/icon/icon-paths';
import { PillComponent, PillTone } from '../../ui/pill/pill.component';

export type ObfuscationReason =
  | 'segment_set_sibling'
  | 'path_a_decoy'
  | 'makemkv_msg3307'
  | 'duration_short'
  | 'low_bitrate_decoy'
  | 'play_all_wrapper'
  | null
  | undefined;

interface BadgeView {
  tone: PillTone;
  icon: IconName;
  label: string;
  tooltip: string;
}

const HIDDEN_VIEW: BadgeView = { tone: 'slate', icon: 'check', label: '', tooltip: '' };

/**
 * Tier-aware "Likely decoy / Decoy" badge — surfaces the
 * `disc_titles.obfuscation_reason` column in the labeling UI.
 *
 * Two tiers:
 * - HIGH (`segment_set_sibling` / `path_a_decoy` / `duration_short` /
 *   `low_bitrate_decoy`) → red **Decoy** pill. Sorted-segment-set
 *   membership, a Path A skip, a post-ffprobe duration mismatch
 *   (issue #374), or a bitrate that's implausible for the resolution
 *   (also #374, the post-rip remnant signal) are the strongest decoy
 *   signals we have — each is either relational or arithmetic ground
 *   truth.
 * - MEDIUM (`makemkv_msg3307`) → slate **Likely decoy** pill. MakeMKV's
 *   per-title MSG:3307 bit on its own — useful hint, but the source
 *   has known false positives on real bumpers, so we keep the visual
 *   softer than HIGH.
 *
 * Backward-compat: when `reason` is not supplied but `flagged=true`, we
 * render as MEDIUM (the pre-Phase-1 behavior) so callers that haven't
 * been updated to pass the new field keep showing the old slate badge.
 */
@Component({
  selector: 'app-obfuscation-badge',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, PillComponent, IconComponent],
  template: `
    <ui-pill
      *ngIf="view.label as label"
      [tone]="view.tone"
      [attr.title]="view.tooltip">
      <ui-icon uiPillIcon [name]="view.icon" [size]="11"></ui-icon>
      {{ label }}
    </ui-pill>
  `,
})
export class ObfuscationBadgeComponent {
  /** True iff the title's MSG:3307 flag bit 0x01000000 is set. Legacy
   * input — kept for backward compatibility. New callers should pass
   * `reason` instead. */
  @Input() flagged = false;

  /** Tier-aware decoy reason. Drives the visual tone + label + tooltip.
   * NULL/undefined renders nothing (no badge). */
  @Input() reason: ObfuscationReason = null;

  /** Optional specifics appended to the tooltip — e.g. which titles a
   * play-all wrapper runs ("Titles 8–13"). */
  @Input() detail: string | null = null;

  get view(): BadgeView {
    const base = this.baseView;
    if (this.detail && base.label) {
      return { ...base, tooltip: `${base.tooltip} ${this.detail}`.trim() };
    }
    return base;
  }

  private get baseView(): BadgeView {
    switch (this.reason) {
      case 'segment_set_sibling':
        return {
          tone: 'red',
          icon: 'info',
          label: 'Decoy',
          tooltip:
            'Permutation of the canonical playlist on this disc — same segments in a different order.',
        };
      case 'path_a_decoy':
        return {
          tone: 'red',
          icon: 'info',
          label: 'Decoy',
          tooltip:
            "Path A's segment-reorder workflow skipped this title because it isn't the canonical match.",
        };
      case 'makemkv_msg3307':
        return {
          tone: 'slate',
          icon: 'info',
          label: 'Likely decoy',
          tooltip:
            'MakeMKV flagged this title as part of the suspected playlist-obfuscation mass (MSG:3307).',
        };
      case 'duration_short':
        return {
          tone: 'red',
          icon: 'info',
          label: 'Decoy',
          tooltip:
            'The actual playable content is much longer than what MakeMKV declared — a short-declared / long-actual decoy pattern. The duration shown has been corrected from the post-rip ffprobe.',
        };
      case 'low_bitrate_decoy':
        return {
          tone: 'red',
          icon: 'info',
          label: 'Decoy',
          tooltip:
            'Bitrate is implausibly low for the declared resolution (e.g. 4K HEVC at ~1 Mbps when real UHD content is 30-100 Mbps). The rip looks like decoy / filler content masquerading as a full title.',
        };
      case 'play_all_wrapper':
        return {
          tone: 'amber',
          icon: 'info',
          label: 'Play All',
          tooltip:
            'This title runs the titles listed next to it back to back — its duration is exactly their sum. The parts are ripped individually, so this copy is skipped; give it a type if you want the single file instead.',
        };
    }
    // Legacy boolean fallback so pre-Phase-1 callers still render.
    if (this.flagged) {
      return {
        tone: 'slate',
        icon: 'info',
        label: 'Likely decoy',
        tooltip: 'MakeMKV flagged this title as part of the suspected playlist-obfuscation mass.',
      };
    }
    return HIDDEN_VIEW;
  }
}
