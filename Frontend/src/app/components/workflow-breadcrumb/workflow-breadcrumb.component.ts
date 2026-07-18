import { Component, Input, Output, EventEmitter, HostListener } from '@angular/core';
import { CommonModule } from '@angular/common';
import { WorkflowStep } from '../../services/workflow.service';
import { IconComponent } from '../../ui/icon/icon.component';
import { IconName } from '../../ui/icon/icon-paths';
import { PillComponent } from '../../ui/pill/pill.component';

const STEP_ICONS: Record<WorkflowStep, IconName> = {
  film: 'film',
  exploratory_rip: 'sort',
  boxset: 'book',
  disc: 'disc',
  titles: 'edit',
  summary: 'info',
  // #365 Phase 2 § 6.4 — 'postprocess' icon removed (step collapsed
  // into transfer).
  transfer: 'upload',
};

@Component({
  selector: 'app-workflow-breadcrumb',
  standalone: true,
  imports: [CommonModule, IconComponent, PillComponent],
  templateUrl: './workflow-breadcrumb.component.html',
  styleUrls: ['./workflow-breadcrumb.component.scss'],
})
export class WorkflowBreadcrumbComponent {
  @Input() steps: WorkflowStep[] = [];
  @Input() currentStep: WorkflowStep = 'film';
  @Input() canNavigateToStep: (step: WorkflowStep) => boolean = () => false;
  @Input() getStepLabel: (step: WorkflowStep) => string = () => '';
  @Input() movieCover: string | null = null;
  @Input() movieName: string | null = null;
  @Input() productionYear: number | null = null;
  @Input() isMobile: boolean = false;
  /** TheDiscDB matched this disc — informational badge (independent of short-workflow / discdbHit). */
  @Input() showDiscdbSuggestedBadge: boolean = false;

  /** #371 — Disc-level primary-season selector. When `tvSeasonCount` is null
   * or 0 the control hides. `primarySeason` defaults to 1 if unset. */
  @Input() tvSeasonCount: number | null = null;
  @Input() primarySeason: number | null = null;

  @Output() stepNavigate = new EventEmitter<WorkflowStep>();
  @Output() primarySeasonChange = new EventEmitter<number>();

  dropdownOpen = false;

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: Event): void {
    if (this.isMobile && this.dropdownOpen) {
      const target = event.target as HTMLElement;
      // Don't close if clicking on the dropdown trigger, menu, or menu items
      const isClickInsideDropdown = target.closest('.breadcrumb-dropdown') !== null;
      const isClickOnMenuItem = target.closest('.breadcrumb-dropdown-item.clickable') !== null;
      if (!isClickInsideDropdown && !isClickOnMenuItem) {
        this.closeDropdown();
      }
    }
  }

  onStepClick(step: WorkflowStep, event: Event): void {
    event.stopPropagation();
    // Only prevent default for touch events to avoid double-firing
    if (event.type === 'touchend') {
      event.preventDefault();
    }
    if (this.canNavigateToStep(step)) {
      this.stepNavigate.emit(step);
      this.dropdownOpen = false;
    }
  }

  toggleDropdown(event: Event): void {
    event.stopPropagation();
    this.dropdownOpen = !this.dropdownOpen;
  }

  closeDropdown(): void {
    this.dropdownOpen = false;
  }

  getDisplayName(): string {
    if (this.movieName) {
      const year = this.productionYear ? ` (${this.productionYear})` : '';
      return `${this.movieName}${year}`;
    }
    return '';
  }

  getStepIndex(step: WorkflowStep): number {
    return this.steps.indexOf(step);
  }

  /** True when step is ahead of current and not navigable (blocked by a prior step). */
  isMuted(step: WorkflowStep): boolean {
    return this.getStepIndex(step) > this.getStepIndex(this.currentStep) && !this.canNavigateToStep(step);
  }

  /** Lucide-style icon for a workflow step, used in the desktop pill-rail breadcrumb. */
  stepIcon(step: WorkflowStep): IconName {
    return STEP_ICONS[step] ?? 'info';
  }

  /** [1..n] inclusive helper for the primary-season `<select>`. */
  seasonsRange(n: number | null): number[] {
    if (!n || n < 1) return [];
    return Array.from({ length: n }, (_, i) => i + 1);
  }

  onPrimarySeasonChange(value: string | number): void {
    const n = Number(value);
    if (Number.isInteger(n) && n > 0) this.primarySeasonChange.emit(n);
  }
}
