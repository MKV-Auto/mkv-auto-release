import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MetadataTagBadgeComponent } from '../metadata-tag-badge/metadata-tag-badge.component';

/** Renders diff tags first (comparison vs other duplicates), then non-diff tags. Matches template: diff tags show difference in metadata; non-diff tags come after. */
@Component({
  selector: 'app-metadata-tag-list',
  standalone: true,
  imports: [CommonModule, MetadataTagBadgeComponent],
  template: `
    <div class="metadata-tag-list" *ngIf="allTags.length">
      <app-metadata-tag-badge
        *ngFor="let item of visibleTags; trackBy: trackByTag"
        [tag]="item.tag"
        [isDiff]="item.isDiff"
        [size]="size"
        [currentTitle]="currentTitle"
        [comparedTitles]="comparedTitles"
        [groupTitles]="groupTitles"
      ></app-metadata-tag-badge>
      <span *ngIf="remainingCount > 0" class="metadata-tag-more">+{{ remainingCount }} more</span>
    </div>
  `,
  styles: [
    `
      .metadata-tag-list {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.375rem;
      }
      .metadata-tag-more {
        font-size: 0.75rem;
        color: rgba(255, 255, 255, 0.5);
        padding: 0 0.25rem;
      }
    `,
  ],
})
export class MetadataTagListComponent {
  @Input() tags: string[] = [];
  @Input() diffTags: string[] = [];
  @Input() maxVisible = 6;
  @Input() size: 'sm' | 'md' = 'md';
  /** Current title payload (for comparison tooltip on diff tags). */
  @Input() currentTitle: Record<string, unknown> | null = null;
  /** Other titles in duplicate group (for comparison tooltip). */
  @Input() comparedTitles: Array<Record<string, unknown>> = [];
  /** All titles in the duplicate group (matrix tooltip). */
  @Input() groupTitles: Array<Record<string, unknown>> = [];

  /** Diff tags first (comparison between duplicates), then non-diff tags (shared metadata). */
  get allTags(): { tag: string; isDiff: boolean }[] {
    const fromDiff = (this.diffTags ?? []).map((tag) => ({ tag, isDiff: true }));
    const fromTags = (this.tags ?? []).map((tag) => ({ tag, isDiff: false }));
    return [...fromDiff, ...fromTags];
  }

  get visibleTags(): { tag: string; isDiff: boolean }[] {
    return this.allTags.slice(0, this.maxVisible);
  }

  get remainingCount(): number {
    return Math.max(0, this.allTags.length - this.maxVisible);
  }

  /** Stable identity for *ngFor so badge/tooltip components are reused, not recreated, when CD runs. */
  trackByTag(_index: number, item: { tag: string; isDiff: boolean }): string {
    return `${item.isDiff}:${item.tag}`;
  }
}
