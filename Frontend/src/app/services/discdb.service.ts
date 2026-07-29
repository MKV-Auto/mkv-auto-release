// src/app/services/discdb.service.ts
import { Injectable } from '@angular/core';
import { environment } from '../environments/environment';

export interface DiscDbResult {
  id: string;
  title: string;
  type: string;
  slug: string;
  image?: string | null;
  year?: number | null;
}

export interface DiscDbDetail extends DiscDbResult {
  synopsis?: string | null;
  releases?: Array<{ year?: number | null; imageUrl?: string | null }>;
  discs?: Array<{ name?: string | null; format?: string | null; contentHash?: string | null; year?: number | null }>;
}

export interface ContributionBundle {
  schema: string;
  generated_at: string;
  disc_id: string;
  content_hash: string | null;
  disc_number: number | null;
  release_slug: string;
  release: Record<string, unknown>;
  disc: Record<string, unknown>;
  summary: string;
  info_log_included: boolean;
}

@Injectable({ providedIn: 'root' })
export class DiscDbService {
  private readonly apiBase = environment.apiBase ?? 'http://localhost:8000';

  async search(term: string): Promise<DiscDbResult[]> {
    const url = `${this.apiBase}/discdb/search?q=${encodeURIComponent(term)}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Search failed (${resp.status})`);
    const data = await resp.json();
    return data.results || [];
  }

  async detail(slug: string): Promise<DiscDbDetail> {
    const url = `${this.apiBase}/discdb/detail?slug=${encodeURIComponent(slug)}`;
    const resp = await fetch(url);
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(text || `Detail failed (${resp.status})`);
    }
    return await resp.json();
  }

  /** #590 — Given a list of search-result titles, returns the subset
   * that matches a movie already in the user's library. The backend
   * normalises (case-insensitive, strips leading "the"/"a"/"an") so
   * "The Goonies" matches "Goonies" and vice versa. Returns an empty
   * Set on transport error rather than throwing — the chip is a soft
   * affordance, the search result list is still useful without it. */
  async libraryMatches(titles: string[]): Promise<Set<string>> {
    if (!titles.length) return new Set();
    try {
      const url = `${this.apiBase}/discdb/library-matches`;
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ titles }),
      });
      if (!resp.ok) return new Set();
      const data = await resp.json();
      return new Set(data.matched_titles || []);
    } catch {
      return new Set();
    }
  }

  /** #86 — TheDiscDB-shaped contribution bundle for a disc. The backend
   * stamps discdb_exported_at / status='exported' on success. */
  async getContributionBundle(discId: string): Promise<ContributionBundle> {
    const resp = await fetch(this.bundleUrl(discId, 'json'));
    if (!resp.ok) throw new Error(await this.bundleError(resp));
    return await resp.json();
  }

  /**
   * #741 — the submission as a zip laid out like the upstream repository.
   *
   * Returns the blob and the server's filename: the name encodes the film and
   * disc number, and reconstructing it here would mean duplicating the backend's
   * sanitising rules in a second place.
   */
  async getContributionZip(discId: string): Promise<{ blob: Blob; filename: string }> {
    const resp = await fetch(this.bundleUrl(discId, 'zip'));
    if (!resp.ok) throw new Error(await this.bundleError(resp));
    return {
      blob: await resp.blob(),
      filename: this.filenameFrom(resp.headers.get('Content-Disposition')) ?? 'thediscdb-submission.zip',
    };
  }

  private bundleUrl(discId: string, format: 'zip' | 'json'): string {
    return `${this.apiBase}/discdb/contributions/${encodeURIComponent(discId)}/bundle?format=${format}`;
  }

  /** The backend puts the actionable reason in `detail`; surface it, not the status code. */
  private async bundleError(resp: Response): Promise<string> {
    let detail = '';
    try {
      detail = (await resp.json())?.detail ?? '';
    } catch {
      detail = '';
    }
    return detail || `Bundle export failed (${resp.status})`;
  }

  private filenameFrom(header: string | null): string | null {
    const m = header?.match(/filename="?([^"]+)"?/);
    return m ? m[1] : null;
  }
}
