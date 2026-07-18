import { getStepOrder, getStepOrderForContext } from './workflow-step-order.util';

// #365 Phase 2 § 6.4 — the standalone 'postprocess' step was collapsed
// into transfer's "preparing" sub-phase, so it no longer appears in any
// step order. Earlier revisions of these tests asserted lists like
// ['summary', 'postprocess', 'transfer']; the assertions below pin the
// new (collapsed) shape.

describe('getStepOrder', () => {
  it('returns the short hit list when profile is hit', () => {
    expect(getStepOrder({ profile: 'hit' })).toEqual(['summary', 'transfer']);
  });

  it('ignores hasExploratoryRip on the hit profile (Path A is miss-only)', () => {
    expect(getStepOrder({ profile: 'hit', hasExploratoryRip: true })).toEqual(['summary', 'transfer']);
  });

  it('returns the standard miss list when no Path A', () => {
    expect(getStepOrder({ profile: 'miss' })).toEqual(['film', 'boxset', 'disc', 'titles', 'transfer']);
  });

  it('injects exploratory_rip between film and boxset on miss + Path A', () => {
    expect(getStepOrder({ profile: 'miss', hasExploratoryRip: true })).toEqual([
      'film',
      'exploratory_rip',
      'boxset',
      'disc',
      'titles',
      'transfer',
    ]);
  });
});

describe('getStepOrderForContext', () => {
  it('treats discdbHit=true as the hit profile', () => {
    const ctx = { discdbHit: true };
    expect(getStepOrderForContext(ctx)).toEqual(['summary', 'transfer']);
  });

  it('treats missing/false discdbHit as miss', () => {
    expect(getStepOrderForContext({})).toEqual(['film', 'boxset', 'disc', 'titles', 'transfer']);
    expect(getStepOrderForContext(null)).toEqual(['film', 'boxset', 'disc', 'titles', 'transfer']);
  });

  it('detects Path A from jobStatus.segment_reorder_state and injects the pill', () => {
    const ctx = {
      discdbHit: false,
      jobStatus: { segment_reorder_state: { stage: 'exploratory_ripping' } },
    };
    expect(getStepOrderForContext(ctx)).toEqual([
      'film',
      'exploratory_rip',
      'boxset',
      'disc',
      'titles',
      'transfer',
    ]);
  });

  it('keeps the standard list when segment_reorder_state is null', () => {
    const ctx = { discdbHit: false, jobStatus: { segment_reorder_state: null } };
    expect(getStepOrderForContext(ctx)).toEqual(['film', 'boxset', 'disc', 'titles', 'transfer']);
  });
});
