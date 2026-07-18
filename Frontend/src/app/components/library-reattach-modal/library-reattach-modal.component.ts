import { ChangeDetectionStrategy, ChangeDetectorRef, Component, EventEmitter, OnInit, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subject } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { WorkflowService } from '../../services/workflow.service';
import { ToastService, formatHttpErrorDetail } from '../../services/toast.service';
import type { LibraryReattachReport } from '../../services/library-reattach.types';

import { BtnComponent } from '../../ui/btn/btn.component';
import { CardComponent } from '../../ui/card/card.component';
import { IconComponent } from '../../ui/icon/icon.component';

/**
 * "Verify Library Links" modal (#449).
 *
 * Two-pass preview/apply UX. On open, fires
 * ``workflowService.verifyLibraryLinks(dryRun=true)`` and renders the
 * report. The Apply button re-fires with ``dryRun=false`` to commit
 * the deterministic + heuristic matches; conflicts and orphans are
 * surface-only (no writes).
 *
 * Opened from two surfaces on the History page:
 *   * Header icon button — always available (mid-flight "I moved files
 *     in Plex" recovery).
 *   * Empty-state CTA — primary path for the wipe-and-reimport case.
 *
 * Modal styles mirror the inline shell used by ``history-page.component.ts``
 * (.hist-modal-backdrop / .hist-modal / .hist-modal-head /
 * .hist-modal-body / .hist-modal-footer) so the visual language is
 * consistent across the page's modals.
 */
@Component({
  selector: 'app-library-reattach-modal',
  standalone: true,
  imports: [CommonModule, BtnComponent, CardComponent, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './library-reattach-modal.component.html',
  styleUrls: ['./library-reattach-modal.component.scss'],
})
export class LibraryReattachModalComponent implements OnInit {
  @Output() dismiss = new EventEmitter<void>();
  @Output() applied = new EventEmitter<LibraryReattachReport>();

  /** dry-run report; populated on ngOnInit. Null until the request lands. */
  report: LibraryReattachReport | null = null;
  /** Error message from the dry-run call, when the endpoint rejected
   * (e.g. 400 no active config, 400 remote-mode config). */
  loadError: string | null = null;
  /** True while the dry-run fetch is in flight (initial open). */
  loading = true;
  /** True while the wet-run Apply is in flight. */
  applying = false;

  /** Section expand/collapse — start with the most informative sections
   * open and the long lists collapsed. */
  expanded: Record<'deterministic' | 'heuristic' | 'conflicts' | 'orphan_files' | 'orphan_titles', boolean> = {
    deterministic: true,
    heuristic: false,
    conflicts: true,
    orphan_files: false,
    orphan_titles: false,
  };

  private readonly destroy$ = new Subject<void>();

  constructor(
    private readonly workflowService: WorkflowService,
    private readonly toast: ToastService,
    private readonly cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.workflowService.verifyLibraryLinks(true)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (report) => {
          this.report = report;
          this.loading = false;
          // OnPush: state mutation alone doesn't fire CD; mark explicitly
          // so the scanning-spinner swap to the report renders.
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.loadError = formatHttpErrorDetail(err);
          this.loading = false;
          this.cdr.markForCheck();
        },
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  toggleSection(key: keyof typeof this.expanded): void {
    // Triggered from a (click) handler in the template — Angular's CD
    // runs automatically for DOM events, so no markForCheck() needed
    // here. Kept the comment so future-us doesn't add a redundant one.
    this.expanded[key] = !this.expanded[key];
  }

  /** Total number of writes the Apply button would commit. */
  get applyCount(): number {
    if (!this.report) return 0;
    return this.report.deterministic_matches.length + this.report.heuristic_matches.length;
  }

  /** Apply is enabled when there's something to apply and we're not
   * already in flight (and the dry-run succeeded). */
  get canApply(): boolean {
    return !this.loading
      && !this.applying
      && this.loadError === null
      && this.applyCount > 0;
  }

  onApply(): void {
    if (!this.canApply) return;
    this.applying = true;
    this.cdr.markForCheck();
    this.workflowService.verifyLibraryLinks(false)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (report) => {
          this.applying = false;
          this.toast.show(
            `Reattached ${report.deterministic_matches.length + report.heuristic_matches.length} file(s) to the library`,
            'success',
          );
          this.applied.emit(report);
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.applying = false;
          this.toast.show(formatHttpErrorDetail(err), 'error', 5000);
          // Keep the modal open so the user can read the report and try
          // again or cancel.
          this.cdr.markForCheck();
        },
      });
  }

  onCancel(): void {
    this.dismiss.emit();
  }

  onBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      this.dismiss.emit();
    }
  }
}
