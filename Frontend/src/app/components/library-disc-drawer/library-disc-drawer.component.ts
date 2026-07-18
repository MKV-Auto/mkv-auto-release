/**
 * library-disc-drawer — Phase 4 of the Library redesign (#500).
 *
 * The slide-over surface for **title-level work**. Opens when a release
 * card's disc row is clicked. Mobile = full-screen overlay; desktop =
 * right-rail at ~480px.
 *
 * Sections (top → bottom):
 *   1. Header — hierarchy breadcrumb + close button.
 *   2. Disc metadata edit form — name, format (Blu-Ray / UHD / DVD).
 *   3. Title list — virtualized via CDK virtual-scroll. Discs with
 *      100+ titles exist (Star Wars: Phantom Menace ~230), so a flat
 *      ngFor would gore the DOM.
 *   4. Per-title row — title / type / season / episode / edition /
 *      description fields + file_path display + greyed [Rename⊘] slot
 *      reserved for v2 (#381).
 *
 * Optimistic-edit pattern: edits write locally on blur, PATCH backend
 * via `WorkflowService.patchDiscTitle` (which already carries the #383
 * stale_seq toast + refetch on conflict). Disc-level edits hit
 * `MetadataService.patchDiscRecord`.
 *
 * Phase 4 scope:
 *   - Doesn't yet show the DiscDB chip (Phase 5).
 *   - The [Rename⊘] button is disabled with a "Coming in v2" tooltip
 *     and emits nothing — wired in v2 against the existing
 *     `/releases/disc/{id}/rename` route shipped via #325.
 */
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
  inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ScrollingModule } from '@angular/cdk/scrolling';
import { Subject, debounceTime, takeUntil } from 'rxjs';

import {
  MetadataService,
  DiscSummary,
  DiscRecord,
  ReleaseSummary,
  TitleSummary,
} from '../../services/metadata.service';
import {
  WorkflowService,
  TitlePatchRequest,
} from '../../services/workflow.service';
import {
  ToastService,
  formatHttpErrorDetail,
} from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';
import { DiscDbService } from '../../services/discdb.service';

type DiscFormat = 'Blu-Ray' | 'UHD' | 'DVD' | null;

/**
 * Title-type sort priority for the drawer's titles list (#599).
 * Lower number = sorted earlier. `Ignore` lands at the very end so a
 * contiguous-slice filter ("drop everything past index of first
 * ignored row") suffices to hide them without rebuilding the array.
 * Persisted values can be CamelCase (`MainMovie`) or space-separated
 * (`Main Movie`) depending on whether they came from the dropdown or
 * a backend default — normalize both into one bucket.
 */
function normalizeType(type: string): string {
  return (type || '').toLowerCase().replace(/[\s_-]+/g, '');
}

function isIgnored(type: string): boolean {
  return normalizeType(type) === 'ignore';
}

const PRIORITY_UNSET = 100;
const PRIORITY_UNKNOWN_NAMED = 41;
const PRIORITY_IGNORED = 1000;

function typePriority(type: string): number {
  const t = normalizeType(type);
  if (!t) return PRIORITY_UNSET;
  if (t === 'ignore') return PRIORITY_IGNORED;
  if (t === 'mainmovie' || t === 'main' || t === 'movie') return 0;
  if (t === 'episode') return 10;
  if (t === 'featurette') return 20;
  if (t === 'interview') return 21;
  if (t === 'behindthescenes') return 22;
  if (t === 'deletedscene') return 23;
  if (t === 'extra') return 24;
  if (t === 'trailer') return 25;
  if (t === 'clip') return 26;
  if (t === 'sample') return 27;
  if (t === 'scene') return 28;
  if (t === 'short') return 29;
  if (t === 'thememusic') return 30;
  if (t === 'backdrop') return 31;
  if (t === 'other') return 40;
  return PRIORITY_UNKNOWN_NAMED;
}

/** Sort rows by type priority, then by duration descending so the
 * longest title in each bucket leads. For Episode rows we override
 * with season/episode ascending when both are set so the natural
 * series order falls out. */
function sortTitleRowsForDisplay(rows: TitleRowState[]): TitleRowState[] {
  return [...rows].sort((a, b) => {
    const pa = typePriority(a.type);
    const pb = typePriority(b.type);
    if (pa !== pb) return pa - pb;
    // Episode bucket: order by season/episode if both have them.
    if (pa === 10 && a.season != null && b.season != null) {
      if (a.season !== b.season) return a.season - b.season;
      const ae = a.episode ?? 0;
      const be = b.episode ?? 0;
      if (ae !== be) return ae - be;
    }
    return (b.duration ?? 0) - (a.duration ?? 0);
  });
}

interface EditableDiscFields {
  disc_name: string | null;
  disc_format: DiscFormat;
}

/** Local row state: tracks the user's in-progress title edit + the
 * last-known title_seq from the server (refreshed on every successful
 * PATCH response via the existing WorkflowService title-seq cache). */
interface TitleRowState {
  title_id: string;
  title: string;
  type: string;
  season: number | null;
  episode: number | null;
  edition: string;
  description: string;
  // Read-only display fields.
  file_path: string | null;
  file_path_stage: string | null;
  duration: number | null;
  // #500 Phase 5 — passive "Contributed to DiscDB" indicator. Reserved
  // for v2 auto-stream; the backend doesn't project this column yet, so
  // it's always falsy in v1. When v2 lands, the chip lights up
  // automatically — no template change required.
  contributed_to_discdb: boolean;
}

@Component({
  selector: 'app-library-disc-drawer',
  standalone: true,
  imports: [CommonModule, FormsModule, ScrollingModule],
  changeDetection: ChangeDetectionStrategy.Default,
  templateUrl: './library-disc-drawer.component.html',
  styleUrls: ['./library-disc-drawer.component.scss'],
})
export class LibraryDiscDrawerComponent implements OnChanges, OnDestroy {
  private readonly metadataSvc = inject(MetadataService);
  private readonly workflowSvc = inject(WorkflowService);
  private readonly toast = inject(ToastService);
  private readonly logger = inject(LoggerService);
  private readonly discdbSvc = inject(DiscDbService);

  /** Open trigger — set to a DiscSummary when the parent wants the drawer
   * shown, null to close it. */
  @Input() disc: DiscSummary | null = null;
  /** Release context for the breadcrumb header. */
  @Input() release: ReleaseSummary | null = null;
  @Input() releaseDisplayName: string | null = null;

  @Output() closed = new EventEmitter<void>();
  /** Emits when the disc-level form persists, so the parent can refresh
   * its in-memory copy. Title-level edits don't emit — WorkflowService's
   * patchDiscTitle handles the in-flight state and conflict toast. */
  @Output() discUpdated = new EventEmitter<DiscRecord>();

  private readonly destroy$ = new Subject<void>();

  loading = false;
  loadError: string | null = null;
  saving = false;
  discRecord: DiscRecord | null = null;
  /** Working copy of the disc-level form. */
  discForm: EditableDiscFields = { disc_name: null, disc_format: null };
  /** All title rows, presorted by type priority then duration desc.
   * `visibleTitles` is what the *cdkVirtualFor* iterates — it slices off
   * the trailing ignored bucket unless `showIgnored` is on. */
  allTitles: TitleRowState[] = [];
  /** UX: 57/97 titles on a typical Blu-ray are auto-ignored. Hiding them
   * by default surfaces the 2 MainMovies + named extras the user actually
   * needs to edit. `Show ignored (57)` button reveals them; users can
   * still un-ignore any false positive. (#599) */
  showIgnored = false;

  /** Single-row inline editor (#601 redesign refinement). At most one row
   * is in edit mode at a time; the rest display plain text + an Edit
   * button. Setting this to null collapses everyone back to display mode.
   * The fixed itemSize (#531 contract) covers both modes — display mode
   * just has more whitespace than edit mode. */
  editingTitleId: string | null = null;

  /** Compatibility shim: legacy template binding. Now resolves to
   * `visibleTitles`. Keep for any external references. */
  get titles(): TitleRowState[] {
    return this.visibleTitles;
  }

  /** Number of titles whose effective type is `Ignore`. Drives the
   * toggle button label. */
  get ignoredCount(): number {
    return this.allTitles.filter((r) => isIgnored(r.type)).length;
  }

  /** Number of non-ignored titles. */
  get visibleCount(): number {
    return this.allTitles.length - this.ignoredCount;
  }

  /** Slice fed to the virtual scroll. When `showIgnored` is false, the
   * ignored tail is dropped — the sort puts ignored last so a contiguous
   * slice is enough. */
  get visibleTitles(): TitleRowState[] {
    if (this.showIgnored) return this.allTitles;
    return this.allTitles.filter((r) => !isIgnored(r.type));
  }

  toggleShowIgnored(): void {
    this.showIgnored = !this.showIgnored;
  }

  /** Helper exposed for the template's per-row dimming. */
  isRowIgnored(row: TitleRowState): boolean {
    return isIgnored(row.type);
  }

  /** Backdrop / theme-video rows are auto-named — Plex / Jellyfin just
   * need the file under `backdrops/` to play it as the movie's ambient
   * loop. Hide title / season / episode / edition inputs on these rows
   * so the user doesn't scroll past empty fields they'd never fill
   * (#602). Description stays available for optional notes. */
  isBackdrop(row: TitleRowState): boolean {
    return normalizeType(row.type) === 'backdrop';
  }

  /** True when the given row is the one currently being edited inline. */
  isEditingRow(row: TitleRowState): boolean {
    return this.editingTitleId === row.title_id;
  }

  /** Enter edit mode for a single row (collapsing any other open editor). */
  startEditing(row: TitleRowState): void {
    if (this.isFinalized) return;
    this.editingTitleId = row.title_id;
  }

  /** Exit edit mode. Pending debounced PATCHes flush as normal — we don't
   * trigger an immediate save here because the existing per-field
   * `onTitleFieldChange` debounce already handles persistence. */
  stopEditing(): void {
    this.editingTitleId = null;
  }

  /** Compact subline shown in display mode: edition · description.
   * Returns `null` when both are empty so the template can skip the row
   * and stay tidy. */
  rowSubline(row: TitleRowState): string | null {
    const parts: string[] = [];
    if (row.edition) parts.push(row.edition);
    if (row.description) parts.push(row.description);
    return parts.length ? parts.join(' · ') : null;
  }

  /** Type-tone palette for the per-row chip + left-edge accent (#601).
   * Mirrors the sort-priority buckets in `typePriority()` so a row's chip
   * color matches where it sorts. Returns `null` for unset rows (no chip,
   * no accent — keeps the row visually quiet) and `null` for Ignore (the
   * row already dims via `.is-ignored`). */
  typeChipTone(type: string): string | null {
    const t = normalizeType(type);
    if (!t || t === 'ignore') return null;
    if (t === 'mainmovie' || t === 'main' || t === 'movie') return 'cyan';
    if (t === 'episode') return 'indigo';
    if (
      t === 'featurette' ||
      t === 'interview' ||
      t === 'behindthescenes' ||
      t === 'deletedscene' ||
      t === 'extra'
    ) {
      return 'amber';
    }
    if (t === 'trailer' || t === 'clip' || t === 'sample' || t === 'scene' || t === 'short') {
      return 'blue';
    }
    if (t === 'thememusic' || t === 'backdrop') return 'purple';
    return 'slate'; // Other / unknown named
  }

  /** Short display label for the row's type chip (#601). Returns the
   * dropdown label that matches the persisted value, normalised so both
   * CamelCase (`MainMovie`) and space-separated (`Main Movie`) reach the
   * same chip text. `null` for unset rows means "render no chip". */
  typeChipLabel(type: string): string | null {
    const t = normalizeType(type);
    if (!t) return null;
    const match = this.titleTypeOptions.find((opt) => normalizeType(opt.value) === t);
    return match?.label || null;
  }

  /** Per-title debounce subjects — fires PATCH after 300ms idle on the
   * editable input. We coalesce same-title rapid edits into one PATCH. */
  private readonly titleEdits$ = new Map<string, Subject<void>>();

  readonly titleTypeOptions = [
    { value: '', label: '(unset)' },
    { value: 'Main Movie', label: 'Main Movie' },
    { value: 'Episode', label: 'Episode' },
    { value: 'Backdrop', label: 'Backdrop' },
    { value: 'Behind The Scenes', label: 'Behind The Scenes' },
    { value: 'Clip', label: 'Clip' },
    { value: 'Deleted Scene', label: 'Deleted Scene' },
    { value: 'Extra', label: 'Extra' },
    { value: 'Featurette', label: 'Featurette' },
    { value: 'Interview', label: 'Interview' },
    { value: 'Other', label: 'Other' },
    { value: 'Sample', label: 'Sample' },
    { value: 'Scene', label: 'Scene' },
    { value: 'Short', label: 'Short' },
    { value: 'Theme Music', label: 'Theme Music' },
    { value: 'Trailer', label: 'Trailer' },
    { value: 'Ignore', label: 'Ignore' },
  ];

  /** Breadcrumb shown in the header — release / disc N. */
  get breadcrumb(): string {
    const rel = this.releaseDisplayName ?? this.release?.name ?? '';
    if (!this.disc) return rel;
    const num = this.disc.disc_number;
    const name = this.disc.disc_name?.trim();
    let discPart: string;
    if (num && name) discPart = `Disc ${num}: ${name}`;
    else if (num) discPart = `Disc ${num}`;
    else if (name) discPart = name;
    else discPart = 'Disc';
    return rel ? `${rel} / ${discPart}` : discPart;
  }

  get isFinalized(): boolean {
    return this.discRecord?.finalized === true || this.disc?.finalized === true;
  }

  get isSeries(): boolean {
    const t = (this.release?.type ?? 'movie').toLowerCase();
    return t === 'series' || t === 'tv';
  }

  /** #86 — export only makes sense for DiscDB misses: hits came FROM
   * TheDiscDB, so there is nothing new to contribute. */
  get canExportDiscDbBundle(): boolean {
    return this.disc?.discdb_hit !== true;
  }

  exporting = false;

  async exportDiscDbBundle(): Promise<void> {
    const discId = this.disc?.id;
    if (!discId || this.exporting) return;
    this.exporting = true;
    try {
      const bundle = await this.discdbSvc.getContributionBundle(discId);
      const slug = bundle.release_slug || 'release';
      const discNum = bundle.disc_number ?? 1;
      const filename = `discdb-bundle-${slug}-disc${String(discNum).padStart(2, '0')}.json`;
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      this.toast.show(
        'DiscDB bundle exported — see the contribution guide for how to submit it upstream',
        'success',
        6000,
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Bundle export failed';
      this.toast.show(msg, 'error', 6000);
      this.logger.warn('[LibraryDiscDrawer] bundle export failed', err);
    } finally {
      this.exporting = false;
    }
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['disc']) {
      // disc input flipped — load (or clear) the drawer body.
      if (this.disc?.id) {
        this.loadDisc(this.disc.id);
      } else {
        this.discRecord = null;
        this.allTitles = [];
        this.showIgnored = false;
        this.titleEdits$.forEach((s) => s.complete());
        this.titleEdits$.clear();
      }
    }
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
    this.titleEdits$.forEach((s) => s.complete());
    this.titleEdits$.clear();
  }

  close(): void {
    this.closed.emit();
  }

  /** Esc to close — bound from the host template. */
  onKeydown(ev: KeyboardEvent): void {
    if (ev.key === 'Escape') this.close();
  }

  private loadDisc(discId: string): void {
    this.loading = true;
    this.loadError = null;
    this.metadataSvc.getDiscRecord(discId)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (record) => {
          this.discRecord = record;
          this.discForm = {
            disc_name: record.disc_name ?? null,
            disc_format: this.normalizeFormat(record.format ?? null),
          };
          this.allTitles = sortTitleRowsForDisplay(this.buildTitleRowsFromRecord(record));
          this.loading = false;
        },
        error: (err) => {
          this.loadError = formatHttpErrorDetail(err) || 'Failed to load disc';
          this.loading = false;
          this.logger.warn('[LibraryDiscDrawer] getDiscRecord failed', err);
        },
      });
  }

  private normalizeFormat(raw: string | null | undefined): DiscFormat {
    if (!raw) return null;
    const v = raw.toString().trim().toUpperCase();
    if (v === 'UHD' || v === '4K UHD' || v === '4K') return 'UHD';
    if (v === 'BLU-RAY' || v === 'BLURAY' || v === 'BD') return 'Blu-Ray';
    if (v === 'DVD') return 'DVD';
    return null;
  }

  private buildTitleRowsFromRecord(record: DiscRecord): TitleRowState[] {
    const rows: TitleRowState[] = [];
    for (const t of (record.titles ?? []) as any[]) {
      const titleId = t.title_id ?? t.id;
      if (!titleId) continue;
      rows.push({
        title_id: String(titleId),
        title: (t.title ?? '').toString(),
        type: (t.type ?? '').toString(),
        season: t.season ?? null,
        episode: t.episode ?? null,
        edition: (t.edition ?? '').toString(),
        description: (t.description ?? '').toString(),
        file_path: t.file_path ?? null,
        file_path_stage: t.file_path_stage ?? null,
        duration: t.duration ?? null,
        contributed_to_discdb: t.contributed_to_discdb === true,
      });
    }
    return rows;
  }

  /** Persist the disc-level form. Tight payload (only changed fields). */
  saveDiscForm(): void {
    const id = this.disc?.id;
    if (!id || !this.discRecord) return;
    const payload: Record<string, unknown> = {};
    if ((this.discForm.disc_name ?? '') !== (this.discRecord.disc_name ?? '')) {
      payload['disc_name'] = this.discForm.disc_name;
    }
    if ((this.discForm.disc_format ?? null) !==
        (this.normalizeFormat(this.discRecord.format) ?? null)) {
      payload['disc_format'] = this.discForm.disc_format;
    }
    if (Object.keys(payload).length === 0) return;

    this.saving = true;
    this.metadataSvc.patchDiscRecord(id, payload)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (record) => {
          this.discRecord = record;
          this.saving = false;
          this.discUpdated.emit(record);
        },
        error: (err) => {
          this.saving = false;
          this.toast.show(formatHttpErrorDetail(err) || 'Disc save failed', 'error', 5000);
        },
      });
  }

  selectFormat(fmt: DiscFormat): void {
    if (this.isFinalized) return;
    this.discForm.disc_format = fmt;
    this.saveDiscForm();
  }

  /** Per-title field change — debounce 300ms then PATCH. */
  onTitleFieldChange(row: TitleRowState): void {
    if (this.isFinalized) return;
    let subj = this.titleEdits$.get(row.title_id);
    if (!subj) {
      subj = new Subject<void>();
      subj
        .pipe(debounceTime(300), takeUntil(this.destroy$))
        .subscribe(() => this.flushTitleEdit(row.title_id));
      this.titleEdits$.set(row.title_id, subj);
    }
    subj.next();
  }

  /** Build a TitlePatchRequest from the row + send via WorkflowService.
   * WorkflowService auto-fills title_seq from its cache (#383); stale
   * conflicts toast + refetch automatically. */
  private flushTitleEdit(titleId: string): void {
    // Search the canonical store (not the filtered slice) so PATCH still
    // works on ignored rows the user revealed via the toggle.
    const row = this.allTitles.find((t) => t.title_id === titleId);
    if (!row || !this.disc?.id) return;
    const patch: TitlePatchRequest = {
      title_id: titleId,
      title: row.title,
      type: row.type || null,
      season: row.season ?? null,
      episode: row.episode ?? null,
      edition: row.edition || null,
      description: row.description || null,
    };
    this.workflowSvc.patchDiscTitle(this.disc.id, patch)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: () => {/* WorkflowService updates its own caches */},
        error: (err) => {
          // WorkflowService surfaces stale_seq via its own toast (#383).
          // We only handle hard HTTP errors here.
          this.logger.warn('[LibraryDiscDrawer] title patch failed', { titleId, err });
        },
      });
  }

  /** Pretty file_path label — strips the long /data/mkvauto/data/jobs/<uuid>/
   * prefix in transient files, keeps the library-relative tail. */
  formatFilePath(row: TitleRowState): string {
    if (!row.file_path) return '';
    const path = row.file_path;
    const jobMarker = '/transient/';
    const ix = path.indexOf(jobMarker);
    return ix >= 0 ? '…' + path.slice(ix + jobMarker.length - 1) : path;
  }

  /** Where-the-bytes-landed label (#380). transfer = at destination;
   * postprocess = in transient (not yet transferred); rip = raw rip dir. */
  filePathStageLabel(row: TitleRowState): string {
    switch (row.file_path_stage) {
      case 'transfer': return 'At destination';
      case 'postprocess': return 'In transient';
      case 'rip': return 'Rip output';
      default: return 'Path unknown';
    }
  }

  formatDuration(seconds: number | null): string {
    if (!seconds) return '—';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m`;
    return `${Math.round(seconds)}s`;
  }

  trackByTitleId(_idx: number, row: TitleRowState): string {
    return row.title_id;
  }
}
