import {
  buildTitleLabelEntities,
  componentClipCount,
  computeDuplicateGroupsFromTitles,
  getDiscTitleId,
  getPrimaryTitleForEntity,
  isComponentClip,
  parseDuplicateInfo,
  realSiblingCount,
} from './title-label-entities.util';

describe('title-label-entities.util', () => {
  it('getDiscTitleId reads title_id', () => {
    expect(getDiscTitleId({ title_id: 'a' })).toBe('a');
    expect(getDiscTitleId({})).toBeNull();
  });

  it('parseDuplicateInfo computes effectiveGroupSize from sameAs', () => {
    const all = [
      { title_id: '1', duplicate_info: { group_id: 'g1', same_as: ['2'] } },
      { title_id: '2', duplicate_info: { group_id: 'g1', same_as: ['1'] } },
    ];
    const i = parseDuplicateInfo(all[0], all);
    expect(i?.groupId).toBe('g1');
    expect(i?.effectiveGroupSize).toBe(2);
  });

  it('computeDuplicateGroupsFromTitles returns one group for two members', () => {
    const titles = [
      { title_id: '1', order_index: 0, duplicate_info: { group_id: 'g1', same_as: ['2'] } },
      { title_id: '2', order_index: 1, duplicate_info: { group_id: 'g1', same_as: ['1'] } },
    ];
    const groups = computeDuplicateGroupsFromTitles(titles);
    expect(groups.length).toBe(1);
    expect(groups[0].titles.map((t) => t.title_id).sort()).toEqual(['1', '2']);
  });

  it('singleton with duplicate metadata but only one row in list is not a group', () => {
    const titles = [{ title_id: '1', duplicate_info: { group_id: 'g1', same_as: ['missing'] } }];
    const groups = computeDuplicateGroupsFromTitles(titles);
    expect(groups.length).toBe(0);
    const entities = buildTitleLabelEntities(titles);
    expect(entities.length).toBe(1);
    expect(entities[0].kind).toBe('single');
  });

  it('buildTitleLabelEntities counts duplicate pair as one entity plus singles', () => {
    const titles = [
      { title_id: '1', duplicate_info: { group_id: 'g1', same_as: ['2'] } },
      { title_id: '2', duplicate_info: { group_id: 'g1', same_as: ['1'] } },
      { title_id: '3', type: 'MainMovie' },
    ];
    const entities = buildTitleLabelEntities(titles);
    expect(entities.length).toBe(2);
    expect(entities.filter((e) => e.kind === 'group').length).toBe(1);
    expect(entities.filter((e) => e.kind === 'single').length).toBe(1);
  });

  it('getPrimaryTitleForEntity prefers active', () => {
    const g = [
      { title_id: '1', active: false },
      { title_id: '2', active: true },
    ];
    expect(getPrimaryTitleForEntity(g).title_id).toBe('2');
  });

  it('getPrimaryTitleForEntity uses active row even when secondary is non-ignore (canonical primary)', () => {
    const g = [
      { title_id: '1', active: true, type: 'MainMovie' },
      { title_id: '2', active: false, type: 'ignore' },
    ];
    expect(getPrimaryTitleForEntity(g).title_id).toBe('1');
  });

  it('getPrimaryTitleForEntity uses active row when it is ignore (whole group ignored)', () => {
    const g = [
      { title_id: '1', active: true, type: 'ignore' },
      { title_id: '2', active: false, type: 'MainMovie' },
    ];
    expect(getPrimaryTitleForEntity(g).title_id).toBe('1');
  });

  it('getPrimaryTitleForEntity when no active falls back to first non-ignore', () => {
    const g = [
      { title_id: '1', active: false, type: 'ignore' },
      { title_id: '2', active: false, type: 'MainMovie' },
    ];
    expect(getPrimaryTitleForEntity(g).title_id).toBe('2');
  });

  describe('component-clip vs duplicate-sibling differentiation (#534 Phase 2)', () => {
    it('isComponentClip is true iff subsumed_by_title_id is set', () => {
      expect(isComponentClip({ title_id: 'm2ts-a', subsumed_by_title_id: 'mpls-1' })).toBeTrue();
      expect(isComponentClip({ title_id: 'mpls-1' })).toBeFalse();
      expect(isComponentClip({ title_id: 'orphan', subsumed_by_title_id: null })).toBeFalse();
      expect(isComponentClip({ title_id: 'orphan', subsumed_by_title_id: '' })).toBeFalse();
    });

    it('realSiblingCount excludes component clips from same_as', () => {
      const titles = [
        { title_id: 'mpls-1', duplicate_info: { group_id: 'g', same_as: ['mpls-2', 'm2ts-a'] } },
        { title_id: 'mpls-2', duplicate_info: { group_id: 'g', same_as: ['mpls-1', 'm2ts-a'] } },
        { title_id: 'm2ts-a', subsumed_by_title_id: 'mpls-1',
          duplicate_info: { group_id: 'g', same_as: ['mpls-1', 'mpls-2'] } },
      ];
      // mpls-1 has one real sibling (mpls-2); m2ts-a is a component clip.
      expect(realSiblingCount(titles[0], titles)).toBe(1);
      // m2ts-a's "real siblings" exclude its wrapper-as-duplicate framing —
      // both wrappers are non-component-clip rows that share the key, so
      // they count from m2ts-a's perspective too. (Helper is symmetric; the
      // gate is "exclude component clips from the candidate pool", not
      // "exclude same_as members that point at me".)
      expect(realSiblingCount(titles[2], titles)).toBe(2);
    });

    it('realSiblingCount returns 0 for a wrapper whose only same_as is a child clip', () => {
      const titles = [
        { title_id: 'mpls-1', duplicate_info: { group_id: 'g', same_as: ['m2ts-a'] } },
        { title_id: 'm2ts-a', subsumed_by_title_id: 'mpls-1',
          duplicate_info: { group_id: 'g', same_as: ['mpls-1'] } },
      ];
      expect(realSiblingCount(titles[0], titles)).toBe(0);
    });

    it('componentClipCount counts inbound subsumed_by pointers', () => {
      const titles = [
        { title_id: 'mpls-1' },
        { title_id: 'm2ts-a', subsumed_by_title_id: 'mpls-1' },
        { title_id: 'm2ts-b', subsumed_by_title_id: 'mpls-1' },
        { title_id: 'stranger', subsumed_by_title_id: 'mpls-other' },
        { title_id: 'orphan' },
      ];
      expect(componentClipCount(titles[0], titles)).toBe(2);
      expect(componentClipCount(titles[4], titles)).toBe(0);
    });

    it('computeDuplicateGroupsFromTitles drops a wrapper whose only same_as is a component clip', () => {
      const titles = [
        { title_id: 'mpls-1', order_index: 0,
          duplicate_info: { group_id: 'g', same_as: ['m2ts-a'] } },
        { title_id: 'm2ts-a', subsumed_by_title_id: 'mpls-1', order_index: 1,
          duplicate_info: { group_id: 'g', same_as: ['mpls-1'] } },
      ];
      // Child-only "group" collapses — no mobile duplicate-card.
      expect(computeDuplicateGroupsFromTitles(titles)).toEqual([]);
    });

    it('computeDuplicateGroupsFromTitles keeps a real-duplicate pair even when component clips also share the key', () => {
      const titles = [
        { title_id: 'mpls-1', order_index: 0,
          duplicate_info: { group_id: 'g', same_as: ['mpls-2', 'm2ts-a'] } },
        { title_id: 'mpls-2', order_index: 1,
          duplicate_info: { group_id: 'g', same_as: ['mpls-1', 'm2ts-a'] } },
        { title_id: 'm2ts-a', subsumed_by_title_id: 'mpls-1', order_index: 2,
          duplicate_info: { group_id: 'g', same_as: ['mpls-1', 'mpls-2'] } },
      ];
      const groups = computeDuplicateGroupsFromTitles(titles);
      expect(groups.length).toBe(1);
      // Component clip is excluded from the group's `titles` rendering.
      expect(groups[0].titles.map((t) => t.title_id).sort()).toEqual(['mpls-1', 'mpls-2']);
    });
  });
});
