import { TestBed } from '@angular/core/testing';
import {
  LabelForm,
  LabelFormService,
  RELEASE_REQUIRED_FIELDS,
  isReleaseSufficientlyComplete,
} from './label-form.service';
import { LoggerService } from '../../../services/logger.service';

describe('LabelFormService', () => {
  let service: LabelFormService;
  let mockLogger: { log: jasmine.Spy };

  beforeEach(() => {
    mockLogger = { log: jasmine.createSpy('log') };
    TestBed.configureTestingModule({
      providers: [
        LabelFormService,
        { provide: LoggerService, useValue: mockLogger },
      ],
    });
    service = TestBed.inject(LabelFormService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('buildLabelForm', () => {
    it('builds form from draft with defaults', () => {
      const form = service.buildLabelForm({
        release_name: 'R',
        release_slug: 'r',
        disc_group: 'g',
        disc_name: 'D',
        disc_slug: 'd',
        disc_format: 'UHD',
      });
      expect(form.mode).toBe('movie');
      expect(form.group_type).toBe('movie');
      expect(form.release_name).toBe('R');
      expect(form.release_slug).toBe('r');
      expect(form.disc_group).toBe('g');
      expect(form.disc_name).toBe('D');
      expect(form.disc_slug).toBe('d');
      expect(form.disc_format).toBe('UHD');
      expect(Array.isArray(form.tracks)).toBe(true);
    });

    it('normalizes group_type and mode away from boxset', () => {
      const form = service.buildLabelForm(
        { group_type: 'boxset', release_slug: 'x', disc_group: 'g', disc_name: 'd', disc_slug: 'd' },
        true,
        'movie'
      );
      expect(form.group_type).toBe('movie');
      expect(form.mode).toBe('movie');
    });

    it('uses pendingGroupType when draft has no group_type', () => {
      const form = service.buildLabelForm(
        { release_slug: 'x', disc_group: 'g', disc_name: 'd', disc_slug: 'd' },
        true,
        'series'
      );
      expect(form.group_type).toBe('series');
    });
  });

  describe('buildMetadataPayload', () => {
    it('returns empty struct when labelForm is null', () => {
      const out = service.buildMetadataPayload(null);
      expect(out).toEqual({ release: {}, disc: {}, titles: [] });
    });

    it('builds release, disc, titles from labelForm', () => {
      const form = service.buildLabelForm({
        release_name: 'R',
        release_slug: 'r',
        disc_group: 'g',
        disc_name: 'D',
        disc_slug: 'd',
        disc_format: 'Blu-Ray',
        production_year: 2020,
        tracks: [],
      });
      form.movie_id = 'm1';
      const out = service.buildMetadataPayload(form);
      expect(out.release.release_slug).toBe('r');
      expect(out.release.release_name).toBe('R');
      expect(out.disc.disc_name).toBe('D');
      expect(out.disc.disc_slug).toBe('d');
      expect(out.disc.disc_format).toBe('Blu-Ray');
      expect(Array.isArray(out.titles)).toBe(true);
    });
  });

  describe('buildReleasePatchPayload', () => {
    it('returns {} when labelForm is null', () => {
      expect(service.buildReleasePatchPayload(null)).toEqual({});
    });

    it('builds payload with recalculate_disc_numbers when set', () => {
      const form = service.buildLabelForm({
        release_slug: 'r',
        disc_group: 'g',
        disc_name: 'd',
        disc_slug: 'd',
        tracks: [],
      });
      form.recalculate_disc_numbers = true;
      const out = service.buildReleasePatchPayload(form);
      expect(out.recalculate_disc_numbers).toBe(true);
    });
  });

  describe('validateLabelForm', () => {
    it('returns valid when labelForm is null', () => {
      expect(service.validateLabelForm(null)).toEqual({ valid: true, errors: [] });
    });

    it('returns valid when isDiscDbHit is true', () => {
      const form = service.buildLabelForm({ release_slug: 'r', disc_group: 'g', disc_name: 'd', disc_slug: 'd', tracks: [] });
      expect(service.validateLabelForm(form, true)).toEqual({ valid: true, errors: [] });
    });

    it('returns invalid with errors when required fields missing', () => {
      const form = service.buildLabelForm({ release_slug: 'r', disc_group: 'g', disc_name: 'd', disc_slug: 'd', tracks: [] });
      const out = service.validateLabelForm(form, false);
      expect(out.valid).toBe(false);
      expect(out.errors.length).toBeGreaterThan(0);
      expect(out.errors.some((e) => e.includes('Movie ID'))).toBe(true);
      expect(out.errors.some((e) => e.includes('Disc format'))).toBe(true);
    });

    it('returns valid when required fields present', () => {
      const form = service.buildLabelForm({
        release_slug: 'r',
        disc_group: 'g',
        disc_name: 'd',
        disc_slug: 'd',
        disc_format: 'UHD',
        tracks: [],
      });
      form.movie_id = 'm1';
      form.mode = 'movie';
      form.group_type = 'movie';
      const out = service.validateLabelForm(form, false);
      expect(out.valid).toBe(true);
      expect(out.errors).toEqual([]);
    });

    it('allows empty disc_slug when other required fields are present (auto-slug on save)', () => {
      const form = service.buildLabelForm({
        release_slug: 'r',
        disc_group: 'g',
        disc_name: 'd',
        disc_slug: '',
        disc_format: 'UHD',
        tracks: [],
      });
      form.movie_id = 'm1';
      form.mode = 'movie';
      form.group_type = 'movie';
      const out = service.validateLabelForm(form, false);
      expect(out.valid).toBe(true);
      expect(out.errors).toEqual([]);
    });
  });

  describe('hasLabelContent', () => {
    it('returns false when labelForm is null', () => {
      expect(service.hasLabelContent(null)).toBe(false);
    });

    it('returns true when movie_id is set', () => {
      const form = service.buildLabelForm({ release_slug: 'r', disc_group: 'g', disc_name: 'd', disc_slug: 'd', tracks: [] });
      form.movie_id = 'm1';
      expect(service.hasLabelContent(form)).toBe(true);
    });

    it('returns true when release_name is set', () => {
      const form = service.buildLabelForm({ release_name: 'R', release_slug: 'r', disc_group: 'g', disc_name: 'd', disc_slug: 'd', tracks: [] });
      expect(service.hasLabelContent(form)).toBe(true);
    });

    it('returns false when only empty-like fields', () => {
      const form = service.buildLabelForm({ release_slug: '', disc_group: '', disc_name: '', disc_slug: '', tracks: [] });
      form.movie_id = null;
      expect(service.hasLabelContent(form)).toBe(false);
    });
  });

  describe('isReleaseSufficientlyComplete (#580)', () => {
    const baseForm = (): LabelForm => service.buildLabelForm({
      release_name: 'Venom (2018)',
      release_slug: 'venom-2018',
      disc_group: 'venom-2018',
      disc_name: 'Disc 1',
      disc_slug: 'disc-1',
      disc_format: 'Blu-Ray',
      release_year: 2018,
      tracks: [],
    });

    it('returns false for null labelForm', () => {
      expect(isReleaseSufficientlyComplete(null)).toBe(false);
    });

    it('returns true when release_id + name + slug + year are populated', () => {
      const form = baseForm();
      form.release_id = 'rel-uuid';
      expect(isReleaseSufficientlyComplete(form)).toBe(true);
    });

    it('returns true via boxset_id link (release_id absent)', () => {
      const form = baseForm();
      form.release_id = null;
      form.boxset_id = 'boxset-uuid';
      expect(isReleaseSufficientlyComplete(form)).toBe(true);
    });

    it('returns false when no release identifier is linked', () => {
      // The legacy "release exists" check: name/slug/year populated but
      // no release_id and no real boxset link. The legacy gate would have
      // accepted this; #580 must reject because there is no DB record to
      // attach the metadata to.
      const form = baseForm();
      form.release_id = null;
      form.boxset_id = null;
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('returns false when boxset_id is the pending sentinel', () => {
      const form = baseForm();
      form.release_id = null;
      form.boxset_id = '__pending__';
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('returns false when release_name is empty (the bug from #580)', () => {
      const form = baseForm();
      form.release_id = 'rel-uuid';
      form.release_name = '';
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('returns false when release_name is whitespace-only', () => {
      const form = baseForm();
      form.release_id = 'rel-uuid';
      form.release_name = '   ';
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('returns false when release_slug is empty', () => {
      const form = baseForm();
      form.release_id = 'rel-uuid';
      form.release_slug = '';
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('returns false when release_year is null', () => {
      const form = baseForm();
      form.release_id = 'rel-uuid';
      form.release_year = null;
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('returns false when release_year is 0 (sentinel for "unset")', () => {
      const form = baseForm();
      form.release_id = 'rel-uuid';
      form.release_year = 0;
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('returns false when release_year is negative', () => {
      const form = baseForm();
      form.release_id = 'rel-uuid';
      form.release_year = -1;
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('returns false when release_year is a non-integer', () => {
      const form = baseForm();
      form.release_id = 'rel-uuid';
      (form as any).release_year = 2018.5;
      expect(isReleaseSufficientlyComplete(form)).toBe(false);
    });

    it('RELEASE_REQUIRED_FIELDS pin: exactly name/slug/year', () => {
      // Guard against a refactor that drops one of the required fields
      // without updating the helper. Adding a field is fine; removing one
      // requires explicitly updating this test.
      expect([...RELEASE_REQUIRED_FIELDS]).toEqual([
        'release_name', 'release_slug', 'release_year',
      ]);
    });
  });
});
