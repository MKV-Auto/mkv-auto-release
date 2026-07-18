import { ChangeDetectionStrategy, Component, EventEmitter, inject, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Observable, of } from 'rxjs';
import { map, switchMap, distinctUntilChanged, startWith } from 'rxjs/operators';
import { PreviewViewerComponent } from '../preview-viewer/preview-viewer.component';
import { IconComponent } from '../../ui/icon/icon.component';
import { PillComponent, PillTone } from '../../ui/pill/pill.component';
import { BtnComponent } from '../../ui/btn/btn.component';
import { ObfuscationBadgeComponent } from '../obfuscation-badge/obfuscation-badge.component';
import { DuplicateCompareModalComponent } from '../duplicate-compare-modal/duplicate-compare-modal.component';
import { TITLE_TYPE_SELECT_OPTIONS } from '../../constants/title-type-options';
import { WorkflowService, TmdbEpisodeSummary, TitlePatchRequest } from '../../services/workflow.service';

const STATUS_TONE: Record<string, PillTone> = {
  completed: 'emerald',
  running: 'blue',
  failed: 'red',
};

const STATUS_LABEL: Record<string, string> = {
  completed: 'Ripped',
  running: 'Ripping',
  failed: 'Rip failed',
};

const STATUS_TOOLTIP: Record<string, string> = {
  running: 'This title is currently being ripped from the disc to MKV. The percentage is its per-title progress.',
  failed: 'The rip for this title failed. The MKV file may be incomplete or missing.',
};

/**
 * Editor surface for one disc title — the form half of the prototype's
 * list+editor split. Renders the title metadata fields (title name, type,
 * edition / season+episode, description), the status pill, the preview
 * controls, and the autosave indicator.
 *
 * # Emissions and persistence
 *
 * Every user-driven field change MUST emit BOTH:
 *   1. `titleChanged` — nudges the parent's in-memory context so UI re-renders.
 *   2. `titlePatched({ title_id, ...fields })` — persists to backend via
 *      the parent's onTitlePatch → workflowService.patchDiscTitle →
 *      PATCH /api/discs/{id}/titles.
 *
 * Regression history: when this component was extracted from the inline
 * label editor (commit 9cc142e4, 2026-05-08 "wire title-label desktop to
 * TitleRow + TitleEditor"), the persistence half was dropped. Handlers
 * only emitted `titleChanged`, so the parent's `labelChanged` bridge
 * updated BehaviorSubject state but never issued a PATCH. Every edit
 * silently reverted on the next context refetch (page reload, WS
 * reconnect, visibility resync). See title-modal for the mobile
 * counterpart.
 *
 * Designed to be embedded as a docked side panel on desktop AND as the body
 * of the existing `TitleModal` mobile drawer.
 */
@Component({
  selector: 'app-title-editor',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    PreviewViewerComponent,
    IconComponent,
    PillComponent,
    BtnComponent,
    ObfuscationBadgeComponent,
    DuplicateCompareModalComponent,
  ],
  changeDetection: ChangeDetectionStrategy.Default,
  templateUrl: './title-editor.component.html',
  styleUrls: ['./title-editor.component.scss'],
})
export class TitleEditorComponent {
  readonly titleTypeOptions = TITLE_TYPE_SELECT_OPTIONS;

  @Input() title: any = null;
  @Input() isSeries = false;
  @Input() titleProgress: Record<string, number> = {};
  @Input() titleStatusFn: (id: string | null | undefined) => string = () => 'pending';
  @Input() titleProgressValueFn: (id: string | null | undefined) => number = () => 0;
  @Input() titleActiveFn: (id: string | null | undefined) => boolean = () => false;
  @Input() previewUrlFn: (t: any) => string | null = () => null;
  @Input() titlePathFn: (t: any) => string | null = () => null;
  @Input() previewStateFn: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null = () => null;
  @Input() labelSaving = false;
  @Input() lastAutosaveOk = true;
  @Input() devMode = false;
  /** When true, renders an X close button in the header. Side panel use. */
  @Input() showCloseButton = false;

  /**
   * Sibling titles in the same Path B sorted-segment-set dedupe group as the
   * active title. Empty when the active title isn't part of a group OR when
   * the parent doesn't know about the group. The parent (title-label) reads
   * this from workflow-context's dedupeGroups field. The active title is NOT
   * included in this list — only its siblings.
   */
  @Input() siblings: any[] = [];
  /** True when the active title is the group's representative_title_id. */
  @Input() isGroupRepresentative = false;
  /**
   * "Component clips" — every disc_title whose `subsumed_by_title_id`
   * points at the editor's active title. Rendered as a separate
   * section under the DuplicateGroupPanel so the user can swap the
   * editor onto an m2ts wrapped by this mpls. Empty (no section) when
   * the active title doesn't wrap any clips.
   */
  @Input() componentClips: any[] = [];

  @Output() close = new EventEmitter<void>();
  @Output() titleChanged = new EventEmitter<void>();
  @Output() titleBlur = new EventEmitter<void>();
  /**
   * Field-level patch emission for the current title. The parent bubbles this up
   * to workflow-labeling.onTitlePatch → workflowService.patchDiscTitle →
   * PATCH /api/discs/{id}/titles. Every user edit inside this component must
   * emit both (a) `titleChanged` — which nudges the in-memory context so the UI
   * re-renders — and (b) `titlePatched` — which persists to the backend. See
   * regression note on this class's docstring.
   */
  @Output() titlePatched = new EventEmitter<TitlePatchRequest>();
  /** Switch the editor to a different title in the same dedupe group. */
  @Output() switchToSibling = new EventEmitter<any>();
  /** User clicked "Make primary" on a sibling row. Parent fires the
   * PATCH /discs/{id}/titles/batch (or the existing set-primary endpoint)
   * — keeping the network call out of the editor keeps it presentational. */
  @Output() makeSiblingPrimary = new EventEmitter<any>();
  /** User clicked "Ungroup" / "Re-group" on the panel header. Parent
   * hits POST /discs/{disc_id}/titles/{title_id}/ungroup-duplicate. */
  @Output() ungroupDuplicate = new EventEmitter<void>();

  previewTitle: any | null = null;
  previewUrl: string | null = null;
  /** True when the side-by-side Compare modal is open over the editor. */
  showCompareModal = false;

  // #371 — TMDB episode picker. Observes the active workflow context's
  // primary season + tmdbEpisodeCatalog. The current title's `season`
  // (if set) overrides the disc primary. When the value resolves to a
  // TmdbEpisodeSummary[] the picker renders; sentinels hide it.
  private readonly workflow = inject(WorkflowService);
  readonly episodeOptions$: Observable<TmdbEpisodeSummary[] | 'loading' | 'error' | 'unavailable'> =
    this.workflow.getPrimarySeason$().pipe(
      switchMap((primary) => this.workflow.getEpisodesForSeason$(this.effectiveSeason(primary))),
      distinctUntilChanged(),
    );

  /** #602 — `true` when TMDB has returned a usable episode catalog for
   * the active season, so the manual title / season / episode inputs on
   * Episode rows are redundant noise. Stays `false` in the
   * `'loading'` / `'error'` / `'unavailable'` window so the inputs
   * never blink away while the picker is still resolving. */
  readonly tmdbCatalogReady$: Observable<boolean> = this.episodeOptions$.pipe(
    map((opts) => this.isEpisodeList(opts) && opts.length > 0),
    startWith(false),
    distinctUntilChanged(),
  );

  /** Per-row effective season — track.season override, else disc primary. */
  private effectiveSeason(primary: number): number {
    const t = this.title?.season;
    const n = Number(t);
    return Number.isFinite(n) && n > 0 ? n : (primary || 1);
  }

  /** Index of the currently-set (season, episode) in the options list, or -1.
   * Drives the `<select>` selectedIndex so the picker reflects manual edits. */
  findEpisodeIndex(options: TmdbEpisodeSummary[]): number {
    if (!options || !this.title) return -1;
    const s = Number(this.title.season);
    const e = Number(this.title.episode);
    if (!Number.isFinite(s) || !Number.isFinite(e)) return -1;
    return options.findIndex((opt) => opt.season_number === s && opt.episode_number === e);
  }

  /** User picked an episode from the dropdown. Writes season+episode+title
   * onto the bound `title` (the existing two-way binding propagates to the
   * autosave pipeline via `titleChanged`). The track schema has no
   * `episode_name` field — episode names live in plain `title`. */
  onEpisodePicked(options: TmdbEpisodeSummary[] | null, indexValue: string | number | null): void {
    if (!this.title || !options) return;
    const idx = Number(indexValue);
    if (!Number.isInteger(idx) || idx < 0 || idx >= options.length) return;
    const ep = options[idx];
    this.title.season = ep.season_number;
    this.title.episode = ep.episode_number;
    this.title.title = ep.name;
    this.emitFieldPatch({
      season: ep.season_number,
      episode: ep.episode_number,
      title: ep.name || null,
    });
    this.titleChanged.emit();
  }

  /** Typeguard helper for the template's ngSwitch — narrows `value` to the
   * array branch. Avoids inline `$any` casts in the markup. */
  isEpisodeList(value: TmdbEpisodeSummary[] | 'loading' | 'error' | 'unavailable'): value is TmdbEpisodeSummary[] {
    return Array.isArray(value);
  }

  /** Group members in display order for the Compare modal — current
   * primary first, then siblings as the parent passed them in. */
  get groupMembersForCompare(): any[] {
    const members: any[] = [];
    if (this.title) members.push(this.title);
    for (const sib of this.siblings || []) members.push(sib);
    return members;
  }

  /** True when the title-editor's current title sits in a dedupe group
   * that was force-split via Ungroup. Drives the "Re-group" label
   * variant on the panel-header button. */
  get isForceIndependent(): boolean {
    return !!this.title?.force_independent_group;
  }

  /** True when the current title is the primary (group representative).
   * Make-primary on a sibling is offered only when the current row
   * isn't already the primary OR when there's a sibling worth promoting. */
  isSiblingPrimary(sibling: any): boolean {
    return sibling?.active === true;
  }

  openCompare(): void {
    this.showCompareModal = true;
  }

  closeCompare(): void {
    this.showCompareModal = false;
  }

  onMakeSiblingPrimary(sibling: any): void {
    if (!sibling) return;
    this.makeSiblingPrimary.emit(sibling);
  }

  onUngroupDuplicate(): void {
    this.ungroupDuplicate.emit();
  }

  get showSpinner(): boolean {
    return this.labelSaving;
  }

  openPreview(): void {
    if (!this.title) return;
    const url = this.previewUrlFn(this.title);
    if (!url) return;
    this.previewTitle = this.title;
    this.previewUrl = url;
  }

  closePreview(): void {
    this.previewTitle = null;
    this.previewUrl = null;
  }

  updateDescription(value: any): void {
    if (!this.title || this.isIgnored()) return;
    this.title.description = value;
    this.title.note = value;
    const normalized = value === '' ? null : value;
    this.emitFieldPatch({ description: normalized });
    this.titleChanged.emit();
  }

  markAsIgnore(): void {
    if (!this.title) return;
    const currentType = (this.title.type || '').toString().toLowerCase();
    if (currentType === 'ignore') {
      this.title.type = '';
      // Un-ignore: clear only type. Row-level markAsIgnore matches this
      // (title-label.component.ts:733).
      this.emitFieldPatch({ type: null });
    } else {
      this.title.type = 'ignore';
      this.clearIgnoredFields();
      // Ignore: mirror clearIgnoredFields() + edition null so the backend
      // matches what row-level markAsIgnore sends (title-label.component.ts:724-731).
      this.emitFieldPatch({
        type: 'ignore',
        title: null,
        description: null,
        season: null,
        episode: null,
        edition: null,
      });
    }
    this.titleChanged.emit();
  }

  /** True when only automated detection (DiscDB / Path A sibling-ignore /
   * subsumption / FFmpeg padding-detect) flagged this row as ignore and
   * the user hasn't reviewed yet. Surfaces the Confirm-ignore CTA. */
  get isAutoIgnoredAwaitingReview(): boolean {
    if (!this.title) return false;
    const auto = (this.title.auto_type || '').toString().toLowerCase();
    const user = (this.title.user_type || '').toString();
    return auto === 'ignore' && !user;
  }

  /** User confirms an automated ignore decision. Flips `user_type` to
   * 'ignore' so the chip system promotes the row from blank to
   * "Ignored" and Show-ignored gating kicks in on the next render.
   * The backend's PATCH endpoint already routes type-writes through
   * `set_title_type(source='user')` — this also primes the optimistic
   * UI by setting user_type locally so the chip flips before the
   * round-trip completes. */
  confirmAutoIgnore(): void {
    if (!this.title) return;
    this.title.user_type = 'ignore';
    this.title.type = 'ignore';
    // Same PATCH shape as markAsIgnore going TO ignore — backend routes
    // type-writes through set_title_type(source='user') which flips user_type
    // automatically, so we don't have to include it explicitly.
    this.emitFieldPatch({
      type: 'ignore',
      title: null,
      description: null,
      season: null,
      episode: null,
      edition: null,
    });
    this.titleChanged.emit();
  }

  onTypeChange(value: any): void {
    if (!this.title) return;
    this.title.type = value;
    const normalizedType = value === '' ? null : value;
    if (this.isIgnored()) {
      this.clearIgnoredFields();
      this.emitFieldPatch({
        type: normalizedType,
        title: null,
        description: null,
        season: null,
        episode: null,
        edition: null,
      });
    } else {
      this.emitFieldPatch({ type: normalizedType });
    }
    this.titleChanged.emit();
  }

  onClose(): void {
    this.close.emit();
  }

  onSwitchToSibling(sibling: any): void {
    this.switchToSibling.emit(sibling);
  }

  /** Short identifier for a sibling row — source filename trimmed. */
  siblingLabel(sibling: any): string {
    const src = (sibling?.source_file || '').toString();
    if (!src) return sibling?.title || sibling?.title_id || '';
    // Strip leading directories so the row stays scannable.
    const lastSlash = Math.max(src.lastIndexOf('/'), src.lastIndexOf('\\'));
    return lastSlash >= 0 ? src.slice(lastSlash + 1) : src;
  }

  /** Duration in "Mm Ss" format for sibling rows. */
  siblingDuration(sibling: any): string {
    return this.formatDuration(sibling?.duration ?? 0);
  }

  /** Size label for a sibling row — prefers backend's pre-formatted
   * `display_size` when present, else formats `size` (bytes) inline.
   * Returns '' so the template can hide the column when no size data. */
  siblingSize(sibling: any): string {
    const display = (sibling?.display_size || '').toString().trim();
    if (display) return display;
    const bytes = Number(sibling?.size);
    return Number.isFinite(bytes) && bytes > 0 ? this.formatSize(bytes) : '';
  }

  /** Human-readable subtext for a sibling row (matches the prototype's
   * dupReason). Hooks off obfuscation_reason + subsumed_by so the
   * collapsed group communicates *why* a row is hidden in one phrase. */
  siblingDupReason(sibling: any): string {
    if (sibling?.subsumed_by_title_id) {
      return 'Component clip wrapped by this playlist';
    }
    const reason = (sibling?.obfuscation_reason || '').toString();
    if (reason === 'segment_set_sibling') return 'Same segments as the primary';
    if (reason === 'path_a_decoy') return 'Skipped by exploratory-rip match';
    if (reason === 'makemkv_msg3307') return 'MakeMKV flagged as likely decoy';
    if (reason === 'subsumed') return 'Component clip wrapped by this playlist';
    return '';
  }

  trackBySiblingId(_idx: number, sibling: any): string {
    return sibling?.title_id ?? sibling?.source_file ?? String(_idx);
  }

  /**
   * Live preview of the post-process output filename. Mirrors the
   * prototype's indigo "Will be saved as" box. Best-effort heuristic — the
   * canonical filename construction lives in the backend post-process
   * stage, so this is a UX cue, not the actual output path. Returns null
   * when there's nothing meaningful to preview (no title, ignored, no name).
   */
  getFilenamePreview(): string | null {
    if (!this.title || this.isIgnored()) return null;
    const name = (this.title.title || '').toString().trim();
    if (!name) return null;
    const safeName = name.replace(/[\/\\:*?"<>|]/g, '_');
    if (this.isSeries) {
      const season = Number(this.title.season);
      const episode = Number(this.title.episode);
      if (Number.isFinite(season) && Number.isFinite(episode) && season > 0 && episode > 0) {
        const s = String(season).padStart(2, '0');
        const e = String(episode).padStart(2, '0');
        return `S${s}E${e} - ${safeName}.mkv`;
      }
      return `${safeName}.mkv`;
    }
    const edition = (this.title.edition || '').toString().trim();
    if (edition) {
      return `${safeName} (${edition}).mkv`;
    }
    return `${safeName}.mkv`;
  }

  formatDuration(seconds: number): string {
    if (!seconds) return '';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (hours > 0) {
      return `${hours}h ${minutes}m`;
    }
    if (minutes > 0) {
      return `${minutes}m`;
    }
    return `${Math.round(seconds)}s`;
  }

  formatSize(bytes: number): string {
    if (!bytes) return '';
    const mb = bytes / 1024 / 1024;
    return `${mb.toFixed(0)} MB`;
  }

  isIgnored(): boolean {
    return (this.title?.type || '').toString().toLowerCase() === 'ignore';
  }

  getChapterCount(): number | null {
    const chapters = this.title?.chapters;
    if (chapters == null) return null;
    if (typeof chapters === 'number') return chapters > 0 ? chapters : null;
    if (Array.isArray(chapters)) return chapters.length > 0 ? chapters.length : null;
    if (typeof chapters === 'object') {
      const count = (chapters as any).count;
      if (typeof count === 'number') return count > 0 ? count : null;
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

  /** Maps the title's status to a pill tone for the header status indicator. */
  statusPillTone(): PillTone {
    if (!this.title) return 'slate';
    const status = this.titleStatusFn(this.title.title_id);
    return STATUS_TONE[status] ?? 'slate';
  }

  /** Maps status to a label, with running showing the percentage inline. */
  statusPillLabel(): string {
    if (!this.title) return '';
    const status = this.titleStatusFn(this.title.title_id);
    if (status === 'running') {
      return `Ripping ${this.titleProgressValueFn(this.title.title_id) || 0}%`;
    }
    return STATUS_LABEL[status] ?? '';
  }

  /** Tooltip text for the status pill — gives plain-language meaning so
   * the user knows what "Rip failed" or a stuck spinner actually implies. */
  statusPillTooltip(): string | null {
    if (!this.title) return null;
    const status = this.titleStatusFn(this.title.title_id);
    return STATUS_TOOLTIP[status] ?? null;
  }

  /** Whether to render the status pill at all. Quiet-by-default: only
   * surface for `running` / `failed` (actionable states). `pending` and
   * `completed` are silent — the global stage breadcrumb and the title
   * row's labeling-complete check icon cover them without per-title noise. */
  showStatusPill(): boolean {
    if (!this.title) return false;
    const status = this.titleStatusFn(this.title.title_id);
    return status === 'running' || status === 'failed';
  }

  private clearIgnoredFields(): void {
    if (!this.title) return;
    this.title.title = '';
    this.title.description = '';
    this.title.note = '';
    this.title.season = null;
    this.title.episode = null;
  }

  /** Emit a field-level PATCH for the current title. No-op when there's no
   * title bound or no `title_id` on the row (defensive — every disc_title
   * row from the backend carries `title_id`). */
  private emitFieldPatch(fields: Partial<TitlePatchRequest>): void {
    const titleId = this.title?.title_id;
    if (!titleId) return;
    this.titlePatched.emit({ title_id: titleId, ...fields });
  }

  /** ngModelChange handlers for direct-bound fields — the template used to
   * call `titleChanged.emit()` inline, but that only nudged the in-memory
   * context. Now each field also emits a `titlePatched` so the backend is
   * kept in sync per keystroke (matches the pre-9cc142e4 row-editor path). */
  onTitleNameChange(value: any): void {
    if (!this.title) return;
    const normalized = value === '' ? null : value;
    this.emitFieldPatch({ title: normalized });
    this.titleChanged.emit();
  }

  onSeasonChange(value: any): void {
    if (!this.title) return;
    const num = value === null || value === '' ? null : Number(value);
    const normalized = Number.isFinite(num) ? num : null;
    this.emitFieldPatch({ season: normalized as number | null });
    this.titleChanged.emit();
  }

  onEpisodeChange(value: any): void {
    if (!this.title) return;
    const num = value === null || value === '' ? null : Number(value);
    const normalized = Number.isFinite(num) ? num : null;
    this.emitFieldPatch({ episode: normalized as number | null });
    this.titleChanged.emit();
  }

  onEditionChange(value: any): void {
    if (!this.title) return;
    const normalized = value === '' ? null : value;
    this.emitFieldPatch({ edition: normalized });
    this.titleChanged.emit();
  }
}
