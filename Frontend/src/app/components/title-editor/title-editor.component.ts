import { ChangeDetectionStrategy, Component, EventEmitter, inject, Input, OnChanges, OnDestroy, Output, SimpleChanges } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { BehaviorSubject, Observable, combineLatest, of, Subscription } from 'rxjs';
import { map, switchMap, distinctUntilChanged, startWith } from 'rxjs/operators';
import { PreviewViewerComponent } from '../preview-viewer/preview-viewer.component';
import { IconComponent } from '../../ui/icon/icon.component';
import { PillComponent, PillTone } from '../../ui/pill/pill.component';
import { BtnComponent } from '../../ui/btn/btn.component';
import { ObfuscationBadgeComponent } from '../obfuscation-badge/obfuscation-badge.component';
import { DuplicateCompareModalComponent } from '../duplicate-compare-modal/duplicate-compare-modal.component';
import {
  BACKDROP_TITLE_NAME,
  isBackdropTitleType,
  TITLE_TYPE_SELECT_OPTIONS,
} from '../../constants/title-type-options';
import { SystemService } from '../../services/system.service';
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

/** Extras type → (plex folder, jellyfin folder). Mirrors
 * core/title_type_extras_layout.py; preview cue only, backend is canonical. */
const EXTRAS_FOLDERS: Record<string, [string, string]> = {
  BehindTheScenes: ['Behind The Scenes', 'behind the scenes'],
  DeletedScene: ['Deleted Scenes', 'deleted scenes'],
  Featurette: ['Featurettes', 'featurettes'],
  Interview: ['Interviews', 'interviews'],
  Scene: ['Scenes', 'scenes'],
  Short: ['Shorts', 'shorts'],
  Trailer: ['Trailers', 'trailers'],
  Other: ['Other', 'other'],
  Extra: ['Other', 'extras'],
  Sample: ['Other', 'samples'],
  Clip: ['Other', 'clips'],
  ThemeMusic: ['Other', 'theme-music'],
  Backdrop: ['Other', 'backdrops'],
};

/** Plex filename suffix for episode-level extras. */
const PLEX_EPISODE_EXTRA_SUFFIXES: Record<string, string> = {
  BehindTheScenes: 'behindthescenes',
  DeletedScene: 'deleted',
  Featurette: 'featurette',
  Interview: 'interview',
  Scene: 'scene',
  Short: 'short',
  Trailer: 'trailer',
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
export class TitleEditorComponent implements OnChanges, OnDestroy {
  /** Last line of defence: tearing down mid-edit (navigating away, switching
   *  card) must not strand a buffered typed field unsaved. */
  ngOnDestroy(): void {
    this.flushPendingFieldEdits();
    this.editorSubs.unsubscribe();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if ('title' in changes) {
      this.extraScopeSeason$.next(this.extraSeason);
      // New row: the description link collapses again (unless the row has
      // content, which descriptionVisible() covers on its own).
      this.showDescriptionField = false;
    }
  }

  private readonly editorSubs = new Subscription();
  /** Plex needs a library setting enabled before season-scoped extras are
   * honoured, so the hint only renders for Plex. */
  mediaServer: 'plex' | 'jellyfin' | null = null;
  /** Seasons TMDB knows about, or null when the catalog hasn't resolved. */
  tvSeasonCount: number | null = null;

  /** Seasons to offer in the extras scope dropdown. Empty when TMDB hasn't
   * told us how many there are — the template falls back to a number box. */
  get seasonChoices(): number[] {
    if (!this.tvSeasonCount) return [];
    return Array.from({ length: this.tvSeasonCount }, (_, i) => i + 1);
  }

  readonly titleTypeOptions = TITLE_TYPE_SELECT_OPTIONS;

  @Input() title: any = null;
  /** @deprecated Do not gate form fields on this — it describes the DISC,
   * not the row. Gating on it is what made every title on a series disc
   * show the episode picker and lose its name field (#798). Use
   * isEpisodeType() / isMainMovie(). Retained only because parent
   * templates still bind it. */
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
  /** #848: true when embedded in the preview+label modal — the Preview
   * row is redundant there (the video is beside the form). */
  @Input() hidePreviewControls = false;
  /** True inside the preview modal — its action bar owns Ignore, so the
   * inline eye toggle would be redundant. */
  @Input() hideIgnoreToggle = false;

  /** Description reveals on demand (cleanup mock); auto-open when it has
   * content. Reset per row in ngOnChanges. */
  showDescriptionField = false;

  descriptionVisible(): boolean {
    return this.showDescriptionField || !!this.title?.description || !!this.title?.note;
  }

  /** Header preview affordances: the compact Play button when a clip is
   * ready, a small spinner while one is being generated. */
  headerPreviewAvailable(): boolean {
    if (!this.title) return false;
    const state = this.previewStateFn(this.title);
    if (state) return state.status === 'completed';
    return !!this.previewUrlFn(this.title);
  }

  headerPreviewPending(): boolean {
    if (!this.title) return false;
    const state = this.previewStateFn(this.title);
    return !!state && (state.status === 'queued' || state.status === 'running');
  }

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
  /** #848/#849: bound by hosts that render the shared preview+label modal.
   * When observed, Play preview delegates instead of opening the internal
   * overlay (which fixed-positions against filtered ancestors, not the
   * viewport). */
  @Output() previewOpen = new EventEmitter<any>();

  previewTitle: any | null = null;
  previewUrl: string | null = null;
  /** True when the side-by-side Compare modal is open over the editor. */
  showCompareModal = false;

  // #371 — TMDB episode picker. Observes the active workflow context's
  // primary season + tmdbEpisodeCatalog. The current title's `season`
  // (if set) overrides the disc primary. When the value resolves to a
  // TmdbEpisodeSummary[] the picker renders; sentinels hide it.
  private readonly workflow = inject(WorkflowService);
  private readonly systemSvc = inject(SystemService);

  constructor() {
    // Season count comes from TMDB. Without it we cannot list the seasons, so
    // the control degrades to a plain number box rather than disappearing.
    this.editorSubs.add(
      this.workflow.getTvSeasonCount$().subscribe((n) => {
        this.tvSeasonCount = typeof n === 'number' && n > 0 ? n : null;
      })
    );
    this.editorSubs.add(
      this.systemSvc.getMediaServerConfig().subscribe({
        next: (cfg) => { this.mediaServer = cfg?.media_server ?? null; },
        error: () => { this.mediaServer = null; },
      })
    );
  }

  readonly episodeOptions$: Observable<TmdbEpisodeSummary[] | 'loading' | 'error' | 'unavailable'> =
    this.workflow.getPrimarySeason$().pipe(
      switchMap((primary) => {
        const own = this.effectiveSeason(primary);
        // The main group is the row's own season — or, when the row is
        // already on Specials, the disc's primary season, so the user can
        // always move a title back out of Specials (seen on the RC: a
        // Siege of Lothal part set to season 0 offered Specials only).
        const season = own === 0 ? (primary || 1) : own;
        // The prefetch only loads the disc's PRIMARY season, and the getter
        // below is a pure reader — so a title on any other season resolved to
        // 'unavailable' forever and the picker never appeared. Ask for this
        // title's own season; the call is idempotent.
        this.workflow.ensureEpisodeSeasonLoaded(season);
        // Always offer the show's Specials too (#830). A feature-length
        // special the disc files at the head of a season lives in TMDB's
        // season 0; a user has no way to know that, so it has to be visible
        // next to the season's episodes. Specials are optional: if season 0
        // is missing or errors, the season's list still renders alone.
        this.workflow.ensureEpisodeSeasonLoaded(0);
        return combineLatest([
          this.workflow.getEpisodesForSeason$(season),
          this.workflow.getEpisodesForSeason$(0),
        ]).pipe(
          map(([main, specials]) => {
            if (!Array.isArray(main)) return main;
            return Array.isArray(specials) ? [...main, ...specials] : main;
          }),
        );
      }),
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

  /** Season the extras scope dropdowns are pointed at. Driven by
   * onExtraSeasonChange and by row switches (ngOnChanges) — unlike
   * episodeOptions$ above, which keys off the disc primary and does not
   * re-evaluate when this row's own season changes. */
  private readonly extraScopeSeason$ = new BehaviorSubject<number | null>(null);

  readonly extraEpisodeOptions$: Observable<TmdbEpisodeSummary[] | 'loading' | 'error' | 'unavailable'> =
    this.extraScopeSeason$.pipe(
      distinctUntilChanged(),
      switchMap((season) => {
        if (season === null || !Number.isFinite(Number(season)) || Number(season) < 0) {
          return of('unavailable' as const);
        }
        this.workflow.ensureEpisodeSeasonLoaded(Number(season));
        return this.workflow.getEpisodesForSeason$(Number(season));
      }),
    );

  /** Per-row effective season — track.season override, else disc primary. */
  private effectiveSeason(primary: number): number {
    const t = this.title?.season;
    // Unset means "use the disc's season". Checked explicitly because
    // Number(null) === 0, and 0 is now a real season (Specials, #830) —
    // the old `> 0` test hid that coincidence; `>= 0` alone would have sent
    // every untyped title to the Specials catalog.
    if (t === null || t === undefined || t === '') return primary || 1;
    const n = Number(t);
    return Number.isFinite(n) && n >= 0 ? n : (primary || 1);
  }

  /** Season 0 in TMDB terms. Drives the Specials hint under the season field. */
  get isSpecialsRow(): boolean {
    return Number(this.title?.season) === 0;
  }

  /** Episodes from season 0, for the Specials group in the picker. */
  specialsOf(opts: TmdbEpisodeSummary[]): TmdbEpisodeSummary[] {
    return (opts || []).filter(e => e.season_number === 0);
  }

  /** Episodes from the row's own season, for the main group in the picker. */
  regularOf(opts: TmdbEpisodeSummary[]): TmdbEpisodeSummary[] {
    return (opts || []).filter(e => e.season_number !== 0);
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
    // Carry buffered edits in this write; the picker's season/episode/title
    // override them (explicit pick wins over half-typed text, and a late
    // idle-flush must not overwrite the picked name).
    const pending = this.takePendingFieldsFor(this.title.title_id);
    this.title.season = ep.season_number;
    this.title.episode = ep.episode_number;
    this.title.title = ep.name;
    this.emitFieldPatch({
      ...pending,
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
    // #849: when a parent listens, it owns the overlay (portaled to body so
    // fixed-positioning isn't hijacked by backdrop-filter ancestors). The
    // internal overlay remains for hosts that don't bind (title-modal).
    if (this.previewOpen.observed) {
      this.previewOpen.emit(this.title);
      return;
    }
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
    this.bufferFieldPatch({ description: normalized });
    this.titleChanged.emit();
  }

  markAsIgnore(): void {
    if (!this.title) return;
    // Carry (or deliberately drop) any buffered typed fields in this same
    // write — see takePendingFieldsFor.
    const pending = this.takePendingFieldsFor(this.title.title_id);
    const currentType = (this.title.type || '').toString().toLowerCase();
    if (currentType === 'ignore') {
      this.title.type = '';
      // Un-ignore: clear only type. Row-level markAsIgnore matches this
      // (title-label.component.ts:733).
      this.emitFieldPatch({ ...pending, type: null });
    } else {
      this.title.type = 'ignore';
      this.clearIgnoredFields();
      // Ignore: mirror clearIgnoredFields() + edition null so the backend
      // matches what row-level markAsIgnore sends (title-label.component.ts:724-731).
      // Nulls override pending text: ignore clears.
      this.emitFieldPatch({
        ...pending,
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

  /** True when "Un-ignore" would actually do something.
   *
   * Un-ignore clears only the USER's type; the effective type is
   * `user_type ?? auto_type`. On a row automated detection flagged
   * (`auto_type === 'ignore'`) clearing the user opinion just reveals the
   * auto opinion again, so the row stays ignored — whether or not the user
   * has since confirmed it. The only way off an automatic ignore is to pick
   * a type, which the banner above the field already says. Offering a button
   * that silently does nothing is what confused users. */
  get canUnignore(): boolean {
    if (!this.title || !this.isIgnored()) return false;
    return (this.title.auto_type || '').toString().toLowerCase() !== 'ignore';
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
    // Drop any buffered typed fields into this write (they're about to be
    // cleared anyway — but they must not flush later as a separate write).
    const pending = this.takePendingFieldsFor(this.title.title_id);
    this.title.user_type = 'ignore';
    this.title.type = 'ignore';
    // Same PATCH shape as markAsIgnore going TO ignore — backend routes
    // type-writes through set_title_type(source='user') which flips user_type
    // automatically, so we don't have to include it explicitly.
    this.emitFieldPatch({
      ...pending,
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
    // Picked, not typed — saves immediately. A name typed just before may
    // still be buffered; it rides in THIS write rather than flushing as a
    // separate one (two same-tick writes to one row carry the same
    // base_seq — one of them always loses).
    if (!this.title) return;
    const pending = this.takePendingFieldsFor(this.title.title_id);
    this.flushPendingFieldEdits(); // other rows' leftovers, if any
    const prevType = this.title.type;
    this.title.type = value;
    const normalizedType = value === '' ? null : value;
    if (this.isIgnored()) {
      this.clearIgnoredFields();
      // Nulls intentionally override any pending text: ignore clears.
      this.emitFieldPatch({
        ...pending,
        type: normalizedType,
        title: null,
        description: null,
        season: null,
        episode: null,
        edition: null,
      });
    } else {
      // An extra on a single-season disc belongs to that season, same as the
      // episodes beside it — take it from what's actually labelled rather than
      // the disc-level hint. On a disc that spans seasons there is no safe
      // default, so leave it blank and let the dropdown ask.
      const patch: Record<string, unknown> = { ...pending, type: normalizedType };
      const implied = this.impliedExtraSeason;
      if (this.isSeasonScopableExtra() && this.title.season === null && implied !== null) {
        this.title.season = implied;
        patch['season'] = implied;
      }
      if (isBackdropTitleType(value)) {
        // Backdrop's name is fixed — overrides any pending typed name.
        this.title.title = BACKDROP_TITLE_NAME;
        patch['title'] = BACKDROP_TITLE_NAME;
      } else if (isBackdropTitleType(prevType) && this.title.title === BACKDROP_TITLE_NAME) {
        // Leaving Backdrop: the forced name wasn't user data.
        this.title.title = '';
        patch['title'] = null;
      }
      this.emitFieldPatch(patch as any);
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
    if (reason === 'play_all_wrapper') return 'Play All of titles listed separately on this disc';
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
    if (this.isEpisodeType()) {
      const season = Number(this.title.season);
      const episode = Number(this.title.episode);
      if (Number.isFinite(season) && Number.isFinite(episode) && season > 0 && episode > 0) {
        const s = String(season).padStart(2, '0');
        const e = String(episode).padStart(2, '0');
        // Mirrors format_episode_designator / format_part_suffix in
        // core/disc.py. Still a cue rather than the canonical path, but the
        // multi-part suffixes are the whole point of the Layout control —
        // without them the setting has no visible effect until postprocess.
        let designator = `S${s}E${e}`;
        const end = Number(this.title.episode_end);
        if (Number.isFinite(end) && end > episode) {
          designator += `-E${String(end).padStart(2, '0')}`;
        }
        const part = Number(this.title.part);
        const partSuffix = Number.isFinite(part) && part > 0 ? ` - part${part}` : '';
        return `${designator} - ${safeName}${partSuffix}.mkv`;
      }
      return `${safeName}.mkv`;
    }
    // Series extras: show the scope in the preview — the folder (or, for a
    // Plex episode-level extra, the filename attachment) is the whole point
    // of the Belongs to control.
    if (this.isSeasonScopableExtra()) {
      const typeKey = (this.title.type || '').toString();
      const folders = EXTRAS_FOLDERS[typeKey];
      if (folders) {
        const ms = this.mediaServer || 'plex';
        const seasonN = this.extraSeason;
        const episodeN = this.extraEpisode;
        if (seasonN !== null && episodeN !== null && ms !== 'jellyfin') {
          const ref = this.siblingEpisodeName(seasonN, episodeN);
          if (ref) {
            const ss = String(seasonN).padStart(2, '0');
            const ee = String(episodeN).padStart(2, '0');
            const suffix = PLEX_EPISODE_EXTRA_SUFFIXES[typeKey] || 'other';
            return `Season ${ss}/… - s${ss}e${ee} - ${ref}-${safeName}-${suffix}.mkv`;
          }
        }
        const folder = ms === 'jellyfin' ? folders[1] : folders[0];
        if (seasonN !== null) {
          return `Season ${String(seasonN).padStart(2, '0')}/${folder}/${safeName}.mkv`;
        }
        return `${folder}/${safeName}.mkv`;
      }
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

  /** True when THIS title is an episode — not when the disc happens to be a series.
   *
   * The form used to gate on the disc-level `isSeries`, so every row on a
   * series disc got the episode picker and no name field — an extra could
   * not be named at all (#798). Mirrors `showSeasonEpisode()` in
   * title-label.component.ts minus its disc-level short-circuit, and matches
   * the completeness rules in title-label-stats.util.ts, which were already
   * per-type.
   */
  isEpisodeType(): boolean {
    return (this.title?.type || '').toString().toLowerCase() === 'episode';
  }

  /** True when this title carries an edition — a main movie, per-type. */
  isMainMovie(): boolean {
    return (this.title?.type || '').toString().toLowerCase() === 'mainmovie';
  }

  /** Backdrop's name is fixed to "Backdrop" — the name field locks. */
  isBackdropLocked(): boolean {
    return isBackdropTitleType(this.title?.type);
  }

  /** True when this row is a series extra that can be scoped to one season.
   *
   * Both Plex and Jellyfin read an extras folder nested inside a season folder
   * as belonging to that season — `Season 03/Behind The Scenes/…` — and
   * `core.disc.compute_expected_path` already emits that shape whenever an
   * extra carries a season. Without a control the season could never be set,
   * so every disc extra landed at show level.
   */
  isSeasonScopableExtra(): boolean {
    if (!this.isSeries || this.isIgnored()) return false;
    const t = (this.title?.type || '').toString().toLowerCase();
    if (!t || t === 'episode' || t === 'mainmovie') return false;
    return true;
  }

  private _seasonScanKey: unknown = null;
  private _seasonScanResult: number[] = [];

  /** Distinct seasons across everything labelled on this disc.
   *
   * Memoised on the titles array identity — this is read from the template on
   * every change-detection pass, and the component runs Default CD.
   */
  get discSeasons(): number[] {
    const titles = this.workflow.getCurrentContext()?.titles ?? [];
    if (titles === this._seasonScanKey) return this._seasonScanResult;
    const seen = new Set<number>();
    for (const t of titles as any[]) {
      const type = (t?.type || '').toString().toLowerCase();
      if (!type || type === 'ignore') continue;
      const raw = t?.season;
      if (raw === null || raw === undefined || raw === '') continue;
      const n = Number(raw);
      if (!Number.isNaN(n)) seen.add(n);
    }
    this._seasonScanKey = titles;
    this._seasonScanResult = Array.from(seen).sort((a, b) => a - b);
    return this._seasonScanResult;
  }

  /** True when everything labelled on this disc sits in one season.
   *
   * Then an extra needs no decision — it belongs to that season, same as the
   * episodes beside it — so the control is replaced by a statement of where
   * the file lands. Only a disc that genuinely spans seasons (a boxset bonus
   * disc, where TheDiscDB tags extras individually across seasons) has an
   * ambiguity worth asking about.
   */
  get discIsSingleSeason(): boolean {
    return this.discSeasons.length === 1;
  }

  /** The season an extra takes automatically on a single-season disc. */
  get impliedExtraSeason(): number | null {
    return this.discIsSingleSeason ? this.discSeasons[0] : null;
  }

  /** Season this extra belongs to; null means the whole series. */
  get extraSeason(): number | null {
    const v = this.title?.season;
    return v === null || v === undefined || v === '' ? null : Number(v);
  }

  /** Season this extra belongs to; changing it invalidates any episode
   * choice, since an episode only means something inside its season. Dropdown
   * picks save immediately (same contract as onTypeChange). */
  onExtraSeasonChange(value: unknown): void {
    if (!this.title) return;
    const raw = value === '' || value === null || value === undefined ? null : Number(value);
    const normalized = raw === null || Number.isNaN(raw) || raw < 0 ? null : raw;
    const pending = this.takePendingFieldsFor(this.title.title_id);
    const patch: Record<string, unknown> = { ...pending, season: normalized };
    if (normalized !== this.extraSeason && this.title.episode != null) {
      this.title.episode = null;
      patch['episode'] = null;
    }
    this.title.season = normalized;
    this.extraScopeSeason$.next(normalized);
    this.emitFieldPatch(patch as any);
    this.titleChanged.emit();
  }

  /** Episode this extra is attached to; null means all of the season. */
  get extraEpisode(): number | null {
    const v = this.title?.episode;
    return v === null || v === undefined || v === '' ? null : Number(v);
  }

  onExtraEpisodeChange(value: unknown): void {
    if (!this.title) return;
    const raw = value === '' || value === null || value === undefined ? null : Number(value);
    const normalized = raw === null || Number.isNaN(raw) || raw < 1 ? null : raw;
    const pending = this.takePendingFieldsFor(this.title.title_id);
    this.title.episode = normalized;
    this.emitFieldPatch({ ...pending, episode: normalized } as any);
    this.titleChanged.emit();
  }

  /** Title of the sibling Episode row at (season, episode) on this disc, or
   * null. Plex attaches an episode-level extra by filename prefix built from
   * exactly this — mirrors the backend's sibling lookup in _rename_series. */
  siblingEpisodeName(season: number, episode: number): string | null {
    const titles = this.workflow.getCurrentContext()?.titles ?? [];
    for (const t of titles as any[]) {
      if ((t?.type || '').toString().toLowerCase() !== 'episode') continue;
      if (Number(t?.season) === season && Number(t?.episode) === episode) {
        const name = (t?.title || '').toString().trim();
        if (name) return name;
      }
    }
    return null;
  }

  /** The three shapes an episode can take on disc (#796).
   *
   * Derived from the stored fields rather than stored itself, so there is
   * one source of truth: `part` set means the disc split one episode across
   * files, `episode_end` set means one file covers several episodes.
   */
  get episodeLayout(): 'single' | 'split' | 'span' {
    if (this.title?.part != null) return 'split';
    if (this.title?.episode_end != null) return 'span';
    return 'single';
  }

  /** Layout is a picked control — it writes immediately, so it must absorb
   * any buffered typed fields rather than let them flush as a second
   * same-tick write (both would carry the same base_seq and one would lose).
   * Same contract as onTypeChange / onEpisodePicked. */
  onEpisodeLayoutChange(value: string): void {
    if (!this.title) return;
    const pending = this.takePendingFieldsFor(this.title.title_id);
    this.flushPendingFieldEdits();
    if (value === 'split') {
      this.title.part = this.title.part ?? 1;
      this.title.part_of = this.title.part_of ?? 2;
      this.title.episode_end = null;
    } else if (value === 'span') {
      const ep = Number(this.title.episode);
      this.title.episode_end = this.title.episode_end ?? (Number.isFinite(ep) ? ep + 1 : null);
      this.title.part = null;
      this.title.part_of = null;
    } else {
      this.title.part = null;
      this.title.part_of = null;
      this.title.episode_end = null;
    }
    this.emitFieldPatch({
      ...pending,
      part: this.title.part ?? null,
      part_of: this.title.part_of ?? null,
      episode_end: this.title.episode_end ?? null,
    });
    this.titleChanged.emit();
  }

  onPartChange(value: any): void {
    if (!this.title) return;
    const num = value === null || value === '' ? null : Number(value);
    this.bufferFieldPatch({ part: Number.isFinite(num as number) ? (num as number) : null });
    this.titleChanged.emit();
  }

  onPartOfChange(value: any): void {
    if (!this.title) return;
    const num = value === null || value === '' ? null : Number(value);
    this.bufferFieldPatch({ part_of: Number.isFinite(num as number) ? (num as number) : null });
    this.titleChanged.emit();
  }

  onEpisodeEndChange(value: any): void {
    if (!this.title) return;
    const num = value === null || value === '' ? null : Number(value);
    this.bufferFieldPatch({ episode_end: Number.isFinite(num as number) ? (num as number) : null });
    this.titleChanged.emit();
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

  /** Typed-field edits awaiting flush, keyed by title id.
   *
   *  These fields used to PATCH on every ngModelChange — literally per
   *  keystroke, as the previous comment here proudly noted. Each response
   *  echoed the server's value back into the ngModel-bound input, so a burst
   *  of late echoes visibly re-typed the field character by character and
   *  could drop the last few: the final keystrokes' write had not returned
   *  when an older echo landed.
   *
   *  Typed fields now buffer and flush once on blur. Fields the user *picks*
   *  (the type dropdown) still save immediately — one deliberate action, one
   *  write. */
  private pendingFieldEdits = new Map<string, Partial<TitlePatchRequest>>();
  private autosaveTimer: any = null;

  /** Idle delay before a buffered edit is written. Long enough that a normal
   *  typing burst produces ONE write instead of one per character, short
   *  enough that the edit is durable without the user doing anything.
   *
   *  Blur alone is not a sufficient trigger: if focus never leaves the field
   *  — the editor re-renders, the user tabs away in a way that doesn't fire
   *  blur, the pane closes — the edit is silently never saved, and the next
   *  refresh shows the last value that did save. That regression is exactly
   *  what a blur-only design produced. */
  private static readonly AUTOSAVE_IDLE_MS = 700;

  private bufferFieldPatch(fields: Partial<TitlePatchRequest>): void {
    const titleId = this.title?.title_id;
    if (!titleId) return;
    const existing = this.pendingFieldEdits.get(titleId) || {};
    this.pendingFieldEdits.set(titleId, { ...existing, ...fields });
    this.scheduleAutosave();
  }

  /** Restart the idle timer. Each keystroke pushes the write out, so a burst
   *  of typing collapses into a single request after the user pauses. */
  private scheduleAutosave(): void {
    if (this.autosaveTimer) clearTimeout(this.autosaveTimer);
    this.autosaveTimer = setTimeout(() => {
      this.autosaveTimer = null;
      this.flushPendingFieldEdits();
    }, TitleEditorComponent.AUTOSAVE_IDLE_MS);
  }

  /** Send everything buffered. Safe to call repeatedly — a flush with nothing
   *  pending is a no-op, so blur on an untouched field costs no request. */
  flushPendingFieldEdits(): void {
    if (this.autosaveTimer) {
      clearTimeout(this.autosaveTimer);
      this.autosaveTimer = null;
    }
    if (this.pendingFieldEdits.size === 0) return;
    const pending = this.pendingFieldEdits;
    this.pendingFieldEdits = new Map();
    pending.forEach((fields, titleId) => {
      this.titlePatched.emit({ title_id: titleId, ...fields } as TitlePatchRequest);
    });
  }

  /** Remove and return the buffered edits for one title so an immediate
   *  write (type pick, ignore, episode pick) can carry them in the SAME
   *  request. Users don't wait for autosave — they type a name and go
   *  straight for the dropdown. Flushing the buffer as a *separate*
   *  request races the immediate one: both leave with the same base_seq,
   *  so one is guaranteed a stale-seq conflict. One request can't race
   *  itself. Also stops a late idle-flush from resurrecting a typed name
   *  onto a row the immediate write just cleared (ignore). */
  private takePendingFieldsFor(titleId: string | null | undefined): Partial<TitlePatchRequest> {
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

  /** ngModelChange handlers for direct-bound fields. They update the bound
   *  model (so typing stays responsive) and buffer the write for blur. */
  onTitleNameChange(value: any): void {
    if (!this.title) return;
    const normalized = value === '' ? null : value;
    this.bufferFieldPatch({ title: normalized });
    this.titleChanged.emit();
  }

  onSeasonChange(value: any): void {
    if (!this.title) return;
    const num = value === null || value === '' ? null : Number(value);
    const normalized = Number.isFinite(num) ? num : null;
    this.bufferFieldPatch({ season: normalized as number | null });
    this.titleChanged.emit();
  }

  onEpisodeChange(value: any): void {
    if (!this.title) return;
    const num = value === null || value === '' ? null : Number(value);
    const normalized = Number.isFinite(num) ? num : null;
    this.bufferFieldPatch({ episode: normalized as number | null });
    this.titleChanged.emit();
  }

  onEditionChange(value: any): void {
    if (!this.title) return;
    const normalized = value === '' ? null : value;
    this.bufferFieldPatch({ edition: normalized });
    this.titleChanged.emit();
  }
}
