import type { DuplicateMetrics, ParsedTag, TagCategory, GroupColor } from '../models/duplicate';

export type { DuplicateInfo, ParsedTag, TagCategory, DuplicateMetrics } from '../models/duplicate';

/** Comparative wire tags from backend `diff_tags` (subset we hide on full-group ties in the UI). */
const COMPARATIVE_WIRE_DIFF_TAGS = new Set([
  'chapters:more',
  'subs:more-languages',
  'audio:best',
  'audio:more-languages',
  'video:best',
]);

export function isComparativeWireDiffTag(tag: string): boolean {
  return COMPARATIVE_WIRE_DIFF_TAGS.has(tag);
}

/**
 * Comparative chips to show for one title: same as API `diff_tags` except tags that every
 * group member also has (full tie on that axis — omit visually; payload unchanged).
 */
export function diffTagsForDisplay(
  groupMembers: Record<string, unknown>[],
  rawDiffTags: string[] | null | undefined
): string[] {
  const tags = Array.isArray(rawDiffTags) ? rawDiffTags : [];
  if (groupMembers.length < 2 || tags.length === 0) return tags;

  const everyoneHasTag = (tag: string): boolean =>
    groupMembers.every((m) => {
      const di = (m['duplicateInfo'] ?? m['duplicate_info']) as Record<string, unknown> | undefined;
      const dt = di?.['diffTags'] ?? di?.['diff_tags'];
      return Array.isArray(dt) && dt.includes(tag);
    });

  return tags.filter((tag) => {
    if (!isComparativeWireDiffTag(tag)) return true;
    return !everyoneHasTag(tag);
  });
}

function titleLabel(t: Record<string, unknown>): string {
  const sf = t['source_file'] ?? t['sourceFile'];
  if (typeof sf === 'string' && sf.trim()) return sf.trim();
  const id = String(t['title_id'] ?? t['id'] ?? '').trim();
  return id ? `Title ${id.slice(-6)}` : '—';
}

function scanMeta(t: Record<string, unknown>): Record<string, unknown> | null {
  const m = t['metadata_scan'] ?? t['metadataScan'];
  return m && typeof m === 'object' ? (m as Record<string, unknown>) : null;
}

function readDuplicateMetrics(t: Record<string, unknown>): DuplicateMetrics | null {
  const di = (t['duplicateInfo'] ?? t['duplicate_info']) as Record<string, unknown> | undefined;
  const m = di?.['metrics'];
  if (!m || typeof m !== 'object') return null;
  const r = m as Record<string, unknown>;
  const vb = r['videoBitrate'] ?? r['video_bitrate'];
  return {
    chaptersCount: Number(r['chaptersCount'] ?? r['chapters_count'] ?? 0),
    subtitleTrackCount: Number(r['subtitleTrackCount'] ?? r['subtitle_track_count'] ?? 0),
    subtitleLanguageCount: Number(r['subtitleLanguageCount'] ?? r['subtitle_language_count'] ?? 0),
    audioScore: Number(r['audioScore'] ?? r['audio_score'] ?? 0),
    audioLanguageCount: Number(r['audioLanguageCount'] ?? r['audio_language_count'] ?? 0),
    videoBitrate: vb != null && vb !== '' && Number.isFinite(Number(vb)) ? Number(vb) : null,
    videoPixels: Number(r['videoPixels'] ?? r['video_pixels'] ?? 0),
    scanUsable: !!(r['scanUsable'] ?? r['scan_usable']),
  };
}

function chaptersFromTitle(t: Record<string, unknown>): number {
  const mm = readDuplicateMetrics(t);
  if (mm) return mm.chaptersCount;
  const meta = scanMeta(t);
  const c = meta?.['chapters_count'];
  return typeof c === 'number' ? c : 0;
}

function subtitleLangCountFromTitle(t: Record<string, unknown>): number {
  const mm = readDuplicateMetrics(t);
  if (mm?.scanUsable) return mm.subtitleLanguageCount;
  const meta = scanMeta(t);
  const rows = meta?.['subtitle_summary'];
  if (!Array.isArray(rows)) return 0;
  const langs = new Set<string>();
  for (const row of rows) {
    if (row && typeof row === 'object') {
      const lang = (row as Record<string, unknown>)['language'];
      if (lang != null && String(lang).trim()) langs.add(String(lang).trim().toLowerCase());
    }
  }
  return langs.size > 0 ? langs.size : rows.length > 0 ? 1 : 0;
}

function audioLangCountFromTitle(t: Record<string, unknown>): number {
  const mm = readDuplicateMetrics(t);
  if (mm?.scanUsable) return mm.audioLanguageCount;
  const meta = scanMeta(t);
  const rows = meta?.['audio_summary'];
  if (!Array.isArray(rows)) return 0;
  const langs = new Set<string>();
  for (const row of rows) {
    if (row && typeof row === 'object') {
      const lang = (row as Record<string, unknown>)['language'];
      if (lang != null && String(lang).trim()) langs.add(String(lang).trim().toLowerCase());
    }
  }
  return langs.size > 0 ? langs.size : rows.length > 0 ? 1 : 0;
}

function videoPixelsFromTitle(t: Record<string, unknown>): number {
  const mm = readDuplicateMetrics(t);
  if (mm && mm.videoPixels > 0) return mm.videoPixels;
  const meta = scanMeta(t);
  const v = meta?.['video_hints'];
  if (!v || typeof v !== 'object') return 0;
  const w = (v as Record<string, unknown>)['width'] ?? (v as Record<string, unknown>)['Width'];
  const h = (v as Record<string, unknown>)['height'] ?? (v as Record<string, unknown>)['Height'];
  if (typeof w === 'number' && typeof h === 'number' && w > 0 && h > 0) return w * h;
  return 0;
}

function effectiveVideoBitrateBps(t: Record<string, unknown>): number | null {
  const mm = readDuplicateMetrics(t);
  if (mm?.videoBitrate != null && Number.isFinite(mm.videoBitrate)) return mm.videoBitrate;
  const meta = scanMeta(t);
  const fmt = meta?.['format'];
  if (fmt && typeof fmt === 'object') {
    const br = (fmt as Record<string, unknown>)['bit_rate'];
    if (br != null && Number.isFinite(Number(br))) return Number(br);
  }
  return null;
}

/** Adaptive Mbps string; extra decimals at mid/high rates so small bps deltas stay visible. */
export function formatVideoBitrateDisplay(bps: number | null | undefined): string {
  if (bps == null || !Number.isFinite(bps)) return '—';
  const mbps = bps / 1e6;
  if (mbps >= 100) return `${mbps.toFixed(3)} Mbps`;
  if (mbps >= 10) return `${mbps.toFixed(4)} Mbps`;
  return `${mbps.toFixed(5)} Mbps`;
}

function formatResolutionFromPixels(px: number): string {
  if (px <= 0) return '—';
  const w = Math.round(Math.sqrt(px * (16 / 9)));
  const h = Math.round(px / w);
  const common: [number, string][] = [
    [3840 * 2160, '3840×2160 (4K)'],
    [1920 * 1080, '1920×1080 (1080p)'],
    [1280 * 720, '1280×720 (720p)'],
  ];
  for (const [presetPx, label] of common) {
    if (Math.abs(px - presetPx) < presetPx * 0.02) return label;
  }
  const metaW = Math.sqrt(px * (16 / 9));
  return `${Math.round(metaW)}×${Math.round(px / metaW)}`;
}

function formatResolutionFromScan(t: Record<string, unknown>): string {
  const px = videoPixelsFromTitle(t);
  if (px > 0) return formatResolutionFromPixels(px);
  const meta = scanMeta(t);
  const v = meta?.['video_hints'];
  if (v && typeof v === 'object') {
    const w = (v as Record<string, unknown>)['width'] ?? (v as Record<string, unknown>)['Width'];
    const h = (v as Record<string, unknown>)['height'] ?? (v as Record<string, unknown>)['Height'];
    if (typeof w === 'number' && typeof h === 'number' && w > 0 && h > 0) return `${w}×${h}`;
  }
  return '—';
}

function audioProfileLabelFromScan(t: Record<string, unknown>): string {
  const meta = scanMeta(t);
  const rows = meta?.['audio_summary'];
  if (!Array.isArray(rows) || rows.length === 0) return '—';
  let best = '';
  let bestCh = 0;
  for (const a of rows) {
    if (!a || typeof a !== 'object') continue;
    const rec = a as Record<string, unknown>;
    const ch = Number(rec['channels']) || 0;
    const layout = String(rec['channel_layout'] || '').toLowerCase();
    const codec = String(rec['codec_name'] || '').toUpperCase();
    const channels = layout.includes('7.1') || layout.includes('7')
      ? 8
      : layout.includes('5.1') || layout.includes('6')
        ? 6
        : ch;
    if (channels > bestCh || (channels === bestCh && codec && !best)) {
      bestCh = channels;
      best = codec
        ? `${codec} ${channels === 8 ? '7.1' : channels === 6 ? '5.1' : channels >= 2 ? 'stereo' : 'mono'}`
        : best;
    }
  }
  return best || '—';
}

function audioScoreFromMetrics(t: Record<string, unknown>): number {
  const mm = readDuplicateMetrics(t);
  return mm?.audioScore ?? 0;
}

export function parseTag(rawTag: string, isDiff = false): ParsedTag {
  const parts = rawTag.split(':');
  const category = (parts[0] || 'quality') as TagCategory;
  const value = parts.slice(1).join(':') || '';

  const isPositive =
    isDiff &&
    (value === 'best' ||
      value === 'more-languages' ||
      (value === 'more' && category === 'chapters'));

  let label = value;
  if (category === 'audio') {
    if (value === 'best') label = 'Best audio profile';
    else if (value === 'more-languages') label = 'Most audio languages';
    else
      label = value
        .replace('lossless', 'Lossless')
        .replace('surround', 'Surround')
        .replace('lossy', 'Lossy');
  } else if (category === 'subs') {
    label = value
      .replace('forced', 'Forced')
      .replace('more-languages', 'Most subtitle languages')
      .replace('multiple-languages', 'Multiple languages')
      .replace('sdh', 'SDH');
  } else if (category === 'chapters') {
    label = value === 'more' ? 'Most chapters' : value;
  } else if (category === 'video') {
    if (value === 'best') label = 'Best video (scan)';
    else label = value.replace('better', 'Better Quality');
  } else if (category === 'metadata') {
    label = value.replace('better-scan', 'Better scan').replace('worse-scan', 'Worse scan');
  }

  return {
    category,
    label,
    isDiff,
    isPositive,
    isNeutral: false,
    rawTag,
  };
}

export function getTagColor(
  category: TagCategory,
  isDiff: boolean,
  isPositive: boolean,
  isNeutral = false
): { bg: string; border: string; text: string } {
  if (isDiff) {
    if (isNeutral) {
      return {
        bg: 'rgba(255, 255, 255, 0.08)',
        border: 'rgba(255, 255, 255, 0.22)',
        text: '#e2e8f0',
      };
    }
    return isPositive
      ? { bg: 'rgba(34, 197, 94, 0.15)', border: 'rgba(34, 197, 94, 0.3)', text: '#4ade80' }
      : { bg: 'rgba(251, 146, 60, 0.15)', border: 'rgba(251, 146, 60, 0.3)', text: '#fb923c' };
  }
  switch (category) {
    case 'audio':
      return { bg: 'rgba(99, 102, 241, 0.15)', border: 'rgba(99, 102, 241, 0.3)', text: '#818cf8' };
    case 'video':
      return { bg: 'rgba(168, 85, 247, 0.15)', border: 'rgba(168, 85, 247, 0.3)', text: '#c084fc' };
    case 'subs':
      return { bg: 'rgba(236, 72, 153, 0.15)', border: 'rgba(236, 72, 153, 0.3)', text: '#f472b6' };
    case 'chapters':
      return { bg: 'rgba(14, 165, 233, 0.15)', border: 'rgba(14, 165, 233, 0.3)', text: '#38bdf8' };
    case 'metadata':
      return { bg: 'rgba(148, 163, 184, 0.15)', border: 'rgba(148, 163, 184, 0.35)', text: '#cbd5e1' };
    default:
      return { bg: 'rgba(255, 255, 255, 0.1)', border: 'rgba(255, 255, 255, 0.2)', text: '#fff' };
  }
}

export const DUPLICATE_GROUP_COLORS: GroupColor[] = [
  { name: 'blue', color: '#3b82f6', glow: 'rgba(59, 130, 246, 0.4)' },
  { name: 'purple', color: '#a855f7', glow: 'rgba(168, 85, 247, 0.4)' },
  { name: 'pink', color: '#ec4899', glow: 'rgba(236, 72, 153, 0.4)' },
  { name: 'orange', color: '#f97316', glow: 'rgba(249, 115, 22, 0.4)' },
  { name: 'teal', color: '#14b8a6', glow: 'rgba(20, 184, 166, 0.4)' },
  { name: 'yellow', color: '#eab308', glow: 'rgba(234, 179, 8, 0.4)' },
];

export function getGroupColor(groupId: string): GroupColor {
  if (!groupId) return DUPLICATE_GROUP_COLORS[0];
  let hash = 0;
  for (let i = 0; i < groupId.length; i++) {
    hash = (hash << 5) - hash + groupId.charCodeAt(i);
    hash = hash & hash;
  }
  const index = Math.abs(hash) % DUPLICATE_GROUP_COLORS.length;
  return DUPLICATE_GROUP_COLORS[index];
}

export function getSameAsText(
  sameAsTitles: Array<{ id: string; title?: string | null; sourceFile?: string }>,
  currentTitleId: string
): string {
  if (!sameAsTitles || sameAsTitles.length === 0) return '';
  const others = sameAsTitles.filter((t) => t && t.id && t.id !== currentTitleId);
  if (others.length === 0) return '';
  const formatTitle = (t: (typeof others)[0]) => {
    if (t.title) return t.title;
    if (t.sourceFile) return t.sourceFile;
    return `Title ${t.id?.slice(-4) || 'unknown'}`;
  };
  if (others.length === 1) return formatTitle(others[0]);
  if (others.length === 2) {
    const names = others.map(formatTitle);
    return `${names[0]} and ${names[1]}`;
  }
  return `${others.length} other titles`;
}

export function getGroupIdentifier(groupId: string): string {
  if (!groupId) return '1';
  let hash = 0;
  for (let i = 0; i < groupId.length; i++) {
    hash = (hash << 5) - hash + groupId.charCodeAt(i);
    hash = hash & hash;
  }
  const number = (Math.abs(hash) % 99) + 1;
  return number.toString();
}

export function getTitleDisplayName(title: {
  title?: string | null;
  sourceFile?: string;
  id: string;
}): string {
  if (title.title) return title.title;
  if (title.sourceFile) return title.sourceFile;
  return `Title ${title.id.slice(-4)}`;
}

export interface ComparisonDetail {
  label: string;
  currentValue: string;
  comparedValue: string;
  unit?: string;
  isBetter: boolean;
  isEquivalent?: boolean;
}

export interface ComparisonMatrixSection {
  heading: string;
  lines: string[];
}

/**
 * Pick the single other variant in the group that is strongest on the axis for this diff tag.
 */
export function pickStrongestOtherForDiffTag(
  tag: string,
  _currentTitle: Record<string, unknown>,
  otherTitles: Record<string, unknown>[]
): Record<string, unknown> | null {
  if (!otherTitles.length) return null;
  const [, value] = tag.split(':');
  const score = (t: Record<string, unknown>): number => {
    if (tag === 'chapters:more' || (tag.startsWith('chapters:') && value === 'more'))
      return chaptersFromTitle(t);
    if (tag === 'subs:more-languages') return subtitleLangCountFromTitle(t);
    if (tag === 'audio:more-languages') return audioLangCountFromTitle(t);
    if (tag === 'audio:best') return audioScoreFromMetrics(t);
    if (tag === 'video:best') {
      const px = videoPixelsFromTitle(t);
      const br = effectiveVideoBitrateBps(t) ?? 0;
      return px * 1e15 + br;
    }
    return 0;
  };
  let best = otherTitles[0];
  let bestS = score(best);
  for (let i = 1; i < otherTitles.length; i++) {
    const t = otherTitles[i];
    const s = score(t);
    if (s > bestS) {
      best = t;
      bestS = s;
    }
  }
  return best;
}

function langCountLabel(n: number, kind: 'audio' | 'subtitle'): string {
  if (kind === 'audio') return n === 1 ? '1 audio language' : `${n} audio languages`;
  return n === 1 ? '1 subtitle language' : `${n} subtitle languages`;
}

function maxAmongOthers(values: number[], idx: number): number {
  let m = 0;
  for (let i = 0; i < values.length; i++) {
    if (i !== idx) m = Math.max(m, values[i]);
  }
  return m;
}

/** Full-group matrix (ffprobe / metrics) for comparative tooltip. */
export function buildDuplicateComparisonMatrix(groupTitles: Record<string, unknown>[]): ComparisonMatrixSection[] {
  if (!groupTitles.length) return [];
  const n = groupTitles.length;
  const ch = groupTitles.map(chaptersFromTitle);
  const subL = groupTitles.map(subtitleLangCountFromTitle);
  const audL = groupTitles.map(audioLangCountFromTitle);
  const px = groupTitles.map(videoPixelsFromTitle);
  const br = groupTitles.map((t) => effectiveVideoBitrateBps(t));
  const maxPx = Math.max(...px);
  const minPx = Math.min(...px);
  const sameRes = maxPx === minPx;

  const videoLines: string[] = [];
  for (let i = 0; i < n; i++) {
    const t = groupTitles[i];
    const lab = titleLabel(t);
    const res = formatResolutionFromScan(t);
    const othersPx = maxAmongOthers(px, i);
    let line = `${lab} — ${res}`;
    if (px[i] > othersPx && px[i] > 0) {
      line += ' / ^ higher resolution';
    }
    if (sameRes && px[i] > 0) {
      const b = br[i];
      const ob = maxAmongOthers(
        br.map((x) => x ?? 0),
        i
      );
      if (b != null) {
        line += ` — Bitrate ${formatVideoBitrateDisplay(b)}`;
        if (b > ob && ob > 0 && b - ob < 1e6) {
          line += ` / ^ ${b - ob} bps higher`;
        } else if (b > ob && ob > 0) {
          line += ` / ^ higher bitrate`;
        }
      }
    }
    videoLines.push(line);
  }

  const audioProfileLines = groupTitles.map((t) => `${titleLabel(t)} — ${audioProfileLabelFromScan(t)}`);
  const maxAudScore = Math.max(...groupTitles.map(audioScoreFromMetrics));
  const audioLangLines: string[] = [];
  for (let i = 0; i < n; i++) {
    const c = audL[i];
    const mo = maxAmongOthers(audL, i);
    let line = `${titleLabel(groupTitles[i])} — ${langCountLabel(c, 'audio')}`;
    if (c > mo) line += ` / ^ ${c - mo} More Languages`;
    audioLangLines.push(line);
  }
  const subLines: string[] = [];
  for (let i = 0; i < n; i++) {
    const c = subL[i];
    const mo = maxAmongOthers(subL, i);
    let line = `${titleLabel(groupTitles[i])} — ${langCountLabel(c, 'subtitle')}`;
    if (c > mo) line += ` / ^ ${c - mo} More Languages`;
    subLines.push(line);
  }
  const chapLines: string[] = [];
  for (let i = 0; i < n; i++) {
    const c = ch[i];
    const mo = maxAmongOthers(ch, i);
    let line = `${titleLabel(groupTitles[i])} — ${c} Chapters`;
    if (c > mo) line += ` / ^ ${c - mo} More Chapters`;
    chapLines.push(line);
  }

  const audioProfileNote =
    maxAudScore > 0
      ? audioProfileLines.map((line, i) =>
          audioScoreFromMetrics(groupTitles[i]) === maxAudScore ? `${line} (tied for best profile)` : line
        )
      : audioProfileLines;

  return [
    { heading: 'Video (scan)', lines: videoLines },
    { heading: 'Audio profile', lines: audioProfileNote },
    { heading: 'Audio languages', lines: audioLangLines },
    { heading: 'Subtitles', lines: subLines },
    { heading: 'Chapters', lines: chapLines },
  ];
}

export function getComparisonDetail(
  tag: string,
  currentTitle: Record<string, unknown>,
  comparedTitle: Record<string, unknown>
): ComparisonDetail | null {
  const curM = readDuplicateMetrics(currentTitle);
  const cmpM = readDuplicateMetrics(comparedTitle);

  if (tag === 'chapters:more') {
    const cur = curM?.chaptersCount ?? chaptersFromTitle(currentTitle);
    const cmp = cmpM?.chaptersCount ?? chaptersFromTitle(comparedTitle);
    const isEquivalent = cur === cmp;
    return {
      label: 'Chapters',
      currentValue: String(cur),
      comparedValue: String(cmp),
      unit: 'chapters',
      isBetter: true,
      isEquivalent,
    };
  }

  if (tag === 'subs:more-languages') {
    const cur = curM?.subtitleLanguageCount ?? subtitleLangCountFromTitle(currentTitle);
    const cmp = cmpM?.subtitleLanguageCount ?? subtitleLangCountFromTitle(comparedTitle);
    const isEquivalent = cur === cmp;
    return {
      label: 'Subtitle languages',
      currentValue: String(cur),
      comparedValue: String(cmp),
      unit: 'languages',
      isBetter: true,
      isEquivalent,
    };
  }

  if (tag === 'audio:more-languages') {
    const cur = curM?.audioLanguageCount ?? audioLangCountFromTitle(currentTitle);
    const cmp = cmpM?.audioLanguageCount ?? audioLangCountFromTitle(comparedTitle);
    const isEquivalent = cur === cmp;
    return {
      label: 'Audio languages',
      currentValue: String(cur),
      comparedValue: String(cmp),
      unit: 'languages',
      isBetter: true,
      isEquivalent,
    };
  }

  if (tag === 'audio:best') {
    const cur = curM?.audioScore ?? audioScoreFromMetrics(currentTitle);
    const cmp = cmpM?.audioScore ?? audioScoreFromMetrics(comparedTitle);
    const isEquivalent = cur === cmp;
    return {
      label: 'Audio profile',
      currentValue: audioProfileLabelFromScan(currentTitle),
      comparedValue: audioProfileLabelFromScan(comparedTitle),
      unit: '',
      isBetter: true,
      isEquivalent,
    };
  }

  if (tag === 'video:best') {
    const curPx = curM?.videoPixels ?? videoPixelsFromTitle(currentTitle);
    const cmpPx = cmpM?.videoPixels ?? videoPixelsFromTitle(comparedTitle);
    const curBr = curM?.videoBitrate ?? effectiveVideoBitrateBps(currentTitle);
    const cmpBr = cmpM?.videoBitrate ?? effectiveVideoBitrateBps(comparedTitle);
    const resEq = curPx === cmpPx;
    const brEq =
      curBr != null && cmpBr != null && curBr === cmpBr;
    const isEquivalent = resEq && (curBr == null || cmpBr == null || brEq);
    const curV =
      formatResolutionFromScan(currentTitle) +
      (curBr != null ? ` · ${formatVideoBitrateDisplay(curBr)}` : '');
    const cmpV =
      formatResolutionFromScan(comparedTitle) +
      (cmpBr != null ? ` · ${formatVideoBitrateDisplay(cmpBr)}` : '');
    return {
      label: 'Video (scan)',
      currentValue: curV,
      comparedValue: cmpV,
      unit: '',
      isBetter: true,
      isEquivalent,
    };
  }

  return null;
}
