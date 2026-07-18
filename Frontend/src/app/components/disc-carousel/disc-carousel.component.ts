import { Component, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { DiscMetadata } from '../../services/workflow.service';

export type CardType = { type: 'drive' | 'job', id: string, data: DiscMetadata };

@Component({
  selector: 'app-disc-carousel',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './disc-carousel.component.html',
  styleUrls: ['./disc-carousel.component.scss'],
})
export class DiscCarouselComponent {
  @Input() discs: DiscMetadata[] = [];
  @Input() selectedCard: { type: 'drive' | 'job', id: string } | null = null;
  @Input() driveLoadingStates: Map<string, boolean> = new Map();
  @Input() driveScanState: string = 'idle';
  @Input() movieOptions: any[] = [];
  @Input() discTitleFn?: (disc: DiscMetadata) => string;
  @Input() discMetaFn?: (disc: DiscMetadata) => string;

  @Output() cardSelected = new EventEmitter<{ type: 'drive' | 'job', id: string }>();

  get allCards(): CardType[] {
    // Sort discs: drive cards first, then unfinished job cards ordered by creation time (newest first)
    const sortedDiscs = [...this.discs].sort((a, b) => {
      // Drive cards come first
      if (a.disc_state === 'in_drive' && b.disc_state === 'unfinished') {
        return -1;
      }
      if (a.disc_state === 'unfinished' && b.disc_state === 'in_drive') {
        return 1;
      }
      
      // For unfinished discs, sort by created_at (newest first)
      if (a.disc_state === 'unfinished' && b.disc_state === 'unfinished') {
        const aCreated = a.created_at ? new Date(a.created_at).getTime() : 0;
        const bCreated = b.created_at ? new Date(b.created_at).getTime() : 0;
        return bCreated - aCreated; // Descending order (newest first)
      }
      
      // Drive cards maintain their original order (no sorting needed)
      return 0;
    });
    
    return sortedDiscs.map(disc => ({
      type: disc.disc_state === 'in_drive' ? 'drive' as const : 'job' as const,
      id: disc.disc_state === 'in_drive' 
        ? (disc.mount_point || disc.disc_id)
        : (disc.job_id || disc.disc_id),
      data: disc
    }));
  }

  trackByCardId(index: number, card: CardType): string {
    return `${card.type}-${card.id}`;
  }

  isCardActive(card: CardType): boolean {
    if (!this.selectedCard) return false;
    return this.selectedCard.type === card.type && this.selectedCard.id === card.id;
  }

  isCardLoading(card: CardType): boolean {
    if (card.type === 'drive') {
      // Check scan_state from coordinator - show loading if pending or scanning
      const scanState = card.data.scan_state;
      if (scanState === 'pending' || scanState === 'scanning') {
        return true;
      }
      // Fallback to driveLoadingStates for backward compatibility
      return this.driveLoadingStates.get(card.id) || false;
    }
    return false;
  }

  onCardClick(card: CardType): void {
    // Prevent clicks during scanning for drive cards
    if (card.type === 'drive') {
      const scanState = card.data.scan_state;
      if (scanState === 'pending' || scanState === 'scanning' || this.driveScanState === 'scanning') {
        return; // Don't allow selection during scanning
      }
    }
    // Emit the card selection event
    this.cardSelected.emit({ type: card.type, id: card.id });
  }
  
  getCardType(disc: DiscMetadata): 'drive' | 'job' {
    return disc.disc_state === 'in_drive' ? 'drive' : 'job';
  }

  onMouseEnter(card: CardType): void {
    // Hover state handled by CSS
  }

  onMouseLeave(card: CardType): void {
    // Hover state handled by CSS
  }

  onMouseDown(card: CardType): void {
    // Mouse down handled by click event
  }

  /** Card title: release title (release_name), then movie name, then info_title. */
  getDiscTitle(disc: DiscMetadata): string {
    if (this.discTitleFn) {
      return this.discTitleFn(disc);
    }
    if (disc.disc_state === 'in_drive') {
      if (!disc.release_name && !disc.movie_name && !disc.info_title && !disc.disc_hash) {
        return 'Insert Disc';
      }
      if (disc.release_name) {
        return disc.release_name;
      }
      if (disc.movie_name) {
        return disc.movie_name;
      }
      if (disc.info_title) {
        return disc.info_title;
      }
      return `Drive ${disc.disc_num || '?'}`;
    }
    if (disc.release_name) {
      return disc.release_name;
    }
    if (disc.movie_name) {
      return disc.movie_name;
    }
    if (disc.info_title) {
      return disc.info_title;
    }
    return 'Unknown Disc';
  }

  getDiscMeta(disc: DiscMetadata): string {
    if (this.discMetaFn) {
      return this.discMetaFn(disc);
    }
    
    const parts: string[] = [];
    const year = disc.production_year || disc.release_year;
    if (year) {
      parts.push(`(${year})`);
    }
    // Disc Format only (Blu-Ray, UHD, or DVD)
    if (disc.disc_format) {
      parts.push(disc.disc_format);
    }
    return parts.length > 0 ? parts.join(' · ') : '—';
  }
  
  // For drive cards, show mount point
  getDriveMount(disc: DiscMetadata): string {
    return disc.mount_point || '';
  }
  
  // For job cards, show stage (simplified - job status not available in DiscMetadata)
  getJobStage(disc: DiscMetadata): string {
    // DiscMetadata doesn't include job status, so we can't determine stage
    // This will need to be enhanced if we want to show job stages
    return 'Unfinished';
  }
}
