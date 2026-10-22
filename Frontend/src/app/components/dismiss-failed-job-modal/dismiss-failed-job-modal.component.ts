/**
 * DismissFailedJobModalComponent (#877) — asks what to do with a failed
 * job's files before its card is dismissed.
 *
 * Failed jobs are skipped by every automated cleanup, so their files stayed
 * until someone deleted them by hand. The ✕ on a failed card opens this:
 * dismiss and delete the files, dismiss and keep them, or cancel. It loads
 * the size and whether the files are the only copy of a finished rip from
 * GET /jobs/{id}/dismiss.
 */
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  EventEmitter,
  HostListener,
  Input,
  OnInit,
  Output,
} from '@angular/core';
import { CommonModule } from '@angular/common';

import { DismissFailedPreview, WorkflowService } from '../../services/workflow.service';
import { formatHttpErrorDetail } from '../../services/toast.service';
import { BtnComponent } from '../../ui/btn/btn.component';
import { CardComponent } from '../../ui/card/card.component';
import { IconComponent } from '../../ui/icon/icon.component';

@Component({
  selector: 'app-dismiss-failed-job-modal',
  standalone: true,
  imports: [CommonModule, BtnComponent, CardComponent, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './dismiss-failed-job-modal.component.html',
  styleUrls: ['./dismiss-failed-job-modal.component.scss'],
})
export class DismissFailedJobModalComponent implements OnInit {
  @Input({ required: true }) jobId!: string;
  /** The card's title, shown so the user knows which disc this is. */
  @Input() title = '';

  /** Emits true once the job is dismissed, false on cancel. */
  @Output() closed = new EventEmitter<boolean>();

  preview: DismissFailedPreview | null = null;
  loading = true;
  error: string | null = null;
  /** Which button is working: 'delete' | 'keep'. */
  busy: 'delete' | 'keep' | null = null;

  constructor(
    private workflowService: WorkflowService,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    this.workflowService.getDismissFailedPreview(this.jobId).subscribe({
      next: preview => {
        this.preview = preview;
        this.loading = false;
        this.cdr.markForCheck();
      },
      error: err => {
        this.error = formatHttpErrorDetail(err);
        this.loading = false;
        this.cdr.markForCheck();
      },
    });
  }

  get jobCount(): number {
    return this.preview?.job_ids.length ?? 1;
  }

  get hasFiles(): boolean {
    return (this.preview?.bytes ?? 0) > 0;
  }

  get size(): string {
    const bytes = this.preview?.bytes ?? 0;
    if (bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    const value = bytes / Math.pow(1024, i);
    return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
  }

  dismiss(deleteFiles: boolean): void {
    if (this.busy) return;
    this.busy = deleteFiles ? 'delete' : 'keep';
    this.error = null;
    this.workflowService.dismissFailedJob(this.jobId, deleteFiles).subscribe({
      next: () => this.closed.emit(true),
      error: err => {
        this.error = `Couldn't dismiss this job: ${formatHttpErrorDetail(err)}`;
        this.busy = null;
        this.cdr.markForCheck();
      },
    });
  }

  cancel(): void {
    if (!this.busy) this.closed.emit(false);
  }

  onBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) this.cancel();
  }

  @HostListener('document:keydown.escape')
  onEscape(): void {
    this.cancel();
  }
}
