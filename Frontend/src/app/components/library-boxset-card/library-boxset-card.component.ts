/**
 * library-boxset-card — Phase 3 of the Library redesign (#500).
 *
 * Same three-state pattern as library-release-card (view / edit / expanded),
 * but the expanded body lists nested `library-release-card` instances (one
 * per release in the boxset). Inline-edit covers name / year / cover_url
 * via MetadataService.updateBoxset().
 *
 * Boxset accent is purple per the ui-lab tokens; nested release cards
 * automatically switch to their `is-nested` muted styling so the boxset
 * remains the dominant frame.
 */
import {
  ChangeDetectionStrategy,
  Component,
  HostListener,
  EventEmitter,
  Input,
  Output,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  MetadataService,
  BoxsetSummary,
  BoxsetRecord,
  BoxsetUpdate,
  ReleaseSummary,
  DiscSummary,
} from '../../services/metadata.service';
import { ToastService, formatHttpErrorDetail } from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';
import { LibraryReleaseCardComponent } from '../library-release-card/library-release-card.component';

interface EditableBoxsetFields {
  name: string | null;
  year: number | null;
  cover_front_url: string | null;
}

@Component({
  selector: 'app-library-boxset-card',
  standalone: true,
  imports: [CommonModule, FormsModule, LibraryReleaseCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './library-boxset-card.component.html',
  styleUrls: ['./library-boxset-card.component.scss'],
})
export class LibraryBoxsetCardComponent {
  private readonly metadataSvc = inject(MetadataService);
  private readonly toast = inject(ToastService);
  private readonly logger = inject(LoggerService);

  @Input({ required: true }) boxset!: BoxsetSummary;
  /** Releases that belong to this boxset (parent filters them out of the
   * standalone list). */
  @Input() releases: ReleaseSummary[] = [];
  /** Discs per release id, forwarded into the nested release cards. */
  @Input() releaseDiscs: Record<string, DiscSummary[]> = {};

  @Output() updated = new EventEmitter<BoxsetSummary>();
  @Output() deleted = new EventEmitter<BoxsetSummary>();
  @Output() releaseUpdated = new EventEmitter<ReleaseSummary>();
  @Output() releaseDeleted = new EventEmitter<ReleaseSummary>();
  @Output() discOpen = new EventEmitter<DiscSummary>();
  /** #741: scoped TheDiscDB export request, handled by the page (one poller). */
  @Output() exportDiscs = new EventEmitter<string[]>();
  @Input() eligibleDiscIds: ReadonlySet<string> = new Set();
  @Input() updateDiscIds: ReadonlySet<string> = new Set();

  expanded = false;
  editing = false;
  saving = false;
  saveError: string | null = null;

  editForm: EditableBoxsetFields = {
    name: null,
    year: null,
    cover_front_url: null,
  };

  get displayName(): string {
    const year = this.boxset.year;
    return year
      ? `${this.boxset.name ?? '(untitled boxset)'} (${year})`
      : (this.boxset.name ?? '(untitled boxset)');
  }

  get releaseCount(): number {
    return this.boxset.release_count ?? this.releases.length;
  }

  /** #649: A boxset is a *collection*, not a movie/series — showing an
   * individual title's poster would misrepresent what the card is.
   * Boxset cards render `boxset.cover_front_url` (user-supplied or
   * TMDB-collection-auto-populated) with a 📦 placeholder when null.
   * Release cards inside the boxset drawer still cascade via the
   * per-release poster path from #648. */
  get posterUrl(): string | null {
    return this.boxset.cover_front_url || null;
  }

  get discCount(): number {
    if (this.boxset.disc_count != null) return this.boxset.disc_count;
    let total = 0;
    for (const r of this.releases) {
      total += (this.releaseDiscs[String(r.id)] ?? []).length;
    }
    return total;
  }

  toggleExpanded(): void {
    this.expanded = !this.expanded;
  }

  menuOpen = false;

  toggleMenu(ev: Event): void {
    ev.stopPropagation();
    this.menuOpen = !this.menuOpen;
  }

  @HostListener('document:click')
  closeMenu(): void {
    this.menuOpen = false;
  }

  /** Discs across every member release that the export would include. */
  get exportableDiscIdList(): string[] {
    const ids: string[] = [];
    for (const rel of this.releases) {
      for (const d of this.releaseDiscs[String(rel.id)] ?? []) {
        if (d.id && this.eligibleDiscIds.has(String(d.id))) ids.push(String(d.id));
      }
    }
    return ids;
  }

  get exportableDiscCount(): number {
    return this.exportableDiscIdList.length;
  }

  private allDiscs(): DiscSummary[] {
    const out: DiscSummary[] = [];
    for (const rel of this.releases) out.push(...(this.releaseDiscs[String(rel.id)] ?? []));
    return out;
  }

  get readyCount(): number {
    return this.allDiscs().filter(
      d => d.id && this.eligibleDiscIds.has(String(d.id)) && !this.updateDiscIds.has(String(d.id)),
    ).length;
  }

  get changedCount(): number {
    return this.allDiscs().filter(d => d.id && this.updateDiscIds.has(String(d.id))).length;
  }

  get inDbCount(): number {
    return this.allDiscs().filter(
      d => d.discdb_hit === true || d.discdb_disc_num != null,
    ).length;
  }

  onMenuEdit(ev: Event): void {
    ev.stopPropagation();
    this.menuOpen = false;
    this.startEdit();
  }

  onMenuExport(ev: Event): void {
    ev.stopPropagation();
    this.menuOpen = false;
    const ids = this.exportableDiscIdList;
    if (ids.length) this.exportDiscs.emit(ids);
  }

  onMenuDelete(ev: Event): void {
    ev.stopPropagation();
    this.menuOpen = false;
    this.confirmDelete();
  }

  startEdit(): void {
    this.editing = true;
    this.saveError = null;
    this.editForm = {
      name: this.boxset.name ?? null,
      year: this.boxset.year ?? null,
      cover_front_url: this.boxset.cover_front_url ?? null,
    };
  }

  cancelEdit(): void {
    this.editing = false;
    this.saveError = null;
  }

  saveEdit(): void {
    if (this.saving) return;
    const id = this.boxset.id;
    if (!id) {
      this.saveError = 'Boxset has no id — cannot save.';
      return;
    }
    const payload: Partial<BoxsetUpdate> = {};
    if ((this.editForm.name ?? '') !== (this.boxset.name ?? '')) {
      payload.name = this.editForm.name ?? undefined;
    }
    if ((this.editForm.year ?? null) !== (this.boxset.year ?? null)) {
      payload.year = this.editForm.year ?? undefined;
    }
    if ((this.editForm.cover_front_url ?? null) !== (this.boxset.cover_front_url ?? null)) {
      payload.cover_front_url = this.editForm.cover_front_url ?? undefined;
    }
    if (Object.keys(payload).length === 0) {
      this.editing = false;
      return;
    }

    this.saving = true;
    this.saveError = null;
    this.metadataSvc.updateBoxset(id, payload as BoxsetUpdate).subscribe({
      next: (updated) => {
        this.saving = false;
        this.editing = false;
        this.updated.emit(updated);
      },
      error: (err) => {
        this.saving = false;
        this.saveError = formatHttpErrorDetail(err) || 'Save failed';
        this.logger.warn('[LibraryBoxsetCard] updateBoxset failed', err);
      },
    });
  }

  confirmDelete(): void {
    if (!confirm(`Delete boxset "${this.boxset.name}"? Releases inside it are NOT deleted — they become standalone. This cannot be undone.`)) return;
    const id = this.boxset.id;
    if (!id) return;
    this.metadataSvc.deleteBoxset(id).subscribe({
      next: () => {
        this.toast.show(`Deleted boxset "${this.boxset.name}"`, 'success', 4000);
        this.deleted.emit(this.boxset);
      },
      error: (err) => {
        this.toast.show(formatHttpErrorDetail(err) || 'Delete failed', 'error', 5000);
      },
    });
  }

  // Pass-throughs from nested release cards.
  onReleaseUpdated(r: ReleaseSummary): void { this.releaseUpdated.emit(r); }
  onReleaseDeleted(r: ReleaseSummary): void { this.releaseDeleted.emit(r); }
  onDiscOpen(d: DiscSummary): void { this.discOpen.emit(d); }

  getDiscsForRelease(rel: ReleaseSummary): DiscSummary[] {
    return this.releaseDiscs[String(rel.id)] ?? [];
  }

  trackByReleaseId(_idx: number, r: ReleaseSummary): string {
    return String(r.id);
  }
}
