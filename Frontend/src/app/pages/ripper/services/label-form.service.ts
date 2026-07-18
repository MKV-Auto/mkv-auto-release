// src/app/pages/ripper/services/label-form.service.ts
import { Injectable } from '@angular/core';
import { LoggerService } from '../../../services/logger.service';
import { normalizeTitleTypeForSelect } from '../../../constants/title-type-options';
import { canonicalTrackTitle } from '../../../utils/canonical-track-title.util';

/**
 * Required fields for the Release step (#580). The Release step is "sufficiently
 * complete" only when every field here is populated — not merely when a Release
 * record is linked. Without this, a user with a partially-hydrated release
 * (DiscDB partial response, abandoned earlier session, etc.) could advance to
 * Disc/Titles without ever filling out the metadata needed for downstream
 * output filenames and library organization.
 *
 * Kept as a module-level constant so the same source of truth is reused by
 * the three gates that consume it (see ``isReleaseSufficientlyComplete``).
 */
export const RELEASE_REQUIRED_FIELDS = [
  'release_name',
  'release_slug',
  'release_year',
] as const;


/**
 * Return true when the user has linked a release/boxset AND populated every
 * field in ``RELEASE_REQUIRED_FIELDS`` (#580).
 *
 * Trims string fields before testing — a release_name of ``"   "`` would
 * otherwise sneak past a naive truthiness check.
 */
export function isReleaseSufficientlyComplete(labelForm: LabelForm | null): boolean {
  if (!labelForm) return false;

  // Identifier present (matches the legacy existence-only check).
  const hasReleaseLink =
    !!labelForm.release_id ||
    !!(labelForm.boxset_id && labelForm.boxset_id !== '__pending__');
  if (!hasReleaseLink) return false;

  // Required fields populated. ``release_year`` is numeric — must be a
  // positive integer (0 / negative don't make sense for a release year).
  const name = (labelForm.release_name || '').trim();
  const slug = (labelForm.release_slug || '').trim();
  const year = labelForm.release_year;
  if (!name || !slug) return false;
  if (typeof year !== 'number' || !Number.isInteger(year) || year <= 0) return false;

  return true;
}


export interface LabelForm {
  mode: 'movie' | 'series';
  group_type: 'movie' | 'series';
  disc_group: string;
  disc_number: number | null;
  tmdb_id: string;
  disc_format: 'Blu-Ray' | 'UHD' | 'DVD' | null;
  release_name: string;
  release_slug: string;
  info_title: string | null;
  info_label?: string | null;
  upc: string | null;
  asin: string | null;
  cover_front_url: string | null;
  cover_back_url: string | null;
  release_year: number | null;
  production_year: number | null;
  disc_name: string;
  disc_slug: string;
  movie_id: string | null;
  movie_name?: string | null;
  movie_tmdb_id?: string | null;
  movie_tmdb_type?: string | null;
  movie_cover_url?: string | null;
  movie_cover_path?: string | null;
  movie_production_year?: number | null;
  boxset_id: string | null;
  boxset_slug?: string | null;
  workflow_step: string | null;
  tracks: any[];
  titles?: any[];
  disc_id?: string | null;
  release_id?: string | null;
  recalculate_disc_numbers?: boolean;
}

@Injectable({
  providedIn: 'root'
})
export class LabelFormService {
  constructor(private logger: LoggerService) {}

  /**
   * Build label form from draft payload
   */
  buildLabelForm(
    draft: any,
    applyDefaults: boolean = true,
    pendingGroupType: 'movie' | 'series' = 'movie'
  ): LabelForm {
    const normalizeUiType = (val: any): string => normalizeTitleTypeForSelect(val);

    const normalizeFormat = (fmt: any): string | null => {
      const raw = (fmt || '').toString().toLowerCase();
      if (raw.includes('uhd') || raw.includes('4k')) return 'UHD';
      if (raw.includes('blu') || raw.includes('bd')) return 'Blu-Ray';
      if (raw.includes('dvd')) return 'DVD';
      return raw ? fmt : null;
    };

    const infoLabel = draft?.info_title || draft?.info_label || null;
    const baseReleaseName = draft?.release_name || '';
    const suggestedDiscFormat = normalizeFormat(draft?.disc_format);
    const releaseYear = draft?.release_year ?? draft?.year ?? null;
    const productionYear = draft?.production_year ?? draft?.year ?? null;
    const releaseSlug = draft?.release_slug || draft?.disc_group || '';

    const defaultDiscName = draft?.disc_name ?? draft?.disc_label ?? '';
    const defaultDiscSlug = draft?.disc_slug || '';
    const tracksSource = (draft?.titles && Array.isArray(draft.titles) ? draft.titles : draft?.tracks) || [];
    const tracks = tracksSource.map((t: any, idx: number) => ({
      source_file: t.source_file ?? t.output_file ?? null,
      track_id: t.title_id ?? t.id ?? null,
      title_id: t.title_id ?? t.id ?? null,
      disc_track_id: t.id ?? null,
      title: canonicalTrackTitle(t),
      description: t.description ?? t.note ?? '',
      note: t.description ?? t.note ?? '',
      comment: t.comment ?? null,
      season: t.season ?? null,
      episode: t.episode ?? null,
      type: normalizeUiType(t.type) || (t.content === false ? 'ignore' : ''),
      output_file: t.output_file || null,
      preview_url: t.preview_url || t.output_file || null,
      duration: t.duration ?? null,
      size: t.size ?? null,
      streams: t.streams ?? t.probe?.streams ?? null,
      chapters: t.chapters ?? null,
    }));

    // Filter out 'boxset' as a type - boxset is a relationship, not a type
    const rawGroupType = draft?.group_type || draft?.title_type || pendingGroupType || 'movie';
    const normalizedGroupType = (rawGroupType === 'boxset' ? 'movie' : rawGroupType) as 'movie' | 'series';
    const rawMode = draft?.mode || pendingGroupType || 'movie';
    const normalizedMode = (rawMode === 'boxset' ? 'movie' : rawMode) as 'movie' | 'series';

    const form: LabelForm = {
      mode: normalizedMode,
      group_type: normalizedGroupType,
      disc_group: draft?.disc_group || draft?.release_slug || '',
      disc_number: draft?.disc_number ?? null,
      tmdb_id: '',
      disc_format: (suggestedDiscFormat as 'Blu-Ray' | 'UHD' | 'DVD' | null) || null,
      release_name: baseReleaseName,
      release_slug: releaseSlug,
      info_title: infoLabel,
      upc: null,
      asin: null,
      cover_front_url: null,
      cover_back_url: null,
      release_year: releaseYear,
      production_year: productionYear,
      disc_name: defaultDiscName,
      disc_slug: defaultDiscSlug,
      movie_id: draft?.movie_id || null,
      boxset_id: draft?.boxset_id || null,
      workflow_step: draft?.workflow_step || null,
      tracks,
    };

    if (draft?.workflow_step) {
      this.logger.log('[LabelForm] buildLabelForm - Restored workflow_step from draft:', draft.workflow_step);
    }

    return form;
  }

  /**
   * Build metadata payload for saving
   */
  buildMetadataPayload(
    labelForm: LabelForm | null,
    releaseId: string | null = null
  ): {
    release: any;
    disc: any;
    titles: any[];
  } {
    if (!labelForm) {
      return { release: {}, disc: {}, titles: [] };
    }

    const clean = (v: any) => {
      if (v === undefined || v === null) return null;
      if (typeof v === 'string' && v.trim() === '') return null;
      return v;
    };

    const asNumber = (v: any) => {
      if (v === undefined || v === null || `${v}`.trim() === '') return null;
      const n = Number(v);
      return Number.isNaN(n) ? null : n;
    };

    const releaseSlug = clean(labelForm.release_slug || labelForm.disc_group);
    const isLinkedToBoxset = !!(labelForm.boxset_id);

    const releasePayload: any = {
      release_id: releaseId || labelForm.release_id || null,
      release_slug: releaseSlug,
      release_name: clean(labelForm.release_name),
      info_title: clean(labelForm.info_title),
      production_year: asNumber(labelForm.production_year),
      tmdb_id: clean(labelForm.tmdb_id),
      group_type: clean(labelForm.group_type || labelForm.mode),
      mode: clean(labelForm.mode),
      boxset_id: clean(labelForm.boxset_id),
    };

    // Only include boxset-owned fields if NOT linked to a boxset
    if (!isLinkedToBoxset) {
      releasePayload.release_year = asNumber(labelForm.release_year);
      releasePayload.upc = clean(labelForm.upc);
      releasePayload.asin = clean(labelForm.asin);
      releasePayload.cover_front_url = clean(labelForm.cover_front_url);
      releasePayload.cover_back_url = clean(labelForm.cover_back_url);
    }

    return {
      release: releasePayload,
      disc: {
        disc_number: asNumber(labelForm.disc_number),
        disc_slug: clean(labelForm.disc_slug),
        disc_name: clean(labelForm.disc_name),
        disc_format: clean(labelForm.disc_format),
        info_title: clean(labelForm.info_title),
        disc_group: releaseSlug,
      },
      titles: Array.isArray(labelForm.tracks)
        ? labelForm.tracks.map((t: any) => ({
            source_file: t.source_file ?? null,
            title_id: t.title_id ?? null,
            track_id: t.title_id ?? null,
            title: clean(t.title),
            description: clean(t.description ?? t.note),
            comment: clean(t.comment),
            season: asNumber(t.season),
            episode: asNumber(t.episode),
            type: clean(t.type),
            duration: asNumber(t.duration),
            size: asNumber(t.size),
            streams: t.streams ?? null,
          }))
        : [],
    };
  }

  /**
   * Build release patch payload
   */
  buildReleasePatchPayload(labelForm: LabelForm | null): any {
    if (!labelForm) return {};

    const clean = (v: any) => {
      if (v === undefined || v === null) return null;
      if (typeof v === 'string' && v.trim() === '') return null;
      return v;
    };

    const asNumber = (v: any) => {
      if (v === undefined || v === null || `${v}`.trim() === '') return null;
      const n = Number(v);
      return Number.isNaN(n) ? null : n;
    };

    const isLinkedToBoxset = !!labelForm.boxset_id;
    const payload: any = {
      release_name: clean(labelForm.release_name),
      production_year: asNumber(labelForm.production_year),
      movie_id: clean(labelForm.movie_id),
      tmdb_id: clean(labelForm.tmdb_id),
      group_type: clean(labelForm.group_type || labelForm.mode),
      mode: clean(labelForm.mode),
      boxset_id: clean(labelForm.boxset_id),
    };

    if (labelForm.recalculate_disc_numbers) {
      payload.recalculate_disc_numbers = true;
    }

    // Only include boxset-owned fields if NOT linked to a boxset
    if (!isLinkedToBoxset) {
      payload.release_year = asNumber(labelForm.release_year);
      payload.upc = clean(labelForm.upc);
      payload.asin = clean(labelForm.asin);
      payload.cover_front_url = clean(labelForm.cover_front_url);
      payload.cover_back_url = clean(labelForm.cover_back_url);
    }

    return payload;
  }

  /**
   * Validate label form
   */
  validateLabelForm(labelForm: LabelForm | null, isDiscDbHit: boolean = false): { valid: boolean; errors: string[] } {
    // No validation needed if there's no labelForm (e.g., DiscDB hits)
    if (!labelForm) {
      return { valid: true, errors: [] };
    }

    // No validation needed for DiscDB hits
    if (isDiscDbHit) {
      return { valid: true, errors: [] };
    }

    const errors: string[] = [];
    const f = labelForm;

    if (!f.movie_id) errors.push('Movie ID is required (lookup from TMDB URL)');
    if (!f.mode) errors.push('Mode is required');
    if (!f.disc_format) errors.push('Disc format is required');
    if (!f.release_slug) errors.push('Release slug is required');
    if (!f.disc_name) errors.push('Disc name is required');
    // disc_slug optional: leave blank to auto-generate from disc name on save
    if (!f.disc_group) errors.push('Group slug is required');
    if (!f.group_type) errors.push('Group type is required');

    return {
      valid: errors.length === 0,
      errors,
    };
  }

  /**
   * Merge user edits into rebuilt form
   */
  mergeUserEdits(
    rebuilt: LabelForm,
    prevForm: LabelForm | null,
    priorLocks: { discNameLocked: boolean; discSlugLocked: boolean }
  ): LabelForm {
    if (!prevForm) return rebuilt;

    // Preserve user edits while allowing backend updates
    const merged: LabelForm = {
      ...rebuilt,
      // Preserve user-entered values unless locked
      disc_name: priorLocks.discNameLocked && prevForm.disc_name ? prevForm.disc_name : rebuilt.disc_name,
      disc_slug: priorLocks.discSlugLocked && prevForm.disc_slug ? prevForm.disc_slug : rebuilt.disc_slug,
      // Preserve other user edits
      release_name: prevForm.release_name || rebuilt.release_name,
      release_slug: prevForm.release_slug || rebuilt.release_slug,
      disc_group: prevForm.disc_group || rebuilt.disc_group,
      // Preserve IDs
      disc_id: prevForm.disc_id || rebuilt.disc_id,
      release_id: prevForm.release_id || rebuilt.release_id,
      movie_id: prevForm.movie_id || rebuilt.movie_id,
      boxset_id: prevForm.boxset_id || rebuilt.boxset_id,
      // Preserve workflow step
      workflow_step: prevForm.workflow_step || rebuilt.workflow_step,
      // Merge tracks - prefer user edits but allow backend updates
      tracks: this.mergeTracks(rebuilt.tracks, prevForm.tracks),
    };

    return merged;
  }

  /**
   * Merge tracks arrays, preserving user edits
   */
  private mergeTracks(rebuilt: any[], prev: any[]): any[] {
    if (!prev || prev.length === 0) return rebuilt;
    if (!rebuilt || rebuilt.length === 0) return prev;

    // Create a map of previous tracks by source_file
    const prevMap = new Map(prev.map(t => [t.title_id, t]));

    // Merge: use rebuilt as base, but preserve user edits from prev
    return rebuilt.map(t => {
      const key = t.title_id;
      const prevTrack = prevMap.get(key);
      if (!prevTrack) return t;

      // Merge: prefer user edits but allow backend updates for missing fields
      return {
        ...t,
        title: canonicalTrackTitle({
          title: prevTrack.title || t.title,
          episode_name: prevTrack.episode_name || t.episode_name,
        }),
        description: prevTrack.description || t.description,
        comment: prevTrack.comment || t.comment,
        season: prevTrack.season ?? t.season,
        episode: prevTrack.episode ?? t.episode,
        type: prevTrack.type || t.type,
      };
    });
  }

  /**
   * Check if label form has content
   */
  hasLabelContent(labelForm: LabelForm | null): boolean {
    if (!labelForm) return false;
    return !!(
      labelForm.movie_id ||
      labelForm.release_name ||
      labelForm.disc_name ||
      labelForm.disc_slug ||
      (labelForm.tracks && labelForm.tracks.length > 0)
    );
  }
}

