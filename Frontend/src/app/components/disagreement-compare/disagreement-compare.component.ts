import { Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { BtnComponent } from '../../ui/btn/btn.component';
import { CardComponent } from '../../ui/card/card.component';
import { IconComponent } from '../../ui/icon/icon.component';
import { PillComponent } from '../../ui/pill/pill.component';

/**
 * Side-by-side compare card surfaced when DiscDB and MakeMKV's obfuscation
 * flag pick different siblings of the same Path B dedupe group.
 *
 * Three actions: pick DiscDB candidate, pick MakeMKV candidate, or run Path A
 * segment-reorder to determine the canonical via deterministic matching.
 *
 * Inputs are the disc title payloads from the workflow-context; the parent
 * emits the user's pick back as a title id. Re-skinned against the new
 * design system primitives — outer ui-card + per-candidate ui-card, ui-pill
 * tones for source labels, ui-btn for actions.
 */
@Component({
  selector: 'app-disagreement-compare',
  standalone: true,
  imports: [CommonModule, CardComponent, PillComponent, BtnComponent, IconComponent],
  templateUrl: './disagreement-compare.component.html',
  styleUrls: ['./disagreement-compare.component.scss'],
})
export class DisagreementCompareComponent {
  /** Title payload for the DiscDB-classified candidate. */
  @Input() discdbCandidate: any = null;
  /** Title payload for the MakeMKV-flag-clear candidate. */
  @Input() makemkvCandidate: any = null;
  /** Number of additional same-group siblings hidden behind disclosure. */
  @Input() hiddenDecoyCount = 0;

  /** User picked the DiscDB candidate; emits the picked title_id. */
  @Output() pickDiscdb = new EventEmitter<string>();
  /** User picked the MakeMKV-flag-clear candidate. */
  @Output() pickMakemkv = new EventEmitter<string>();
  /** User wants to run Path A segment-reorder to disambiguate deterministically. */
  @Output() trySegmentReorder = new EventEmitter<void>();
  /** User asked to expand the hidden decoys list (handled by parent). */
  @Output() showDecoys = new EventEmitter<void>();

  formatDuration(seconds: number | string | null | undefined): string {
    if (typeof seconds === 'string') return seconds;
    if (!seconds) return '—';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}m ${s.toString().padStart(2, '0')}s`;
  }

  formatSize(bytes: number | null | undefined): string {
    if (!bytes) return '—';
    return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  }

  onPickDiscdb(): void {
    if (this.discdbCandidate?.title_id) {
      this.pickDiscdb.emit(String(this.discdbCandidate.title_id));
    }
  }

  onPickMakemkv(): void {
    if (this.makemkvCandidate?.title_id) {
      this.pickMakemkv.emit(String(this.makemkvCandidate.title_id));
    }
  }
}
