/** Disc title type <option> rows: placeholder, primary types, extras A–Z by label, Ignore last. */
export interface TitleTypeSelectOption {
  value: string;
  label: string;
}

export const TITLE_TYPE_SELECT_OPTIONS: TitleTypeSelectOption[] = [
  { value: '', label: 'Select Type...' },
  { value: 'MainMovie', label: 'Main Movie' },
  { value: 'Episode', label: 'Episode' },
  { value: 'Backdrop', label: 'Backdrop (Theme Video)' },
  { value: 'BehindTheScenes', label: 'Behind The Scenes' },
  { value: 'Clip', label: 'Clip' },
  { value: 'DeletedScene', label: 'Deleted Scene' },
  { value: 'Extra', label: 'Extra' },
  { value: 'Featurette', label: 'Featurette' },
  { value: 'Interview', label: 'Interview' },
  { value: 'Other', label: 'Other' },
  { value: 'Sample', label: 'Sample' },
  { value: 'Scene', label: 'Scene' },
  { value: 'Short', label: 'Short' },
  { value: 'ThemeMusic', label: 'Theme Music' },
  { value: 'Trailer', label: 'Trailer' },
  { value: 'ignore', label: 'Ignore' },
];

/** Sort/stats: Main Movie & Episode first, then same relative order as dropdown (no placeholder). */
export const TITLE_TYPE_STATS_ORDER: string[] = [
  'MainMovie',
  'Episode',
  'Backdrop',
  'BehindTheScenes',
  'Clip',
  'DeletedScene',
  'Extra',
  'Featurette',
  'Interview',
  'Other',
  'Sample',
  'Scene',
  'Short',
  'ThemeMusic',
  'Trailer',
  'ignore',
];

const CANONICAL_TYPES = new Set(
  TITLE_TYPE_SELECT_OPTIONS.filter((o) => o.value !== '').map((o) => o.value),
);

/**
 * Map API / draft strings to a value that matches <option [value]>.
 * Unknown strings become Extra (aligned with backend normalize_title_type_for_api).
 */
export function normalizeTitleTypeForSelect(val: string | null | undefined): string {
  const raw = (val ?? '').toString().trim();
  if (!raw) return '';
  if (CANONICAL_TYPES.has(raw)) return raw;

  const key = raw.toLowerCase().replace(/\s+/g, ' ').trim();
  const spaced: Record<string, string> = {
    episode: 'Episode',
    movie: 'MainMovie',
    mainmovie: 'MainMovie',
    main: 'MainMovie',
    ignore: 'ignore',
    deleted: 'DeletedScene',
    deletedscene: 'DeletedScene',
    extra: 'Extra',
    trailer: 'Trailer',
    'behind the scenes': 'BehindTheScenes',
    featurette: 'Featurette',
    featurettes: 'Featurette',
    interview: 'Interview',
    interviews: 'Interview',
    scene: 'Scene',
    scenes: 'Scene',
    short: 'Short',
    shorts: 'Short',
    other: 'Other',
    sample: 'Sample',
    samples: 'Sample',
    clip: 'Clip',
    clips: 'Clip',
    'theme-music': 'ThemeMusic',
    thememusic: 'ThemeMusic',
    backdrop: 'Backdrop',
    backdrops: 'Backdrop',
  };
  if (spaced[key]) return spaced[key];

  const compact = key.replace(/[-_\s]/g, '');
  const compactMap: Record<string, string> = {
    deletedscene: 'DeletedScene',
    behindthescenes: 'BehindTheScenes',
    thememusic: 'ThemeMusic',
    mainmovie: 'MainMovie',
    featurettes: 'Featurette',
    interviews: 'Interview',
    scenes: 'Scene',
    shorts: 'Short',
    samples: 'Sample',
    clips: 'Clip',
    backdrops: 'Backdrop',
  };
  if (compactMap[compact]) return compactMap[compact];

  return 'Extra';
}
