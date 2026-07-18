/**
 * LibraryPageComponent — Phase 2 of the Library redesign (#500).
 *
 * Scope (intentional skeleton):
 *   - New `/library` route reaches this component. The old `/history`
 *     route stays as an alias redirected here so bookmarks don't break.
 *   - Renders a sticky header (title + search + tab filter) and a flat
 *     card list of releases / boxsets that pass the **completed-rips**
 *     filter — i.e. only releases the user has actually finished.
 *   - "Continue Workflow" affordance is **deliberately not present** —
 *     pending discs live on the Ripper carousel after #492. Library is
 *     done media only.
 *   - Each card is a placeholder. The full card design (inline-edit,
 *     overflow menu, disclosure to disc list) lands in Phase 3.
 *   - Drawer that surfaces title-level metadata + file_path lands in
 *     Phase 4.
 *
 * What this PHASE does NOT do:
 *   - No edit affordances yet.
 *   - No DiscDB chip.
 *   - No drawer.
 *   - No bulk select.
 *   - No virtualization.
 */
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  OnInit,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subject, debounceTime, takeUntil } from 'rxjs';

import {
  MetadataService,
  ReleaseSummary,
  DiscSummary,
  BoxsetSummary,
  BoxsetRecord,
  LibraryPageResponse,
} from '../../services/metadata.service';
import { LoggerService } from '../../services/logger.service';
import { LibraryReleaseCardComponent } from '../../components/library-release-card/library-release-card.component';
import { LibraryBoxsetCardComponent } from '../../components/library-boxset-card/library-boxset-card.component';
import { LibraryDiscDrawerComponent } from '../../components/library-disc-drawer/library-disc-drawer.component';

type LibraryTab = 'all' | 'movies' | 'series' | 'boxsets';

@Component({
  selector: 'app-library-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Default,
  imports: [CommonModule, FormsModule, LibraryReleaseCardComponent, LibraryBoxsetCardComponent, LibraryDiscDrawerComponent],
  templateUrl: './library-page.component.html',
  styleUrls: ['./library-page.component.scss'],
})
export class LibraryPageComponent implements OnInit, OnDestroy {
  private readonly metadataSvc = inject(MetadataService);
  private readonly logger = inject(LoggerService);
  private readonly destroy$ = new Subject<void>();
  private readonly searchInput$ = new Subject<string>();

  /** All loaded releases across pages (post-filter). */
  releases: ReleaseSummary[] = [];
  /** Discs grouped by release id, keyed for cheap lookup. */
  releaseDiscs: Record<string, DiscSummary[]> = {};
  boxsets: BoxsetSummary[] = [];
  boxsetDetails: BoxsetRecord[] = [];

  searchTerm = '';
  activeTab: LibraryTab = 'all';
  loading = false;
  error: string | null = null;
  /** Backend pagination cursor (next page). null = exhausted. */
  private nextCursor: string | null = null;
  hasMore = true;

  /** Counts for the tab pills (post-filter, not raw backend totals). */
  get tabCounts() {
    const all = this.releases.length;
    const movies = this.releases.filter(
      (r) => (r.type ?? 'movie').toLowerCase() === 'movie' && !r.boxset_id,
    ).length;
    const series = this.releases.filter((r) => {
      const t = (r.type ?? '').toLowerCase();
      return (t === 'series' || t === 'tv') && !r.boxset_id;
    }).length;
    const boxsets = this.boxsets.length;
    return { all, movies, series, boxsets };
  }

  /** Releases displayed in the current tab (boxset releases are nested
   * under their boxset card, never repeated as standalone). */
  get visibleReleases(): ReleaseSummary[] {
    return this.releases.filter((rel) => {
      if (rel.boxset_id) return false; // shown under boxset card
      if (this.activeTab === 'movies') {
        return (rel.type ?? 'movie').toLowerCase() === 'movie';
      }
      if (this.activeTab === 'series') {
        const t = (rel.type ?? '').toLowerCase();
        return t === 'series' || t === 'tv';
      }
      return this.activeTab !== 'boxsets';
    });
  }

  get visibleBoxsets(): BoxsetSummary[] {
    if (this.activeTab !== 'all' && this.activeTab !== 'boxsets') return [];
    return this.boxsets;
  }

  ngOnInit(): void {
    this.searchInput$
      .pipe(debounceTime(300), takeUntil(this.destroy$))
      .subscribe((term) => {
        this.searchTerm = term;
        this.resetAndLoad();
      });
    this.resetAndLoad();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  onSearchInput(term: string): void {
    this.searchInput$.next(term);
  }

  selectTab(tab: LibraryTab): void {
    if (this.activeTab === tab) return;
    this.activeTab = tab;
    // For Phase 2 we filter client-side off the loaded page; refetching is
    // unnecessary until backend tab-aware pagination matters. Tabs filter
    // the visible getter results from the already-loaded set.
  }

  loadMore(): void {
    if (this.loading || !this.hasMore) return;
    this.loadNextPage();
  }

  /**
   * Phase 2 filter: a release belongs in the Library iff the user has
   * **finished at least one disc's full workflow** for it — concretely:
   * any of its discs has `finalized=true` OR `transfer_state='completed'`.
   * Releases whose discs are still mid-pipeline (the 7 "stuck pending"
   * Midway/Joker/News-of-the-World cohort from earlier this session) are
   * filtered out; they belong on the Ripper carousel, not the Library.
   */
  private isReleaseCompleted(rel: ReleaseSummary): boolean {
    const discs = this.releaseDiscs[String(rel.id)] ?? [];
    if (discs.length === 0) return false;
    return discs.some(
      (d) => d.finalized === true || d.transfer_state === 'completed',
    );
  }

  private resetAndLoad(): void {
    this.releases = [];
    this.releaseDiscs = {};
    this.boxsets = [];
    this.boxsetDetails = [];
    this.nextCursor = null;
    this.hasMore = true;
    this.error = null;
    this.loadNextPage();
  }

  private loadNextPage(): void {
    this.loading = true;
    this.metadataSvc
      .getLibraryPage(
        this.nextCursor,
        20,
        this.searchTerm?.trim() || undefined,
        // Page 2 keeps the backend tab filter out of the picture so the
        // completed-rips filter happens client-side. Backend `tab=` may
        // come back if we ever push the filter server-side.
        undefined,
      )
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (page: LibraryPageResponse) => this.applyPage(page),
        error: (err: any) => {
          this.error =
            err?.error?.detail || err?.message || 'Failed to load library';
          this.loading = false;
          this.logger.error('[LibraryPage] getLibraryPage failed', err);
        },
      });
  }

  private applyPage(page: LibraryPageResponse): void {
    // Merge new disc data into the cumulative map BEFORE we filter, so the
    // completed-rips check has the full disc set available.
    for (const [relId, discs] of Object.entries(page.release_discs ?? {})) {
      this.releaseDiscs[relId] = discs;
    }
    const pageReleases = (page.items ?? []).filter((rel) =>
      this.isReleaseCompleted(rel),
    );
    this.releases = [...this.releases, ...pageReleases];

    // Boxsets: backend already filters to those referenced on this page;
    // we de-dupe across pages and keep the union.
    const seenBoxsetIds = new Set(this.boxsets.map((b) => b.id));
    for (const bs of page.boxsets ?? []) {
      if (!seenBoxsetIds.has(bs.id)) this.boxsets.push(bs);
    }
    const seenDetailIds = new Set(this.boxsetDetails.map((b) => b.id));
    for (const bd of page.boxset_details ?? []) {
      if (!seenDetailIds.has(bd.id)) this.boxsetDetails.push(bd);
    }

    this.nextCursor = page.next_cursor ?? null;
    this.hasMore = !!page.has_more && !!page.next_cursor;
    this.loading = false;
  }

  /** Phase 3 — wire updated/deleted callbacks from the new card components. */

  onReleaseUpdated(updated: ReleaseSummary): void {
    this.releases = this.releases.map((r) =>
      String(r.id) === String(updated.id) ? { ...r, ...updated } : r,
    );
  }

  onReleaseDeleted(deleted: ReleaseSummary): void {
    this.releases = this.releases.filter((r) => String(r.id) !== String(deleted.id));
    delete this.releaseDiscs[String(deleted.id)];
  }

  onBoxsetUpdated(updated: BoxsetSummary): void {
    this.boxsets = this.boxsets.map((b) => (b.id === updated.id ? { ...b, ...updated } : b));
  }

  onBoxsetDeleted(deleted: BoxsetSummary): void {
    this.boxsets = this.boxsets.filter((b) => b.id !== deleted.id);
    this.boxsetDetails = this.boxsetDetails.filter((b) => b.id !== deleted.id);
    // Releases that lived inside the boxset become standalone (backend
    // detaches them). We don't re-classify locally — the next page load
    // will reflect the change.
  }

  /** Drawer state. The drawer renders when `drawerDisc` is non-null. */
  drawerDisc: DiscSummary | null = null;
  drawerRelease: ReleaseSummary | null = null;

  /** Open the disc drawer when a card emits discOpen. Looks up the
   * enclosing release so the drawer's breadcrumb has context. */
  onDiscOpen(disc: DiscSummary): void {
    if (!disc) return;
    this.drawerDisc = disc;
    const relId = disc.release_id ? String(disc.release_id) : null;
    this.drawerRelease = relId
      ? (this.releases.find((r) => String(r.id) === relId) ?? null)
      : null;
  }

  closeDrawer(): void {
    this.drawerDisc = null;
    this.drawerRelease = null;
  }

  /** When the drawer saves disc-level edits, refresh our local DiscSummary
   * so the card meta (e.g. disc name) reflects the new state. */
  onDrawerDiscUpdated(record: any): void {
    const discId = record?.id;
    if (!discId) return;
    for (const [relId, discs] of Object.entries(this.releaseDiscs)) {
      const idx = discs.findIndex((d) => d.id === discId);
      if (idx >= 0) {
        this.releaseDiscs[relId] = discs.map((d, i) =>
          i === idx ? { ...d, disc_name: record.disc_name, format: record.format ?? d.format } : d,
        );
      }
    }
    // Keep drawer's local disc reference in sync.
    if (this.drawerDisc && this.drawerDisc.id === discId) {
      this.drawerDisc = {
        ...this.drawerDisc,
        disc_name: record.disc_name,
        format: record.format ?? this.drawerDisc.format,
      };
    }
  }

  /** Releases that belong to a boxset, in slug/name order, for nesting. */
  getReleasesForBoxset(boxsetId: string): ReleaseSummary[] {
    return this.releases
      .filter((r) => r.boxset_id === boxsetId)
      .sort((a, b) =>
        (a.name ?? '').localeCompare(b.name ?? '', undefined, { sensitivity: 'base' }),
      );
  }

  /** Helpers for templates — kept tiny because Phase 3 redesigns the cards. */

  getReleaseDiscs(rel: ReleaseSummary): DiscSummary[] {
    return this.releaseDiscs[String(rel.id)] ?? [];
  }

  getDiscCount(rel: ReleaseSummary): number {
    return this.getReleaseDiscs(rel).length;
  }

  getTitleCount(rel: ReleaseSummary): number {
    let count = 0;
    for (const d of this.getReleaseDiscs(rel)) {
      count += d.total_titles ?? d.title_count ?? d.titles?.length ?? 0;
    }
    return count;
  }

  /** "Wednesday (2022)" / "Midway (2019)" — release-card heading. */
  getReleaseDisplayName(rel: ReleaseSummary): string {
    const year = rel.production_year ?? rel.release_year;
    return year ? `${rel.name ?? '(untitled)'} (${year})` : rel.name ?? '(untitled)';
  }

  /** Boxset displays its own name + release count for now (Phase 3 expands). */
  getBoxsetDisplayName(bs: BoxsetSummary): string {
    const year = bs.year ? ` (${bs.year})` : '';
    return `${bs.name ?? '(untitled boxset)'}${year}`;
  }

  trackByReleaseId(_idx: number, rel: ReleaseSummary): string {
    return String(rel.id);
  }

  trackByBoxsetId(_idx: number, bs: BoxsetSummary): string {
    return String(bs.id);
  }
}
