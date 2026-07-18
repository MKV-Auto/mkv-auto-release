/**
 * Library — delete.
 *
 * Click Delete on a release card, accept the confirm() prompt, confirm
 * DELETE `/api/releases/{id}` fires and the card disappears.
 */
import { test, expect } from '@playwright/test';
import {
  installBaseMocks,
  gotoLibrary,
  makeRelease,
  makeDisc,
  noopJson,
} from './library-helpers';

test.describe('Library — delete release', () => {
  test('Delete → confirm → DELETE /releases/{id} → card disappears', async ({ page }) => {
    // Release.finalize_state must NOT be completed/finalized, else Delete
    // is disabled.
    const release = makeRelease({ finalize_state: 'pending' });
    const disc = makeDisc({ release_id: release.id });

    await installBaseMocks(page, {
      libraryPage: {
        items: [release],
        release_discs: { [release.id]: [disc] },
        boxsets: [],
        boxset_details: [],
        next_cursor: null,
        has_more: false,
      },
    });

    // DELETE mock — registered after installBaseMocks so it wins LIFO.
    await page.route(`**/api/releases/${encodeURIComponent(release.id)}`, async (route) => {
      if (route.request().method() === 'DELETE') {
        await route.fulfill(noopJson({}));
        return;
      }
      await route.fallback();
    });

    // Accept the window.confirm() that the delete button triggers.
    page.on('dialog', (dialog) => dialog.accept());

    await gotoLibrary(page);

    // Sanity: card present before delete.
    await expect(page.getByText('Test Movie (2024)')).toBeVisible();

    const reqP = page.waitForRequest(
      (r) => r.method() === 'DELETE' && r.url().includes(`/releases/${release.id}`),
      { timeout: 5_000 },
    );
    await page.getByRole('button', { name: 'Delete release' }).click();
    await reqP;

    // Card gone.
    await expect(page.getByText('Test Movie (2024)')).toHaveCount(0);
  });
});
