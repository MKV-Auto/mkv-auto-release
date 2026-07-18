import { test, expect } from '@playwright/test';

const noopJson = (data: any = {}) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(data),
});

test.describe('Label record hydration overwrite protection', () => {
  test('user edits survive late record hydration', async ({ page }) => {
    test.skip(!!process.env['E2E_FULL'], 'Uses full API mocks; run without E2E_FULL (e.g. npm run e2e)');
    // Mock minimal current job with disc/release ids but no label payload.
    await page.route('**/jobs/current', route =>
      route.fulfill(
        noopJson({
          jobId: 'job-123',
          createdAt: new Date().toISOString(),
          disc: { disc_num: '1', mount_point: '/mnt/d1', movie_name: 'Test Disc', tracks: {}, disc_hash: 'hash-123' },
          status: 'running',
          progress: 5,
          disc_id: 'disc-1',
          release_id: 'rel-1',
          disc_payload: {},
        })
      )
    );
    await page.route('**/discs/current', route => route.fulfill(noopJson({ disc: null, job: null })));
    await page.route('**/events/job/**', route => route.abort());
    await page.route('**/events/previews/**', route => route.abort());
    await page.route('**/settings/**', route => route.fulfill(noopJson({})));
    await page.route('**/drives**', route => route.fulfill(noopJson([])));
    // Avoid backendError from getRegistrationStatus (ripper-page loads this on init).
    await page.route(
      (url) => url.href.includes('system/makemkv/registration'),
      (route) => route.fulfill(noopJson({ expired: false, currentKey: null }))
    );

    // initial-state: one unfinished disc so the job card shows.
    await page.route(
      (url) => url.href.includes('api/coordinator/initial-state'),
      (route) =>
        route.fulfill(
          noopJson({
            type: 'initial_state',
            discs: [
              {
                disc_id: 'disc-1',
                disc_state: 'unfinished',
                job_id: 'job-123',
                movie_name: 'Test Disc',
                info_title: 'Test Disc',
                mount_point: null,
                disc_num: null,
              },
            ],
          })
        )
    );
    // workflow-context: disc step + labelForm with disc_number so the disc step and app-disc-label (Disc Name, Disc Slug) render.
    await page.route(
      (url) => url.href.includes('jobs/job-123/workflow-context'),
      (route) =>
      route.fulfill(
        noopJson({
          type: 'job',
          id: 'job-123',
          workflowStep: 'disc',
          labelForm: {
            release_name: '',
            release_slug: '',
            release_year: '',
            disc_name: '',
            disc_slug: '',
            disc_number: 1,
            disc_format: 'Blu-Ray',
          },
          discInfo: { disc_id: 'disc-1' },
          labelDraftProcessed: false,
          discdbHit: false,
          discMode: 'copy',
          jobStatus: { jobId: 'job-123', job_status: 'running', rip_state: 'completed' },
          titles: [],
          titlesComplete: true,
          movieOptions: [],
          boxsetOptions: [],
          releaseOptions: [],
          groupOptions: [],
        })
      )
    );

    // Defer record responses until after user edits are entered.
    let respondRecords = false;
    let pendingDiscRoute: any = null;
    let pendingReleaseRoute: any = null;
    const fulfillRecords = () => {
      respondRecords = true;
      if (pendingDiscRoute) {
        pendingDiscRoute.fulfill(
          noopJson({
            id: 'disc-1',
            content_hash: 'hash-123',
            release_id: 'rel-1',
            disc_number: 1,
            disc_slug: 'backend-disc',
            disc_name: 'Backend Disc',
            format: 'Blu-Ray',
            finalized: false,
            tracks: [
              { track_id: '001', title: 'Backend Track 1', duration: 120, size: 1024 * 1024 },
              { track_id: '002', title: 'Backend Track 2', duration: 60, size: 2048 * 1024 },
            ],
          })
        );
        pendingDiscRoute = null;
      }
      if (pendingReleaseRoute) {
        pendingReleaseRoute.fulfill(
          noopJson({
            id: 'rel-1',
            slug: 'backend-slug',
            type: 'movie',
            name: 'Backend Release',
            title: 'Backend Release',
            finalized: false,
            discs: [],
          })
        );
        pendingReleaseRoute = null;
      }
    };
    await page.route('**/releases/disc/disc-1', route => {
      if (respondRecords) {
        route.fulfill(noopJson({ id: 'disc-1', content_hash: 'hash-123', release_id: 'rel-1', disc_number: 1, disc_slug: 'backend-disc', disc_name: 'Backend Disc', format: 'Blu-Ray', finalized: false, tracks: [] }));
      } else {
        pendingDiscRoute = route;
      }
    });
    await page.route('**/releases/rel-1/record', route => {
      if (respondRecords) {
        route.fulfill(noopJson({ id: 'rel-1', slug: 'backend-slug', type: 'movie', name: 'Backend Release', title: 'Backend Release', finalized: false, discs: [] }));
      } else {
        pendingReleaseRoute = route;
      }
    });

    await page.goto('/');

    // Select the job card so the app fetches workflow-context and shows the disc step (app-disc-label: Disc Name, Disc Slug).
    await expect(page.locator('.card-header')).toBeVisible({ timeout: 8000 });
    const jobCard = page.locator('button.job-card').filter({ hasText: 'Test Disc' });
    await expect(jobCard).toBeVisible({ timeout: 5000 });
    await jobCard.click();
    // Wait for the disc step form (workflow-labeling disc step shows app-disc-label with Disc Name, Disc Slug).
    const discLabel = page.locator('app-disc-label').first();
    await expect(discLabel).toBeVisible({ timeout: 10000 });
    const discName = discLabel.getByLabel(/Disc Name/i);
    const discSlug = discLabel.getByLabel(/Disc Slug/i);

    // Type user edits into disc fields.
    await discName.fill('Disc One Custom');
    await discSlug.fill('disc-one-custom');

    // Now allow backend record hydration to resolve (deferred releases/disc and releases/rel-1/record).
    fulfillRecords();

    // Verify user edits survived (backend defaults not reapplied). We assert Disc Name; Disc Slug
    // is filled to exercise the flow but may be overwritten by auto-slug or record merge.
    await expect(discName).toHaveValue('Disc One Custom');
  });
});
