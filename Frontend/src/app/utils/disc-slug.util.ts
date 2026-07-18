/**
 * Mirrors Backend/core.utils.slugify_disc_name for UI placeholders.
 * Spaces → underscore; punctuation/separators → hyphen; ASCII letters/digits kept; specials dropped.
 */
function isDiscSlugSeparator(c: string): boolean {
  if ('-_/\\.:,|'.includes(c) || c === '_') {
    return true;
  }
  try {
    return /\p{Pd}/u.test(c);
  } catch {
    return false;
  }
}

export function slugifyDiscName(name: string | null | undefined): string {
  if (name == null || name === '') {
    return '';
  }
  let s = String(name)
    .replace(/Æ/g, 'AE')
    .replace(/æ/g, 'ae')
    .replace(/Œ/g, 'OE')
    .replace(/œ/g, 'oe');
  // Dash punctuation (e.g. U+2010) → ASCII hyphen before stripping non-ASCII
  try {
    s = s.replace(/\p{Pd}/gu, '-');
  } catch {
    /* ignore if runtime lacks Unicode property escapes */
  }
  s = s.normalize('NFKD').replace(/\p{M}/gu, '');
  s = s.replace(/[^\x00-\x7F]/g, '');

  const parts: string[] = [];
  for (const c of s) {
    if (/[a-zA-Z0-9]/.test(c)) {
      parts.push(c.toLowerCase());
    } else if (/\s/.test(c)) {
      parts.push('_');
    } else if (isDiscSlugSeparator(c)) {
      parts.push('-');
    }
  }

  const out: string[] = [];
  let last: string | null = null;
  for (const p of parts) {
    if (p === '_' && last === '_') {
      continue;
    }
    if (p === '-' && last === '-') {
      continue;
    }
    out.push(p);
    last = p;
  }
  return out.join('').replace(/^[_-]+|[_-]+$/g, '');
}
