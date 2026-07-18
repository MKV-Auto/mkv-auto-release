import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { BtnComponent } from '../../ui/btn/btn.component';
import { CardComponent } from '../../ui/card/card.component';
import { PillComponent } from '../../ui/pill/pill.component';
import { IconComponent } from '../../ui/icon/icon.component';

/**
 * Side-by-side comparison of every member of a dedupe group.
 *
 * Opened from the right-editor's DuplicateGroupPanel header. Lets the
 * user eyeball the differences (size, duration, chapters, audio,
 * resolution) and pick the best primary inline without leaving the
 * comparison view. Mirrors the prototype's CompareModal
 * (`research/MKV Auto UI/labeling.jsx:1201-1296`).
 *
 * Data shape comes from the workflow-context title payload — every
 * member already carries `display_size`, `duration`, `chapters`,
 * `metadata_summary` (audio_summary, quality_hints), and
 * `duplicate_info.metrics`. The modal is purely presentational;
 * Make-primary fires `select(candidate)` and the parent does the PATCH.
 */
@Component({
  selector: 'app-duplicate-compare-modal',
  standalone: true,
  imports: [CommonModule, CardComponent, BtnComponent, PillComponent, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './duplicate-compare-modal.component.html',
  styleUrls: ['./duplicate-compare-modal.component.scss'],
})
export class DuplicateCompareModalComponent {
  /** Every member of the dedupe group (current primary first). */
  @Input() members: any[] = [];
  /** The currently-loaded title in the parent editor — highlighted
   * in the modal so the user knows which row they came from. */
  @Input() currentTitleId: string | null = null;

  @Output() select = new EventEmitter<any>();
  @Output() dismiss = new EventEmitter<void>();

  trackByMemberId(_i: number, m: any): string {
    return m?.title_id ?? m?.source_file ?? String(_i);
  }

  isPrimary(m: any): boolean {
    return m?.active === true;
  }

  isCurrent(m: any): boolean {
    return m?.title_id === this.currentTitleId;
  }

  sourceLabel(m: any): string {
    const src = (m?.source_file || '').toString();
    if (!src) return m?.title || m?.title_id || '';
    const lastSlash = Math.max(src.lastIndexOf('/'), src.lastIndexOf('\\'));
    return lastSlash >= 0 ? src.slice(lastSlash + 1) : src;
  }

  formatDuration(seconds: number | null | undefined): string {
    if (!seconds) return '—';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m`;
    return `${Math.round(seconds)}s`;
  }

  formatSize(m: any): string {
    const display = (m?.display_size || '').toString().trim();
    if (display) return display;
    const bytes = Number(m?.size);
    if (!Number.isFinite(bytes) || bytes <= 0) return '—';
    const gb = bytes / (1024 ** 3);
    if (gb >= 1) return `${gb.toFixed(2)} GB`;
    const mb = bytes / (1024 ** 2);
    return `${mb.toFixed(0)} MB`;
  }

  chaptersCount(m: any): string {
    const ch = m?.chapters;
    if (Array.isArray(ch)) return String(ch.length);
    const fromMetrics = m?.duplicate_info?.metrics?.chapters_count;
    if (typeof fromMetrics === 'number') return String(fromMetrics);
    return '—';
  }

  audioSummary(m: any): string {
    const summary = m?.metadata_summary?.audio_summary;
    if (Array.isArray(summary) && summary.length) {
      return summary
        .map((a: any) => {
          const codec = (a?.codec_name || a?.codec || '').toUpperCase();
          const ch = a?.channels ? `${a.channels}.0` : '';
          const lang = a?.language ? ` ${a.language}` : '';
          return [codec, ch].filter(Boolean).join(' ') + lang;
        })
        .join(' · ');
    }
    return '—';
  }

  videoResolution(m: any): string {
    const hints = m?.metadata_summary?.quality_hints;
    if (Array.isArray(hints) && hints.length) {
      return hints
        .filter((h: string) => /^\d+p$|^4k$/i.test(h))
        .join(' ') || '—';
    }
    return '—';
  }

  onSelect(m: any): void {
    this.select.emit(m);
  }

  onBackdropClick(event: MouseEvent): void {
    if ((event.target as HTMLElement).classList.contains('dcm-backdrop')) {
      this.dismiss.emit();
    }
  }
}
