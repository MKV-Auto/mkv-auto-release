import { Component, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { StageProgressBarComponent, StageTimelineItem, StageKey } from '../stage-progress-bar/stage-progress-bar.component';
import { StageProgressValues, StageCompletionValues } from '../../services/workflow.service';
import { BtnComponent } from '../../ui/btn/btn.component';

export type { StageTimelineItem, StageKey } from '../stage-progress-bar/stage-progress-bar.component';

@Component({
  selector: 'app-workflow-action-bar',
  standalone: true,
  imports: [CommonModule, StageProgressBarComponent, BtnComponent],
  templateUrl: './workflow-action-bar.component.html',
})
export class WorkflowActionBarComponent {
  @Input() stageTimeline: StageTimelineItem[] = [];
  @Input() stageProgress: StageProgressValues | null = null;
  @Input() activeStage: StageKey | null = null;
  @Input() isStageCompleted: StageCompletionValues | null = null;
  @Input() canContinue: boolean = false;
  /** When ``canContinue`` is false, a short human-readable explanation
   * surfaced as a tooltip on the disabled Continue button so the user
   * knows what's blocking them (e.g. "Waiting for copy to finish before
   * titles can be labeled"). Null/empty when the button is enabled. */
  @Input() disabledReason: string | null = null;
  @Input() canGoBack: boolean = false;
  @Input() buttonText: string = 'Continue';
  @Input() buttonSpinner: boolean = false;
  /** Suppress the primary Continue button entirely (Back still renders).
   * Used when the active step delegates its own primary CTA to an
   * embedded child component (e.g. the segment-reorder UI's Submit
   * button during `awaiting_segment_order`), so the user isn't
   * presented with two competing primary actions — one of which is
   * disabled and the other of which is the real one. */
  @Input() hideContinue: boolean = false;

  @Output() continue = new EventEmitter<void>();
  @Output() back = new EventEmitter<void>();

  onContinue(): void {
    this.continue.emit();
  }

  onBack(): void {
    this.back.emit();
  }
}
