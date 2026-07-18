/**
 * Normalize segment_map for grouping (mirrors Backend/core/duplicate_info._normalize_segment_map).
 */
export function normalizeSegmentMap(segmentMap: unknown): string | null {
  if (segmentMap === undefined || segmentMap === null) return null;
  const s = String(segmentMap).trim();
  if (!s) return null;
  const parts = s.replace(/\s/g, '').split(',');
  const normalized = parts.map((p) => p.trim()).filter(Boolean).join(',');
  return normalized || null;
}
