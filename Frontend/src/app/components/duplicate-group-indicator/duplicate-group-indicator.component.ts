import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  getGroupColor,
  getSameAsText,
  type DuplicateInfo,
} from '../../utils/duplicate-tags.util';

/** Renders the left accent bar (and optional "same as" badge) when duplicateInfo is present and groupSize > 1. */
@Component({
  selector: 'app-duplicate-group-indicator',
  standalone: true,
  imports: [CommonModule],
  template: `
    <ng-container *ngIf="duplicateInfo && (duplicateInfo.effectiveGroupSize ?? duplicateInfo.groupSize) > 1 && allTitles?.length">
      <div
        class="duplicate-accent-bar"
        [style.background]="gradientStyle"
        [style.boxShadow]="'0 0 12px ' + groupColor.glow"
        aria-hidden="true"
      ></div>
    </ng-container>
  `,
  styles: [
    `
      :host {
        display: block;
        position: relative;
      }
      .duplicate-accent-bar {
        position: absolute;
        left: 0;
        top: 0;
        bottom: 0;
        width: 4px;
        border-radius: 4px 0 0 4px;
      }
    `,
  ],
})
export class DuplicateGroupIndicatorComponent {
  @Input() duplicateInfo: DuplicateInfo | null | undefined = null;
  @Input() allTitles: Array<{ id: string; title?: string | null; source_file?: string; sourceFile?: string }> = [];
  @Input() currentTitleId = '';

  get groupColor(): { color: string; glow: string } {
    if (!this.duplicateInfo?.groupId) return { color: '#3b82f6', glow: 'rgba(59, 130, 246, 0.4)' };
    return getGroupColor(this.duplicateInfo.groupId);
  }

  get gradientStyle(): string {
    return `linear-gradient(180deg, ${this.groupColor.color} 0%, ${this.groupColor.glow} 100%)`;
  }

  get sameAsText(): string {
    if (!this.duplicateInfo?.sameAs?.length || !this.allTitles?.length) return '';
    const sameAsTitles = this.allTitles.filter(
      (t) =>
        t &&
        (this.duplicateInfo!.sameAs.includes(t.id) || t.id === this.currentTitleId)
    );
    return getSameAsText(
      sameAsTitles.map((t) => ({
        id: t.id,
        title: t.title ?? (t.source_file ?? t.sourceFile) ?? null,
        sourceFile: t.source_file ?? t.sourceFile,
      })),
      this.currentTitleId
    );
  }
}
