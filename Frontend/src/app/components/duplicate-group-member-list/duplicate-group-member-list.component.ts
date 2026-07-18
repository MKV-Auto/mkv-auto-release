import { Component, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { getGroupColor, getTitleDisplayName, type DuplicateInfo } from '../../utils/duplicate-tags.util';

/** Expandable list of titles in the group with display name fallback (title → source_file → id). */
@Component({
  selector: 'app-duplicate-group-member-list',
  standalone: true,
  imports: [CommonModule],
  template: `
    <ng-container *ngIf="duplicateInfo && (duplicateInfo.effectiveGroupSize ?? duplicateInfo.groupSize) > 1 && allTitles?.length && duplicateInfo.sameAs">
      <div class="duplicate-member-list">
        <button
          type="button"
          class="duplicate-member-item"
          *ngFor="let t of sameAsTitles"
          [class.duplicate-member-current]="t.id === currentTitleId"
          [style.background]="t.id === currentTitleId ? groupColorGlow15 : 'rgba(255,255,255,0.03)'"
          [style.borderColor]="t.id === currentTitleId ? groupColorGlow40 : 'rgba(255,255,255,0.05)'"
          (click)="t.id !== currentTitleId && navigateToTitle.emit(t.id)"
        >
          <span class="duplicate-member-dot" [style.background]="groupColor.color"></span>
          <span class="duplicate-member-name">{{ getDisplayName(t) }}</span>
          <span *ngIf="t.id === currentTitleId" class="duplicate-member-current-label">(current)</span>
        </button>
      </div>
    </ng-container>
  `,
  styles: [
    `
      .duplicate-member-list {
        display: flex;
        flex-direction: column;
        gap: 0.375rem;
      }
      .duplicate-member-item {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.375rem 0.625rem;
        border-radius: 0.5rem;
        font-size: 0.875rem;
        text-align: left;
        width: 100%;
        border: 1px solid;
        background: rgba(255, 255, 255, 0.03);
        color: rgba(255, 255, 255, 0.7);
        cursor: default;
      }
      .duplicate-member-item.duplicate-member-current {
        font-weight: 500;
        color: #fff;
      }
      .duplicate-member-item:not(.duplicate-member-current) {
        cursor: pointer;
      }
      .duplicate-member-item:not(.duplicate-member-current):hover {
        color: #fff;
      }
      .duplicate-member-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        flex-shrink: 0;
      }
      .duplicate-member-name {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .duplicate-member-current-label {
        font-size: 0.75rem;
        color: rgba(255, 255, 255, 0.4);
        margin-left: auto;
      }
    `,
  ],
})
export class DuplicateGroupMemberListComponent {
  @Input() duplicateInfo: DuplicateInfo | null | undefined = null;
  @Input() allTitles: Array<{
    id: string;
    title?: string | null;
    source_file?: string;
    sourceFile?: string;
  }> = [];
  @Input() currentTitleId = '';
  @Output() navigateToTitle = new EventEmitter<string>();

  get groupColor(): { color: string; glow: string } {
    if (!this.duplicateInfo?.groupId) return { color: '#3b82f6', glow: 'rgba(59, 130, 246, 0.4)' };
    return getGroupColor(this.duplicateInfo.groupId);
  }

  get groupColorGlow15(): string {
    return this.groupColor.glow.replace('0.4)', '0.15)');
  }

  get groupColorGlow40(): string {
    return this.groupColor.glow.replace('0.4)', '0.4)');
  }

  get sameAsTitles(): Array<{
    id: string;
    title?: string | null;
    source_file?: string;
    sourceFile?: string;
  }> {
    if (!this.duplicateInfo?.sameAs || !this.allTitles?.length) return [];
    return this.allTitles.filter(
      (t) => this.duplicateInfo!.sameAs.includes(t.id) || t.id === this.currentTitleId
    );
  }

  getDisplayName(t: { id: string; title?: string | null; source_file?: string; sourceFile?: string }): string {
    return getTitleDisplayName({
      id: t.id,
      title: t.title ?? null,
      sourceFile: t.source_file ?? t.sourceFile,
    });
  }
}
