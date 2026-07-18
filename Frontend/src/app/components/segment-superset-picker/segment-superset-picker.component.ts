import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { BtnComponent } from '../../ui/btn/btn.component';
import { CardComponent } from '../../ui/card/card.component';
import { IconComponent } from '../../ui/icon/icon.component';
import { PillComponent } from '../../ui/pill/pill.component';
import { SupersetCandidate } from '../../services/job.service';

/**
 * Path B iteration — modal that surfaces subsequence-superset matcher
 * candidates after the user has confirmed their order via the
 * confirmation gate. Each "cluster" is a group of mpls that share a
 * sorted-segment-set (i.e. they're permutations of each other within
 * the superset family) — the cluster with the most members is most
 * likely to contain the real movie.
 *
 * UX:
 *  - The first (largest) cluster is rendered expanded.
 *  - Additional clusters render collapsed with a header summarising
 *    member count + the extras shared across the cluster.
 *  - Within a cluster, candidates are sorted by fewest extras first
 *    (closest match) then by largest mpls size.
 *  - Clicking a candidate emits `select(candidate)`; the parent page
 *    triggers the rip + slice-into-previews flow on that mpls.
 */

export type SegmentSupersetCluster = SupersetCandidate[];

@Component({
  selector: 'app-segment-superset-picker',
  standalone: true,
  imports: [CommonModule, CardComponent, PillComponent, BtnComponent, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './segment-superset-picker.component.html',
  styleUrls: ['./segment-superset-picker.component.scss'],
})
export class SegmentSupersetPickerComponent {
  /** Clusters in display order: largest first. */
  @Input() clusters: SegmentSupersetCluster[] = [];

  /** Candidate the parent is currently acting on (rip in flight).
   *  Disables the rest of the picker to prevent double-fire. */
  @Input() pendingTitleIndex: number | null = null;

  /** User picked a candidate to rip. */
  @Output() select = new EventEmitter<SupersetCandidate>();

  /** User dismissed the picker without choosing. */
  @Output() dismiss = new EventEmitter<void>();

  /** Tracks expanded-cluster index (0-based). Top cluster expanded by default. */
  expandedIndex = 0;

  trackByClusterKey(i: number, cluster: SegmentSupersetCluster): string {
    return cluster[0]?.sorted_set_key || String(i);
  }

  trackByCandidate(i: number, c: SupersetCandidate): number {
    return c.title_index;
  }

  get isPending(): boolean {
    return this.pendingTitleIndex !== null;
  }

  toggleCluster(i: number): void {
    if (this.isPending) return;
    this.expandedIndex = this.expandedIndex === i ? -1 : i;
  }

  onSelect(candidate: SupersetCandidate): void {
    if (this.isPending) return;
    this.select.emit(candidate);
  }

  onBackdropClick(event: MouseEvent): void {
    if (this.isPending) return;
    if ((event.target as HTMLElement).classList.contains('ssp-backdrop')) {
      this.dismiss.emit();
    }
  }

  clusterSize(cluster: SegmentSupersetCluster): number {
    return cluster.length;
  }

  totalExtrasForCluster(cluster: SegmentSupersetCluster): number {
    return cluster.reduce((acc, c) => acc + c.extras_clips.length, 0);
  }

  formatSize(bytes: number | null): string {
    if (bytes == null) return 'unknown size';
    const gb = bytes / (1024 ** 3);
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    const mb = bytes / (1024 ** 2);
    return `${mb.toFixed(0)} MB`;
  }
}
