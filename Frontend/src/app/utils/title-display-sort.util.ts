import { normalizeSegmentMap } from './segment-map.util';
import { TITLE_TYPE_STATS_ORDER } from '../constants/title-type-options';

/** Cluster key: shared segment_map groups variants; otherwise one title per key. */
function clusterKey(t: any): string {
  const raw = t?.segment_map ?? t?.segmentMap;
  const n = normalizeSegmentMap(raw);
  if (n !== null) return `seg:${n}`;
  return `id:${(t?.title_id ?? '').toString()}`;
}

function compareTitles(
  a: any,
  b: any,
  titleActiveFn?: (id: string | null | undefined) => boolean
): number {
  const typeOrder = TITLE_TYPE_STATS_ORDER.filter((t) => t !== 'ignore');
  const normalizeType = (t: any): string => (t?.type || '').toString().trim();
  const isIgnore = (t: any): boolean => {
    const raw = normalizeType(t);
    if (raw) return raw.toLowerCase() === 'ignore';
    return t?.content === false;
  };
  const typeRank = (t: any): number => {
    if (isIgnore(t)) return 99;
    const val = normalizeType(t);
    if (!val) return 50;
    const idx = typeOrder.findIndex((v) => v.toLowerCase() === val.toLowerCase());
    return idx === -1 ? 40 : idx;
  };
  const seasonVal = (t: any): number => {
    const raw = t?.season;
    if (typeof raw === 'number') return raw;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : -1;
  };
  const episodeVal = (t: any): number => {
    const raw = t?.episode;
    if (typeof raw === 'number') return raw;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : -1;
  };
  const durationVal = (t: any): number => {
    const raw = t?.duration || t?.duration_seconds;
    if (typeof raw === 'number') return raw;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : -1;
  };
  const sizeVal = (t: any): number => {
    const raw = t?.size;
    if (typeof raw === 'number') return raw;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : -1;
  };
  const orderVal = (t: any): number =>
    typeof t?.order_index === 'number' ? t.order_index : Number.MAX_SAFE_INTEGER;
  const stableKey = (t: any): string => (t?.title_id ?? '').toString();

  if (titleActiveFn) {
    const aActive = titleActiveFn(a.title_id || null);
    const bActive = titleActiveFn(b.title_id || null);
    if (aActive !== bActive) return aActive ? -1 : 1;
  }

  const aType = typeRank(a);
  const bType = typeRank(b);
  if (aType !== bType) return aType - bType;

  const aSeason = seasonVal(a);
  const bSeason = seasonVal(b);
  if (aSeason !== bSeason) return aSeason - bSeason;

  const aEpisode = episodeVal(a);
  const bEpisode = episodeVal(b);
  if (aEpisode !== bEpisode) return aEpisode - bEpisode;

  const aDuration = durationVal(a);
  const bDuration = durationVal(b);
  if (aDuration !== bDuration) return bDuration - aDuration;

  const aSize = sizeVal(a);
  const bSize = sizeVal(b);
  if (aSize !== bSize) return bSize - aSize;

  const aStable = stableKey(a);
  const bStable = stableKey(b);
  if (aStable !== bStable) return aStable.localeCompare(bStable);

  return orderVal(a) - orderVal(b);
}

/**
 * Sort titles for initial load / full reorder: cluster by segment_map, order clusters by
 * the first title in each cluster (after intra-cluster sort), same tiebreakers as before.
 */
export function sortTitlesForDisplay(
  titles: any[],
  titleActiveFn?: (id: string | null | undefined) => boolean
): any[] {
  if (!titles || titles.length === 0) return [];

  const cmp = (a: any, b: any) => compareTitles(a, b, titleActiveFn);
  const byCluster = new Map<string, any[]>();
  for (const t of titles) {
    const k = clusterKey(t);
    if (!byCluster.has(k)) byCluster.set(k, []);
    byCluster.get(k)!.push(t);
  }

  const clusters = [...byCluster.values()].map((items) => [...items].sort(cmp));
  clusters.sort((c1, c2) => cmp(c1[0], c2[0]));
  return clusters.flat();
}
