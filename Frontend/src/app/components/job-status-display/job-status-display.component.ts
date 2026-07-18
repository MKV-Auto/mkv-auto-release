// src/app/components/job-status-display/job-status-display.component.ts
import { Component, Input, Output, EventEmitter, ChangeDetectionStrategy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { JobStatus } from '../../services/job.service';
import { StageKey, StageTimelineItem, StageProgressBarComponent } from '../stage-progress-bar/stage-progress-bar.component';
import { StageProgressValues, StageCompletionValues } from '../../services/workflow.service';

export type CtaAction = 'start' | 'finalize_disc' | 'postprocess' | 'transfer' | 'finalize_release' | 'none';

export interface CtaState {
  label: string;
  disabled: boolean;
  spinner: boolean;
  action: CtaAction;
  intent: 'start' | 'progress' | 'transfer' | 'finalize' | 'done' | 'retry';
}

@Component({
  selector: 'app-job-status-display',
  standalone: true,
  imports: [CommonModule, StageProgressBarComponent],
  templateUrl: './job-status-display.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class JobStatusDisplayComponent {
  @Input() jobStatus: JobStatus | null = null;
  @Input() stageProgress: StageProgressValues | null = null;
  @Input() activeStage: StageKey | null = null;
  @Input() isStageCompleted: StageCompletionValues | null = null;
  @Input() stageTimeline: StageTimelineItem[] = [];
  @Input() ctaState: CtaState | null = null;
  @Input() error: string | null = null;

  @Output() actionRequested = new EventEmitter<{ action: string; jobId: string }>();

  constructor(private cdr: ChangeDetectorRef) {}

  onActionClick(): void {
    if (!this.jobStatus?.jobId || !this.ctaState || this.ctaState.disabled) {
      return;
    }

    this.actionRequested.emit({
      action: this.ctaState.action,
      jobId: this.jobStatus.jobId,
    });
  }

  getStageProgress(key: StageKey | 'done'): number | null {
    if (key === 'done') {
      return this.isStageCompleted?.transfer ? 100 : null;
    }
    if (!this.stageProgress) return null;
    return this.stageProgress[key] ?? null;
  }

  isStageCompletedCheck(key: StageKey | 'done'): boolean {
    if (!this.isStageCompleted) return false;
    if (key === 'done') {
      return this.isStageCompleted.transfer;
    }
    return this.isStageCompleted[key] ?? false;
  }
}






