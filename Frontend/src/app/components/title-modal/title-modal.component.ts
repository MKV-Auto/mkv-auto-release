// src/app/components/title-modal/title-modal.component.ts
import { Component, Input, Output, EventEmitter } from '@angular/core';
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
export class TitleModalComponent {
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
    this.emitFieldPatch({ description: normalized });
    this.titleChanged.emit();
  }

  markAsIgnore(): void {
    if (!this.title) return;
    const currentType = (this.title.type || '').toString().toLowerCase();
    if (currentType === 'ignore') {
      this.title.type = '';
      this.emitFieldPatch({ type: null });
    } else {
      this.title.type = 'ignore';
      this.clearIgnoredFields();
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

  /** Directly-bound field ngModelChange handlers — persist to backend. */
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

  private emitFieldPatch(fields: Partial<TitlePatchRequest>): void {
    const titleId = this.title?.title_id;
    if (!titleId) return;
    this.titlePatched.emit({ title_id: titleId, ...fields });
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

