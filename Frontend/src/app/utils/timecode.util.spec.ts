import { formatTimecode } from './timecode.util';

describe('formatTimecode (#891)', () => {
  it('formats seconds as full H:MM:SS, MakeMKV style', () => {
    expect(formatTimecode(5627)).toBe('1:33:47');
    expect(formatTimecode(2530)).toBe('0:42:10');
    expect(formatTimecode(245)).toBe('0:04:05');
    expect(formatTimecode(45)).toBe('0:00:45');
    expect(formatTimecode(36000)).toBe('10:00:00');
  });

  it('never rounds seconds away — titles a few seconds apart stay distinct', () => {
    expect(formatTimecode(5627)).not.toBe(formatTimecode(5629));
    expect(formatTimecode(5627.9)).toBe('1:33:47');
  });

  it('normalises existing timecode and numeric strings (duration_raw)', () => {
    expect(formatTimecode('1:33:47')).toBe('1:33:47');
    expect(formatTimecode('01:33:47')).toBe('1:33:47');
    expect(formatTimecode('93:47')).toBe('1:33:47');
    expect(formatTimecode('5627')).toBe('1:33:47');
  });

  it('returns empty for no duration so callers keep their own placeholder', () => {
    for (const v of [null, undefined, 0, -3, NaN, '', '  ', 'n/a', '1:2:3:4']) {
      expect(formatTimecode(v as any)).toBe('');
    }
  });
});
