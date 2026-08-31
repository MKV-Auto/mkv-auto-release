import { ChangeDetectionStrategy, ChangeDetectorRef, Component, EventEmitter, Input, OnChanges, OnDestroy, OnInit, Output, SimpleChanges, ViewEncapsulation } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../environments/environment';
import { PreviewViewerComponent } from '../preview-viewer/preview-viewer.component';
import { MobileService } from '../../services/mobile.service';
import { ToastService } from '../../services/toast.service';
import { DedupeGroup, TitlePatchRequest } from '../../services/workflow.service';
import { ObfuscationBadgeComponent } from '../obfuscation-badge/obfuscation-badge.component';
import { PillComponent } from '../../ui/pill/pill.component';
import { MobileDrawerComponent } from '../mobile-drawer/mobile-drawer.component';
import { DuplicateGroupBadgeComponent } from '../duplicate-group-badge/duplicate-group-badge.component';
import { MetadataTagListComponent } from '../metadata-tag-list/metadata-tag-list.component';
import { DetectionBadgeComponent } from '../detection-badge/detection-badge.component';
import { TitleRowComponent, TitleRowStatus } from '../title-row/title-row.component';
import { TitleEditorComponent } from '../title-editor/title-editor.component';
import { PreviewLabelModalComponent } from '../preview-label-modal/preview-label-modal.component';
import { EmptyStateComponent } from '../../ui/empty-state/empty-state.component';
import { IconComponent } from '../../ui/icon/icon.component';
import { BtnComponent } from '../../ui/btn/btn.component';
import { Subject, takeUntil } from 'rxjs';
import { diffTagsForDisplay, getGroupColor, getGroupIdentifier } from '../../utils/duplicate-tags.util';
import { sortTitlesForDisplay } from '../../utils/title-display-sort.util';
import {
  componentClipCount,
  computeDuplicateGroupsFromTitles,
  getDiscTitleId,
  getPrimaryTitleForEntity,
  isComponentClip,
  parseDuplicateInfo,
  realSiblingCount,
} from '../../utils/title-label-entities.util';
import {
  BACKDROP_TITLE_NAME,
  isBackdropTitleType,
  TITLE_TYPE_SELECT_OPTIONS,
} from '../../constants/title-type-options';

/** Title labeling step (template: TitlesStep.tsx / TitleCard.tsx). Duplicate group UI when backend provides title duplicate_info. */
@Component({
  selector: 'app-title-label',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    PreviewViewerComponent,
    MobileDrawerComponent,
    DuplicateGroupBadgeComponent,
    MetadataTagListComponent,
    DetectionBadgeComponent,
    TitleRowComponent,
    TitleEditorComponent,
    EmptyStateComponent,
    IconComponent,
    ObfuscationBadgeComponent,
    PillComponent,
    BtnComponent,
    PreviewLabelModalComponent,
  ],
  templateUrl: './title-label.component.html',
  styleUrls: ['./title-label.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
})
export class TitleLabelComponent implements OnChanges, OnInit, OnDestroy {
  /** Options for type <select> (order: Main Movie, Episode first; Ignore last). */
  readonly titleTypeOptions = TITLE_TYPE_SELECT_OPTIONS;

  @Input() titles: any[] = [];
  /** Disc these titles belong to. The parent always knows this; reaching into
   * `titles[0].disc_id` for it is what let per-title endpoints break silently
   * when the payload omitted the field (mkv-auto-release#8). */
  @Input() discId: string | null = null;
  @Input() isSeries = false;
  @Input() titleProgress: Record<string, number> = {};
  @Input() titleStatusFn: (id: string | null | undefined) => string = () => 'pending';
  @Input() titleProgressValueFn: (id: string | null | undefined) => number = () => 0;
  @Input() titleActiveFn: (id: string | null | undefined) => boolean = () => false;
  @Input() showTitleStatus = true;
  @Input() previewUrlFn: (t: any) => string | null = () => null;
  @Input() titlePathFn: (t: any) => string | null = () => null;
  @Input() previewStateFn: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null = () => null;
  @Input() retryPreviewFn: (t: any) => void = () => {};
  @Input() labelSaving = false;
  @Input() lastAutosaveOk = true;
  @Input() devMode = false;
  /**
   * Path B sorted-segment-set dedupe groups. When present, sibling rows hide
   * behind the representative — click "+N grouped duplicates" on a representative
   * to expand them inline. Empty list = no Path B groups (the typical case).
   */
  @Input() dedupeGroups: DedupeGroup[] = [];
  /**
   * Path A: the disc_titles index that segment-reorder identified as the
   * canonical playlist. When set, the row whose `index` equals this value
   * is badged "User Selected" and its MakeMKV obfuscation hint is hidden
   * (Path A's confirmation overrides the scan-time flag).
   */
  @Input() matchedCanonicalIndex: number | null = null;
  /** Forwarded to each title-row's chip system. Determines whether
   * `auto_type` renders as "DiscDB" (true) or "Pending Review"
   * (false — auto came from scan defaults / dedupe consensus /
   * padding-detect / etc, not from DiscDB). */
  @Input() discdbHit: boolean = false;
  @Output() labelChanged = new EventEmitter<any[]>();
  @Output() labelBlur = new EventEmitter<void>();
  @Output() titlePatched = new EventEmitter<TitlePatchRequest>();
  /** Multiple title patches in one HTTP round-trip (duplicate groups). */
  @Output() titleBatchPatched = new EventEmitter<TitlePatchRequest[]>();
  @Output() primaryChanged = new EventEmitter<{ discId: string; titleId: string }>();
  /** Fired after a successful Ungroup/Re-group so the parent can refresh the
   * workflow context. Unlike set-primary, this changes GROUP SHAPE — a row
   * leaves `dedupeGroups[].sibling_title_ids` and the group may elect a new
   * representative — and the left rail renders straight off those groups.
   * Without the refresh the flag flips, the server regroups, and the rail
   * keeps showing the old collapse until the user navigates away and back. */
  @Output() ungrouped = new EventEmitter<{ discId: string; titleId: string }>();

  private focusDepth = 0;
  isActive = false;
  
  // Mobile state
  isMobile: boolean = false;
  /** #701: title whose preview clip is open in the mobile quick-preview overlay. */
  quickPreviewTitle: any | null = null;
  expandedTitleId: string | null = null;
  openDrawerTitleId: string | null = null;
  /** Desktop list+editor split: which title is loaded into the side-panel
   * editor. Auto-selects the first non-ignored title when titles arrive so the
   * editor is never empty if there's something to edit. */
  selectedTitleId: string | null = null;
  /** Title ID briefly emphasized after navigate (e.g. from duplicate group "same as" link). */
  emphasizedTitleId: string | null = null;
  private emphasizedTimeout: ReturnType<typeof setTimeout> | null = null;
  /** Per-title preview aspect ratio from video metadata (e.g. "1920/1080") so container matches video. */
  aspectRatioMap: Record<string, string> = {};
  /** Optimistic set of title IDs whose preview retry has been clicked but not yet confirmed by backend. */
  private retryingPreviews = new Set<string>();
  private destroy$ = new Subject<void>();
  private lastEditedTitleKey: string | null = null;
  private lastEditedValueLength: number | null = null;
  /** Display order: title_id list. Preserved across edits; rebuilt on load / ID set change; ignore repartition only. */
  private displayOrderIds: string[] = [];
  /** Sorted title_id list for change detection (same multiset => preserve display order). */
  private lastTitleIdSnapshot: string[] = [];

  /** Pre-computed duplicate groups. Updated in ngOnChanges / recomputeDerivedState(). */
  duplicateGroups: { groupId: string; titles: any[] }[] = [];
  /** Pre-computed single (non-duplicate) titles in display order. Updated in ngOnChanges / recomputeDerivedState(). */
  singleTitles: any[] = [];
  /** Interleaved rows: duplicate group card or single title card in display order (desktop + mobile). */
  titleListRows: Array<
    | { kind: 'group'; group: { groupId: string; titles: any[] } }
    | { kind: 'single'; title: any }
  > = [];

  /** Desktop: which duplicate groups are expanded (default: expanded when first seen). */
  expandedGroups = new Set<string>();
  /** Group IDs we have already defaulted to expanded — avoids re-expanding on every recompute. */
  private seenDuplicateGroupIds = new Set<string>();
  /** User explicitly collapsed a duplicate group; do not auto re-expand on recompute. */
  private userCollapsedDuplicateGroupIds = new Set<string>();
  /** Mobile: the duplicate group currently open in the drawer (null = closed). */
  duplicateDrawerGroup: { groupId: string; titles: any[] } | null = null;

  get showSpinner(): boolean {
    return this.labelSaving || this.isActive;
  }
  
  constructor(
    private mobileService: MobileService,
    private cdr: ChangeDetectorRef,
    private http: HttpClient,
    private toast: ToastService,
  ) {}

  // ── Duplicate-group editor outputs (Make-primary / Ungroup) ─────────────

  /** Handler for the editor's "Make primary" button on a sibling row.
   * Reuses the existing optimistic primary-swap + primaryChanged emit
   * path so the parent's POST /discs/{id}/titles/{title_id}/set-primary
   * gets called the same way the legacy click would. */
  onMakeSiblingPrimary(sibling: any): void {
    if (!sibling) return;
    this.handleSetPrimary(sibling);
    // Re-target the editor to the new primary so the user sees its row
    // become "Editing + Primary" in the panel without another click.
    const newId = this.getTitleId(sibling);
    if (newId) {
      this.selectedTitleId = newId;
    }
  }

  /** Editor patches pass through here so the rail — which sees every title
   * on the disc — can notice when two titles resolve to ONE episode.
   *
   * A disc that files a feature-length special as "Part 1" / "Part 2" maps
   * both to the same TMDB entry (Rebels S00E02 "The Siege of Lothal", #830).
   * Plex and Jellyfin stack such files into one episode only if they carry
   * part numbers; without them two files claim the same s00e02 and one is
   * dropped. So when a patch lands a title on a (season, episode) another
   * title already holds, and no one in the set has chosen parts yet, assign
   * Part 1..N in disc order and tell the user. Hand-set parts are left alone. */
  onEditorPatch(patch: TitlePatchRequest): void {
    const companions = this.multiPartCompanions(patch);
    if (companions.length) {
      this.titleBatchPatched.emit([patch, ...companions]);
    } else {
      this.titlePatched.emit(patch);
    }
  }

  /** True when the row's effective type is Episode — the only type the
   * multi-part stacking rationale applies to. Episode-scoped EXTRAS
   * legitimately carry the same (season, episode) as their episode (that's
   * how Plex attaches them), and must never be pulled into a part group:
   * on Rebels S2 every "Rebels Recon" short sharing its episode's number
   * was getting stamped part 2 of 2 ("Split across files"). */
  private isEpisodeTyped(t: any): boolean {
    return String(t?.type || '').trim().toLowerCase() === 'episode';
  }

  private multiPartCompanions(patch: TitlePatchRequest): TitlePatchRequest[] {
    const season = patch.season;
    const episode = patch.episode;
    if (season == null || episode == null) return [];
    const me = (this.titles || []).find((t) => this.getTitleId(t) === patch.title_id);
    if (!me) return [];
    // Parts are for one EPISODE split across files. The patched row and
    // every companion must be Episode-typed; use the patch's type when the
    // edit is changing it in the same stroke.
    const meType = patch.type != null ? patch.type : me.type;
    if (String(meType || '').trim().toLowerCase() !== 'episode') return [];
    const sameEpisode = (this.titles || []).filter((t) =>
      this.getTitleId(t) !== patch.title_id &&
      !this.isIgnored(t) &&
      this.isEpisodeTyped(t) &&
      Number(t.season) === Number(season) &&
      Number(t.episode) === Number(episode)
    );
    if (!sameEpisode.length) return [];
    const group = [me, ...sameEpisode];
    // Someone already decided parts (or a range) — not ours to rewrite.
    if (group.some((t) => t.part != null || t.part_of != null || t.episode_end != null)) return [];

    const ord = (t: any) => (t.order_index ?? t.index ?? 0);
    group.sort((a, b) => ord(a) - ord(b));
    const n = group.length;
    const out: TitlePatchRequest[] = [];
    group.forEach((t, i) => {
      const part = i + 1;
      t.part = part;
      t.part_of = n;
      if (this.getTitleId(t) === patch.title_id) {
        patch.part = part;
        patch.part_of = n;
      } else {
        out.push({ title_id: this.getTitleId(t) as string, part, part_of: n });
      }
    });
    this.toast.show(
      `${n} titles on this disc are the same episode — set as Part 1 to Part ${n} so they stack as one.`,
      'info',
    );
    this.cdr.markForCheck();
    return out;
  }

  /** Handler for the editor's "Ungroup" / "Re-group" toggle. Fires the
   * backend endpoint directly (no parent indirection — this is a
   * self-contained per-title flag flip; the workflow-context refresh
   * picks up the new force_independent_group value on next fetch). */
  /** Disc id for per-title endpoints: the input the parent supplies, falling
   * back to whatever the payload carries. Never silently empty — callers
   * report instead of doing nothing. */
  private resolveDiscId(t: any): string | null {
    return this.discId || t?.disc_id || (this.titles?.[0] as any)?.disc_id || null;
  }

  onUngroupDuplicate(): void {
    const t = this.selectedTitle;
    if (!t) return;
    const discId = this.resolveDiscId(t);
    const titleId = this.getTitleId(t);
    if (!discId || !titleId) {
      // Previously a bare `return` — the button looked dead and left no trace
      // in the console or the server logs, which is what made this take a
      // support bundle to diagnose.
      console.error('[title-label] cannot ungroup: missing identity', { discId, titleId });
      this.toast.show('Could not ungroup this title — please report this.', 'error');
      return;
    }
    // Optimistic: flip the flag locally so the right-editor's button
    // label swaps Ungroup ↔ Re-group immediately. Server state catches
    // up on the next workflow-context refresh.
    t.force_independent_group = !t.force_independent_group;
    this.cdr.markForCheck();
    this.http.post<{ title_id: string; force_independent_group: boolean }>(
      `${environment.apiBase}/discs/${discId}/titles/${titleId}/ungroup-duplicate`,
      {},
    ).subscribe({
      next: (resp) => {
        // Sync with server's authoritative value in case the optimistic
        // flip raced a concurrent toggle elsewhere.
        t.force_independent_group = resp.force_independent_group;
        this.cdr.markForCheck();
        // The local flip only re-labels the button. Group membership is
        // recomputed server-side, so the rail needs the fresh context.
        this.ungrouped.emit({ discId, titleId });
      },
      error: () => {
        // Rollback the optimistic flip — the workflow-context will
        // also refresh and correct anyway, but this avoids a flash.
        t.force_independent_group = !t.force_independent_group;
        this.cdr.markForCheck();
      },
    });
  }
  
  ngOnInit(): void {
    // Subscribe to mobile service
    this.mobileService.isMobile$.pipe(
      takeUntil(this.destroy$)
    ).subscribe(isMobile => {
      this.isMobile = isMobile;
      this.cdr.markForCheck();
    });
  }


  
  ngOnDestroy(): void {
    // Last line of defence: navigating away mid-edit must not strand a
    // buffered typed field unsaved.
    this.flushPendingFieldEdits();
    if (this.emphasizedTimeout) {
      clearTimeout(this.emphasizedTimeout);
    }
    this.destroy$.next();
    this.destroy$.complete();
  }
  
  isTitleActive(title: any): boolean {
    return this.titleActiveFn(this.getTitleId(title));
  }
  
  isExpanded(title: any): boolean {
    return this.expandedTitleId === this.getTitleId(title);
  }
  
  toggleExpand(titleId: string | null | undefined): void {
    const id = titleId || null;
    if (this.expandedTitleId === id) {
      this.expandedTitleId = null;
    } else {
      this.expandedTitleId = id;
    }
  }

  isDrawerOpen(title: any): boolean {
    return this.openDrawerTitleId === this.getTitleId(title);
  }

  openDrawer(title: any): void {
    const id = this.getTitleId(title);
    this.openDrawerTitleId = id;
    this.cdr.markForCheck();
  }

  closeDrawer(): void {
    this.openDrawerTitleId = null;
    this.cdr.markForCheck();
  }

  /** Scroll to a title card by id and briefly emphasize it; on mobile close current drawer, scroll, then open target drawer. */
  handleNavigateToTitle(titleId: string): void {
    if (this.isMobile && this.openDrawerTitleId) {
      this.closeDrawer();
    }
    if (this.emphasizedTimeout) {
      clearTimeout(this.emphasizedTimeout);
      this.emphasizedTimeout = null;
    }
    this.emphasizedTitleId = titleId;
    this.cdr.markForCheck();
    this.emphasizedTimeout = setTimeout(() => {
      this.emphasizedTitleId = null;
      this.emphasizedTimeout = null;
      this.cdr.markForCheck();
    }, 2500);
    // Use data-title-id so we find the correct card when multiple cards share the same title_id (duplicate entries)
    const el = document.querySelector('[data-title-id="' + CSS.escape(titleId) + '"]') as HTMLElement | null;
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });

      // On mobile, open the target drawer after scroll completes
      if (this.isMobile) {
        setTimeout(() => {
          this.openDrawerTitleId = titleId;
          this.cdr.markForCheck();
        }, 800); // Wait for smooth scroll to complete (typically 500-800ms)
      }
    }
  }

  closeDrawerWithoutSave(): void {
    this.closeDrawer();
  }

  saveDrawerAndClose(): void {
    this.onBlur();
    this.closeDrawer();
  }

  getDrawerTitle(title: any): string {
    return title?.title || 'Edit Title';
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['titles']) {
      const titles = this.titles || [];
      if (titles.length === 0) {
        this.displayOrderIds = [];
        this.lastTitleIdSnapshot = [];
      } else {
        const nextSnap = this.sortedTitleIdsSnapshot(titles);
        if (this.sameSortedIds(this.lastTitleIdSnapshot, nextSnap)) {
          this.mergeDisplayOrderIdsWithTitles(titles);
        } else {
          const ordered = sortTitlesForDisplay(titles, (id) => this.titleActiveFn(id));
          this.displayOrderIds = ordered.map((t) => this.getTitleId(t) ?? '').filter(Boolean);
          this.lastTitleIdSnapshot = nextSnap;
        }
      }
      this.recomputeDerivedState();
      if (this.lastEditedTitleKey) {
        const updated = titles.find((t) => this.getTitleId(t) === this.lastEditedTitleKey);
        const updatedLength = (updated?.title ?? '').toString().length;
      }
    }
    // Grouping is an input in its own right. `titleListRows` bakes in
    // `isDedupeSibling` at compute time, so a dedupeGroups-only change — which
    // is exactly what Ungroup produces — left the rail showing the old
    // collapse until something happened to touch `titles`
    // (mkv-auto-release#8). Guarded so a combined change recomputes once.
    else if (changes['dedupeGroups']) {
      this.recomputeDerivedState();
    }
  }

  private sortedTitleIdsSnapshot(titles: any[]): string[] {
    const ids = titles.map((t) => this.getTitleId(t)).filter((id): id is string => !!id);
    return [...ids].sort((a, b) => a.localeCompare(b));
  }

  private sameSortedIds(a: string[], b: string[]): boolean {
    if (a.length !== b.length) return false;
    return a.every((v, i) => v === b[i]);
  }

  /** Keep display order for existing IDs; drop removed; append new at end. */
  private mergeDisplayOrderIdsWithTitles(titles: any[]): void {
    const nextSet = new Set<string>();
    for (const t of titles) {
      const id = this.getTitleId(t);
      if (id) nextSet.add(id);
    }
    const next: string[] = [];
    const seen = new Set<string>();
    for (const id of this.displayOrderIds) {
      if (nextSet.has(id) && !seen.has(id)) {
        next.push(id);
        seen.add(id);
      }
    }
    for (const t of titles) {
      const id = this.getTitleId(t) ?? '';
      if (id && !seen.has(id)) {
        next.push(id);
        seen.add(id);
      }
    }
    this.displayOrderIds = next;
  }

  /** Non-ignored titles first, then ignored; preserve relative order within each block. */
  private repartitionIgnoredToBottom(): void {
    const titles = this.titles || [];
    const byId = new Map<string, any>();
    for (const t of titles) {
      const id = this.getTitleId(t);
      if (id) byId.set(id, t);
    }
    const non: string[] = [];
    const ign: string[] = [];
    const placed = new Set<string>();
    const placeId = (id: string) => {
      const t = byId.get(id);
      if (!t || placed.has(id)) return;
      placed.add(id);
      if (this.isIgnored(t)) ign.push(id);
      else non.push(id);
    };
    for (const id of this.displayOrderIds) {
      placeId(id);
    }
    for (const t of titles) {
      placeId(this.getTitleId(t) ?? '');
    }
    this.displayOrderIds = [...non, ...ign];
    this.cdr.markForCheck();
  }

  /** Path B sorted-segment-set dedupe — track which group disclosures are open. */
  expandedDedupeGroups = new Set<string>();

  /** Set of titleIds that are dedupe siblings (collapse behind their representative). */
  private get _dedupeSiblingIds(): Set<string> {
    const set = new Set<string>();
    for (const g of (this.dedupeGroups || [])) {
      for (const sid of (g.sibling_title_ids || [])) set.add(sid);
    }
    return set;
  }

  /** Map representative_title_id → DedupeGroup for fast lookup in the template. */
  private get _dedupeRepMap(): Map<string, DedupeGroup> {
    const m = new Map<string, DedupeGroup>();
    for (const g of (this.dedupeGroups || [])) {
      if (g.representative_title_id) m.set(g.representative_title_id, g);
    }
    return m;
  }

  /** True if this titleId is a hidden sibling (filtered from the visible row list). */
  isDedupeSibling(titleId: string | null | undefined): boolean {
    return !!titleId && this._dedupeSiblingIds.has(titleId);
  }

  /** Clips inside a play-all wrapper that the USER has claimed (#797).
   *
   * A `.mpls` can wrap several `.m2ts` clips; subsumption folds them into the
   * wrapper's dedupe group so they collapse out of the rail. That is right for
   * clips nobody cares about, and wrong for one the user has labelled — the
   * backend now keeps those `active`, so hiding them meant a title that WILL
   * be ripped was invisible.
   *
   * A non-ignore `user_type` is the claim signal, matching
   * `user_claimed_row()` in core/duplicate_group_sync.py. Presentation only:
   * these are ordinary rows that render indented under their parent, and
   * selecting one opens it in the editor like any other title.
   */
  claimedClipsOf(title: any): any[] {
    const parentId = this.getTitleId(title);
    if (!parentId) return [];
    return (this.titles || []).filter((t) => {
      const sub = (t as { subsumed_by_title_id?: string | null }).subsumed_by_title_id;
      if (!sub || sub !== parentId) return false;
      const ut = ((t as { user_type?: string | null }).user_type || '').toString().toLowerCase();
      return !!ut && ut !== 'ignore';
    });
  }

  /** True when this row is a claimed clip rendered under its parent — used to
   * keep it from ALSO appearing as a top-level row. */
  isClaimedClip(title: any): boolean {
    const sub = (title as { subsumed_by_title_id?: string | null })?.subsumed_by_title_id;
    if (!sub) return false;
    const ut = ((title as { user_type?: string | null }).user_type || '').toString().toLowerCase();
    return !!ut && ut !== 'ignore';
  }

  /** True when a wrapper stepped aside because its clips are claimed — the
   * backend auto-ignores it so the same footage isn't ripped twice. */
  isSupersededWrapper(title: any): boolean {
    if (!this.claimedClipsOf(title).length) return false;
    const auto = ((title as { auto_type?: string | null })?.auto_type || '').toString().toLowerCase();
    const user = ((title as { user_type?: string | null })?.user_type || '').toString().toLowerCase();
    return auto === 'ignore' && !user;
  }

  /** "Titles 8–13" for a play-all wrapper the backend detected by duration
   * arithmetic (DVD; `play_all_of` on the context title, #831). */
  playAllDetail(title: any): string | null {
    const parts = title?.play_all_of;
    if (!Array.isArray(parts) || parts.length === 0) return null;
    const nums = parts.map((n: unknown) => Number(n)).filter((n: number) => Number.isFinite(n));
    if (nums.length === 0) return null;
    const sorted = [...nums].sort((a, b) => a - b);
    const contiguous = sorted.every((n, i) => i === 0 || n === sorted[i - 1] + 1);
    return contiguous && sorted.length > 1
      ? `Titles ${sorted[0]}–${sorted[sorted.length - 1]}.`
      : `Titles ${sorted.join(', ')}.`;
  }

  /** True when this title is the canonical playlist Path A identified. */
  isMatchedCanonical(title: any): boolean {
    if (this.matchedCanonicalIndex == null || !title) return false;
    const idx = (title as { index?: number | null }).index;
    return typeof idx === 'number' && idx === this.matchedCanonicalIndex;
  }

  /** Default false — hide rows marked type='ignore' from the left rail and
   * surface the count behind a Show-ignored toggle (prototype pattern). */
  showIgnored = false;

  /** Number of content rows currently *user*-ignored. Used for the
   * Show ignored (N) toggle label. Excludes dedupe siblings + auto-
   * ignored-awaiting-review rows (those stay visible by default; only
   * user-confirmed ignores hide). */
  get ignoredCount(): number {
    let count = 0;
    for (const t of this.titles || []) {
      if (!this.isUserIgnored(t)) continue;
      if (this.isDedupeSibling(this.getTitleId(t))) continue;
      count += 1;
    }
    return count;
  }

  /** Toggle Show ignored on/off. Bound to the ghost button in the template. */
  toggleShowIgnored(): void {
    this.showIgnored = !this.showIgnored;
    this.cdr.markForCheck();
  }

  /** Returns the DedupeGroup this titleId represents, or null. */
  getDedupeGroupForRepresentative(titleId: string | null | undefined): DedupeGroup | null {
    if (!titleId) return null;
    return this._dedupeRepMap.get(titleId) ?? null;
  }

  isDedupeGroupExpanded(groupId: string): boolean {
    return this.expandedDedupeGroups.has(groupId);
  }

  toggleDedupeGroup(groupId: string): void {
    if (this.expandedDedupeGroups.has(groupId)) {
      this.expandedDedupeGroups.delete(groupId);
    } else {
      this.expandedDedupeGroups.add(groupId);
    }
    this.cdr.markForCheck();
  }

  /** Sibling title objects for a group, in original display order. */
  getDedupeSiblingTitles(group: DedupeGroup): any[] {
    const ids = new Set(group.sibling_title_ids || []);
    return (this.titles || []).filter(t => ids.has(this.getTitleId(t) ?? ''));
  }

  /** Titles in display order. Uses displayOrderIds (preserved across benign input updates). */
  getDisplayOrderedTitles(): any[] {
    const titles = this.titles || [];
    if (titles.length === 0) return [];
    const byId = new Map<string, any>();
    for (const t of titles) {
      const id = this.getTitleId(t);
      if (id) byId.set(id, t);
    }
    const ordered: any[] = [];
    for (const id of this.displayOrderIds) {
      const t = byId.get(id);
      if (t) ordered.push(t);
    }
    for (const t of titles) {
      const id = this.getTitleId(t) ?? '';
      if (id && !this.displayOrderIds.includes(id)) ordered.push(t);
    }
    return ordered;
  }

  /** Blur: flush buffered typed-field edits, then persist visual order. */
  onBlur(): void {
    this.flushPendingFieldEdits();
    const ordered = this.getDisplayOrderedTitles();
    this.displayOrderIds = ordered.map((t) => this.getTitleId(t) ?? '').filter(Boolean);
    this.recomputeDerivedState();
    this.labelChanged.emit(ordered);
    this.labelBlur.emit();
  }


  onTitleChange(title: any, value: any): void {
    if (!title) return;
    this.lastEditedTitleKey = this.getTitleId(title);
    this.lastEditedValueLength = (value ?? '').toString().length;
    const normalizedTitle = value === '' ? null : value;
    const members = this.getDuplicateGroupMembers(title);
    if (members.length > 1) {
      for (const m of members) {
        m.title = value;
        this.bufferFieldEdit(m, { title: normalizedTitle });
      }
    } else {
      title.title = value;
      this.bufferFieldEdit(title, { title: normalizedTitle });
    }
    this.labelChanged.emit(this.titles);
  }

  updateDescription(title: any, value: any): void {
    if (!title) return;
    const normalizedDescription = value === '' ? null : value;
    const members = this.getDuplicateGroupMembers(title);
    if (members.length > 1) {
      for (const m of members) {
        m.description = value;
        m.note = value;
      }
      for (const m of members) this.bufferFieldEdit(m, { description: normalizedDescription });
    } else {
      title.description = value;
      title.note = value;
      this.bufferFieldEdit(title, { description: normalizedDescription });
    }
    this.labelChanged.emit(this.titles);
  }

  /** Backdrop's name is fixed to "Backdrop" — the name field locks. */
  isBackdropLockedTitle(title: any): boolean {
    return isBackdropTitleType(title?.type);
  }

  onTypeChange(title: any, value: any): void {
    // Type is picked, not typed, so it saves immediately. A name the user
    // typed just before may still be buffered — it rides in THIS write
    // rather than flushing as a separate one (two same-tick writes to one
    // row carry the same base_seq, so one of them always loses). Users
    // don't wait for autosave; the next action carries the pending edit.
    if (!title) return;
    const members = this.getDuplicateGroupMembers(title);
    const primary = this.getPrimaryTitle(members) || title;
    const wasPrimaryIgnore = this.isIgnored(primary);
    const normalizedType = value === '' ? null : value;
    const gid =
      members.length > 1 ? (this.getDuplicateInfo(primary)?.groupId as string | undefined) : undefined;

    if (members.length > 1) {
      const prevTypes = new Map(members.map((m) => [this.getTitleId(m), m.type]));
      for (const m of members) {
        m.type = value;
        if (this.isIgnored(m)) {
          this.clearIgnoredFieldsInMemory(m);
        }
      }
      const patches: TitlePatchRequest[] = members.map((m) => {
        const id = this.getTitleId(m);
        const pending = this.takePendingFieldsFor(m);
        const p: TitlePatchRequest = { ...pending, title_id: id!, type: normalizedType };
        if (this.isIgnored(m)) {
          // Nulls intentionally override pending text: ignore clears.
          p.title = null;
          p.description = null;
          p.season = null;
          p.episode = null;
          p.edition = null;
        } else if (isBackdropTitleType(value)) {
          // Backdrop's name is fixed — overrides any pending typed name.
          m.title = BACKDROP_TITLE_NAME;
          p.title = BACKDROP_TITLE_NAME;
        } else if (isBackdropTitleType(prevTypes.get(id)) && m.title === BACKDROP_TITLE_NAME) {
          // Leaving Backdrop: the forced name wasn't user data.
          m.title = '';
          p.title = null;
        }
        return p;
      });
      this.flushPendingFieldEdits(); // unrelated rows' leftovers, if any
      this.emitBatchOrSingle(patches);
      if (gid) {
        if (this.isDuplicateGroupIgnoredFromTitles(members)) {
          this.expandedGroups.delete(gid);
        } else if (wasPrimaryIgnore && !this.isIgnored(primary)) {
          this.expandedGroups.add(gid);
          this.userCollapsedDuplicateGroupIds.delete(gid);
        }
      }
      if (wasPrimaryIgnore !== this.isIgnored(primary)) {
        this.repartitionIgnoredToBottom();
      }
    } else {
      const wasIgnore = this.isIgnored(title);
      const pending = this.takePendingFieldsFor(title);
      this.flushPendingFieldEdits(); // unrelated rows' leftovers, if any
      const prevType = title.type;
      title.type = value;
      if (this.isIgnored(title)) {
        this.clearIgnoredFieldsInMemory(title);
        // Nulls intentionally override pending text: ignore clears.
        this.emitPatch(title, {
          ...pending,
          type: normalizedType,
          title: null,
          description: null,
          season: null,
          episode: null,
          edition: null,
        });
      } else {
        const p: TitlePatchRequest = { ...pending, type: normalizedType } as TitlePatchRequest;
        if (isBackdropTitleType(value)) {
          // Backdrop's name is fixed — overrides any pending typed name.
          title.title = BACKDROP_TITLE_NAME;
          p.title = BACKDROP_TITLE_NAME;
        } else if (isBackdropTitleType(prevType) && title.title === BACKDROP_TITLE_NAME) {
          // Leaving Backdrop: the forced name wasn't user data.
          title.title = '';
          p.title = null;
        }
        this.emitPatch(title, p);
      }
      if (wasIgnore !== this.isIgnored(title)) {
        this.repartitionIgnoredToBottom();
      }
    }
    this.labelChanged.emit(this.titles);
  }

  onSeasonChange(title: any, value: any): void {
    if (!title) return;
    const normalized = value === '' || value === null ? null : Number(value);
    const season = Number.isNaN(normalized as number) ? null : normalized;
    const members = this.getDuplicateGroupMembers(title);
    if (members.length > 1) {
      for (const m of members) {
        m.season = season;
      }
      for (const m of members) this.bufferFieldEdit(m, { season });
    } else {
      title.season = season;
      this.bufferFieldEdit(title, { season });
    }
    this.labelChanged.emit(this.titles);
  }

  onEpisodeChange(title: any, value: any): void {
    if (!title) return;
    const normalized = value === '' || value === null ? null : Number(value);
    const episode = Number.isNaN(normalized as number) ? null : normalized;
    const members = this.getDuplicateGroupMembers(title);
    if (members.length > 1) {
      for (const m of members) {
        m.episode = episode;
      }
      for (const m of members) this.bufferFieldEdit(m, { episode });
    } else {
      title.episode = episode;
      this.bufferFieldEdit(title, { episode });
    }
    this.labelChanged.emit(this.titles);
  }

  onEditionChange(title: any, value: any): void {
    if (!title) return;
    const normalizedEdition = value === '' ? null : value;
    const members = this.getDuplicateGroupMembers(title);
    if (members.length > 1) {
      for (const m of members) {
        m.edition = value;
      }
      for (const m of members) this.bufferFieldEdit(m, { edition: normalizedEdition });
    } else {
      title.edition = value;
      this.bufferFieldEdit(title, { edition: normalizedEdition });
    }
    this.labelChanged.emit(this.titles);
  }

  /** #701: resolved preview URL for a title, or null when none is available
   *  (drives the mobile play button's disabled state). */
  getQuickPreviewUrl(title: any): string | null {
    if (!title) return null;
    try {
      return this.previewUrlFn ? this.previewUrlFn(title) : null;
    } catch {
      return null;
    }
  }

  /** #701/#848: open the preview+label modal for a title (no-op without a clip). */
  openQuickPreview(title: any): void {
    if (!this.getQuickPreviewUrl(title)) return;
    this.quickPreviewTitle = title;
  }

  closeQuickPreview(): void {
    this.quickPreviewTitle = null;
  }

  // ---- #848: the in-modal labeling loop -------------------------------

  /** Rail-visible rows the modal can navigate: no dedupe siblings, no
   * claimed clips, no ignored rows. Same gate the rail itself applies. */
  private quickPreviewCandidates(): any[] {
    return (this.getDisplayOrderedTitles() || []).filter(
      (t) =>
        !this.isDedupeSibling(this.getTitleId(t)) &&
        !this.isClaimedClip(t) &&
        !this.isIgnored(t),
    );
  }

  /** Unlabeled rows remaining — the modal's header badge. */
  quickPreviewUnlabeledCount(): number {
    return this.quickPreviewCandidates().filter((t) => !this.isLabelingComplete(t)).length;
  }

  /** Advance the modal to the next/previous UNLABELED title, wrapping
   * around. Titles without a preview clip still open (placeholder shown) —
   * skipping them silently would hide rows from the loop. Stays put when
   * everything is labeled. */
  quickPreviewStep(dir: 1 | -1): void {
    if (!this.quickPreviewTitle) return;
    const all = this.quickPreviewCandidates();
    const n = all.length;
    if (!n) return;
    const curId = this.getTitleId(this.quickPreviewTitle);
    const idx = all.findIndex((t) => this.getTitleId(t) === curId);
    const start = idx < 0 ? (dir === 1 ? -1 : 0) : idx;
    for (let step = 1; step <= n; step++) {
      const cand = all[(((start + dir * step) % n) + n) % n];
      if (this.getTitleId(cand) === curId) continue;
      if (!this.isLabelingComplete(cand)) {
        this.quickPreviewTitle = cand;
        this.selectTitle(cand);
        return;
      }
    }
  }

  /** Ignore the modal's current title, then advance the loop. */
  quickPreviewIgnoreAndNext(): void {
    const current = this.quickPreviewTitle;
    if (!current) return;
    this.quickPreviewStep(1);
    this.markAsIgnore(current);
    // Ignoring the last unlabeled title leaves nothing to advance to —
    // close so the user sees the finished rail, not a stale form.
    if (this.quickPreviewTitle === current) {
      this.closeQuickPreview();
    }
  }

  markAsIgnore(title: any): void {
    if (!title) return;
    const members = this.getDuplicateGroupMembers(title);
    const primary = this.getPrimaryTitle(members) || title;
    const refType = (primary.type || '').toString().toLowerCase();
    const wasIgnore = refType === 'ignore';
    const gid = this.getDuplicateInfo(primary)?.groupId as string | undefined;

    if (members.length > 1) {
      if (wasIgnore) {
        for (const m of members) {
          m.type = '';
        }
      } else {
        for (const m of members) {
          m.type = 'ignore';
          this.clearIgnoredFieldsInMemory(m);
        }
        if (this.isMobile) {
          this.closeDrawer();
          this.closeDuplicateDrawer();
        }
      }
      const normalizedType = primary.type === '' ? null : primary.type;
      const patches: TitlePatchRequest[] = members.map((m) => {
        const id = this.getTitleId(m)!;
        // Buffered typed fields ride in this write; see takePendingFieldsFor.
        const pending = this.takePendingFieldsFor(m);
        if (wasIgnore) {
          return { ...pending, title_id: id, type: normalizedType };
        }
        // Nulls intentionally override pending text: ignore clears.
        return {
          ...pending,
          title_id: id,
          type: normalizedType,
          title: null,
          description: null,
          season: null,
          episode: null,
          edition: null,
        };
      });
      this.emitBatchOrSingle(patches);
      if (gid) {
        if (!wasIgnore) {
          this.expandedGroups.delete(gid);
        } else {
          this.expandedGroups.add(gid);
          this.userCollapsedDuplicateGroupIds.delete(gid);
        }
      }
      if (wasIgnore !== this.isIgnored(primary)) {
        this.repartitionIgnoredToBottom();
      }
    } else {
      const currentType = (title.type || '').toString().toLowerCase();
      const singleWasIgnore = currentType === 'ignore';
      if (currentType === 'ignore') {
        title.type = '';
      } else {
        title.type = 'ignore';
        this.clearIgnoredFieldsInMemory(title);
        if (this.isMobile) {
          this.closeDrawer();
        }
      }
      const normalizedType = title.type === '' ? null : title.type;
      // Buffered typed fields ride in this write; see takePendingFieldsFor.
      const pending = this.takePendingFieldsFor(title);
      if (!singleWasIgnore) {
        // Nulls intentionally override pending text: ignore clears.
        this.emitPatch(title, {
          ...pending,
          type: normalizedType,
          title: null,
          description: null,
          season: null,
          episode: null,
          edition: null,
        });
      } else {
        this.emitPatch(title, { ...pending, type: normalizedType });
      }
      if (singleWasIgnore !== this.isIgnored(title)) {
        this.repartitionIgnoredToBottom();
      }
    }
    this.labelChanged.emit(this.titles);
  }

  onFocusIn(): void {
    this.focusDepth += 1;
    this.isActive = true;
  }

  onFocusOut(): void {
    this.focusDepth = Math.max(0, this.focusDepth - 1);
    this.isActive = this.focusDepth > 0;
  }

  /**
   * Get title ID from title object for use with titleProgressValueFn
   * Matches backend key format: backend uses titles_map[k]["file"] as key in perTitleProgress
   */
  getTitleId(title: any): string | null {
    return getDiscTitleId(title);
  }

  /** duplicate_info from backend (snake_case) or duplicateInfo (camelCase). Always returns camelCase so tags/diffTags are available for metadata tag list.
   * effectiveGroupSize: count of group members (current + sameAs) that are in this.titles; use for display so we don't show "2 titles" when only one is in the list. */
  getDuplicateInfo(title: any): any {
    return parseDuplicateInfo(title, this.titles);
  }

  /** Titles as list with id for duplicate components (id = title_id). */
  getTitlesForDuplicate(): Array<{ id: string; title?: string | null; source_file?: string; sourceFile?: string }> {
    return (this.titles ?? []).map((t) => ({
      id: this.getTitleId(t) ?? '',
      title: t?.title ?? null,
      source_file: t?.source_file,
      sourceFile: t?.source_file,
    }));
  }

  /** Title payloads for sameAs IDs (for comparison tooltip on diff tags). */
  getComparedTitles(title: any): any[] {
    const info = this.getDuplicateInfo(title);
    const sameAs = info?.sameAs ?? [];
    if (!sameAs.length || !this.titles?.length) return [];
    const ids = new Set(sameAs);
    return this.titles.filter((t) => ids.has(this.getTitleId(t) ?? ''));
  }

  // ── Duplicate group helpers ──────────────────────────────────────

  /** Compute grouped duplicate titles: each entry has a groupId and the group's titles (sorted by orderIndex, active first). Pure — no state mutation. */
  private computeDuplicateGroups(): { groupId: string; titles: any[] }[] {
    return computeDuplicateGroupsFromTitles(this.titles);
  }

  /** Compute titles that are NOT part of any multi-member duplicate group, in display order. Uses pre-computed duplicateGroups. */
  private computeSingleTitles(): any[] {
    const duplicateIds = new Set<string>();
    for (const g of this.duplicateGroups) {
      for (const t of g.titles) {
        const id = this.getTitleId(t);
        if (id) duplicateIds.add(id);
      }
    }
    return this.getDisplayOrderedTitles().filter(t => {
      const id = this.getTitleId(t);
      return !id || !duplicateIds.has(id);
    });
  }

  /** Re-compute duplicateGroups, singleTitles, and interleaved titleListRows. Called in ngOnChanges and onBlur — never during template evaluation. */
  private recomputeDerivedState(): void {
    this.duplicateGroups = this.computeDuplicateGroups();
    const currentGroupIds = new Set(this.duplicateGroups.map((g) => g.groupId));
    for (const id of [...this.seenDuplicateGroupIds]) {
      if (!currentGroupIds.has(id)) {
        this.seenDuplicateGroupIds.delete(id);
        this.expandedGroups.delete(id);
        this.userCollapsedDuplicateGroupIds.delete(id);
      }
    }
    for (const g of this.duplicateGroups) {
      if (!this.seenDuplicateGroupIds.has(g.groupId)) {
        this.seenDuplicateGroupIds.add(g.groupId);
        this.expandedGroups.add(g.groupId);
      }
    }
    for (const g of this.duplicateGroups) {
      if (g.titles.length <= 1) continue;
      if (this.isDuplicateGroupIgnored(g)) continue;
      if (
        this.seenDuplicateGroupIds.has(g.groupId) &&
        !this.expandedGroups.has(g.groupId) &&
        !this.userCollapsedDuplicateGroupIds.has(g.groupId)
      ) {
        this.expandedGroups.add(g.groupId);
      }
    }
    this.singleTitles = this.computeSingleTitles();
    this.titleListRows = this.computeTitleListRows();
    this.cleanupRetryingPreviews();
    this.autoSelectFirstTitleIfNeeded();
  }

  /** One row per duplicate group (first occurrence in display order) or single title card. */
  private computeTitleListRows(): Array<
    | { kind: 'group'; group: { groupId: string; titles: any[] } }
    | { kind: 'single'; title: any }
  > {
    const ordered = this.getDisplayOrderedTitles();
    const emittedGroups = new Set<string>();
    const titleIdToGroup = new Map<string, { groupId: string; titles: any[] }>();
    for (const g of this.duplicateGroups) {
      if (g.titles.length <= 1) continue;
      for (const t of g.titles) {
        const id = this.getTitleId(t);
        if (id) titleIdToGroup.set(id, g);
      }
    }
    const rows: Array<
      | { kind: 'group'; group: { groupId: string; titles: any[] } }
      | { kind: 'single'; title: any }
    > = [];
    // Mobile parity with the desktop rail: hide subsumed m2ts (folded under
    // their wrapper in `dedupeGroups[].sibling_title_ids`) and any title
    // already collapsed as a Path B sibling, so they don't spawn standalone
    // mobile cards alongside the wrapper.
    const dedupeSiblingIds = this._dedupeSiblingIds;
    for (const t of ordered) {
      const tid = this.getTitleId(t);
      const memberGroup = tid ? titleIdToGroup.get(tid) : undefined;
      if (memberGroup) {
        const gid = memberGroup.groupId;
        if (!emittedGroups.has(gid)) {
          rows.push({ kind: 'group', group: memberGroup });
          emittedGroups.add(gid);
        }
        continue;
      }
      if (isComponentClip(t)) continue;
      if (tid && dedupeSiblingIds.has(tid)) continue;
      rows.push({ kind: 'single', title: t });
    }
    return rows;
  }

  /** Remove optimistic retry overrides for titles whose backend state is no longer 'failed'. Called in recomputeDerivedState, not during template eval. */
  private cleanupRetryingPreviews(): void {
    for (const id of [...this.retryingPreviews]) {
      const title = (this.titles || []).find(t => this.getTitleId(t) === id);
      if (title) {
        const realState = this.previewStateFn ? this.previewStateFn(title) : null;
        if (realState && realState.status !== 'failed') {
          this.retryingPreviews.delete(id);
        }
      }
    }
  }

  /** Returns the primary (active) title in a group, or the first if none is active. */
  getPrimaryTitle(group: any[]): any {
    return getPrimaryTitleForEntity(group);
  }

  /** All titles in the same duplicate group as `title` (same membership rule as `handleSetPrimary`). */
  getDuplicateGroupMembers(title: any): any[] {
    const info = this.getDuplicateInfo(title);
    if (!info || (info.effectiveGroupSize ?? 0) <= 1) {
      return title ? [title] : [];
    }
    const titleId = this.getTitleId(title);
    const sameAs: string[] = info.sameAs || [];
    const groupIds = new Set<string>([...(titleId ? [titleId] : []), ...sameAs]);
    return (this.titles || []).filter((t) => groupIds.has(this.getTitleId(t) ?? ''));
  }

  /** True when the duplicate group (by primary) is type ignore. */
  isDuplicateGroupIgnored(group: { groupId: string; titles: any[] }): boolean {
    const p = this.getPrimaryTitle(group.titles);
    return p ? this.isIgnored(p) : false;
  }

  private isDuplicateGroupIgnoredFromTitles(members: any[]): boolean {
    const p = this.getPrimaryTitle(members);
    return p ? this.isIgnored(p) : false;
  }

  isGroupExpanded(groupId: string): boolean {
    return this.expandedGroups.has(groupId);
  }

  toggleGroupExpanded(groupId: string): void {
    if (this.expandedGroups.has(groupId)) {
      this.expandedGroups.delete(groupId);
      this.userCollapsedDuplicateGroupIds.add(groupId);
    } else {
      this.expandedGroups.add(groupId);
      this.userCollapsedDuplicateGroupIds.delete(groupId);
    }
    this.cdr.markForCheck();
  }

  /** Select a variant as primary in a group — delegates to handleSetPrimary. */
  selectPrimary(title: any): void {
    if (title?.active === true) return;
    this.handleSetPrimary(title);
  }

  openDuplicateDrawer(group: { groupId: string; titles: any[] }): void {
    this.duplicateDrawerGroup = group;
    this.cdr.markForCheck();
  }

  closeDuplicateDrawer(): void {
    this.duplicateDrawerGroup = null;
    this.cdr.markForCheck();
  }

  saveDuplicateDrawerAndClose(): void {
    this.onBlur();
    this.duplicateDrawerGroup = null;
    this.cdr.markForCheck();
  }

  getGroupColor(groupId: string): { color: string; glow: string } {
    return getGroupColor(groupId);
  }

  getGroupIdentifier(groupId: string): string {
    return getGroupIdentifier(groupId);
  }

  /** Format size for variant rows (MB or GB). */
  formatSize(bytes: number | null | undefined): string {
    if (!bytes) return '';
    const gb = bytes / 1024 / 1024 / 1024;
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    return `${Math.round(bytes / 1024 / 1024)} MB`;
  }

  /** Comparative diff_tags for chips only (hides full-group ties; API data unchanged). */
  getVisibleDuplicateDiffTags(title: any, groupTitles: any[] | null | undefined): string[] {
    const raw = this.getDuplicateInfo(title)?.diffTags ?? [];
    return diffTagsForDisplay(groupTitles ?? [], Array.isArray(raw) ? raw : []);
  }

  /** True when every row would show no comparative chips after tie filtering. */
  duplicateGroupHasNoComparativeDiff(groupTitles: any[] | null | undefined): boolean {
    if (!groupTitles?.length) return true;
    for (const t of groupTitles) {
      if (this.getVisibleDuplicateDiffTags(t, groupTitles).length > 0) return false;
    }
    return true;
  }

  /** Summary lines + scan warning for duplicate variant rows (non-chip metadata). */
  getVariantMetadataLines(t: any): string[] {
    const lines = [...this.getMetadataSummaryLines(t)];
    const scan = t?.metadata_scan ?? t?.metadataScan;
    if (scan && typeof scan === 'object') {
      const w = scan['warning'];
      if (typeof w === 'string' && w.trim()) {
        lines.push(`Scan: ${w.trim()}`);
      }
    }
    return lines;
  }

  // ── End duplicate group helpers ────────────────────────────────

  /** Trigger retry of preview generation for a title. Shows optimistic spinner immediately. */
  retryPreview(title: any): void {
    const id = this.getTitleId(title);
    if (id) {
      this.retryingPreviews.add(id);
      this.cdr.markForCheck();
    }
    if (this.retryPreviewFn) {
      this.retryPreviewFn(title);
    }
  }

  /**
   * Get effective preview state for a title, applying optimistic "queued" override
   * when the user has clicked Retry but the backend hasn't confirmed yet.
   * Pure method — no state mutation. Cleanup of retryingPreviews happens in recomputeDerivedState().
   */
  getEffectivePreviewState(title: any): { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null {
    const id = this.getTitleId(title);
    const realState = this.previewStateFn ? this.previewStateFn(title) : null;

    // If this title is in the optimistic retry set...
    if (id && this.retryingPreviews.has(id)) {
      // Backend confirmed (status is no longer 'failed') — show real state
      // (cleanup of retryingPreviews set happens in recomputeDerivedState, not here)
      if (realState && realState.status !== 'failed') {
        return realState;
      }
      // Still failed in backend — show optimistic spinner
      return { status: 'queued' };
    }
    return realState;
  }

  /** Aspect ratio string for preview container (e.g. "16/9"). Uses video dimensions when available. */
  getPreviewAspectRatio(title: any): string {
    const id = this.getTitleId(title);
    return (id && this.aspectRatioMap[id]) || '16/9';
  }

  onPreviewAspectRatio(titleId: string | null, dims: { width: number; height: number } | null): void {
    if (!titleId) return;
    if (dims && dims.width > 0 && dims.height > 0) {
      this.aspectRatioMap[titleId] = `${dims.width}/${dims.height}`;
    }
    this.cdr.markForCheck();
  }

  /**
   * TrackBy function for duplicate group *ngFor to prevent re-rendering when array reference changes
   */
  trackByGroupId(index: number, group: { groupId: string }): string {
    return group?.groupId ?? `group-${index}`;
  }

  /** Arrow so *ngFor trackBy keeps component `this` (template passes the fn unbound). */
  trackByTitleListRow = (
    index: number,
    row: { kind: 'group' | 'single'; group?: { groupId: string }; title?: any }
  ): string => {
    if (row.kind === 'group' && row.group) {
      return `g:${row.group.groupId}`;
    }
    return `s:${this.getTitleId(row.title) ?? index}`;
  };

  /**
   * TrackBy function for *ngFor to prevent re-rendering when array reference changes
   * This prevents input fields from losing focus when titles array is updated
   */
  trackByTitleId(index: number, title: any): string {
    // Use stable identifier for tracking to prevent re-rendering when array reference changes
    return title?.title_id || `index-${index}`;
  }

  /** Whether this title is the primary in a duplicate group (active === true). */
  isPrimaryInGroup(title: any): boolean {
    const info = this.getDuplicateInfo(title);
    if (!info || (info.effectiveGroupSize ?? 0) <= 1) return false;
    return title?.active === true;
  }

  /** Whether this title is a secondary member of a duplicate group. */
  isSecondaryInGroup(title: any): boolean {
    const info = this.getDuplicateInfo(title);
    if (!info || (info.effectiveGroupSize ?? 0) <= 1) return false;
    return title?.active !== true;
  }

  /** Optimistically swap primary: transfer metadata from old primary to clicked title, then emit API call. */
  handleSetPrimary(title: any): void {
    const info = this.getDuplicateInfo(title);
    if (!info) return;
    const titleId = this.getTitleId(title);
    if (!titleId) return;

    const sameAs: string[] = info.sameAs || [];
    const groupIds = new Set([titleId, ...sameAs]);
    const groupTitles = (this.titles || []).filter((t: any) => groupIds.has(this.getTitleId(t) ?? ''));

    const currentPrimary = groupTitles.find((t: any) => t.active === true);

    // Optimistic metadata transfer
    const swapFields = ['title', 'type', 'season', 'episode', 'edition', 'description'];
    if (currentPrimary && this.getTitleId(currentPrimary) !== titleId) {
      for (const field of swapFields) {
        const value = currentPrimary[field];
        title[field] = value;
        currentPrimary[field] = null;
      }
      currentPrimary.active = false;
    }
    title.active = true;

    // Emit event for parent to make the API call
    const discId = this.resolveDiscId(title);
    if (!discId) {
      console.error('[title-label] cannot set primary: missing disc id', { titleId });
      this.toast.show('Could not set this title as primary — please report this.', 'error');
      return;
    }
    this.primaryChanged.emit({ discId, titleId });
    this.labelChanged.emit(this.titles);
    this.cdr.markForCheck();
  }

  /** Typed-field edits awaiting a flush, keyed by title id.
   *
   *  Free-text fields used to PATCH on every ngModelChange — i.e. every
   *  keystroke. Each response echoed the server's value back into the
   *  ngModel-bound input, so a burst of late echoes visibly re-typed the
   *  field character by character and could drop the last few (the final
   *  keystrokes' write had not returned when an older echo landed).
   *
   *  Typed fields now buffer locally and flush once on blur. Fields the user
   *  *picks* rather than types (the type dropdown) still save immediately —
   *  one deliberate action, one write. */
  private pendingFieldEdits = new Map<string, Partial<TitlePatchRequest>>();
  private autosaveTimer: any = null;
  private static readonly AUTOSAVE_IDLE_MS = 700;

  /** See TitleEditorComponent: blur alone is not a sufficient save trigger,
   *  so a buffered edit also writes itself after a short idle. */
  private scheduleAutosave(): void {
    if (this.autosaveTimer) clearTimeout(this.autosaveTimer);
    this.autosaveTimer = setTimeout(() => {
      this.autosaveTimer = null;
      this.flushPendingFieldEdits();
    }, TitleLabelComponent.AUTOSAVE_IDLE_MS);
  }

  /** Buffer a typed-field change; the model updates immediately so typing
   *  stays responsive, but nothing goes to the network until blur. */
  private bufferFieldEdit(title: any, fields: Partial<TitlePatchRequest>): void {
    const titleId = this.getTitleId(title);
    if (!titleId) return;
    const existing = this.pendingFieldEdits.get(titleId) || {};
    this.pendingFieldEdits.set(titleId, { ...existing, ...fields });
    this.scheduleAutosave();
  }

  /** Send everything buffered. Called from blur, Enter, and teardown so an
   *  edit can never be stranded unsaved. */
  private flushPendingFieldEdits(): void {
    if (this.autosaveTimer) {
      clearTimeout(this.autosaveTimer);
      this.autosaveTimer = null;
    }
    if (this.pendingFieldEdits.size === 0) return;
    const patches: TitlePatchRequest[] = [];
    this.pendingFieldEdits.forEach((fields, titleId) => {
      patches.push({ title_id: titleId, ...fields } as TitlePatchRequest);
    });
    this.pendingFieldEdits.clear();
    this.emitBatchOrSingle(patches);
  }

  /** Remove and return the buffered edits for one title so an immediate
   *  write (type pick, ignore) carries them in the SAME request. Users
   *  don't wait for autosave — they type and go straight for the
   *  dropdown. Flushing the buffer as a separate request races the
   *  immediate one (same base_seq → one write always loses); one request
   *  can't race itself. Also prevents a late idle-flush resurrecting a
   *  typed name onto a row the immediate write just cleared. */
  private takePendingFieldsFor(title: any): Partial<TitlePatchRequest> {
    const titleId = this.getTitleId(title);
    if (!titleId) return {};
    const pending = this.pendingFieldEdits.get(titleId);
    if (!pending) return {};
    this.pendingFieldEdits.delete(titleId);
    if (this.pendingFieldEdits.size === 0 && this.autosaveTimer) {
      clearTimeout(this.autosaveTimer);
      this.autosaveTimer = null;
    }
    return pending;
  }

  private emitPatch(title: any, fields: Partial<TitlePatchRequest>): void {
    const titleId = this.getTitleId(title);
    if (!titleId) return;
    this.titlePatched.emit({ title_id: titleId, ...fields });
  }

  private emitBatchOrSingle(patches: TitlePatchRequest[]): void {
    const valid = patches.filter((p) => p.title_id);
    if (valid.length === 0) return;
    if (valid.length === 1) {
      this.titlePatched.emit(valid[0]);
    } else {
      this.titleBatchPatched.emit(valid);
    }
  }

  /**
   * Get title progress value by extracting ID and calling titleProgressValueFn
   */
  // Calculate stroke-dashoffset for circular progress (circumference - progress portion)
  getProgressOffset(title: any): number {
    const progress = this.getTitleProgressValue(title) || 0;
    const circumference = 2 * Math.PI * 6; // radius is 6
    return circumference * (1 - progress / 100);
  }

  getTitleProgressValue(title: any): number {
    const id = this.getTitleId(title);
    if (!id) return 0;
    return this.titleProgressValueFn(id) || 0;
  }

  isIgnored(title: any): boolean {
    return (title?.type || '').toString().toLowerCase() === 'ignore';
  }

  /** True when the user explicitly set type='ignore' (PATCH, flag-decoys,
   * Confirm-ignore on an auto-ignored row, etc). Drives the Show-ignored
   * gate — auto-ignored rows stay visible chip-less until the user
   * confirms or overrides via the right-editor. */
  isUserIgnored(title: any): boolean {
    return (title?.user_type || '').toString().toLowerCase() === 'ignore';
  }

  /** True when the row's quick un-ignore toggle would actually do
   * something. Mirrors TitleEditorComponent.canUnignore: clearing the
   * user's type on a row with `auto_type === 'ignore'` reveals the auto
   * opinion again and the row stays ignored, so the toggle is hidden there
   * and the editor's banner points at the type picker instead. */
  canUnignore(title: any): boolean {
    if (!this.isIgnored(title)) return false;
    return (title?.auto_type || '').toString().toLowerCase() !== 'ignore';
  }

  /** True when only automated detection has flagged the row as ignore
   * AND the user hasn't reviewed it yet. Stays visible by default; the
   * editor surfaces a Confirm-ignore CTA so the user can promote
   * auto_type → user_type with one click. */
  isAutoIgnoredAwaitingReview(title: any): boolean {
    const auto = (title?.auto_type || '').toString().toLowerCase();
    const user = (title?.user_type || '').toString();
    return auto === 'ignore' && !user;
  }

  /** True when a title is "ready" — type is set to a non-ignore value
   * AND has a non-empty title string. Drives the green check-circle
   * icon next to the row chips so the user can scan for incomplete
   * rows at a glance (rows without the check still need attention). */
  isLabelingComplete(title: any): boolean {
    if (!title) return false;
    const type = (title.type || '').toString().trim();
    if (!type || type.toLowerCase() === 'ignore') return false;
    const name = (title.title || '').toString().trim();
    return name.length > 0;
  }

  /** Tooltip for the Canonical chip — explains *why* this is the
   * canonical pick and what the alternatives were, so a hover answers
   * "what does this pill mean?" without needing docs. */
  canonicalChipTooltip(title: any): string {
    const group = this.getDedupeGroupForRepresentative(this.getTitleId(title));
    if (group) {
      const total = group.sibling_title_ids.length + 1;
      return (
        `Picked as the canonical playlist out of ${total} siblings via the ` +
        `exploratory rip — segment-reorder tested alternate playlist orderings ` +
        `and this one matched the expected video. The other ${total - 1} are ` +
        `decoy permutations of the same segments.`
      );
    }
    return (
      'The exploratory rip confirmed this is the canonical playlist for the disc — ' +
      'segment-reorder ruled out the decoy permutations.'
    );
  }

  // ── Desktop list+editor helpers (Phase 3) ────────────────────────────────

  /** The title currently loaded into the desktop side-panel editor, or null. */
  get selectedTitle(): any | null {
    if (!this.selectedTitleId) return null;
    return (this.titles ?? []).find((t) => this.getTitleId(t) === this.selectedTitleId) ?? null;
  }

  /** Siblings (excluding the editor's active title) when it belongs to a
   * Path B sorted-segment-set dedupe group. The DuplicateGroupPanel inside
   * TitleEditor renders these as clickable rows so the user can switch the
   * editor to a sibling without leaving the right panel. */
  getEditorSiblings(): any[] {
    const sel = this.selectedTitle;
    if (!sel) return [];
    const selId = this.getTitleId(sel);
    if (!selId) return [];
    // Subsumed m2ts are folded into the wrapper's dedupe group (backend
    // fold_subsumption_into_groups) so they hide from the rail, but the
    // editor already lists them in its dedicated "Component clips"
    // section — exclude them here so they aren't shown twice.
    const isComponentClip = (t: any): boolean => {
      const sub = (t as { subsumed_by_title_id?: string | null }).subsumed_by_title_id;
      return !!sub && sub === selId;
    };
    // Look up the group whose representative OR sibling list contains this id.
    for (const g of (this.dedupeGroups || [])) {
      if (g.representative_title_id === selId) {
        return this.getDedupeSiblingTitles(g).filter((t) => !isComponentClip(t));
      }
      if ((g.sibling_title_ids || []).includes(selId)) {
        // Active title is a sibling — siblings list = all members minus active.
        const members = [
          ...this.getDedupeSiblingTitles(g),
          ...(this.titles || []).filter((t) => this.getTitleId(t) === g.representative_title_id),
        ];
        return members.filter((t) => this.getTitleId(t) !== selId);
      }
    }
    return [];
  }

  /** True iff the editor's active title is its dedupe group's representative. */
  isEditorTitleGroupRepresentative(): boolean {
    const sel = this.selectedTitle;
    if (!sel) return false;
    const selId = this.getTitleId(sel);
    if (!selId) return false;
    return (this.dedupeGroups || []).some((g) => g.representative_title_id === selId);
  }

  /** Count of real duplicate siblings (excludes component clips). Drives
   * "N duplicates" wording on mobile cards / badges so a wrapper whose only
   * `same_as` entries are component clips doesn't read as "N variants". */
  getDuplicateSiblingCount(title: any): number {
    return realSiblingCount(title, this.titles);
  }

  /** Count of component clips wrapped by `title` (m2ts whose
   * `subsumed_by_title_id` points at it). Drives the "M component clips"
   * pill on mobile alongside the duplicates count. */
  getComponentClipCount(title: any): number {
    return componentClipCount(title, this.titles);
  }

  /** Component clips for the editor's active title — every disc_title on
   * the same disc whose `subsumed_by_title_id` points at the active row.
   * Rendered as a separate "Component clips" section in the right-panel
   * DuplicateGroupPanel so the user can swap the editor onto an m2ts
   * wrapped by an mpls without leaving the screen. */
  getComponentClips(title?: any): any[] {
    const sel = title ?? this.selectedTitle;
    if (!sel) return [];
    const selId = this.getTitleId(sel);
    if (!selId) return [];
    const out: any[] = [];
    for (const t of (this.titles || [])) {
      const sub = (t as { subsumed_by_title_id?: string | null }).subsumed_by_title_id;
      if (sub && sub === selId) out.push(t);
    }
    return out;
  }

  /** Map a title to a `TitleRow` status — folds in ignore + duplicate detection
   * on top of the parent-supplied per-title status (rip/postprocess progress).
   *
   * Uses `realSiblingCount` rather than `effectiveGroupSize` so a wrapper mpls
   * whose only `same_as` entries are component-clip m2ts is NOT flagged
   * 'duplicate'. Component clips aren't duplicates of the wrapper — they're
   * the playback pieces it stitches together; calling them duplicates would
   * mis-claim a playlist and its underlying clip are the same content. */
  getTitleRowStatus(title: any): TitleRowStatus {
    if (this.isIgnored(title)) return 'ignored';
    if (realSiblingCount(title, this.titles) > 0) return 'duplicate';
    const id = this.getTitleId(title);
    const status = this.titleStatusFn(id);
    if (status === 'completed') return 'complete';
    if (status === 'running') return 'running';
    if (status === 'failed') return 'failed';
    return 'pending';
  }

  /** Pre-formatted duration for the title row's right side (e.g. "2h 18m").
   * Sub-60s clips render as "Xs" rather than "0m" so a real 45-second
   * bumper doesn't look like a zero-length entry on the titles screen. */
  getTitleDurationLabel(title: any): string | null {
    const seconds = title?.duration;
    if (!seconds) return null;
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m`;
    return `${Math.round(seconds)}s`;
  }

  /** Click-handler for a title row: selects it for the side-panel editor. */
  selectTitle(title: any): void {
    const id = this.getTitleId(title);
    this.selectedTitleId = id;
    this.cdr.markForCheck();
  }

  /** Returns true when the editor side panel should show its "select a title"
   * empty state — no selection and at least one title in the list. */
  get showEditorEmptyState(): boolean {
    return !this.selectedTitle && (this.titles?.length ?? 0) > 0;
  }

  /** Editor change handler — bridges the editor's generic (titleChanged)
   * event back to the parent's labelChanged listener so the in-memory
   * BehaviorSubject state stays in sync with the local mutation.
   *
   * NOTE: labelChanged does NOT persist to the backend — it only updates
   * the client-side context. Persistence happens through the sibling
   * `titlePatched` event, which the editor also emits on every field
   * change and which we forward via the template binding on this class's
   * `titlePatched` @Output. Regression #TBD: 9cc142e4 dropped the field-
   * level titlePatched wiring when the editor was extracted, so every
   * edit in the right-panel silently reverted on the next context
   * refetch — see title-editor's class docstring.
   */
  onEditorChanged(): void {
    this.labelChanged.emit(this.titles);
  }

  /** Auto-pick a title for the editor when the list first arrives. Called from
   * recomputeDerivedState. Keeps the side panel populated so the user doesn't
   * land on an empty editor on every workflow open. */
  private autoSelectFirstTitleIfNeeded(): void {
    if (this.selectedTitleId) {
      const stillExists = (this.titles ?? []).some((t) => this.getTitleId(t) === this.selectedTitleId);
      if (stillExists) return;
    }
    const first = (this.titles ?? []).find((t) => !this.isIgnored(t)) ?? this.titles?.[0] ?? null;
    this.selectedTitleId = first ? this.getTitleId(first) : null;
  }

  /** Show season/episode fields when the title's own type is Episode.
   *
   * The `if (this.isSeries) return true;` short-circuit used to force these
   * onto every row of a series disc, including extras (#798). Now per-type,
   * matching showEdition() below and the completeness rules in
   * title-label-stats.util.ts.
   */
  showSeasonEpisode(title: any): boolean {
    if (this.isIgnored(title)) return false;
    const type = (title?.type || '').toString().toLowerCase();
    return type === 'episode';
  }

  /** Show edition field only for Main Movie type. */
  showEdition(title: any): boolean {
    if (this.isIgnored(title)) return false;
    const type = (title?.type || '').toString().toLowerCase();
    return type === 'mainmovie';
  }

  /** Short hint for title type (tooltip when type is selected). */
  getTypeHint(type: string | null | undefined): string {
    if (!type) return 'Select Type...';
    const hints: Record<string, string> = {
      ignore: 'Skip this title; it will not be included in the output',
      MainMovie: 'Feature film or primary content',
      Episode: 'Series episode',
      Extra: 'Generic extra (Jellyfin extras folder; Plex Other)',
      Trailer: 'Promotional trailer or preview',
      DeletedScene: 'Deleted or alternate scene',
      BehindTheScenes: 'Making-of or behind-the-scenes content',
      Featurette: 'Short documentary or featurette',
      Interview: 'Cast or crew interview',
      Scene: 'Extra scene or clip (not the main episode)',
      Short: 'Short film or bonus short',
      Other: 'Extra that does not fit another category',
      Sample: 'Sample clip (Jellyfin samples folder; Plex Other)',
      Clip: 'Short clip (Jellyfin clips folder; Plex Other)',
      ThemeMusic: 'Theme song or intro music (Jellyfin theme-music; Plex Other)',
      Backdrop: 'Theme/backdrop video (Jellyfin backdrops; Plex Other)',
    };
    return hints[type] ?? type;
  }

  /** Duration in seconds for drawer metadata (duration or duration_seconds). */
  getDrawerDuration(title: any): number | null {
    const raw = title?.duration ?? title?.duration_seconds;
    if (typeof raw === 'number' && raw >= 0) return raw;
    const parsed = Number(raw);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
  }

  getChapterCount(title: any): number | null {
    const chapters = title?.chapters;
    if (chapters != null) {
      if (typeof chapters === 'number') return chapters > 0 ? chapters : null;
      if (Array.isArray(chapters)) return chapters.length > 0 ? chapters.length : null;
      if (typeof chapters === 'object') {
        const count = (chapters as any).count;
        if (typeof count === 'number') return count > 0 ? count : null;
      }
    }
    const meta = title?.metadata_scan ?? title?.metadataScan;
    if (meta && typeof meta === 'object' && typeof meta['chapters_count'] === 'number') {
      const n = meta['chapters_count'];
      return n > 0 ? n : null;
    }
    return null;
  }

  /** Lines for metadata-summary tooltip (quality, subs, audio). */
  getMetadataSummaryLines(t: any): string[] {
    const s = t?.metadata_summary;
    if (!s || typeof s !== 'object') return [];
    const parts: string[] = [];
    const qt = s.quality_tier;
    const qh = Array.isArray(s.quality_hints) && s.quality_hints.length ? s.quality_hints.slice(0, 2).join(', ') : '';
    if (qt) parts.push(`Quality: ${qt}${qh ? ` (${qh})` : ''}`);
    const st = s.subtitle_tier;
    const sh = Array.isArray(s.subtitle_hints) && s.subtitle_hints.length ? s.subtitle_hints[0] : '';
    if (st) parts.push(`Subtitles: ${st}${sh ? ` (${sh})` : ''}`);
    const at = s.audio_tier;
    const ah = Array.isArray(s.audio_hints) && s.audio_hints.length ? s.audio_hints[0] : '';
    if (at) parts.push(`Audio: ${at}${ah ? ` (${ah})` : ''}`);
    return parts;
  }

  private clearIgnoredFieldsInMemory(title: any): void {
    title.title = '';
    title.description = '';
    title.note = '';
    title.season = null;
    title.episode = null;
    title.edition = null;
  }

  /**
   * Check if a field is required for the given title type
   */
  isFieldRequired(title: any, field: 'type' | 'title' | 'season' | 'episode'): boolean {
    const type = (title?.type || '').toLowerCase();
    
    // Type is always required
    if (field === 'type') {
      return !type || type === '';
    }
    
    // If type is ignore, no other fields are required
    if (type === 'ignore') {
      return false;
    }
    
    // If type is episode, season and episode are required
    if (type === 'episode') {
      if (field === 'season' || field === 'episode') {
        return !title?.[field] && title?.[field] !== 0;
      }
    }
    
    // For all non-ignore types, title is required
    if (field === 'title') {
      return !title?.title || !title.title.trim();
    }
    
    return false;
  }

  /**
   * Check if a field should show required outline
   */
  shouldShowRequiredOutline(title: any, field: 'type' | 'title' | 'season' | 'episode'): boolean {
    // Type is always required if not filled
    if (field === 'type') {
      return !title?.type || title.type === '';
    }
    
    // Only show outlines for other fields if type is filled
    const type = (title?.type || '').toLowerCase();
    if (!type || type === '') {
      return false;
    }
    
    return this.isFieldRequired(title, field);
  }
}
