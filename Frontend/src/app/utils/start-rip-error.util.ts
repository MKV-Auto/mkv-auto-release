/**
 * User-facing copy when POST /jobs/rip fails with an ambiguous transport error
 * (browser/proxy/gateway) — the server may still have started the job.
 */
export const START_RIP_AMBIGUOUS_RESPONSE_COPY =
  'Starting the disc sometimes takes longer than expected. Please refresh the page to load the latest job status.';

/**
 * True when the start-rip HTTP call failed in a way that does not reliably indicate
 * whether the backend created or resumed a job (vs structured API errors like 400/503).
 */
export function isAmbiguousStartRipTransportError(err: unknown): boolean {
  const e = err as { status?: number; name?: string; message?: string };
  const status = e?.status;
  if (status === 0) return true;
  if (status === 504 || status === 502 || status === 408) return true;
  const msg = String(e?.message ?? '').toLowerCase();
  if (msg.includes('gateway') && msg.includes('timeout')) return true;
  return false;
}

/** Label for user-facing "Failed to start …" from disc workflow mode. */
export function startRipFailureVerb(discMode: 'copy' | 'rip' | null | undefined): 'copy' | 'rip' {
  return discMode === 'rip' ? 'rip' : 'copy';
}
