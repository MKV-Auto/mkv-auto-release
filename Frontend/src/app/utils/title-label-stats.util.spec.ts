import {
  areLabelTitlesComplete,
  computeTitleLabelStats,
  isTitleLabelComplete,
  sortTitleStatsTypeEntries,
} from './title-label-stats.util';

describe('title-label-stats.util', () => {
  describe('isTitleLabelComplete', () => {
    it('false when type missing', () => {
      expect(isTitleLabelComplete({ type: '', title: 'x' })).toBe(false);
      expect(isTitleLabelComplete({ title: 'x' })).toBe(false);
    });
    it('true for ignore', () => {
      expect(isTitleLabelComplete({ type: 'ignore' })).toBe(true);
    });
    it('episode needs season episode name', () => {
      expect(
        isTitleLabelComplete({ type: 'Episode', season: 1, episode: 2, title: 'Name' })
      ).toBe(true);
      expect(isTitleLabelComplete({ type: 'Episode', season: 1, episode: 2, title: '' })).toBe(
        false
      );
      expect(isTitleLabelComplete({ type: 'Episode', title: 'Name' })).toBe(false);
    });
    it('other types need title', () => {
      expect(isTitleLabelComplete({ type: 'MainMovie', title: 'Film' })).toBe(true);
      expect(isTitleLabelComplete({ type: 'Extra', title: '   ' })).toBe(false);
    });
  });

  describe('areLabelTitlesComplete', () => {
    it('false when empty', () => {
      expect(areLabelTitlesComplete([])).toBe(false);
      expect(areLabelTitlesComplete(null as any)).toBe(false);
    });

    it('true when each entity is complete; duplicate group uses primary only', () => {
      const titles = [
        {
          title_id: '1',
          active: true,
          type: 'MainMovie',
          title: 'Movie',
          duplicate_info: { group_id: 'g', same_as: ['2'] },
        },
        {
          title_id: '2',
          active: false,
          type: 'Extra',
          title: '',
          duplicate_info: { group_id: 'g', same_as: ['1'] },
        },
      ];
      expect(areLabelTitlesComplete(titles)).toBe(true);
    });

    it('false when primary of duplicate group is incomplete even if secondary has type', () => {
      const titles = [
        {
          title_id: '1',
          active: true,
          type: 'MainMovie',
          title: '',
          duplicate_info: { group_id: 'g', same_as: ['2'] },
        },
        {
          title_id: '2',
          active: false,
          type: 'MainMovie',
          title: 'Name',
          duplicate_info: { group_id: 'g', same_as: ['1'] },
        },
      ];
      expect(areLabelTitlesComplete(titles)).toBe(false);
    });
  });

  describe('computeTitleLabelStats', () => {
    it('empty', () => {
      const s = computeTitleLabelStats([]);
      expect(s.total).toBe(0);
      expect(s.rawTitleCount).toBe(0);
      expect(s.ignored + s.labeledComplete + s.remainingIncomplete).toBe(0);
    });

    it('sums to total for flat list', () => {
      const titles = [
        { title_id: 'a', type: 'ignore' },
        { title_id: 'b', type: 'MainMovie', title: 'X' },
        { title_id: 'c', type: 'Extra', title: '' },
      ];
      const s = computeTitleLabelStats(titles);
      expect(s.rawTitleCount).toBe(3);
      expect(s.total).toBe(3);
      expect(s.ignored).toBe(1);
      expect(s.labeledComplete).toBe(1);
      expect(s.remainingIncomplete).toBe(1);
      expect(s.ignored + s.labeledComplete + s.remainingIncomplete).toBe(3);
      expect(s.byType['MainMovie']).toBe(1);
    });

    it('duplicate group counts as one entity using primary', () => {
      const titles = [
        {
          title_id: '1',
          active: true,
          type: 'MainMovie',
          title: 'Movie',
          duplicate_info: { group_id: 'g', same_as: ['2'] },
        },
        {
          title_id: '2',
          active: false,
          type: 'Extra',
          title: '',
          duplicate_info: { group_id: 'g', same_as: ['1'] },
        },
      ];
      const s = computeTitleLabelStats(titles);
      expect(s.rawTitleCount).toBe(2);
      expect(s.total).toBe(1);
      expect(s.labeledComplete).toBe(1);
      expect(s.byType['MainMovie']).toBe(1);
    });
  });

  it('sortTitleStatsTypeEntries orders known types first', () => {
    const sorted = sortTitleStatsTypeEntries({ Trailer: 1, MainMovie: 2, Zed: 1 });
    expect(sorted.map((x) => x.type)).toEqual(['MainMovie', 'Trailer', 'Zed']);
  });
});
