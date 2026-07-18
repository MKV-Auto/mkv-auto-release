import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { parseTag, getTagColor, getComparisonDetail, pickStrongestOtherForDiffTag } from '../../utils/duplicate-tags.util';
import { MetadataComparisonTooltipComponent } from '../metadata-comparison-tooltip/metadata-comparison-tooltip.component';

/** Single tag badge with category-based color. Diff tags can show comparison tooltip on hover. */
@Component({
  selector: 'app-metadata-tag-badge',
  standalone: true,
  imports: [CommonModule, MetadataComparisonTooltipComponent],
  template: `
    <app-metadata-comparison-tooltip
      [tag]="tag"
      [isDiffTag]="isDiff"
      [currentTitle]="currentTitle"
      [comparedTitles]="comparedTitles"
      [groupTitles]="groupTitles"
    >
      <span
        class="metadata-tag-badge"
        [class.metadata-tag-badge-sm]="size === 'sm'"
        [class.metadata-tag-badge-diff]="parsed?.isDiff"
        [style.background]="colors?.bg"
        [style.borderColor]="colors?.border"
        [style.color]="colors?.text"
      >
        {{ parsed?.label ?? tag }}
      </span>
    </app-metadata-comparison-tooltip>
  `,
  styles: [
    `
      .metadata-tag-badge {
        display: inline-flex;
        align-items: center;
        gap: 0.25rem;
        padding: 0.25rem 0.5rem;
        border-radius: 0.375rem;
        font-size: 0.75rem;
        font-weight: 500;
        border: 1px solid;
      }
      .metadata-tag-badge-sm {
        padding: 0.125rem 0.25rem;
        font-size: 10px;
      }
      .metadata-tag-badge-diff {
        cursor: help;
      }
    `,
  ],
})
export class MetadataTagBadgeComponent {
  @Input() tag = '';
  @Input() isDiff = false;
  @Input() size: 'sm' | 'md' = 'md';
  @Input() currentTitle: Record<string, unknown> | null = null;
  @Input() comparedTitles: Array<Record<string, unknown>> = [];
  @Input() groupTitles: Array<Record<string, unknown>> = [];

  get parsed(): {
    category: string;
    label: string;
    isDiff: boolean;
    isPositive: boolean;
    isNeutral: boolean;
  } | null {
    if (!this.tag) return null;
    return parseTag(this.tag, this.isDiff);
  }

  get colors(): { bg: string; border: string; text: string } | null {
    if (!this.parsed) return null;
    const compared = this.isDiff
      ? pickStrongestOtherForDiffTag(this.tag, this.currentTitle ?? {}, this.comparedTitles ?? [])
      : this.comparedTitles?.[0];
    let isNeutral = this.parsed.isNeutral;
    if (this.isDiff && this.tag && this.currentTitle && compared) {
      const d = getComparisonDetail(this.tag, this.currentTitle, compared);
      if (d?.isEquivalent) isNeutral = true;
    }
    return getTagColor(
      this.parsed.category as 'audio' | 'video' | 'subs' | 'chapters' | 'quality' | 'diff' | 'metadata',
      this.parsed.isDiff,
      this.parsed.isPositive,
      isNeutral
    );
  }
}
