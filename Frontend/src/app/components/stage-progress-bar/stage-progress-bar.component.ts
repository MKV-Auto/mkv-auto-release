import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { StageProgressValues, StageCompletionValues } from '../../services/workflow.service';
import { IconComponent } from '../../ui/icon/icon.component';

export type StageKey = 'rip' | 'label' | 'postprocess' | 'transfer' | 'upload';

export interface StageTimelineItem {
  key: StageKey | 'done';
  label: string;
}

@Component({
  selector: 'app-stage-progress-bar',
  standalone: true,
  imports: [CommonModule, IconComponent],
  templateUrl: './stage-progress-bar.component.html',
  styleUrls: ['./stage-progress-bar.component.scss'],
})
export class StageProgressBarComponent {
  @Input() stageTimeline: StageTimelineItem[] = [];
  @Input() stageProgress: StageProgressValues | null = null;
  @Input() activeStage: StageKey | null = null;
  @Input() isStageCompleted: StageCompletionValues | null = null;

  /** Desktop grid template (labels and bars). Used by tests; visibility is controlled by CSS breakpoints. */
  get stageGridTemplate(): string {
    const slots = this.stageTimeline.length * 2 - 1;
    const labelCol = 'minmax(0, 90px)';
    const barCol = 'minmax(120px, 2fr)';
    return Array.from({ length: slots }, (_v, idx) => (idx % 2 === 0 ? labelCol : barCol)).join(' ');
  }

  dotColumn(idx: number): number {
    return idx * 2 + 1;
  }

  barColumn(idx: number): number {
    return idx * 2 + 2;
  }

  getProgressValue(key: StageKey | 'done'): number | null {
    if (key === 'done') {
      return this.isStageCompleted?.transfer ? 100 : null;
    }
    if (!this.stageProgress) return null;
    // Handle 'upload' key which doesn't exist in StageProgressValues
    if (key === 'upload') return null;
    return this.stageProgress[key] ?? null;
  }

  /** Elapsed time since rip started, formatted as "Xm Ys" or "Xh Ym". Null if not ripping or no start time. */
  get ripElapsed(): string | null {
    if (!this.stageProgress?.ripStartedAt) return null;
    if (this.activeStage !== 'rip') return null;
    const pct = this.getProgressValue('rip');
    if (pct == null || pct <= 0) return null;
    try {
      const start = new Date(this.stageProgress.ripStartedAt).getTime();
      if (isNaN(start)) return null;
      const elapsed = Math.max(0, Math.floor((Date.now() - start) / 1000));
      if (elapsed < 60) return `${elapsed}s`;
      const m = Math.floor(elapsed / 60);
      const s = elapsed % 60;
      if (m < 60) return `${m}m ${s}s`;
      const h = Math.floor(m / 60);
      return `${h}h ${m % 60}m`;
    } catch { return null; }
  }

  /** Sub-phase label for a stage (e.g. "Verifying…" when rip is at 100% but not complete). */
  getPhaseLabel(key: StageKey | 'done'): string | null {
    if (!this.stageProgress) return null;
    if (key === 'rip') return this.stageProgress.ripPhaseLabel ?? null;
    if (key === 'postprocess') return this.stageProgress.postPhaseLabel ?? null;
    if (key === 'transfer') return this.stageProgress.transferPhaseLabel ?? null;
    return null;
  }

  connectorProgress(key: StageKey | 'done'): number {
    if (key === 'done') return 0;
    const pct = this.getProgressValue(key);
    return pct == null ? 0 : pct;
  }

  activeStagePercent(key: StageKey | 'done'): number | null {
    const pct = this.getProgressValue(key);
    if (pct === null || pct === undefined) return null;
    const rounded = Math.max(0, Math.min(100, Math.round((pct + Number.EPSILON) * 100) / 100));
    // Only show percentage for active stage
    if (this.activeStage && key === this.activeStage && rounded >= 0 && rounded < 100) {
      return rounded;
    }
    return null;
  }
  
  isStageCompletedCheck(key: StageKey | 'done'): boolean {
    if (!this.isStageCompleted) return false;
    if (key === 'done') {
      return this.isStageCompleted.transfer;
    }
    // Handle 'upload' key which doesn't exist in StageCompletionValues
    if (key === 'upload') return false;
    return this.isStageCompleted[key] ?? false;
  }

  isStageFuture(key: StageKey | 'done', index: number): boolean {
    if (!this.activeStage) return false;
    if (key === 'done') return false; // 'done' is never future
    if (key === 'upload') return false; // 'upload' is not in timeline
    
    // Find the index of the active stage in the timeline
    const activeIndex = this.stageTimeline.findIndex(step => step.key === this.activeStage);
    if (activeIndex === -1) return false;
    
    // A stage is future if it comes after the active stage in the timeline
    return index > activeIndex;
  }

  /**
   * Check if a stage is active (handles null activeStage by defaulting to first stage)
   */
  isStageActive(key: StageKey | 'done', index: number): boolean {
    if (this.isStageCompletedCheck(key)) return false;
    
    // If activeStage is explicitly set, use it
    if (this.activeStage) {
      return this.activeStage === key;
    }
    
    // If no activeStage (pre-rip), default to first stage being active
    // But only if no stages are completed yet
    const anyCompleted = this.stageTimeline.some(s => this.isStageCompletedCheck(s.key));
    if (!anyCompleted && index === 0) {
      return true;
    }
    
    return false;
  }

  /**
   * Check if a stage is pending (not active, not completed)
   */
  isStagePending(key: StageKey | 'done', index: number): boolean {
    // If completed or active, not pending
    if (this.isStageCompletedCheck(key)) return false;
    if (this.isStageActive(key, index)) return false;
    
    // Special case for 'done' - always pending until completed
    if (key === 'done') return true;
    
    // All other stages that are not active or completed are pending
    return true;
  }

  /** Overall progress 0–100 for mobile circular view (legacy; mobile now uses activeStageProgressPercentage) */
  get totalProgressPercentage(): number {
    if (!this.stageTimeline.length) return 0;
    let sum = 0;
    let count = 0;
    for (const step of this.stageTimeline) {
      if (step.key === 'done' || step.key === 'upload') continue;
      const v = this.getProgressValue(step.key);
      sum += v != null ? v : 0;
      count++;
    }
    return count > 0 ? Math.round(sum / count) : 0;
  }

  /** Progress of the current stage only (0–100) for mobile circle — shows stage %, not overall job */
  get activeStageProgressPercentage(): number {
    const n = this.stageTimeline.length;
    const completed = this.stageTimeline.filter(s => this.isStageCompletedCheck(s.key)).length;
    if (n && completed >= n) return 100;
    if (!this.activeStage) return 0;
    const v = this.getProgressValue(this.activeStage);
    return v != null ? Math.round(Math.max(0, Math.min(100, v))) : 0;
  }

  /** Index of active stage in timeline, or -1 */
  get activeStageIndex(): number {
    if (!this.activeStage) return -1;
    return this.stageTimeline.findIndex(step => step.key === this.activeStage);
  }

  /** Label for mobile “stage X/Y” or “Complete” */
  get mobileStageSummary(): string {
    const n = this.stageTimeline.length;
    const completed = this.stageTimeline.filter(s => this.isStageCompletedCheck(s.key)).length;
    if (completed >= n) return 'Complete';
    const idx = this.activeStageIndex;
    if (idx >= 0) return `Stage ${idx + 1}/${n}`;
    return `${completed}/${n}`;
  }

  /** Segment status for circular mobile view */
  getSegmentStatus(key: StageKey | 'done'): 'completed' | 'active' | 'pending' {
    if (this.isStageCompletedCheck(key)) return 'completed';
    if (this.activeStage === key) return 'active';
    return 'pending';
  }

  /** Stroke color for segment in mobile circular view */
  getSegmentStroke(key: StageKey | 'done'): string {
    const s = this.getSegmentStatus(key);
    if (s === 'completed') return '#34d399';
    if (s === 'active') return '#60a5fa';
    return 'rgba(255,255,255,0.2)';
  }

  /** Active stage label for mobile */
  get activeStageLabel(): string {
    const idx = this.activeStageIndex;
    if (idx >= 0 && idx < this.stageTimeline.length) return this.stageTimeline[idx].label;
    const n = this.stageTimeline.length;
    const completed = this.stageTimeline.filter(s => this.isStageCompletedCheck(s.key)).length;
    if (completed >= n) return 'Complete';
    return 'Waiting…';
  }

  private readonly circleR = 42;

  getSegmentDashArray(index: number): string {
    const n = this.stageTimeline.length;
    if (!n) return '0 300';
    const circum = 2 * Math.PI * this.circleR;
    const seg = circum / n - 2;
    return `${seg} ${circum}`;
  }

  getSegmentDashOffset(index: number): number {
    const n = this.stageTimeline.length;
    if (!n) return 0;
    const circum = 2 * Math.PI * this.circleR;
    return -(circum / n * index);
  }
}
