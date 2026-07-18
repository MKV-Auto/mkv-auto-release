import { test, expect } from '@playwright/test';

const noopJson = (data: unknown = {}) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(data),
});

test.describe('Label duplicate metadata UI', () => {
  test('duplicate group card shows variant rows with metadata tag lists (mocked)', async ({ page }) => {
    test.skip(!!process.env['E2E_FULL'], 'Uses API mocks; run without E2E_FULL (e.g. npm run e2e)');

    await page.route('**/jobs/current', (route) =>
      route.fulfill(
        noopJson({
          jobId: 'job-dup',
          createdAt: new Date().toISOString(),
          disc: {
            disc_num: '1',
            mount_point: '/mnt/d1',
            movie_name: 'Dup Disc',
            tracks: {},
            disc_hash: 'hash-dup',
          },
          status: 'running',
          progress: 80,
          disc_id: 'disc-dup',
          release_id: 'rel-dup',
          disc_payload: {},
        })
      )
    );
    await page.route('**/discs/current', (route) => route.fulfill(noopJson({ disc: null, job: null })));
    await page.route('**/events/job/**', (route) => route.abort());
    await page.route('**/events/previews/**', (route) => route.abort());
    await page.route('**/settings/**', (route) => route.fulfill(noopJson({})));
    await page.route('**/drives**', (route) => route.fulfill(noopJson([])));
    await page.route(
      (url) => url.href.includes('system/makemkv/registration'),
      (route) => route.fulfill(noopJson({ expired: false, currentKey: null }))
    );
    await page.route(
      (url) => url.href.includes('api/coordinator/initial-state'),
      (route) =>
        route.fulfill(
          noopJson({
            type: 'initial_state',
            discs: [
              {
                disc_id: 'disc-dup',
                disc_state: 'unfinished',
                job_id: 'job-dup',
                movie_name: 'Dup Disc',
                info_title: 'Dup Disc',
                mount_point: null,
                disc_num: null,
              },
            ],
          })
        )
    );

    const dupGroupId = 'disc:disc-dup:abc';
    const titleA = {
      title_id: 'ta',
      disc_id: 'disc-dup',
      source_file: '00800.mpls',
      segment_map: '59',
      title: 'Feature',
      type: 'MainMovie',
      active: true,
      order_index: 0,
      duration: 7200,
      size: 5_000_000_000,
      streams: [{ type: 'Video', resolution: '1920 x 1080' }],
      metadata_scan: {
        chapters_count: 20,
        subtitle_summary: [{ language: 'eng' }],
        audio_summary: [{ codec_name: 'ac3', channels: 6, channel_layout: '5.1(side)' }],
        format: { bit_rate: 25_000_000, duration: 7200 },
        video_hints: { width: 1920, height: 1080 },
      },
      duplicate_info: {
        group_id: dupGroupId,
        group_size: 2,
        same_as: ['tb'],
        tags: ['quality:1080p'],
        diff_tags: ['chapters:more'],
        metrics: {
          chapters_count: 20,
          subtitle_track_count: 1,
          subtitle_language_count: 1,
          audio_score: 2,
          audio_language_count: 1,
          video_bitrate: 25_000_000,
          video_pixels: 1920 * 1080,
          scan_usable: true,
        },
        confidence: 'high',
      },
    };
    const titleB = {
      title_id: 'tb',
      disc_id: 'disc-dup',
      source_file: '00801.mpls',
      segment_map: '59',
      title: 'Feature',
      type: 'ignore',
      active: false,
      order_index: 1,
      duration: 7200,
      size: 4_900_000_000,
      streams: [{ type: 'Video', resolution: '1920 x 1080' }],
      metadata_scan: {
        chapters_count: 10,
        subtitle_summary: [{ language: 'eng' }],
        audio_summary: [{ codec_name: 'ac3', channels: 6, channel_layout: '5.1(side)' }],
        format: { bit_rate: 24_000_000, duration: 7200 },
        video_hints: { width: 1920, height: 1080 },
      },
      duplicate_info: {
        group_id: dupGroupId,
        group_size: 2,
        same_as: ['ta'],
        tags: ['quality:1080p'],
        diff_tags: [],
        metrics: {
          chapters_count: 10,
          subtitle_track_count: 1,
          subtitle_language_count: 1,
          audio_score: 2,
          audio_language_count: 1,
          video_bitrate: 24_000_000,
          video_pixels: 1920 * 1080,
          scan_usable: true,
        },
        confidence: 'high',
      },
    };

    await page.route(
      (url) => url.href.includes('jobs/job-dup/workflow-context'),
      (route) =>
        route.fulfill(
          noopJson({
            type: 'job',
            id: 'job-dup',
            workflowStep: 'titles',
          labelForm: {
            release_name: 'Dup Release',
            release_slug: 'dup-release',
            release_id: 'rel-dup',
            release_year: '2020',
            movie_id: 'm1',
            disc_name: 'Dup Disc',
            disc_slug: 'dup-disc',
            disc_number: 1,
            disc_format: 'Blu-Ray',
            group_type: 'movie',
          },
            discInfo: { disc_id: 'disc-dup' },
            labelDraftProcessed: true,
            discdbHit: false,
            discMode: 'copy',
            jobStatus: {
              jobId: 'job-dup',
              job_status: 'running',
              rip_state: 'completed',
              label_state: 'pending',
            },
            titles: [titleA, titleB],
            titlesComplete: true,
            movieOptions: [],
            boxsetOptions: [],
            releaseOptions: [],
            groupOptions: [],
          })
        )
    );

    await page.goto('/');
    await expect(page.locator('.card-header')).toBeVisible({ timeout: 8000 });
    await page.locator('button.job-card').filter({ hasText: 'Dup Disc' }).click();

    await expect(page.locator('.duplicate-group-card')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('.duplicate-group-card-label').first()).toContainText(/Duplicate Group/i);
    await expect(page.locator('.variant-source-file').filter({ hasText: '00800.mpls' })).toBeVisible();
    await expect(page.locator('.variant-source-file').filter({ hasText: '00801.mpls' })).toBeVisible();
    await expect(page.locator('app-metadata-tag-list .metadata-tag-badge').first()).toBeVisible();
  });
});
