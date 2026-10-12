/**
 * Title durations as full timecode, `H:MM:SS` — the form MakeMKV itself
 * reports (`1:33:47`, `0:04:05`, `0:00:45`). Always three fields, so durations
 * line up in a column and compare at a glance; rounding to `1h 33m` or
 * `93.8 min` hid the seconds that tell cuts, duplicates and play-alls apart
 * (#891).
 *
 * Accepts seconds (number or numeric string) or an existing `H:MM:SS` /
 * `M:SS` string (e.g. MakeMKV's `duration_raw`), which is normalised.
 * Returns '' when there is no duration (null, undefined, 0, negative,
 * unparseable) so each caller keeps its own placeholder.
 */
export function formatTimecode(value: number | string | null | undefined): string {
  const seconds = toSeconds(value);
  if (seconds === null || seconds <= 0) return '';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h}:${pad2(m)}:${pad2(s)}`;
}

function toSeconds(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  const text = value.trim();
  if (!text) return null;
  if (/^\d+(\.\d+)?$/.test(text)) return Number(text);
  const parts = text.split(':');
  if (parts.length < 2 || parts.length > 3 || !parts.every(p => /^\d+(\.\d+)?$/.test(p))) return null;
  return parts.map(Number).reduce((acc, n) => acc * 60 + n, 0);
}

function pad2(n: number): string {
  return n.toString().padStart(2, '0');
}
