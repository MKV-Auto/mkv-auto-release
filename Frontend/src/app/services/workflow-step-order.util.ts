/**
 * Centralized step-order resolution for the workflow breadcrumb + actions.
 *
 * Before this helper, `['film', 'boxset', 'disc', 'titles', 'transfer']`
 * (miss flow) and `['summary', 'transfer']` (hit flow) were hardcoded in
 * 17 places across the frontend. Introducing the new `exploratory_rip`
 * step (Path A) without centralizing would multiply that fan-out. Every
 * callsite that needs a step list now goes through here.
 *
 * #365 Phase 2 § 6.4 — the standalone `postprocess` step was collapsed
 * into transfer's "preparing" sub-phase, so it no longer appears in the
 * step orders below. The prep work still runs on the backend; the
 * `transferPhaseLabel` shows it as "Preparing files…" on the Transfer
 * card.
 *
 * Path A (selective rip + segment-reorder) is a miss-only flow. The Hit
 * profile never enters it — DiscDB-classified discs go straight to the
 * short summary → transfer path.
 */
import type { WorkflowStep } from './workflow.service';

export type WorkflowProfile = 'hit' | 'miss';

export interface StepOrderOptions {
  profile: WorkflowProfile;
  /**
   * True when the active job has a `segment_reorder_state` — the Path A
   * trigger. Inserts the `exploratory_rip` pill between `film` and `boxset`
   * for the miss profile. Ignored for the hit profile (Path A doesn't apply).
   */
  hasExploratoryRip?: boolean;
}

/**
 * Returns the workflow steps for the given profile / Path A combination, in
 * breadcrumb order. The list is the canonical source of truth — use it for
 * navigation gating, "next step" math, breadcrumb rendering, and validation.
 */
export function getStepOrder(opts: StepOrderOptions): WorkflowStep[] {
  if (opts.profile === 'hit') {
    return ['summary', 'transfer'];
  }
  if (opts.hasExploratoryRip) {
    return ['film', 'exploratory_rip', 'boxset', 'disc', 'titles', 'transfer'];
  }
  return ['film', 'boxset', 'disc', 'titles', 'transfer'];
}

/**
 * Convenience for callers that have a WorkflowContext-shaped object. Reads
 * `discdbHit` for profile and `jobStatus.segment_reorder_state` for the Path
 * A flag. Keeps the call sites short:
 *
 *   const steps = getStepOrderForContext(ctx);
 *
 * `ctx` is loosely typed because workflow.service.ts owns the WorkflowContext
 * interface — importing it here would create a circular module reference.
 */
export function getStepOrderForContext(ctx: {
  discdbHit?: boolean | null;
  jobStatus?: { segment_reorder_state?: unknown } | null;
} | null | undefined): WorkflowStep[] {
  const profile: WorkflowProfile = ctx?.discdbHit ? 'hit' : 'miss';
  const hasExploratoryRip = !!ctx?.jobStatus?.segment_reorder_state;
  return getStepOrder({ profile, hasExploratoryRip });
}
