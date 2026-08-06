// src/app/components/title-modal/title-modal.component.ts
import { Component, Input, Output, EventEmitter, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { PreviewViewerComponent } from '../preview-viewer/preview-viewer.component';
import { TITLE_TYPE_SELECT_OPTIONS } from '../../constants/title-type-options';
import { TitlePatchRequest } from '../../services/workflow.service';

@Component({
  selector: 'app-title-modal',
  standalone: true,
  imports: [CommonModule, FormsModule, PreviewViewerComponent],
  templateUrl: './title-modal.component.html',
  styleUrls: ['./title-modal.component.scss'],
})
export class TitleModalComponent implements OnDestroy {
  readonly titleTypeOptions = TITLE_TYPE_SELECT_OPTIONS;

  /** Last line of defence: closing the modal mid-edit must not strand a
   *  buffered typed field unsaved. */
  ngOnDestroy(): void {
    this.flushPendingFieldEdits();
  }

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

  @Output() close = new EventEmitter<void>();
  @Output() titleChanged = new EventEmitter<void>();
  @Output() titleBlur = new EventEmitter<void>();
  /** Field-level patch for the current title. Persists to backend via the
   * parent's onTitlePatch handler. Every user edit MUST emit both
   * `titleChanged` (in-memory nudge) and `titlePatched` (persistence).
   * See title-editor's class docstring for regression history. */
  @Output() titlePatched = new EventEmitter<TitlePatchRequest>();

  previewTitle: any | null = null;
  previewUrl: string | null = null;

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
    this.title.note = value; // keep legacy field in sync
    const normalized = value === '' ? null : value;
    this.bufferFieldPatch({ description: normalized });
    this.titleChanged.emit();
  }

  markAsIgnore(): void {
    if (!this.title) return;
    // Buffered typed fields ride in this write; see takePendingFieldsFor.
    const pending = this.takePendingFieldsFor(this.title.title_id);
    const currentType = (this.title.type || '').toString().toLowerCase();
    if (currentType === 'ignore') {
      this.title.type = '';
      this.emitFieldPatch({ ...pending, type: null });
    } else {
      this.title.type = 'ignore';
      this.clearIgnoredFields();
      // Nulls intentionally override pending text: ignore clears.
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

  onTypeChange(value: any): void {
    // Picked, not typed — saves immediately. A name typed just before may
    // still be buffered; it rides in THIS write rather than flushing as a
    // separate one (two same-tick writes to one row carry the same
    // base_seq — one of them always loses).
    if (!this.title) return;
    const pending = this.takePendingFieldsFor(this.title.title_id);
    this.flushPendingFieldEdits(); // other rows' leftovers, if any
    this.title.type = value;
    const normalizedType = value === '' ? null : value;
    if (this.isIgnored()) {
      this.clearIgnoredFields();
      // Nulls intentionally override pending text: ignore clears.
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
      this.emitFieldPatch({ ...pending, type: normalizedType });
    }
    this.titleChanged.emit();
  }

  /** Typed-field ngModelChange handlers. These used to PATCH per keystroke
   *  (this modal missed the #781/#782 buffering that title-editor and
   *  title-label got) — every response echo re-rendered the bound input,
   *  which is the phantom-typing / dropped-characters class of bug, worst
   *  on mobile where this modal is the primary editing surface. Typed
   *  fields now buffer and flush on idle/blur/teardown, exactly like
   *  title-editor. */
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

  private emitFieldPatch(fields: Partial<TitlePatchRequest>): void {
    const titleId = this.title?.title_id;
    if (!titleId) return;
    this.titlePatched.emit({ title_id: titleId, ...fields });
  }

  /** Typed-field edits awaiting flush, keyed by title id. Same machinery as
   *  TitleEditorComponent (#782): buffer per keystroke, one write per pause. */
  private pendingFieldEdits = new Map<string, Partial<TitlePatchRequest>>();
  private autosaveTimer: any = null;
  private static readonly AUTOSAVE_IDLE_MS = 700;

  private bufferFieldPatch(fields: Partial<TitlePatchRequest>): void {
    const titleId = this.title?.title_id;
    if (!titleId) return;
    const existing = this.pendingFieldEdits.get(titleId) || {};
    this.pendingFieldEdits.set(titleId, { ...existing, ...fields });
    this.scheduleAutosave();
  }

  /** Restart the idle timer so a typing burst produces one write. */
  private scheduleAutosave(): void {
    if (this.autosaveTimer) clearTimeout(this.autosaveTimer);
    this.autosaveTimer = setTimeout(() => {
      this.autosaveTimer = null;
      this.flushPendingFieldEdits();
    }, TitleModalComponent.AUTOSAVE_IDLE_MS);
  }

  /** Send everything buffered. Safe to call repeatedly — a flush with
   *  nothing pending is a no-op. Wired to blur in the template. */
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
   *  write (type pick, ignore) carries them in the SAME request — users
   *  don't wait for autosave, and two same-tick writes to one row race
   *  each other (same base_seq). See TitleEditorComponent. */
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

  onClose(): void {
    this.close.emit();
  }

  formatDuration(seconds: number): string {
    if (!seconds) return '';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (hours > 0) {
      return `${hours}h ${minutes}m`;
    }
    return `${minutes}m`;
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

  private clearIgnoredFields(): void {
    if (!this.title) return;
    this.title.title = '';
    this.title.description = '';
    this.title.note = '';
    this.title.season = null;
    this.title.episode = null;
  }
}

