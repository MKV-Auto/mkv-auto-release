/**
 * Library — transfer destination.
 *
 * Confirms the per-title file_path landed at the disc drawer's title-row
 * footer with the correct stage label. This is the #380 (extended)
 * surface — `transfer_destination` projected onto TitleSummary.
 */
import { test, expect } from '@playwright/test';
import {
  installBaseMocks,
  gotoLibrary,
  makeRelease,
  makeDisc,
  makeTitle,
} from './library-helpers';

test.describe('Library — transfer destination', () => {
  test('Drawer title row shows file_path landed at the transfer stage', async ({ page }) => {
    const release = makeRelease({ finalize_state: 'pending' });
    const disc = makeDisc({
      release_id: release.id,
      finalized: false,
      transfer_state: 'completed',
    });
    // file_path_stage='transfer' is the canonical landed-at-destination
    // state set by the post-#380 backend projection.
    const title = makeTitle({
      title_id: 't-1',
      title: 'Feature',
      file_path: '/library/Movies/Test Movie (2024)/Test Movie.1080p.mkv',
      file_path_stage: 'transfer',
    });

    await installBaseMocks(page, {
      libraryPage: {
        items: [release],
        release_discs: { [release.id]: [disc] },
        boxsets: [],
        boxset_details: [],
        next_cursor: null,
        has_more: false,
      },
      discRecord: {
        id: disc.id,
        content_hash: disc.content_hash,
        disc_number: disc.disc_number,
        disc_name: disc.disc_name,
        disc_slug: disc.disc_slug,
        format: disc.format,
        finalized: false,
        finalized_at: null,
        titles: [title],
      },
    });

    await gotoLibrary(page);

    await page.getByRole('button', { name: /Expand disc list/ }).click();
    await page.getByRole('button', { name: /Open Disc 1.*details/ }).click();

    // The footer shows the file_path (as <code>), plus a stage label that
    // is_transferred for stage=transfer.
    await expect(
      page.locator('code', { hasText: 'Test Movie.1080p.mkv' }),
    ).toBeVisible();

    // The stage label has `.is-transferred` when file_path_stage='transfer'.
    // (Class is `title-stage` since the #601 compact-card drawer redesign.)
    await expect(page.locator('.library-disc-drawer__title-stage.is-transferred')).toBeVisible();
  });
});
