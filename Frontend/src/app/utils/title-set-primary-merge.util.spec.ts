import { mergeTitleFromSetPrimaryResponse } from './title-set-primary-merge.util';

describe('mergeTitleFromSetPrimaryResponse', () => {
  it('preserves duplicateInfo and applies server labeling fields', () => {
    const existing = {
      title_id: 'a',
      duplicateInfo: { groupId: 'g1', sameAs: ['b'] },
      metadata_scan: { format: {} },
      segment_map: '75',
      type: 'ignore',
      active: false,
      title: null,
    };
    const server = {
      title_id: 'a',
      type: 'MainMovie',
      active: true,
      title: 'Feature',
      title_seq: 5,
      source_file: 'a.mkv',
    };
    const merged = mergeTitleFromSetPrimaryResponse(existing, server);
    expect(merged.duplicateInfo).toEqual(existing.duplicateInfo);
    expect(merged.metadata_scan).toEqual(existing.metadata_scan);
    expect(merged.segment_map).toBe('75');
    expect(merged.type).toBe('MainMovie');
    expect(merged.active).toBe(true);
    expect(merged.title).toBe('Feature');
    expect(merged.title_seq).toBe(5);
    expect(merged.source_file).toBe('a.mkv');
  });

  it('returns existing when server is null', () => {
    const existing = { title_id: 'x', type: 'MainMovie' };
    expect(mergeTitleFromSetPrimaryResponse(existing, null)).toBe(existing);
  });
});
