import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { getGroupColor, getGroupIdentifier, type DuplicateInfo } from '../../utils/duplicate-tags.util';

/** Circular badge with group identifier (letter/number). No-op when duplicateInfo missing or groupSize <= 1. */
@Component({
  selector: 'app-duplicate-group-badge',
  standalone: true,
  imports: [CommonModule],
  template: `
    <ng-container *ngIf="visible">
      <div
        class="duplicate-group-badge"
        [class.duplicate-group-badge-sm]="size === 'sm'"
        [class.duplicate-group-badge-md]="size === 'md'"
        [class.duplicate-group-badge-lg]="size === 'lg'"
        [style.background]="gradientStyle"
        [style.boxShadow]="'0 0 12px ' + groupColor.glow + ', inset 0 1px 0 rgba(255,255,255,0.3)'"
        [style.borderColor]="groupColor.color"
        [title]="badgeTitle"
      >
        {{ identifier }}
      </div>
      <span *ngIf="showLabel" class="duplicate-group-badge-label" [style.color]="groupColor.color">
        Group {{ identifier }}{{ countSuffix }}
      </span>
    </ng-container>
  `,
  styles: [
    `
      :host {
        display: inline-flex;
        align-items: center;
        gap: 0.375rem;
      }
      .duplicate-group-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border-radius: 9999px;
        font-weight: 700;
        color: #fff;
        border: 1.5px solid;
        flex-shrink: 0;
        transition: transform 0.2s;
      }
      .duplicate-group-badge:hover {
        transform: scale(1.1);
      }
      .duplicate-group-badge-sm {
        width: 1.25rem;
        height: 1.25rem;
        font-size: 10px;
      }
      .duplicate-group-badge-md {
        width: 1.5rem;
        height: 1.5rem;
        font-size: 0.75rem;
      }
      .duplicate-group-badge-lg {
        width: 2rem;
        height: 2rem;
        font-size: 0.875rem;
      }
      .duplicate-group-badge-label {
        font-size: 0.75rem;
        font-weight: 500;
      }
    `,
  ],
})
export class DuplicateGroupBadgeComponent {
  @Input() duplicateInfo: DuplicateInfo | null | undefined = null;
  /** Number of real-duplicate siblings (excludes component clips). When set,
   * drives the visibility gate AND the displayed count instead of the raw
   * `effectiveGroupSize`, which lumps component clips in with duplicates. */
  @Input() duplicateSiblingCount: number | null = null;
  /** Number of component-clip m2ts wrapped by this title. Surfaced as a
   * secondary count in the label / tooltip so the user sees both counts
   * separately ("3 dupes · 5 clips") instead of one inflated number. */
  @Input() componentClipCount = 0;
  @Input() showLabel = true;
  @Input() size: 'sm' | 'md' | 'lg' = 'md';

  /** Effective duplicate count: caller-supplied real-sibling count when set,
   * otherwise fall back to the legacy `same_as`-derived effectiveGroupSize. */
  get effectiveDuplicateCount(): number {
    if (typeof this.duplicateSiblingCount === 'number') {
      // Caller already counted siblings (excludes self) — add 1 for the
      // primary so the label matches the "N duplicates" wording elsewhere.
      return this.duplicateSiblingCount > 0 ? this.duplicateSiblingCount + 1 : 0;
    }
    const info = this.duplicateInfo;
    if (!info) return 0;
    return info.effectiveGroupSize ?? info.groupSize ?? 0;
  }

  /** Hide the badge when there's no real-duplicate relationship to surface,
   * even if component clips exist (component clips have their own UI). */
  get visible(): boolean {
    if (!this.duplicateInfo?.groupId) return false;
    return this.effectiveDuplicateCount > 1;
  }

  get countSuffix(): string {
    const n = this.effectiveDuplicateCount;
    if (n <= 1) return '';
    const clips = this.componentClipCount;
    return clips > 0 ? ` (${n} dupes · ${clips} clip${clips === 1 ? '' : 's'})` : ` (${n})`;
  }

  get badgeTitle(): string {
    const n = this.effectiveDuplicateCount;
    const clips = this.componentClipCount;
    const head = `Duplicate Group ${this.identifier}`;
    if (clips > 0 && n > 1) {
      return `${head} — ${n} duplicates, ${clips} component clip${clips === 1 ? '' : 's'}`;
    }
    return head;
  }

  get groupColor(): { color: string; glow: string } {
    if (!this.duplicateInfo?.groupId) return { color: '#3b82f6', glow: 'rgba(59, 130, 246, 0.4)' };
    return getGroupColor(this.duplicateInfo.groupId);
  }

  get identifier(): string {
    return this.duplicateInfo?.groupId ? getGroupIdentifier(this.duplicateInfo.groupId) : '1';
  }

  get gradientStyle(): string {
    return `linear-gradient(135deg, ${this.groupColor.color} 0%, ${this.groupColor.glow} 100%)`;
  }
}
