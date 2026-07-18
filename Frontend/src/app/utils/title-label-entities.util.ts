/**
 * Duplicate grouping for the title label step — shared by TitleLabelComponent and label stats.
 * Mirrors TitleLabelComponent duplicate logic (duplicate_info / duplicateInfo, groupId, effectiveGroupSize).
 */

import type { DuplicateMetrics } from '../models/duplicate';

export function getDiscTitleId(title: any): string | null {
  if (!title) return null;
  return title.title_id || null;
}

export interface ParsedDuplicateInfo {
  groupId: string;
  groupSize: number;
  effectiveGroupSize: number;
  sameAs: string[];
  tags: any[];
  diffTags: any[];
  metrics?: DuplicateMetrics | null;
  confidence: string;
}

/** Same behavior as TitleLabelComponent.getDuplicateInfo (camelCase out). */
export function parseDuplicateInfo(title: any, allTitles: any[] | null | undefined): ParsedDuplicateInfo | null {
  if (!title) return null;
  const raw = title.duplicateInfo ?? title.duplicate_info ?? null;
  if (!raw || typeof raw !== 'object') return null;
  const groupSize = typeof raw.groupSize === 'number' ? raw.groupSize : (raw.group_size ?? 0);
  const sameAs = Array.isArray(raw.sameAs) ? raw.sameAs : (raw.same_as ?? []);
  const currentId = getDiscTitleId(title) ?? '';
  const groupIds = new Set<string>([currentId, ...sameAs]);
  const titles = allTitles ?? [];
  const effectiveGroupSize = titles.filter((t) => groupIds.has(getDiscTitleId(t) ?? '')).length;
  const m = raw.metrics;
  const metrics =
    m && typeof m === 'object'
      ? {
          chaptersCount: typeof m.chaptersCount === 'number' ? m.chaptersCount : (m.chapters_count ?? 0),
          subtitleTrackCount:
            typeof m.subtitleTrackCount === 'number' ? m.subtitleTrackCount : (m.subtitle_track_count ?? 0),
          subtitleLanguageCount:
            typeof m.subtitleLanguageCount === 'number'
              ? m.subtitleLanguageCount
              : (m.subtitle_language_count ?? 0),
          audioScore: typeof m.audioScore === 'number' ? m.audioScore : (m.audio_score ?? 0),
          audioLanguageCount:
            typeof m.audioLanguageCount === 'number' ? m.audioLanguageCount : (m.audio_language_count ?? 0),
          videoBitrate:
            m.videoBitrate != null && Number.isFinite(Number(m.videoBitrate))
              ? Number(m.videoBitrate)
              : m.video_bitrate != null && Number.isFinite(Number(m.video_bitrate))
                ? Number(m.video_bitrate)
                : null,
          videoPixels: typeof m.videoPixels === 'number' ? m.videoPixels : (m.video_pixels ?? 0),
          scanUsable: !!(m.scanUsable ?? m.scan_usable),
        }
      : null;
  return {
    groupId: raw.groupId ?? raw.group_id ?? '',
    groupSize,
    effectiveGroupSize,
    sameAs,
    tags: Array.isArray(raw.tags) ? raw.tags : [],
    diffTags: Array.isArray(raw.diffTags) ? raw.diffTags : (Array.isArray(raw.diff_tags) ? raw.diff_tags : []),
    metrics,
    confidence: raw.confidence ?? 'high',
  };
}

function isTitleTypeIgnore(title: any): boolean {
  return (title?.type || '').toString().toLowerCase() === 'ignore';
}

function getSubsumedBy(title: any): string | null {
  const v = title?.subsumed_by_title_id;
  if (v == null) return null;
  const s = String(v).trim();
  return s ? s : null;
}

/**
 * True iff `title` is an m2ts component clip of some wrapping mpls on the
 * same disc (backend sets `subsumed_by_title_id` to the wrapper's id).
 * Component clips are not duplicates of their wrapper — they're playback
 * pieces stitched together by the playlist.
 */
export function isComponentClip(title: any): boolean {
  return getSubsumedBy(title) !== null;
}

/**
 * Real-duplicate sibling count for `title`: members of its
 * `duplicate_info.same_as` list that exist in `allTitles` and are NOT
 * component clips (i.e. don't have `subsumed_by_title_id` set). Use this
 * — not `effectiveGroupSize` — anywhere that says "N duplicates", because
 * `same_as` lumps real permutation siblings together with subsumed m2ts.
 */
export function realSiblingCount(title: any, allTitles: any[] | null | undefined): number {
  const info = parseDuplicateInfo(title, allTitles ?? null);
  if (!info) return 0;
  const sameAs = new Set<string>((info.sameAs ?? []).map((s) => String(s)));
  if (sameAs.size === 0) return 0;
  let count = 0;
  for (const t of allTitles ?? []) {
    const id = getDiscTitleId(t);
    if (!id || !sameAs.has(id)) continue;
    if (isComponentClip(t)) continue;
    count += 1;
  }
  return count;
}

/**
 * Number of m2ts component clips wrapped by `title` — every title in
 * `allTitles` whose `subsumed_by_title_id` points back to `title.title_id`.
 * Mirrors `TitleLabelComponent.getComponentClips` (same membership rule);
 * exposed as a util so badge/util callers don't need a component handle.
 */
export function componentClipCount(title: any, allTitles: any[] | null | undefined): number {
  const id = getDiscTitleId(title);
  if (!id) return 0;
  let count = 0;
  for (const t of allTitles ?? []) {
    if (getSubsumedBy(t) === id) count += 1;
  }
  return count;
}

function sortTitlesWithinDuplicateGroup(gTitles: any[]): void {
  gTitles.sort((a: any, b: any) => {
    const aActive = a.active === true ? 0 : 1;
    const bActive = b.active === true ? 0 : 1;
    if (aActive !== bActive) return aActive - bActive;
    return (a.order_index ?? Infinity) - (b.order_index ?? Infinity);
  });
}

/** Grouped duplicate titles (multi-member only), same rules as TitleLabelComponent.computeDuplicateGroups.
 *
 * Subsumed m2ts (component clips of a wrapping mpls) are filtered out of
 * `same_as`-driven groups — they aren't duplicates of the wrapper, they're
 * playback pieces. A group whose remaining members reduce to a singleton
 * (e.g. a lone wrapper whose only `same_as` entries are component clips)
 * is dropped so the wrapper renders as a `kind: 'single'` row instead of
 * a misleading "duplicate group" card. The wrapper's editor surfaces the
 * clips under the dedicated "Component clips" section.
 */
export function computeDuplicateGroupsFromTitles(titles: any[] | null | undefined): { groupId: string; titles: any[] }[] {
  const list = titles ?? [];
  const groupMap = new Map<string, any[]>();
  for (const t of list) {
    if (isComponentClip(t)) continue;
    const info = parseDuplicateInfo(t, list);
    if (!info || (info.effectiveGroupSize ?? 0) <= 1) continue;
    const gid = info.groupId;
    if (!gid) continue;
    if (!groupMap.has(gid)) groupMap.set(gid, []);
    groupMap.get(gid)!.push(t);
  }
  const groups: { groupId: string; titles: any[] }[] = [];
  for (const [groupId, gTitles] of groupMap) {
    if (gTitles.length <= 1) continue;
    sortTitlesWithinDuplicateGroup(gTitles);
    groups.push({ groupId, titles: gTitles });
  }
  groups.sort((a, b) => {
    const aHasActive = a.titles.some((t: any) => t.active === true) ? 0 : 1;
    const bHasActive = b.titles.some((t: any) => t.active === true) ? 0 : 1;
    if (aHasActive !== bHasActive) return aHasActive - bHasActive;
    return (a.titles[0]?.order_index ?? Infinity) - (b.titles[0]?.order_index ?? Infinity);
  });
  return groups;
}

/**
 * Primary variant for duplicate groups: trust `active === true` from the backend when set — that row
 * holds the authoritative metadata; secondaries are often type `ignore` on purpose. Only when no row
 * is active do we fall back to the first non-ignore, then any row.
 */
export function getPrimaryTitleForEntity(groupTitles: any[]): any {
  if (!groupTitles?.length) return null;
  const activeRow = groupTitles.find((t: any) => t.active === true);
  if (activeRow) return activeRow;
  const nonIgnore = groupTitles.find((t: any) => !isTitleTypeIgnore(t));
  if (nonIgnore) return nonIgnore;
  return groupTitles[0];
}

export type TitleLabelEntity =
  | { kind: 'group'; groupId: string; titles: any[] }
  | { kind: 'single'; title: any };

/**
 * One logical “card” per duplicate group (multi-member) plus one per non-duplicate title.
 */
export function buildTitleLabelEntities(titles: any[] | null | undefined): TitleLabelEntity[] {
  const list = titles ?? [];
  const groups = computeDuplicateGroupsFromTitles(list);
  const duplicateIds = new Set<string>();
  for (const g of groups) {
    for (const t of g.titles) {
      const id = getDiscTitleId(t);
      if (id) duplicateIds.add(id);
    }
  }
  const entities: TitleLabelEntity[] = groups.map((g) => ({
    kind: 'group' as const,
    groupId: g.groupId,
    titles: g.titles,
  }));
  for (const t of list) {
    const id = getDiscTitleId(t);
    if (!id || duplicateIds.has(id)) continue;
    entities.push({ kind: 'single', title: t });
  }
  return entities;
}
