import { CommonModule } from '@angular/common';
import {
  Component,
  ElementRef,
  EventEmitter,
  HostListener,
  Input,
  OnDestroy,
  OnInit,
  Output,
  ViewChild,
} from '@angular/core';

import { PreviewViewerComponent } from '../preview-viewer/preview-viewer.component';
import { TitleEditorComponent } from '../title-editor/title-editor.component';
import { TitlePatchRequest } from '../../services/workflow.service';

/**
 * #848 / #849 — the shared preview + label modal.
 *
 * One overlay for both the rail's quick-preview button and the editor's
 * "Play preview": video on one side, the REAL TitleEditor (full field set —
 * type, TMDB picker, season/episode, part/range, name, description) on the
 * other, so the disc-matching loop never leaves the modal: watch → label →
 * arrow → watch. The action bar (Ignore & next / Skip / Save & next) is
 * pinned below the scrollable field column.
 *
 * The host element is re-parented onto document.body on init (#849):
 * `position: fixed` resolves against the nearest ancestor with a
 * backdrop-filter/filter/transform, and the labeling layout is full of
 * glass — left inside that subtree the modal centers on the tall list
 * column instead of the viewport.
 */
@Component({
  selector: 'app-preview-label-modal',
  standalone: true,
  imports: [CommonModule, PreviewViewerComponent, TitleEditorComponent],
  templateUrl: './preview-label-modal.component.html',
  styleUrls: ['./preview-label-modal.component.scss'],
})
export class PreviewLabelModalComponent implements OnInit, OnDestroy {
  @Input() title: any | null = null;
  @Input() previewUrl: string | null = null;
  /** "N unlabeled" badge in the header; hidden when null. */
  @Input() unlabeledCount: number | null = null;

  // Pass-through bindings for the embedded TitleEditor — same contract as
  // the drawer/desktop usages in title-label.
  @Input() isSeries = false;
  @Input() titleProgress: any = null;
  @Input() titleStatusFn: (id: string | null | undefined) => string = () => 'pending';
  @Input() titleProgressValueFn: (id: string | null | undefined) => number = () => 0;
  @Input() titleActiveFn: (id: string | null | undefined) => boolean = () => false;
  @Input() previewUrlFn: (t: any) => string | null = () => null;
  @Input() titlePathFn: (t: any) => string | null = () => null;
  @Input() previewStateFn: (t: any) => { status: string; error?: string | null; retryable?: boolean; thumbnail?: string | null } | null = () => null;
  @Input() labelSaving = false;
  @Input() lastAutosaveOk = true;
  @Input() devMode = false;

  @Output() closed = new EventEmitter<void>();
  /** Advance to the next/previous unlabeled title (wrap-around; parent owns
   * the ordering). */
  @Output() next = new EventEmitter<void>();
  @Output() prev = new EventEmitter<void>();
  /** Ignore the current title, then advance. */
  @Output() ignoreNext = new EventEmitter<void>();
  @Output() titleChanged = new EventEmitter<void>();
  @Output() titleBlur = new EventEmitter<void>();
  @Output() titlePatched = new EventEmitter<TitlePatchRequest>();

  @ViewChild(TitleEditorComponent) editor?: TitleEditorComponent;
  @ViewChild('modalRoot') modalRoot?: ElementRef<HTMLElement>;

  /** What had focus before the modal opened — restored on close so a
   * keyboard-only user lands back where they were. */
  private previouslyFocused: HTMLElement | null = null;

  constructor(private readonly el: ElementRef<HTMLElement>) {}

  ngOnInit(): void {
    // #849: escape backdrop-filter/transform ancestors so fixed positioning
    // resolves against the viewport. Angular keeps the logical tree, so
    // bindings and outputs are unaffected by the DOM move.
    document.body.appendChild(this.el.nativeElement);
    this.previouslyFocused = document.activeElement as HTMLElement | null;
    // Focus the modal container (not a field): Tab then enters the form,
    // while ←/→/Space work immediately for the watch-label-advance loop.
    setTimeout(() => this.modalRoot?.nativeElement?.focus());
  }

  ngOnDestroy(): void {
    this.el.nativeElement.remove();
    this.previouslyFocused?.focus?.();
  }

  /** Visible, enabled, tabbable elements inside the modal, in DOM order. */
  private focusables(): HTMLElement[] {
    return Array.from(
      this.el.nativeElement.querySelectorAll<HTMLElement>(
        'button, input, select, textarea, video, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((e) => !e.hasAttribute('disabled') && e.offsetParent !== null);
  }

  /** Header meta, mirroring the rail row: source file · duration · size. */
  headMeta(): string {
    const t = this.title;
    if (!t) return '';
    const parts: string[] = [];
    if (t.source_file) parts.push(String(t.source_file));
    const secs = Number(t.duration);
    if (Number.isFinite(secs) && secs > 0) {
      if (secs < 60) parts.push(`${Math.round(secs)}s`);
      else {
        const h = Math.floor(secs / 3600);
        const m = Math.round((secs % 3600) / 60);
        parts.push(h > 0 ? `${h}h ${m}m` : `${m}m`);
      }
    }
    const bytes = Number(t.size);
    if (Number.isFinite(bytes) && bytes > 0) {
      parts.push(bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${Math.round(bytes / 1024 ** 2)} MB`);
    }
    return parts.join(' · ');
  }

  /** Save & next: the editor autosaves, so "save" is flushing anything the
   * user typed since the last write, then advancing. */
  saveAndNext(): void {
    this.editor?.flushPendingFieldEdits();
    this.next.emit();
  }

  @HostListener('document:keydown', ['$event'])
  onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      this.closed.emit();
      return;
    }
    // Focus trap: the host lives at the end of document.body (portaled), so
    // native tab order would walk out of the dialog into the page behind.
    // Tab/Shift+Tab cycle through the modal's own fields and buttons.
    if (event.key === 'Tab') {
      const els = this.focusables();
      if (!els.length) return;
      const first = els[0];
      const last = els[els.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (!active || !this.el.nativeElement.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && (active === first || active === this.modalRoot?.nativeElement)) {
        event.preventDefault();
        last.focus();
      }
      return;
    }
    const target = event.target as HTMLElement | null;
    const tag = (target?.tagName || '').toLowerCase();
    const typing =
      tag === 'input' || tag === 'textarea' || tag === 'select' || !!target?.isContentEditable;
    // Space toggles playback — the player convention. Not while typing, not
    // when a button has focus (space IS its click), and not when the video
    // itself has focus (the native controls already handle it).
    if (event.key === ' ') {
      if (typing || tag === 'button' || tag === 'video') return;
      const video = this.el.nativeElement.querySelector('video');
      if (video) {
        event.preventDefault();
        if (video.paused) {
          void video.play()?.catch(() => {});
        } else {
          video.pause();
        }
      }
      return;
    }
    // Arrows navigate only when not typing — a caret in a text field needs
    // its arrow keys.
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
    if (typing) return;
    event.preventDefault();
    if (event.key === 'ArrowRight') {
      this.saveAndNext();
    } else {
      this.editor?.flushPendingFieldEdits();
      this.prev.emit();
    }
  }
}
