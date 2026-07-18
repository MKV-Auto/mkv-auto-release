/**
 * Happy-path HIT — full-stack E2E against the fixture-catalog backend.
 *
 * Closes #194 ("E2E: happy path HIT — DiscDB hit → skip label → postprocess").
 *
 * Run with the hit_movie or hit_show fixture loaded:
 *     E2E_FIXTURE=hit_movie npm run e2e:full
 *
 * Phase A scope (this spec):
 *   - Rip completes with rip_progress=100, rip_state=completed.
 *   - Job branches into HIT workflow: stage_profile='hit' and
 *     label_state='skipped' (no manual labeling required).
 *   - Requires the loaded fixture to have a content_hash that is a real
 *     entry in TheDiscDB so the live lookup returns a match.
 *
 * Phase B follow-up (separate work):
 *   - Validate post_state=completed and transfer_state=completed end-to-end.
 *   - Requires postprocess + transfer to handle the full title list of the
 *     real disc (MockMKV produces the right structure; postprocess rename
 *     and transfer plumbing need to be validated against actual disc data).
 */
import { test, expect } from '@playwright/test';

const apiBase = process.env['E2E_API_URL'] || 'http://localhost:8000';

/** Set the DiscDB miss-workflow-with-prefill toggle before starting a rip. */
async function setPrefillToggle(
  req: import('@playwright/test').APIRequestContext,
  enabled: boolean,
): Promise<void> {
  const res = await req.post(`${apiBase}/system/discdb-lookup/config`, {
    data: { discdb_miss_workflow_with_prefill: enabled, eject_on_finish: false },
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok()) {
    const body = await res.text();
    throw new Error(`POST /system/discdb-lookup/config failed: ${res.status()} ${body}`);
  }
  const cfg = await res.json();
  expect(cfg.discdb_miss_workflow_with_prefill).toBe(enabled);
}

/**
 * Start a rip and poll until rip_state=completed. Returns the terminal status.
 */
async function ripAndPoll(
  req: import('@playwright/test').APIRequestContext,
  timeoutMs = 90_000,
): Promise<any> {
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

  let last: any = undefined;
  let maxProgress = 0;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const statusRes = await req.get(`${apiBase}/jobs/${jobId}/status`);
    expect(statusRes.ok()).toBe(true);
    last = await statusRes.json();
    const ripState = last.rip_state ?? last.pipeline?.rip ?? last.job_status;
    const p = last.rip_progress;
    if (typeof p === 'number') maxProgress = Math.max(maxProgress, p);
    if (ripState === 'completed') break;
    await new Promise((r) => setTimeout(r, 300));
  }
  expect(last, 'rip status was never fetched').toBeTruthy();
  expect(last.rip_state ?? last.pipeline?.rip ?? last.job_status).toBe('completed');
  expect(maxProgress).toBeGreaterThanOrEqual(100);
  return { jobId, status: last };
}

test.describe('Happy path HIT (E2E_FULL)', () => {
  test('DiscDB hit branches into label-skipped workflow', async ({ request: req }) => {
    test.skip(!process.env['E2E_FULL'], 'Set E2E_FULL=1 to run (e.g. npm run e2e:full)');
    test.skip(!!process.env['CI'], 'Skip in CI: no MakeMKV on runner');
    const fixtureName = process.env['E2E_FIXTURE'] || '';
    test.skip(
      !(fixtureName === 'hit_movie' || fixtureName === 'hit_show'),
      `Spec requires a hit_* fixture; current fixture=${fixtureName || 'unset'}. ` +
        `Re-run with E2E_FIXTURE=hit_movie or E2E_FIXTURE=hit_show.`
    );
    test.setTimeout(120_000);

    // Explicitly pin the toggle so the assertion below is stable regardless of
    // the shipping default (#615 changed it to true, which would break the
    // stage_profile=hit expectation).
    await setPrefillToggle(req, false);

    const { status } = await ripAndPoll(req);

    // HIT branching: stage_profile=hit + label was skipped
    expect(
      status.stage_profile,
      `expected stage_profile=hit (DiscDB returned a match for the fixture content_hash); ` +
        `got ${status.stage_profile}. If this fails, verify the fixture's content_hash is still in ` +
        `TheDiscDB or use happy-path-miss.spec.ts instead.`
    ).toBe('hit');
    expect(
      status.label_state ?? status.pipeline?.label,
      `expected label_state=skipped for HIT workflow; got ${status.label_state}`
    ).toBe('skipped');
  });
});

/**
 * #620 — prefill toggle × HIT: cells 1 & 2 of the v1.0.0 smoke matrix.
 *
 * The ``discdb_miss_workflow_with_prefill`` setting inverts the rip-completion
 * branching for a DiscDB hit:
 *
 *   - ``false``: hit → stage_profile=hit, label_state=skipped (short workflow;
 *     job goes straight to postprocess/transfer, no label UI walkthrough).
 *   - ``true``:  hit → stage_profile=miss, label_state=pending/ready (label UI
 *     walkthrough is shown, but the fields are pre-filled from DiscDB metadata
 *     — different from the true miss path where the fields are blank).
 *
 * In both cases ``discdb_result`` stays ``"hit"`` (the metadata source is still
 * DiscDB, only the UI path changes).
 */
test.describe('Prefill toggle × HIT (E2E_FULL)', () => {
  test.beforeEach(({}, testInfo) => {
    test.skip(!process.env['E2E_FULL'], 'Set E2E_FULL=1 to run (e.g. npm run e2e:full)');
    test.skip(!!process.env['CI'], 'Skip in CI: no MakeMKV on runner');
    const fixtureName = process.env['E2E_FIXTURE'] || '';
    test.skip(
      !(fixtureName === 'hit_movie' || fixtureName === 'hit_show'),
      `Spec requires a hit_* fixture; current fixture=${fixtureName || 'unset'}.`
    );
    testInfo.setTimeout(120_000);
  });

  test('HIT + prefill=OFF: DiscDB hit skips label UI (label_state=skipped)', async ({ request: req }) => {
    await setPrefillToggle(req, false);
    const { status } = await ripAndPoll(req);
    expect(status.stage_profile, `HIT+OFF must resolve stage_profile=hit`).toBe('hit');
    expect(
      status.label_state ?? status.pipeline?.label,
      `HIT+OFF must have label_state=skipped (label UI bypassed)`
    ).toBe('skipped');
    // discdb_result is the metadata source; still "hit" even with prefill on.
    expect(String(status.discdb_result || '').toLowerCase()).toBe('hit');
  });

  test('HIT + prefill=ON: DiscDB hit routes through label UI with pre-filled fields', async ({ request: req }) => {
    await setPrefillToggle(req, true);
    const { status } = await ripAndPoll(req);
    // Prefill forces label_required=True on the disc payload, which flips
    // stage_profile to "miss" at job creation. label_state advances to
    // "ready" after rip-verification; earlier states may be observed if the
    // poll wins the race.
    expect(status.stage_profile, `HIT+ON must resolve stage_profile=miss (prefill forces label UI)`).toBe('miss');
    expect(
      ['pending', 'running', 'ready'],
      `HIT+ON must have label_state in pending/running/ready (label UI shown, not skipped); got ${status.label_state}`
    ).toContain(status.label_state ?? status.pipeline?.label);
    // discdb_result stays "hit" — metadata source is DiscDB even though the
    // UI walks the miss-style flow.
    expect(String(status.discdb_result || '').toLowerCase()).toBe('hit');
  });
});
