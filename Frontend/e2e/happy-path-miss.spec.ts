/**
 * Happy-path MISS — full-stack E2E against the fixture-catalog backend.
 *
 * Closes #195 ("E2E: happy path MISS — DiscDB miss → label → postprocess").
 *
 * Drives the full MISS workflow against the real backend with MockDrive +
 * MockMKV:
 *   1. POST /jobs/rip — start rip on the seeded mock disc
 *   2. Poll status → rip_state=completed, stage_profile=miss, label_state=pending
 *   3. GET /jobs/{id}/workflow-context → fetch the single mock title's title_id
 *   4. POST /jobs/{id}/label/complete with a minimal labelForm that links the
 *      disc to the seeded Release/Movie (see Backend/scripts/e2e_bootstrap.py)
 *      and marks the title as a `movie` type — passes _validate_all_titles_labeled
 *   5. POST /jobs/{id}/postprocess → enqueue resume_postprocess
 *   6. Poll → post_state=completed
 *   7. POST /jobs/{id}/transfer → use the seeded active local TransferConfig
 *   8. Poll → transfer_state=completed
 *
 * MockMKV writes 1500-byte fake .mkv files; postprocess only renames + moves
 * them into transient (no ffprobe/MKVToolNix at this layer), and the local
 * transfer protocol copies them to .e2e_data/transfer-dest/. The chain reaches
 * transfer_state=completed without needing real media content.
 *
 * Runs only when E2E_FULL=1 (e.g. npm run e2e:full). The 'miss' fixture is
 * the default when E2E_FIXTURE is unset.
 */
import { test, expect } from '@playwright/test';

const apiBase = process.env['E2E_API_URL'] || 'http://localhost:8000';

const SEEDED_RELEASE_ID = 'e2e-miss-release-0001';
const SEEDED_MOVIE_ID = 'e2e-miss-movie-0001';
const FIXTURE_TITLE_NAME = 'E2E Miss Fixture Movie';

async function pollStatus(
  req: import('@playwright/test').APIRequestContext,
  jobId: string,
  predicate: (status: any) => boolean,
  { timeoutMs, intervalMs = 300, label }: { timeoutMs: number; intervalMs?: number; label: string }
): Promise<any> {
  const deadline = Date.now() + timeoutMs;
  let last: any = undefined;
  while (Date.now() < deadline) {
    const res = await req.get(`${apiBase}/jobs/${jobId}/status`);
    expect(res.ok(), `GET /jobs/${jobId}/status while waiting for ${label}: ${res.status()}`).toBe(true);
    last = await res.json();
    if (predicate(last)) return last;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(`Timed out waiting for ${label}; last status=${JSON.stringify(last)}`);
}

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

test.describe('Happy path MISS (E2E_FULL)', () => {
  test('DiscDB-disabled disc rips, labels, postprocesses, and transfers', async ({ request: req }) => {
    test.skip(!process.env['E2E_FULL'], 'Set E2E_FULL=1 to run (e.g. npm run e2e:full)');
    test.skip(!!process.env['CI'], 'Skip in CI: no MakeMKV on runner');
    const fixtureName = process.env['E2E_FIXTURE'] || 'miss';
    test.skip(
      !(fixtureName === 'miss' || fixtureName === 'discdb_error'),
      `Spec validates MISS branching; current fixture=${fixtureName}`
    );
    // Headroom for: rip + first rip-complete callback retry cycle (~60s),
    // label submit, resume_postprocess, transfer, plus polling overhead.
    test.setTimeout(300_000);

    // 1. Start rip via API.
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

    // 2. Poll until rip completes; assert MISS branching.
    const ripStatus = await pollStatus(
      req,
      jobId,
      (s) => (s.rip_state ?? s.pipeline?.rip) === 'completed',
      { timeoutMs: 150_000, label: 'rip_state=completed' }
    );
    expect(ripStatus.rip_progress).toBeGreaterThanOrEqual(100);
    expect(
      ripStatus.stage_profile,
      `expected stage_profile=miss (set by settings.discdb_disabled in fixture), got ${ripStatus.stage_profile}`
    ).toBe('miss');
    // After rip_verification on the MISS path the state machine advances label
    // to ``ready`` (post-rip, pre-label-submit). ``pending``/``running`` are
    // earlier transitional states that the polling loop may or may not catch.
    expect(
      ['pending', 'running', 'ready'],
      `expected label_state in pending/running/ready for MISS workflow, got ${ripStatus.label_state}`
    ).toContain(ripStatus.label_state ?? ripStatus.pipeline?.label);

    // 3. Fetch workflow-context for the disc_titles.id of the single mock track.
    const ctxRes = await req.get(`${apiBase}/jobs/${jobId}/workflow-context`);
    expect(ctxRes.ok(), `GET workflow-context: ${ctxRes.status()}`).toBe(true);
    const ctx = await ctxRes.json();
    const firstTitle = (ctx.titles || [])[0];
    expect(firstTitle, 'workflow-context returned no titles').toBeTruthy();
    const titleId = firstTitle.title_id || firstTitle.id;
    expect(titleId, `no title_id on first title: ${JSON.stringify(firstTitle)}`).toBeTruthy();

    // 4. Submit labels: link disc to the seeded Release/Movie and mark the
    //    track as a `movie` so _validate_all_titles_labeled accepts it.
    const labelRes = await req.post(`${apiBase}/jobs/${jobId}/label/complete`, {
      data: {
        labelForm: {
          release_id: SEEDED_RELEASE_ID,
          movie_id: SEEDED_MOVIE_ID,
          group_type: 'movie',
          release_name: FIXTURE_TITLE_NAME,
          release_year: 2024,
          disc_number: 1,
          tracks: [
            {
              title_id: titleId,
              type: 'movie',
              title: FIXTURE_TITLE_NAME,
              source_file: firstTitle.source_file || firstTitle.file || '00001.mpls',
            },
          ],
        },
      },
      headers: { 'Content-Type': 'application/json' },
    });
    if (!labelRes.ok()) {
      const body = await labelRes.text();
      throw new Error(`POST /label/complete failed: ${labelRes.status()} ${body}`);
    }

    // 5. Enqueue postprocess (label/complete sets post_state=ready but does
    //    not auto-enqueue resume_postprocess on the MISS path).
    const postRes = await req.post(`${apiBase}/jobs/${jobId}/postprocess`);
    if (!postRes.ok()) {
      const body = await postRes.text();
      throw new Error(`POST /postprocess failed: ${postRes.status()} ${body}`);
    }

    // 6. Poll for post_state=completed.
    await pollStatus(
      req,
      jobId,
      (s) => s.post_state === 'completed',
      { timeoutMs: 90_000, label: 'post_state=completed' }
    );

    // 6b. File-location assertion: after postprocess, every non-ignore title
    //     must have file_path populated. The exact location is mode-dependent
    //     after the postprocess→transfer collapse (#325, #365 step 5c flipped
    //     ``MKVAUTO_RENAME_DIRECT_TO_DEST`` default to "1"):
    //       * Local mode (the seeded test TransferConfig is local): rename
    //         writes directly to ``config.transfer_dir`` and ``file_path_stage``
    //         advances straight to ``"transfer"`` via the src==dest shortcut.
    //         No ``transient/`` segment in the path.
    //       * Remote mode: rename still uses a local ``transient/`` staging
    //         area, file_path_stage stays ``"postprocess"`` until transfer.
    //     The contract that file_path is non-null + non-empty after the stage
    //     completes is the load-bearing one; the exact path shape is mode-
    //     specific. Accept either model so the spec doesn't break each time
    //     the rename destination flips.
    const discId = ripStatus.disc_id ?? ctx.disc_id ?? ctx.disc?.id;
    expect(discId, 'disc_id must be discoverable from status or workflow-context').toBeTruthy();
    const titlesAfterPostRes = await req.get(
      `${apiBase}/discs/${discId}/titles?detail=full`
    );
    expect(titlesAfterPostRes.ok()).toBe(true);
    const titlesAfterPost = (await titlesAfterPostRes.json()).items || [];
    const renamedAfterPost = titlesAfterPost.filter(
      (t: any) => (t.type ?? '').toLowerCase() !== 'ignore' && t.file_path
    );
    expect(
      renamedAfterPost.length,
      `at least one non-ignore title should have file_path after postprocess; got ${JSON.stringify(titlesAfterPost)}`
    ).toBeGreaterThan(0);
    for (const t of renamedAfterPost) {
      expect(
        t.file_path,
        `title ${t.title_id} should have a non-empty file_path after postprocess`
      ).toBeTruthy();
      // file_path_stage is either ``postprocess`` (remote: still in transient/)
      // or ``transfer`` (local: src==dest shortcut already advanced it).
      expect(
        ['postprocess', 'transfer'],
        `title ${t.title_id} file_path_stage should be 'postprocess' or 'transfer' after postprocess; got ${t.file_path_stage}`
      ).toContain(t.file_path_stage);
    }

    // 7. Trigger transfer (the seeded local TransferConfig is already active).
    //    Under the post-collapse local-mode flow (#325/#365), rename writes
    //    directly to ``config.transfer_dir`` and the src==dest shortcut at the
    //    transfer step's preamble advances transfer_state to ``completed``
    //    autonomously — meaning by the time the spec reaches this POST, the
    //    transfer may already be done and the request 409s with
    //    "Backward transfer_state transition not allowed: completed -> running".
    //    Treat that 409 as a success: poll loop below confirms the end state.
    const transferStatusRes = await req.get(`${apiBase}/jobs/${jobId}/status`);
    const transferStatusJson = await transferStatusRes.json();
    if (transferStatusJson.transfer_state !== 'completed') {
      const xferRes = await req.post(`${apiBase}/jobs/${jobId}/transfer`, {
        data: {},
        headers: { 'Content-Type': 'application/json' },
      });
      if (!xferRes.ok()) {
        const body = await xferRes.text();
        throw new Error(`POST /transfer failed: ${xferRes.status()} ${body}`);
      }
    }

    // 8. Poll for transfer_state=completed.
    await pollStatus(
      req,
      jobId,
      (s) => s.transfer_state === 'completed',
      { timeoutMs: 90_000, label: 'transfer_state=completed' }
    );

    // 8b. File-location assertion: after transfer, every non-ignore title
    //     must have its file_path advanced to the transfer destination with
    //     the transfer stage marker. Same Phase 0 regression net rationale
    //     as 6b — the absolute path will change in Phase 2 but the contract
    //     that file_path_stage transitions to 'transfer' after a successful
    //     transfer must hold.
    const titlesAfterXferRes = await req.get(
      `${apiBase}/discs/${discId}/titles?detail=full`
    );
    expect(titlesAfterXferRes.ok()).toBe(true);
    const titlesAfterXfer = (await titlesAfterXferRes.json()).items || [];
    const transferredTitles = titlesAfterXfer.filter(
      (t: any) => (t.type ?? '').toLowerCase() !== 'ignore' && t.file_path
    );
    expect(transferredTitles.length).toBeGreaterThan(0);
    for (const t of transferredTitles) {
      expect(
        t.file_path_stage,
        `title ${t.title_id} should have file_path_stage='transfer' after transfer`
      ).toBe('transfer');
      // We don't pin the exact destination prefix here — that depends on the
      // active TransferConfig (.e2e_data/transfer-dest/) and is the transfer
      // worker's concern. Just check that the path isn't still pointing at
      // transient/ from the postprocess stage.
      expect(t.file_path).not.toContain('transient');
    }
  });
});

/**
 * #620 — prefill toggle × MISS: cells 3 & 4 of the v1.0.0 smoke matrix.
 *
 * On the miss path (DiscDB disabled or unknown disc) the
 * ``discdb_miss_workflow_with_prefill`` setting is documented as **inert**:
 * there's no DiscDB metadata to prefill from, so the label UI walkthrough
 * looks and behaves identically whether the toggle is on or off.
 *
 * This block asserts that intent by ripping in both toggle positions and
 * comparing the observable pipeline states. Any divergence between
 * MISS+ON and MISS+OFF is a bug: file a separate ticket rather than
 * paper it over here (per the acceptance criterion in #620).
 */
test.describe('Prefill toggle × MISS (E2E_FULL) — toggle is inert on miss path', () => {
  test.beforeEach(({}, testInfo) => {
    testInfo.setTimeout(120_000);
    test.skip(!process.env['E2E_FULL'], 'Set E2E_FULL=1 to run (e.g. npm run e2e:full)');
    test.skip(!!process.env['CI'], 'Skip in CI: no MakeMKV on runner');
    const fixtureName = process.env['E2E_FIXTURE'] || 'miss';
    test.skip(
      !(fixtureName === 'miss' || fixtureName === 'discdb_error'),
      `Prefill × MISS block validates the MISS branching; current fixture=${fixtureName}`
    );
  });

  async function ripAndCaptureBranching(
    req: import('@playwright/test').APIRequestContext,
  ): Promise<{ stageProfile: string; labelState: string; discdbResult: string }> {
    const ripRes = await req.post(`${apiBase}/jobs/rip`, {
      data: { mount_point: '/dev/sr0', disc_num: '1' },
      headers: { 'Content-Type': 'application/json' },
    });
    if (!ripRes.ok()) {
      const body = await ripRes.text();
      throw new Error(`POST /jobs/rip failed: ${ripRes.status()} ${body}`);
    }
    const { jobId, job_id } = await ripRes.json();
    const id = jobId ?? job_id;
    const st = await pollStatus(
      req,
      id,
      (s) => (s.rip_state ?? s.pipeline?.rip) === 'completed',
      { timeoutMs: 90_000, label: `rip_state=completed (job=${id})` }
    );
    return {
      stageProfile: st.stage_profile,
      labelState: st.label_state ?? st.pipeline?.label,
      discdbResult: String(st.discdb_result || '').toLowerCase(),
    };
  }

  test('MISS + prefill=OFF: standard miss workflow (stage_profile=miss, label_state=ready)', async ({ request: req }) => {
    await setPrefillToggle(req, false);
    const branching = await ripAndCaptureBranching(req);
    expect(branching.stageProfile).toBe('miss');
    expect(
      ['pending', 'running', 'ready'],
      `MISS+OFF label_state must be pending/running/ready; got ${branching.labelState}`
    ).toContain(branching.labelState);
    // MISS fixture ships discdb_disabled → discdb_result must be "miss".
    expect(branching.discdbResult).toBe('miss');
  });

  test('MISS + prefill=ON: identical observable branching to MISS+OFF (toggle inert)', async ({ request: req }) => {
    await setPrefillToggle(req, true);
    const branching = await ripAndCaptureBranching(req);
    expect(
      branching.stageProfile,
      `MISS+ON must resolve stage_profile=miss (same as MISS+OFF); toggle should be inert on the miss path`
    ).toBe('miss');
    expect(
      ['pending', 'running', 'ready'],
      `MISS+ON label_state must be pending/running/ready (same set as MISS+OFF); got ${branching.labelState}`
    ).toContain(branching.labelState);
    expect(
      branching.discdbResult,
      `MISS+ON discdb_result must be "miss" (same as MISS+OFF); toggle does not affect metadata source on miss`
    ).toBe('miss');
  });
});
