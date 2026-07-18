import { test, expect } from '@playwright/test';

const noopJson = (data: unknown = {}) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(data),
});

test.describe('Workflow Transfer Finish', () => {
  test('Finish at transfer completed clears job from UI', async ({ page }) => {
    test.skip(!!process.env['E2E_FULL'], 'Uses full API mocks; run without E2E_FULL (e.g. npm run e2e)');
    const jobId = 'job-tt';
    const discId = 'disc-1';

    // 1. Other API mocks.
    await page.route('**/jobs/unfinished/workflow-contexts', (route) => route.fulfill(noopJson([])));
    await page.route('**/events/job/**', (route) => route.abort());
    await page.route('**/events/previews/**', (route) => route.abort());
    await page.route('**/settings/**', (route) => route.fulfill(noopJson({})));
    await page.route('**/drives**', (route) => route.fulfill(noopJson([])));

    // 2. Avoid backendError from getRegistrationStatus (ripper-page loads this on init).
    await page.route(
      (url) => url.href.includes('system/makemkv/registration'),
      (route) => route.fulfill(noopJson({ expired: false, currentKey: null }))
    );

    // 3. Initial state and workflow-context: use explicit route patterns so we reliably
    //    intercept (app calls :8000; WebSocket does not send initial_state on connect).
    const initialPayload = noopJson({
      type: 'initial_state',
      discs: [
        {
          disc_id: discId,
          disc_state: 'unfinished',
          job_id: jobId,
          movie_name: 'Test Film',
          info_title: 'Test Film',
          mount_point: null,
          disc_num: null,
        },
      ],
    });
    const workflowContextPayload = noopJson({
      type: 'job',
      id: jobId,
      labelForm: {},
      labelDraftProcessed: true,
      workflowStep: 'transfer',
      discdbHit: false,
      discMode: 'copy',
      jobStatus: {
        jobId,
        job_status: 'done',
        rip_state: 'completed',
        label_state: 'completed',
        post_state: 'completed',
        transfer_state: 'completed',
      },
      titles: [],
      titlesComplete: true,
      movieOptions: [],
      boxsetOptions: [],
      releaseOptions: [],
      groupOptions: [],
    });
    await page.route('**/*', (route) => {
      const url = route.request().url();
      if (url.includes('system/makemkv/registration')) return route.fulfill(noopJson({ expired: false, currentKey: null }));
      if (url.includes('initial-state')) return route.fulfill(initialPayload);
      if (url.includes('workflow-context') && url.includes('job-tt')) return route.fulfill(workflowContextPayload);
      return route.continue();
    });

    await page.goto('/');

    // 4. Card header is shown when insertedDiscs or unfinishedJobs has length (from initial_state.discs).
    //    If our mock is applied, we have one unfinished disc so the header and one job card exist.
    await expect(page.locator('.card-header')).toBeVisible({ timeout: 8000 });

    // 5. Wait for the job card and click it. Card text includes the disc title.
    const jobCard = page.locator('button.job-card').filter({ hasText: 'Test Film' });
    await expect(jobCard).toBeVisible({ timeout: 5000 });
    await jobCard.click();

    // 6. Wait for the Finish button (workflow action bar’s primary button) and click it. Finish runs setSelectedCard(null).
    await expect(page.getByRole('button', { name: 'Back' })).toBeVisible({ timeout: 10000 });
    const finishBtn = page.locator('button.btn-primary').filter({ hasText: 'Finish' });
    await expect(finishBtn).toBeVisible({ timeout: 5000 });
    await finishBtn.click();

    // 7. After Finish, the selection is cleared: no card should be active.
    await expect(page.locator('button.job-card.active')).toHaveCount(0);
  });
});
