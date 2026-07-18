import { Component, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { getGroupColor, getGroupIdentifier, type DuplicateInfo } from '../../utils/duplicate-tags.util';
import { DuplicateGroupBadgeComponent } from '../duplicate-group-badge/duplicate-group-badge.component';
import { DuplicateGroupMemberListComponent } from '../duplicate-group-member-list/duplicate-group-member-list.component';

/** Expandable header showing "Duplicate Group X" and member list when expanded. */
@Component({
  selector: 'app-duplicate-group-header',
  standalone: true,
  imports: [CommonModule, DuplicateGroupBadgeComponent, DuplicateGroupMemberListComponent],
  template: `
    <ng-container *ngIf="duplicateInfo && (duplicateInfo.effectiveGroupSize ?? duplicateInfo.groupSize) > 1">
      <button
        type="button"
        class="duplicate-group-header"
        [style.borderColor]="groupColor.glow + '40'"
        [style.background]="groupColor.glow + '15'"
        (click)="expanded = !expanded"
        [attr.aria-expanded]="expanded"
      >
        <app-duplicate-group-badge [duplicateInfo]="duplicateInfo" [showLabel]="true" size="sm"></app-duplicate-group-badge>
        <span class="duplicate-group-header-text">Duplicate Group {{ identifier }} ({{ duplicateInfo.effectiveGroupSize ?? duplicateInfo.groupSize }} titles)</span>
        <span class="duplicate-group-header-chevron" [class.expanded]="expanded" aria-hidden="true">▼</span>
      </button>
      <div *ngIf="expanded" class="duplicate-group-header-list">
        <app-duplicate-group-member-list
          [duplicateInfo]="duplicateInfo"
          [allTitles]="allTitles"
          [currentTitleId]="currentTitleId"
          (navigateToTitle)="navigateToTitle.emit($event)"
        ></app-duplicate-group-member-list>
      </div>
    </ng-container>
  `,
  styles: [
    `
      .duplicate-group-header {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        width: 100%;
        padding: 0.5rem 0.75rem;
        border-radius: 0.5rem;
        border: 1px solid;
        background: rgba(255, 255, 255, 0.03);
        color: rgba(255, 255, 255, 0.9);
        font-size: 0.875rem;
        text-align: left;
        cursor: pointer;
      }
      .duplicate-group-header:hover {
        background: rgba(255, 255, 255, 0.05);
      }
      .duplicate-group-header-text {
        flex: 1;
      }
      .duplicate-group-header-chevron {
        font-size: 0.75rem;
        transition: transform 0.2s;
      }
      .duplicate-group-header-chevron.expanded {
        transform: rotate(180deg);
      }
      .duplicate-group-header-list {
        margin-top: 0.5rem;
        padding-left: 0.25rem;
      }
    `,
  ],
})
export class DuplicateGroupHeaderComponent {
  @Input() duplicateInfo: DuplicateInfo | null | undefined = null;
  @Input() allTitles: Array<{ id: string; title?: string | null; source_file?: string; sourceFile?: string }> = [];
  @Input() currentTitleId = '';
  @Output() navigateToTitle = new EventEmitter<string>();

  expanded = false;

  get groupColor(): { color: string; glow: string } {
    if (!this.duplicateInfo?.groupId) return { color: '#3b82f6', glow: 'rgba(59, 130, 246, 0.4)' };
    return getGroupColor(this.duplicateInfo.groupId);
  }

  get identifier(): string {
    return this.duplicateInfo?.groupId ? getGroupIdentifier(this.duplicateInfo.groupId) : '1';
  }
}
