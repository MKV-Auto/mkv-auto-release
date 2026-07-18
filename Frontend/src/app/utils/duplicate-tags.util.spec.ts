import {
  parseTag,
  getTagColor,
  getComparisonDetail,
  getGroupColor,
  getGroupIdentifier,
  getSameAsText,
  getTitleDisplayName,
  DUPLICATE_GROUP_COLORS,
  pickStrongestOtherForDiffTag,
  buildDuplicateComparisonMatrix,
  formatVideoBitrateDisplay,
  diffTagsForDisplay,
} from './duplicate-tags.util';

describe('duplicate-tags.util', () => {
  describe('parseTag', () => {
    it('parses audio:lossless', () => {
      const r = parseTag('audio:lossless', false);
      expect(r.category).toBe('audio');
      expect(r.label).toBe('Lossless');
      expect(r.isDiff).toBe(false);
    });

    it('parses subs:more-languages as comparative diff', () => {
      const r = parseTag('subs:more-languages', true);
      expect(r.category).toBe('subs');
      expect(r.label).toContain('Most subtitle languages');
      expect(r.isDiff).toBe(true);
      expect(r.isPositive).toBe(true);
    });

    it('parses audio:best', () => {
      const r = parseTag('audio:best', true);
      expect(r.category).toBe('audio');
      expect(r.label).toBe('Best audio profile');
      expect(r.isPositive).toBe(true);
    });

    it('parses video:best', () => {
      const r = parseTag('video:best', true);
      expect(r.category).toBe('video');
      expect(r.label).toBe('Best video (scan)');
      expect(r.isPositive).toBe(true);
    });

    it('parses chapters:more', () => {
      const r = parseTag('chapters:more', true);
      expect(r.category).toBe('chapters');
      expect(r.label).toBe('Most chapters');
      expect(r.isPositive).toBe(true);
    });
  });

  describe('getTagColor', () => {
    it('returns audio color for audio category', () => {
      const c = getTagColor('audio', false, false);
      expect(c.bg).toContain('99');
      expect(c.text).toBe('#818cf8');
    });

    it('returns green for positive diff', () => {
      const c = getTagColor('quality', true, true);
      expect(c.text).toBe('#4ade80');
    });

    it('returns orange for negative diff', () => {
      const c = getTagColor('quality', true, false);
      expect(c.text).toBe('#fb923c');
    });

    it('returns neutral light-on-dark for equivalent / informational diff', () => {
      const c = getTagColor('video', true, false, true);
      expect(c.text).toBe('#e2e8f0');
      expect(c.bg).toContain('255');
    });
  });

  describe('formatVideoBitrateDisplay', () => {
    it('shows Mbps for finite bps and em dash for null', () => {
      expect(formatVideoBitrateDisplay(25_000_000)).toContain('25');
      expect(formatVideoBitrateDisplay(25_000_000)).toContain('Mbps');
      expect(formatVideoBitrateDisplay(null)).toBe('—');
    });
  });

  describe('diffTagsForDisplay', () => {
    const member = (diff_tags: string[]) =>
      ({ duplicate_info: { diff_tags } }) as unknown as Record<string, unknown>;

    it('returns raw tags unchanged for fewer than 2 members', () => {
      expect(diffTagsForDisplay([member(['chapters:more'])], ['chapters:more'])).toEqual(['chapters:more']);
    });

    it('drops comparative tags every member has (full tie)', () => {
      const g = [member(['chapters:more', 'video:best']), member(['chapters:more', 'video:best'])];
      expect(diffTagsForDisplay(g, ['chapters:more', 'video:best'])).toEqual([]);
    });

    it('keeps comparative tag when not every member has it', () => {
      const g = [member(['chapters:more']), member([])];
      expect(diffTagsForDisplay(g, ['chapters:more'])).toEqual(['chapters:more']);
    });
  });

  describe('pickStrongestOtherForDiffTag', () => {
    const mk = (
      id: string,
      ch: number,
      subL: number,
      px: number,
      br: number | null,
      audioScore: number,
      audioL: number
    ) =>
      ({
        title_id: id,
        duplicate_info: {
          metrics: {
            chapters_count: ch,
            subtitle_language_count: subL,
            video_pixels: px,
            video_bitrate: br,
            audio_score: audioScore,
            audio_language_count: audioL,
            scan_usable: true,
          },
        },
      }) as unknown as Record<string, unknown>;

    it('picks other with higher chapter count', () => {
      const cur = mk('a', 10, 1, 0, null, 1, 1);
      const o1 = mk('b', 20, 1, 0, null, 1, 1);
      const o2 = mk('c', 12, 1, 0, null, 1, 1);
      const pick = pickStrongestOtherForDiffTag('chapters:more', cur, [o1, o2]);
      expect((pick as { title_id?: string }).title_id).toBe('b');
    });

    it('picks other with higher resolution pixels for video:best', () => {
      const cur = mk('a', 0, 0, 1920 * 1080, 50e6, 0, 0);
      const o1 = mk('b', 0, 0, 3840 * 2160, 40e6, 0, 0);
      const pick = pickStrongestOtherForDiffTag('video:best', cur, [o1]);
      expect((pick as { title_id?: string }).title_id).toBe('b');
    });
  });

  describe('buildDuplicateComparisonMatrix', () => {
    it('returns sections with one line per title', () => {
      const a = {
        source_file: 'a.mkv',
        duplicate_info: {
          metrics: {
            chapters_count: 10,
            subtitle_language_count: 2,
            video_pixels: 1920 * 1080,
            video_bitrate: 20e6,
            audio_score: 2,
            audio_language_count: 2,
            scan_usable: true,
          },
        },
        metadata_scan: {
          chapters_count: 10,
          subtitle_summary: [{ language: 'eng' }, { language: 'fra' }],
          audio_summary: [{ codec_name: 'AC3', channels: 6, channel_layout: '5.1(side)' }],
          video_hints: { width: 1920, height: 1080 },
          format: { bit_rate: 20_000_000 },
        },
      } as unknown as Record<string, unknown>;
      const b = {
        source_file: 'b.mkv',
        duplicate_info: {
          metrics: {
            chapters_count: 12,
            subtitle_language_count: 2,
            video_pixels: 1920 * 1080,
            video_bitrate: 21e6,
            audio_score: 2,
            audio_language_count: 2,
            scan_usable: true,
          },
        },
        metadata_scan: {
          chapters_count: 12,
          subtitle_summary: [{ language: 'eng' }, { language: 'fra' }],
          audio_summary: [{ codec_name: 'AC3', channels: 6, channel_layout: '5.1(side)' }],
          video_hints: { width: 1920, height: 1080 },
          format: { bit_rate: 21_000_000 },
        },
      } as unknown as Record<string, unknown>;
      const sections = buildDuplicateComparisonMatrix([a, b]);
      expect(sections.length).toBeGreaterThanOrEqual(4);
      const videoSec = sections.find((s) => s.heading === 'Video (scan)');
      expect(videoSec?.lines.length).toBe(2);
    });
  });

  describe('getComparisonDetail', () => {
    const withMetrics = (id: string, m: Record<string, unknown>) =>
      ({
        title_id: id,
        duplicate_info: { metrics: m },
      }) as unknown as Record<string, unknown>;

    it('marks chapters comparison equivalent when counts match', () => {
      const cur = withMetrics('a', { chapters_count: 12 });
      const cmp = withMetrics('b', { chapters_count: 12 });
      const d = getComparisonDetail('chapters:more', cur, cmp);
      expect(d?.isEquivalent).toBe(true);
    });

    it('compares subtitle language counts via metrics', () => {
      const cur = withMetrics('a', { subtitle_language_count: 2 });
      const cmp = withMetrics('b', { subtitle_language_count: 4 });
      const d = getComparisonDetail('subs:more-languages', cur, cmp);
      expect(d?.currentValue).toBe('2');
      expect(d?.comparedValue).toBe('4');
    });
  });

  describe('getGroupColor', () => {
    it('returns a color from the palette', () => {
      const c = getGroupColor('group-1');
      expect(DUPLICATE_GROUP_COLORS.some((p) => p.color === c.color)).toBe(true);
      expect(c.glow).toBeDefined();
    });

    it('returns consistent color for same groupId', () => {
      const a = getGroupColor('abc');
      const b = getGroupColor('abc');
      expect(a.color).toBe(b.color);
    });
  });

  describe('getGroupIdentifier', () => {
    it('returns a string number 1-99', () => {
      const id = getGroupIdentifier('group-x');
      const n = parseInt(id, 10);
      expect(n).toBeGreaterThanOrEqual(1);
      expect(n).toBeLessThanOrEqual(99);
    });

    it('returns consistent identifier for same groupId', () => {
      expect(getGroupIdentifier('g1')).toBe(getGroupIdentifier('g1'));
    });
  });

  describe('getSameAsText', () => {
    it('returns empty for empty list', () => {
      expect(getSameAsText([], 'current')).toBe('');
    });

    it('returns single other title name', () => {
      const titles = [
        { id: 'c', title: 'Current', sourceFile: 'c.mpls' },
        { id: 'o', title: 'Other Title', sourceFile: 'o.mpls' },
      ];
      expect(getSameAsText(titles, 'c')).toBe('Other Title');
    });

    it('falls back to sourceFile when title missing', () => {
      const titles = [
        { id: 'c', sourceFile: 'c.mpls' },
        { id: 'o', sourceFile: '00800.mpls' },
      ];
      expect(getSameAsText(titles, 'c')).toBe('00800.mpls');
    });

    it('returns "X and Y" for two others', () => {
      const titles = [
        { id: 'c', title: 'Current' },
        { id: 'a', title: 'A' },
        { id: 'b', title: 'B' },
      ];
      expect(getSameAsText(titles, 'c')).toBe('A and B');
    });

    it('returns "N other titles" for more than two', () => {
      const titles = [
        { id: 'c', title: 'Current' },
        { id: '1', title: 'One' },
        { id: '2', title: 'Two' },
        { id: '3', title: 'Three' },
      ];
      expect(getSameAsText(titles, 'c')).toBe('3 other titles');
    });
  });

  describe('getTitleDisplayName', () => {
    it('returns title when set', () => {
      expect(getTitleDisplayName({ id: '1', title: 'My Title' })).toBe('My Title');
    });

    it('returns sourceFile when title missing', () => {
      expect(getTitleDisplayName({ id: '1', sourceFile: '00800.mpls' })).toBe('00800.mpls');
    });

    it('returns Title {id-suffix} when both missing', () => {
      expect(getTitleDisplayName({ id: 'abc12345' })).toBe('Title 2345');
    });
  });
});
