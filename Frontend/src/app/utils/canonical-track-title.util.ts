/**
 * Canonical per-title display / persist name. DB stores a single column (`DiscTitle.title`);
 * drive and legacy payloads may use `episode_name` instead of `title`.
 */
export function canonicalTrackTitle(
  track: { title?: unknown; episode_name?: unknown } | null | undefined
): string {
  const tit = track?.title != null ? String(track.title).trim() : '';
  const ep = track?.episode_name != null ? String(track.episode_name).trim() : '';
  return tit || ep || '';
}
