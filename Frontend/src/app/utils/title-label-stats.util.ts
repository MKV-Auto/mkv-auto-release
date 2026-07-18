import { canonicalTrackTitle } from './canonical-track-title.util';
import { buildTitleLabelEntities, getPrimaryTitleForEntity, type TitleLabelEntity } from './title-label-entities.util';
import { TITLE_TYPE_STATS_ORDER } from '../constants/title-type-options';

function hasTitleOrEpisodeName(title: any): boolean {
  return !!canonicalTrackTitle(title);
}

/** Same rules as WorkflowService.areTitlesComplete (single title). */
export function isTitleLabelComplete(title: any): boolean {
  if (!title) return false;
  const titleType = title.type;
  if (titleType == null || titleType === '') {
    return false;
  }
  const typeLower = String(titleType).trim().toLowerCase();
  if (typeLower === 'ignore') {
    return true;
  }
  if (typeLower === 'episode') {
    const hasSeason = title.season != null && title.season !== '';
    const hasEpisode = title.episode != null && title.episode !== '';
    const hasName = hasTitleOrEpisodeName(title);
    return hasSeason && hasEpisode && hasName;
  }
  return hasTitleOrEpisodeName(title);
}

export function isTitleIgnoredForStats(title: any): boolean {
  const raw = (title?.type ?? '').toString().trim();
  return raw.toLowerCase() === 'ignore';
}

export interface TitleLabelStats {
  /** Logical cards (duplicate groups count once). */
  total: number;
  ignored: number;
  labeledComplete: number;
  remainingIncomplete: number;
  /** Non-ignored entities only, keyed by representative type string. */
  byType: Record<string, number>;
  /** Raw rows in context.titles (tracks). */
  rawTitleCount: number;
}

function representativeForEntity(entity: TitleLabelEntity): any {
  if (entity.kind === 'group') {
    return getPrimaryTitleForEntity(entity.titles);
  }
  return entity.title;
}

/**
 * Titles workflow step: complete when each logical entity satisfies labeling rules
 * (duplicate group checked via primary only; sync keeps sibling rows aligned for API validation).
 */
export function areLabelTitlesComplete(titles: any[] | null | undefined): boolean {
  const list = titles ?? [];
  if (!list.length) return false;
  for (const entity of buildTitleLabelEntities(list)) {
    const rep = representativeForEntity(entity);
    if (!isTitleLabelComplete(rep)) return false;
  }
  return true;
}

/**
 * Aggregate label progress by logical entity (duplicate groups = one).
 */
export function computeTitleLabelStats(titles: any[] | null | undefined): TitleLabelStats {
  const list = titles ?? [];
  const rawTitleCount = list.length;
  const entities = buildTitleLabelEntities(list);
  let ignored = 0;
  let labeledComplete = 0;
  let remainingIncomplete = 0;
  const byType: Record<string, number> = {};

  for (const entity of entities) {
    const rep = representativeForEntity(entity);
    if (!rep) {
      remainingIncomplete += 1;
      continue;
    }
    if (isTitleIgnoredForStats(rep)) {
      ignored += 1;
      continue;
    }
    if (isTitleLabelComplete(rep)) {
      labeledComplete += 1;
      const key = (rep.type ?? '').toString().trim() || '(no type)';
      byType[key] = (byType[key] ?? 0) + 1;
    } else {
      remainingIncomplete += 1;
    }
  }

  return {
    total: entities.length,
    ignored,
    labeledComplete,
    remainingIncomplete,
    byType,
    rawTitleCount,
  };
}

/** Sort type keys for disclosure: known order first, then alphabetical. */
export const TITLE_STATS_TYPE_ORDER = TITLE_TYPE_STATS_ORDER;

export function sortTitleStatsTypeEntries(byType: Record<string, number>): { type: string; count: number }[] {
  const entries = Object.entries(byType).map(([type, count]) => ({ type, count }));
  const rank = (t: string): number => {
    const idx = TITLE_STATS_TYPE_ORDER.findIndex((v) => v.toLowerCase() === t.toLowerCase());
    return idx === -1 ? 100 + t.charCodeAt(0) : idx;
  };
  entries.sort((a, b) => {
    const ra = rank(a.type);
    const rb = rank(b.type);
    if (ra !== rb) return ra - rb;
    return a.type.localeCompare(b.type);
  });
  return entries;
}
