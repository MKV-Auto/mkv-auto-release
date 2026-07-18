import type { DiscMetadata } from '../services/workflow.service';

/** Normalize disc_hash for case-insensitive carousel identity matching. */
export function normCarouselDiscHash(h: string | null | undefined): string | null {
  if (h == null || String(h).trim() === '') return null;
  return String(h).trim().toUpperCase();
}

/**
 * Same physical disc as used by carousel superseded-failed filter: disc_id match, else disc_hash match.
 */
export function carouselDiscSameIdentity(a: DiscMetadata, b: DiscMetadata): boolean {
  if (a.disc_id && b.disc_id && a.disc_id === b.disc_id) return true;
  const ha = normCarouselDiscHash(a.disc_hash);
  const hb = normCarouselDiscHash(b.disc_hash);
  return ha != null && ha === hb;
}
