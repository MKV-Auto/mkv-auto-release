import { canonicalTrackTitle } from './canonical-track-title.util';

describe('canonicalTrackTitle', () => {
  it('prefers non-empty title over episode_name', () => {
    expect(canonicalTrackTitle({ title: 'A', episode_name: 'B' })).toBe('A');
  });

  it('falls back to episode_name when title empty', () => {
    expect(canonicalTrackTitle({ title: '', episode_name: 'Pilot' })).toBe('Pilot');
    expect(canonicalTrackTitle({ title: '   ', episode_name: 'Pilot' })).toBe('Pilot');
  });

  it('returns empty string when both missing', () => {
    expect(canonicalTrackTitle({})).toBe('');
    expect(canonicalTrackTitle(null)).toBe('');
  });
});
