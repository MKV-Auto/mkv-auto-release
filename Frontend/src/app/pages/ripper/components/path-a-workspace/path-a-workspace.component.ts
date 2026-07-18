import { Component, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { WorkflowService, WorkflowContext } from '../../../../services/workflow.service';
import { JobService } from '../../../../services/job.service';
import { ToastService } from '../../../../services/toast.service';
import { LoggerService } from '../../../../services/logger.service';
import { BtnComponent } from '../../../../ui/btn/btn.component';
import { CardComponent } from '../../../../ui/card/card.component';
import { IconComponent } from '../../../../ui/icon/icon.component';
import { PillComponent } from '../../../../ui/pill/pill.component';
import { SegmentReorderPageComponent } from '../../../segment-reorder/segment-reorder-page.component';

/**
 * Path A workspace — replaces the normal workflow-labeling/actions UI when
 * the active job is running the segment-reorder selective-rip workflow.
 *
 * Stages (read from jobStatus.segment_reorder_state.stage):
 *   exploratory_ripping        → "Finding the main movie..."
 *   awaiting_segment_order     → "Ready: order N short clips"
 *   matching_playlists         → "Matching..."
 *   canonical_ripping_pending  → "Starting final rip..."
 *   previews_failed            → error banner + bail button
 *   cancelled                  → handled at the workflow-step level
 *
 * Re-skinned against the design system primitives — ui-card supplies the
 * stage shell, ui-pill replaces the bespoke pills, ui-btn replaces the
 * bespoke action buttons, ui-icon supplies the inline spinner.
 */
@Component({
  selector: 'app-path-a-workspace',
  standalone: true,
  imports: [
    CommonModule,
    CardComponent,
    PillComponent,
    BtnComponent,
    IconComponent,
    SegmentReorderPageComponent,
  ],
  templateUrl: './path-a-workspace.component.html',
  styleUrls: ['./path-a-workspace.component.scss'],
})
export class PathAWorkspaceComponent implements OnInit, OnDestroy {
  context: WorkflowContext | null = null;
  private subs = new Subscription();

  constructor(
    private workflowService: WorkflowService,
    private jobService: JobService,
    private router: Router,
    private toast: ToastService,
    private logger: LoggerService,
  ) {}

  ngOnInit(): void {
    this.subs.add(
      this.workflowService.getActiveContext().subscribe((ctx) => {
        this.context = ctx;
      }),
    );
  }

  ngOnDestroy(): void {
    this.subs.unsubscribe();
  }

  // ── Stage accessors ──────────────────────────────────────────────────────

  /** True when the active job has a Path A workflow — i.e. has any
   * segment_reorder_state at all. Mount/unmount is now driven by
   * ripper-page's pathAActive$ observable (currentStep === 'exploratory_rip'),
   * but this getter stays as the inner gate for the *ngIf at the root of the
   * template so a stale render for a non-Path-A job can't accidentally show
   * an empty workspace. */
  get isActive(): boolean {
    return !!this.stage;
  }

  get segmentReorderState(): any | null {
    return this.context?.jobStatus?.segment_reorder_state ?? null;
  }

  get stage(): string | null {
    return this.segmentReorderState?.stage ?? null;
  }

  get jobId(): string | null {
    const id = this.context?.jobStatus?.jobId;
    return id ?? null;
  }

  get ripProgress(): number {
    return this.context?.jobStatus?.rip_progress ?? 0;
  }

  /** Selective rip is N per-title invocations. Total = len(rip_set). */
  get totalRipTitles(): number {
    const rs = this.context?.jobStatus?.rip_set;
    return Array.isArray(rs) ? rs.length : 0;
  }

  /** Titles already finished — number of entries in ripped_files. The
   * exploratory's Midway_t{N}.mkv lingers in ripped_files from the
   * earlier pass, so this can over-count by 1 right at the start of
   * the canonical rip; that's acceptable UX-wise. */
  get titlesCompleted(): number {
    const ripped = this.context?.jobStatus?.ripped_files;
    return ripped ? Object.keys(ripped).length : 0;
  }

  /** Roughly: titles_done / total + within-title fraction / total. */
  get overallRipPercent(): number {
    const total = this.totalRipTitles;
    if (total <= 0) return this.ripProgress;
    const done = Math.min(this.titlesCompleted, total);
    const currentPct = Math.max(0, Math.min(100, this.ripProgress));
    const overall = ((done * 100) + currentPct) / total;
    return Math.min(100, Math.round(overall));
  }

  get previewsCount(): number {
    return this.segmentReorderState?.previews_manifest?.length ?? 0;
  }

  get groupSize(): number {
    return this.segmentReorderState?.group_member_indexes?.length ?? 0;
  }

  // ── Actions ──────────────────────────────────────────────────────────────

  cancelSegmentReorder(): void {
    if (!this.jobId) return;
    this.jobService.cancelSegmentReorder(this.jobId).subscribe({
      next: () => {
        this.toast.show('Segment-reorder cancelled. You can rip the disc manually now.', 'info');
      },
      error: (err) => {
        this.logger.error('Failed to cancel segment-reorder', err);
        this.toast.show('Failed to cancel segment-reorder. See logs.', 'error', 6000);
      },
    });
  }
}
