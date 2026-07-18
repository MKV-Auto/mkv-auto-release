/**
 * Rip happy path against the real E2E backend (MockDrive, MockMKV, test Redis).
 *
 * No page.route for /jobs/rip, /jobs/{id}/status, /api/coordinator, /discs, or WebSocket.
 * Asserts: rip completes and progress was observed (rip_progress reached 100).
 *
 * Runs only when E2E_FULL=1 (e.g. npm run e2e:full).
 */
import { test, expect } from '@playwright/test';

const apiBase = process.env['E2E_API_URL'] || 'http://localhost:8000';

test.describe('Rip happy path (E2E_FULL)', () => {
  test('rip completes and progress is observed', async ({ request: req }) => {
    test.skip(!process.env['E2E_FULL'], 'Set E2E_FULL=1 to run (e.g. npm run e2e:full)');
    test.skip(!!process.env['CI'], 'Skip in CI: no MakeMKV on runner');
    // This spec is older than the fixture catalog and shares a single backend
    // with the other rip-using specs. Playwright runs files alphabetically, so
    // `happy-path-miss.spec.ts` runs first under `E2E_FIXTURE=miss` or
    // `discdb_error` and labels a disc_title with `type='movie'`. Re-using the
    // same disc (same content_hash, fixture-deterministic) makes
    // `rip_verification_complete`'s missing-important-titles check
    // (Backend/api/routers/jobs.py:3196-3231) flag the labeled title as
    // "missing" from the next rip's output and fail the spec.
    //
    // The honest fix is per-spec isolated backends (a tracked follow-up to
    // #195/#196 that the user agreed to defer); until that lands, gate this
    // spec on the hit_movie/hit_show fixtures where the preceding labeling
    // spec (happy-path-hit) doesn't touch disc_title types.
    const fixtureName = process.env['E2E_FIXTURE'] || '';
    test.skip(
      !(fixtureName === 'hit_movie' || fixtureName === 'hit_show'),
      `Spec requires a hit_* fixture so prior specs don't contaminate disc_titles; ` +
        `current fixture=${fixtureName || 'unset'}. Re-run with npm run e2e:full:hit.`
    );
    test.setTimeout(90_000);
    // Start rip via API (e2e_bootstrap: disc_num=1, mount_point=/dev/sr0)
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

    let ripState: string | undefined;
    let maxRipProgress = 0;
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      const statusRes = await req.get(`${apiBase}/jobs/${jobId}/status`);
      expect(statusRes.ok()).toBe(true);
      const status = await statusRes.json();
      ripState = status.rip_state ?? status.pipeline?.rip ?? status.job_status;
      const p = status.rip_progress;
      if (typeof p === 'number') maxRipProgress = Math.max(maxRipProgress, p);
      if (ripState === 'completed') break;
      await new Promise((r) => setTimeout(r, 300));
    }

    expect(ripState).toBe('completed');
    expect(maxRipProgress).toBeGreaterThanOrEqual(100);
  });
});
