/**
 * library-release-card — Phase 3 of the Library redesign (#500).
 *
 * One card per standalone release in the Library main pane. Three states:
 *
 *   view (default) — poster, name, year, cyan release badge, meta, action
 *                    buttons (Edit / Delete / Expand). Read-only browse.
 *   edit           — same poster, inline form (name / year / cover_front_url).
 *                    Save calls MetadataService.updateRelease(); Cancel reverts.
 *                    Per #379's framing: the noop()-handler shell is replaced
 *                    here by real inline-edit on a card the user can see.
 *   expanded       — disc list rendered below the card. Disc rows are minimal
 *                    (number + name + title count + finalize badge); the
 *                    drawer for title-level work lands in Phase 4.
 *
 * edit + expanded are independent toggles. Saving auto-closes edit but
 * leaves expanded as-is.
 */
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  MetadataService,
  ReleaseSummary,
  DiscSummary,
} from '../../services/metadata.service';
import { ToastService, formatHttpErrorDetail } from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';

interface EditableReleaseFields {
  release_name: string | null;
  release_year: number | null;
  cover_front_url: string | null;
}

@Component({
  selector: 'app-library-release-card',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './library-release-card.component.html',
  styleUrls: ['./library-release-card.component.scss'],
})
export class LibraryReleaseCardComponent {
  private readonly metadataSvc = inject(MetadataService);
  private readonly toast = inject(ToastService);
  private readonly logger = inject(LoggerService);

  @Input({ required: true }) release!: ReleaseSummary;
  @Input() discs: DiscSummary[] = [];
  /** When true, the boxset shell hides the type-icon "(in boxset)" hint
   * (the boxset card already provides that context). */
  @Input() nested = false;

  @Output() updated = new EventEmitter<ReleaseSummary>();
  @Output() deleted = new EventEmitter<ReleaseSummary>();
  /** Phase 4 will subscribe to this to open the disc drawer. */
  @Output() discOpen = new EventEmitter<DiscSummary>();

  expanded = false;
  editing = false;
  saving = false;
  saveError: string | null = null;

  /** Working copy of edited fields. Reset to the current release on every
   * `startEdit()`; written into the release + emitted on success. */
  editForm: EditableReleaseFields = {
    release_name: null,
    release_year: null,
    cover_front_url: null,
  };

  /** "Wednesday (2022)" — heading. */
  get displayName(): string {
    const year = this.release.production_year ?? this.release.release_year;
    return year
      ? `${this.release.name ?? '(untitled)'} (${year})`
      : (this.release.name ?? '(untitled)');
  }

  get isSeries(): boolean {
    const t = (this.release.type ?? 'movie').toLowerCase();
    return t === 'series' || t === 'tv';
  }

  /** #647: Library cards prefer TMDB movie/series poster over release-specific
   * box art. Fallback cascade keeps the release cover for edge cases where
   * the movie relation has no TMDB match (e.g. DiscDB miss with only manual
   * TMDB name entry). The Ripper release step still surfaces the release
   * cover directly so users can verify the box art they attached. */
  get posterUrl(): string | null {
    return (this.release as any).movie?.cover_url
      || this.release.cover_front_url
      || null;
  }

  get discCount(): number {
    return this.discs.length;
  }

  get titleCount(): number {
    let count = 0;
    for (const d of this.discs) {
      count += d.total_titles ?? d.titles?.length ?? 0;
    }
    return count;
  }

  /** Releases the backend has marked finalized can't be PATCHed; greys
   * the Edit button so users don't try and get an opaque 400. */
  get isFinalized(): boolean {
    const fs = (this.release as any).finalize_state;
    return fs === 'completed' || fs === 'finalized';
  }

  toggleExpanded(): void {
    this.expanded = !this.expanded;
  }

  startEdit(): void {
    if (this.isFinalized) return;
    this.editing = true;
    this.saveError = null;
    this.editForm = {
      release_name: this.release.name ?? null,
      release_year: this.release.production_year ?? this.release.release_year ?? null,
      cover_front_url: (this.release as any).cover_front_url ?? null,
    };
  }

  cancelEdit(): void {
    this.editing = false;
    this.saveError = null;
  }

  saveEdit(): void {
    if (this.saving) return;
    const idOrSlug = (this.release.id ?? this.release.slug)?.toString();
    if (!idOrSlug) {
      this.saveError = 'Release has no id — cannot save.';
      return;
    }
    // Build a delta payload so we don't send unchanged fields. The
    // backend's PATCH accepts None on every field; this just keeps the
    // wire payload tight.
    const payload: Record<string, unknown> = {};
    if ((this.editForm.release_name ?? '') !== (this.release.name ?? '')) {
      payload['release_name'] = this.editForm.release_name;
    }
    const currentYear = this.release.production_year ?? this.release.release_year;
    if ((this.editForm.release_year ?? null) !== (currentYear ?? null)) {
      payload['release_year'] = this.editForm.release_year;
    }
    const currentCover = (this.release as any).cover_front_url ?? null;
    if ((this.editForm.cover_front_url ?? null) !== currentCover) {
      payload['cover_front_url'] = this.editForm.cover_front_url;
    }
    if (Object.keys(payload).length === 0) {
      this.editing = false;
      return;
    }

    this.saving = true;
    this.saveError = null;
    this.metadataSvc.updateRelease(idOrSlug, payload).subscribe({
      next: (updated) => {
        this.saving = false;
        this.editing = false;
        this.updated.emit(updated);
      },
      error: (err) => {
        this.saving = false;
        this.saveError = formatHttpErrorDetail(err) || 'Save failed';
        this.logger.warn('[LibraryReleaseCard] updateRelease failed', err);
      },
    });
  }

  confirmDelete(): void {
    if (!confirm(`Delete release "${this.release.name}"? This cannot be undone.`)) return;
    const idOrSlug = (this.release.id ?? this.release.slug)?.toString();
    if (!idOrSlug) return;
    this.metadataSvc.deleteRelease(idOrSlug).subscribe({
      next: () => {
        this.toast.show(`Deleted release "${this.release.name}"`, 'success', 4000);
        this.deleted.emit(this.release);
      },
      error: (err) => {
        this.toast.show(formatHttpErrorDetail(err) || 'Delete failed', 'error', 5000);
      },
    });
  }

  /** Phase 4 hook — open the disc drawer. For now we just emit; Phase 4
   * components subscribe. */
  openDisc(disc: DiscSummary): void {
    this.discOpen.emit(disc);
  }

  trackByDiscId(_idx: number, d: DiscSummary): string {
    return d.id ?? d.content_hash;
  }

  /** Disc row label: "Disc N: Disc name" / "Disc N" / disc_name fallback. */
  discRowLabel(d: DiscSummary): string {
    const num = d.disc_number;
    const name = d.disc_name?.trim();
    if (num && name) return `Disc ${num}: ${name}`;
    if (num) return `Disc ${num}`;
    if (name) return name;
    return d.content_hash?.slice(0, 8) ?? '(disc)';
  }

  discRowMeta(d: DiscSummary): string {
    const titles = d.total_titles ?? d.titles?.length ?? 0;
    const completed = d.titles_completed;
    if (completed != null && completed !== titles) {
      return `${completed}/${titles} titles`;
    }
    return `${titles} title${titles === 1 ? '' : 's'}`;
  }

  discRowFinalized(d: DiscSummary): boolean {
    return d.finalized === true;
  }
}
