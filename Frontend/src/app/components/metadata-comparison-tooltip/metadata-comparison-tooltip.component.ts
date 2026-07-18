import {
  Component,
  Input,
  ChangeDetectorRef,
  ViewEncapsulation,
  ViewChild,
  ElementRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  getComparisonDetail,
  parseTag,
  pickStrongestOtherForDiffTag,
  buildDuplicateComparisonMatrix,
  type ComparisonDetail,
  type ComparisonMatrixSection,
} from '../../utils/duplicate-tags.util';

@Component({
  selector: 'app-metadata-comparison-tooltip',
  standalone: true,
  imports: [CommonModule],
  styleUrls: ['./metadata-comparison-tooltip.component.scss'],
  template: `
    <div
      class="metadata-comparison-tooltip-host"
      (mouseenter)="onMouseEnter()"
      (mouseleave)="onMouseLeave()"
    >
      <ng-content></ng-content>
      <div
        #tooltipPanel
        class="metadata-comparison-tooltip-panel"
        [class.show]="show"
        [class.metadata-comparison-tooltip-panel-wide]="isDiffTag && matrixSections.length > 0"
        role="tooltip"
      >
        <div class="metadata-comparison-tooltip-arrow"></div>
        <ng-container *ngIf="isDiffTag && matrixSections.length > 0; else simpleOrFallback">
          <div class="metadata-comparison-tooltip-matrix" *ngIf="summaryDetail as sd">
            <div class="metadata-comparison-tooltip-label">{{ sd.label }} (vs strongest other)</div>
            <div class="metadata-comparison-tooltip-values">
              <span
                class="metadata-comparison-tooltip-current"
                [class.is-better]="sd.isBetter && !sd.isEquivalent"
                [class.is-equivalent]="!!sd.isEquivalent"
              >
                {{ sd.currentValue }}{{ sd.unit ? ' ' + sd.unit : '' }}
              </span>
              <span class="metadata-comparison-tooltip-vs">vs</span>
              <span class="metadata-comparison-tooltip-compared">
                {{ sd.comparedValue }}{{ sd.unit ? ' ' + sd.unit : '' }}
              </span>
            </div>
          </div>
          <div class="metadata-comparison-tooltip-matrix-scroll">
            <div class="metadata-comparison-matrix-section" *ngFor="let sec of matrixSections">
              <div class="metadata-comparison-matrix-heading">{{ sec.heading }}</div>
              <div class="metadata-comparison-matrix-line" *ngFor="let line of sec.lines">{{ line }}</div>
            </div>
          </div>
        </ng-container>
        <ng-template #simpleOrFallback>
          <ng-container *ngIf="simpleDetail as d; else fallbackContent">
            <div class="metadata-comparison-tooltip-label">{{ d.label }}</div>
            <div class="metadata-comparison-tooltip-values">
              <span
                class="metadata-comparison-tooltip-current"
                [class.is-better]="d.isBetter && !d.isEquivalent"
                [class.is-worse]="!d.isBetter && !d.isEquivalent"
                [class.is-equivalent]="!!d.isEquivalent"
              >
                {{ d.currentValue }}{{ d.unit ? ' ' + d.unit : '' }}
              </span>
              <span class="metadata-comparison-tooltip-vs">vs</span>
              <span class="metadata-comparison-tooltip-compared">
                {{ d.comparedValue }}{{ d.unit ? ' ' + d.unit : '' }}
              </span>
            </div>
          </ng-container>
          <ng-template #fallbackContent>
            <div class="metadata-comparison-tooltip-label">{{ fallbackLabel }}</div>
          </ng-template>
        </ng-template>
      </div>
    </div>
  `,
  encapsulation: ViewEncapsulation.None,
})
export class MetadataComparisonTooltipComponent {
  @Input() tag = '';
  @Input() isDiffTag = false;
  @Input() currentTitle: Record<string, unknown> | null = null;
  /** Other variants only (excluding current); used to pick strongest other on axis. */
  @Input() comparedTitles: Array<Record<string, unknown>> = [];
  /** Full duplicate group (including current) for scrollable matrix. */
  @Input() groupTitles: Array<Record<string, unknown>> = [];
  @ViewChild('tooltipPanel', { static: false }) tooltipPanel?: ElementRef<HTMLElement>;

  show = false;

  constructor(private cdr: ChangeDetectorRef) {}

  onMouseEnter(): void {
    if (!this.tag) return;
    const hasMatrix = this.matrixSections.length > 0;
    if (this.isDiffTag && !hasMatrix && (!this.comparedTitles?.length || !this.currentTitle)) return;
    this.show = true;

    if (this.tooltipPanel?.nativeElement) {
      const panel = this.tooltipPanel.nativeElement;
      panel.classList.add('show');
      panel.style.opacity = '1';
      panel.style.visibility = 'visible';
      panel.style.position = 'absolute';
      panel.style.zIndex = '10000';
      panel.style.bottom = '100%';
      panel.style.left = '50%';
      panel.style.transform = 'translateX(-50%)';
      panel.style.marginBottom = '0.5rem';
      panel.style.padding = '0.5rem 0.75rem';
      panel.style.borderRadius = '0.5rem';
      panel.style.background = 'rgba(15, 23, 42, 0.98)';
      panel.style.border = '1px solid rgba(255, 255, 255, 0.15)';
      panel.style.boxShadow = '0 8px 32px rgba(0, 0, 0, 0.4)';
      panel.style.pointerEvents = 'none';
      if (this.isDiffTag && this.matrixSections.length > 0) {
        panel.style.whiteSpace = 'normal';
        panel.style.maxWidth = 'min(22rem, 92vw)';
        panel.style.maxHeight = 'min(18rem, 55vh)';
        panel.style.overflowY = 'auto';
      } else {
        panel.style.whiteSpace = 'nowrap';
        panel.style.maxWidth = '';
        panel.style.maxHeight = '';
        panel.style.overflowY = '';
      }
    }
    this.cdr.markForCheck();
  }

  onMouseLeave(): void {
    this.show = false;

    if (this.tooltipPanel?.nativeElement) {
      const panel = this.tooltipPanel.nativeElement;
      panel.classList.remove('show');
      panel.style.opacity = '0';
      panel.style.visibility = 'hidden';
    }

    this.cdr.markForCheck();
  }

  get strongestOther(): Record<string, unknown> | null {
    if (!this.tag || !this.currentTitle) return null;
    return pickStrongestOtherForDiffTag(this.tag, this.currentTitle, this.comparedTitles ?? []);
  }

  get summaryDetail(): ComparisonDetail | null {
    const compared = this.strongestOther;
    if (!this.tag || !this.currentTitle || !compared) return null;
    return getComparisonDetail(this.tag, this.currentTitle, compared);
  }

  get simpleDetail(): ComparisonDetail | null {
    const compared = this.comparedTitles?.[0];
    if (!this.tag || !this.currentTitle || !compared) return null;
    return getComparisonDetail(this.tag, this.currentTitle, compared);
  }

  get matrixSections(): ComparisonMatrixSection[] {
    const g = this.groupTitles ?? [];
    if (g.length < 2) return [];
    return buildDuplicateComparisonMatrix(g as Record<string, unknown>[]);
  }

  get fallbackLabel(): string {
    const parsed = parseTag(this.tag, true);
    return parsed?.label ?? this.tag;
  }
}
