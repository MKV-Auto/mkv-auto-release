import { normalizeSegmentMap } from './segment-map.util';

describe('normalizeSegmentMap', () => {
  it('returns null for empty/missing', () => {
    expect(normalizeSegmentMap(null)).toBeNull();
    expect(normalizeSegmentMap(undefined)).toBeNull();
    expect(normalizeSegmentMap('')).toBeNull();
    expect(normalizeSegmentMap('  \t  ')).toBeNull();
  });

  it('normalizes comma-separated segments like backend', () => {
    expect(normalizeSegmentMap(' 1 , 2 , 3 ')).toBe('1,2,3');
    expect(normalizeSegmentMap('1,2,3')).toBe('1,2,3');
  });
});
