import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DiscDbService, DiscDbDetail, DiscDbResult } from '../../services/discdb.service';
import { IconComponent } from '../../ui/icon/icon.component';
import { BtnComponent } from '../../ui/btn/btn.component';
import { EmptyStateComponent } from '../../ui/empty-state/empty-state.component';

@Component({
  selector: 'app-discdb-search',
  standalone: true,
  imports: [CommonModule, FormsModule, IconComponent, BtnComponent, EmptyStateComponent],
  templateUrl: './discdb-search.component.html',
})
export class DiscdbSearchComponent {
  term = '';
  loading = false;
  results: DiscDbResult[] = [];
  error: string | null = null;
  detail: DiscDbDetail | null = null;
  detailLoading = false;
  /** Set of result titles confirmed to exist in the user's library
   * (#590). Populated after every successful search via a single
   * batched POST. A miss just means no chip — the result is still
   * shown, just without the affordance. */
  inLibraryTitles = new Set<string>();

  constructor(private discdb: DiscDbService) {}

  async runSearch(): Promise<void> {
    if (!this.term.trim()) return;
    this.loading = true;
    this.error = null;
    this.detail = null;
    this.results = [];
    this.inLibraryTitles = new Set<string>();
    try {
      this.results = await this.discdb.search(this.term.trim());
      if (!this.results.length) {
        this.error = 'No titles found. Try a different search term.';
      } else {
        // Fire-and-forget overlay so the chip lights up shortly after
        // the cards render. We don't gate the result render on this —
        // the chip is a polish layer, not the primary content.
        this.discdb
          .libraryMatches(this.results.map((r) => r.title))
          .then((matched) => {
            this.inLibraryTitles = matched;
          });
      }
    } catch (err: any) {
      this.error = err?.message || 'Search failed. Please try again.';
      this.results = [];
    } finally {
      this.loading = false;
    }
  }

  /** Template helper — true when this result matches a movie already
   * in the user's library (#590). */
  isInLibrary(item: DiscDbResult): boolean {
    return this.inLibraryTitles.has(item.title);
  }

  async loadDetail(item: DiscDbResult): Promise<void> {
    this.detailLoading = true;
    this.error = null;
    try {
      this.detail = await this.discdb.detail(item.slug);
    } catch (err: any) {
      this.error = err?.message || 'Failed to load details. Please try again.';
    } finally {
      this.detailLoading = false;
    }
  }

  clearDetail(): void {
    this.detail = null;
    this.error = null;
  }

  getTypeIcon(type: string): 'film' | 'tv' {
    const lowerType = type.toLowerCase();
    if (lowerType.includes('tv') || lowerType.includes('series')) {
      return 'tv';
    }
    return 'film';
  }
}
