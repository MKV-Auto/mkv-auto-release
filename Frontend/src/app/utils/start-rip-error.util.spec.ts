import {
  isAmbiguousStartRipTransportError,
  START_RIP_AMBIGUOUS_RESPONSE_COPY,
  startRipFailureVerb,
} from './start-rip-error.util';

describe('start-rip-error.util', () => {
  it('exposes canonical ambiguous-response copy', () => {
    expect(START_RIP_AMBIGUOUS_RESPONSE_COPY).toContain('refresh the page');
  });

  describe('isAmbiguousStartRipTransportError', () => {
    it('returns true for status 0', () => {
      expect(isAmbiguousStartRipTransportError({ status: 0 })).toBe(true);
    });

    it('returns true for 504 and 502', () => {
      expect(isAmbiguousStartRipTransportError({ status: 504 })).toBe(true);
      expect(isAmbiguousStartRipTransportError({ status: 502 })).toBe(true);
    });

    it('returns true for 408', () => {
      expect(isAmbiguousStartRipTransportError({ status: 408 })).toBe(true);
    });

    it('returns false for structured API errors', () => {
      expect(isAmbiguousStartRipTransportError({ status: 400 })).toBe(false);
      expect(isAmbiguousStartRipTransportError({ status: 503 })).toBe(false);
      expect(isAmbiguousStartRipTransportError({ status: 409 })).toBe(false);
    });
  });

  describe('startRipFailureVerb', () => {
    it('returns rip when discMode is rip', () => {
      expect(startRipFailureVerb('rip')).toBe('rip');
    });

    it('returns copy when discMode is copy or missing', () => {
      expect(startRipFailureVerb('copy')).toBe('copy');
      expect(startRipFailureVerb(undefined)).toBe('copy');
    });
  });
});
