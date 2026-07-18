import { slugifyDiscName } from './disc-slug.util';

describe('slugifyDiscName', () => {
  it('matches backend rules for format + title', () => {
    expect(slugifyDiscName('Blu-Ray - Sas Rogue Heroes S2 D1')).toBe(
      'blu-ray_-_sas_rogue_heroes_s2_d1'
    );
  });

  it('uses underscore for spaces and hyphen for punctuation', () => {
    expect(slugifyDiscName('Blu Ray')).toBe('blu_ray');
    expect(slugifyDiscName('A & B / C')).toBe('a_b_-_c');
  });

  it('returns empty for nullish', () => {
    expect(slugifyDiscName('')).toBe('');
    expect(slugifyDiscName(null)).toBe('');
    expect(slugifyDiscName(undefined)).toBe('');
  });
});
