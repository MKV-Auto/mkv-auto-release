import { sortTitlesForDisplay } from './title-display-sort.util';

describe('sortTitlesForDisplay', () => {
  it('keeps titles with same segment_map adjacent (cluster)', () => {
    const a = {
      title_id: 'a',
      type: 'MainMovie',
      segment_map: '1,2',
      duration: 100,
      size: 1000,
      order_index: 0,
    };
    const b = {
      title_id: 'b',
      type: 'MainMovie',
      segment_map: '1,2',
      duration: 200,
      size: 2000,
      order_index: 1,
    };
    const c = {
      title_id: 'c',
      type: 'MainMovie',
      segment_map: '9',
      duration: 150,
      size: 1500,
      order_index: 2,
    };
    const ordered = sortTitlesForDisplay([c, a, b]);
    const ids = ordered.map((t) => t.title_id);
    expect(ids.indexOf('a')).toBeLessThan(ids.indexOf('c'));
    expect(ids.indexOf('b')).toBeLessThan(ids.indexOf('c'));
    expect(Math.abs(ids.indexOf('a') - ids.indexOf('b'))).toBe(1);
  });
});
