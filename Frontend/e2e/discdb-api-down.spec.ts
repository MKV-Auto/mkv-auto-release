/**
 * DiscDB API down → MISS — full-stack E2E against the discdb_error fixture.
 *
 * Closes #196 ("E2E: API down treated as MISS; pipeline continues").
 *
 * Run with the discdb_error fixture loaded:
 *     E2E_FIXTURE=discdb_error npm run e2e:full
 *
 * The discdb_error fixture reuses hit_movie's real disc payload but points
 * DISKDBURL at 127.0.0.1:1 (always-refused port) so the lookup fails at the
 * network layer. Acceptance criterion (from #196): "API down or lookup
 * failure treated as MISS; pipeline never blocked".
 *
 * Phase A scope (this spec):
 *   - Rip completes (network error during DiscDB lookup does not block rip).
 *   - Job branches into MISS workflow: stage_profile='miss' and label_state
 *     in pending/running.
 *
 * What this test specifically validates beyond happy-path-miss.spec.ts:
 *   - The path through the *real* DiscDB integration with a failing network
 *     call. happy-path-miss uses settings.discdb_disabled (synthetic miss);
 *     this exercises the exception/timeout path in disc_manager that's
 *     supposed to swallow network errors and return label_required.
 */
import { test, expect } from '@playwright/test';

const apiBase = process.env['E2E_API_URL'] || 'http://localhost:8000';

test.describe('DiscDB API down (E2E_FULL)', () => {
  test('network-failed DiscDB lookup is treated as MISS, pipeline continues', async ({ request: req }) => {
    test.skip(!process.env['E2E_FULL'], 'Set E2E_FULL=1 to run (e.g. npm run e2e:full)');
    test.skip(!!process.env['CI'], 'Skip in CI: no MakeMKV on runner');
    const fixtureName = process.env['E2E_FIXTURE'] || '';
    test.skip(
      fixtureName !== 'discdb_error',
      `Spec requires the discdb_error fixture; current fixture=${fixtureName || 'unset'}. ` +
        `Re-run with E2E_FIXTURE=discdb_error.`
    );
    test.setTimeout(90_000);

    // 1. Start rip via API
    const ripRes = await req.post(`${apiBase}/jobs/rip`, {
      data: { mount_point: '/dev/sr0', disc_num: '1' },
      headers: { 'Content-Type': 'application/json' },
    });
    if (!ripRes.ok()) {
      const body = await ripRes.text();
      throw new Error(`POST /jobs/rip failed: ${ripRes.status()} ${body}`);
    }
    const ripJson = await ripRes.json();
    const jobId = ripJson.jobId ?? ripJson.job_id;
    expect(jobId).toBeTruthy();

    // 2. Poll until rip completes — must finish even though DiscDB is down.
    let ripState: string | undefined;
    let stageProfile: string | undefined;
    let labelState: string | undefined;
    let maxRipProgress = 0;
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      const statusRes = await req.get(`${apiBase}/jobs/${jobId}/status`);
      expect(statusRes.ok()).toBe(true);
      const status = await statusRes.json();
      ripState = status.rip_state ?? status.pipeline?.rip ?? status.job_status;
      stageProfile = status.stage_profile;
      labelState = status.label_state ?? status.pipeline?.label;
      const p = status.rip_progress;
      if (typeof p === 'number') maxRipProgress = Math.max(maxRipProgress, p);
      if (ripState === 'completed') break;
      await new Promise((r) => setTimeout(r, 300));
    }

    // 3. Rip succeeded despite DiscDB being unreachable
    expect(
      ripState,
      'rip must complete even when DiscDB API is down (pipeline must never be blocked)'
    ).toBe('completed');
    expect(maxRipProgress).toBeGreaterThanOrEqual(100);

    // 4. Job branched into MISS (the safe fallback) instead of failing the job
    expect(
      stageProfile,
      `expected stage_profile=miss (DiscDB lookup failure → MISS workflow); ` +
        `got ${stageProfile}. If 'hit', the network override did not take effect.`
    ).toBe('miss');
    // After rip_verification on the MISS path the state machine advances label
    // to `ready` (post-rip, pre-label-submit). `pending`/`running` are earlier
    // transitional states that the polling loop may or may not catch — match
    // the assertion shape happy-path-miss.spec.ts uses for the same reason.
    expect(
      ['pending', 'running', 'ready'],
      `expected label_state in pending/running/ready for MISS workflow; got ${labelState}`
    ).toContain(labelState);
  });
});
