/**
 * Library — edit-disc.
 *
 * Open the drawer for a disc, change the disc name, blur the input,
 * confirm PATCH `/api/releases/disc/{id}` fires with the new name.
 */
import { test, expect } from '@playwright/test';
import {
  installBaseMocks,
  gotoLibrary,
  makeRelease,
  makeDisc,
  makeTitle,
  noopJson,
} from './library-helpers';

test.describe('Library — edit disc', () => {
  test('Drawer → change disc name → blur fires PATCH /releases/disc/{id}', async ({ page }) => {
    // Disc must be visible (transfer_state=completed satisfies the
    // LibraryPage filter) but NOT finalized — else the disc-name input is
    // disabled.
    const release = makeRelease({ finalize_state: 'pending' });
    const disc = makeDisc({
      release_id: release.id,
      finalized: false,
      transfer_state: 'completed',
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
        titles: [makeTitle()],
      },
    });

    // PATCH mock — registered after installBaseMocks so it wins LIFO.
    await page.route(
      `**/api/releases/disc/${encodeURIComponent(disc.id)}`,
      async (route) => {
        const method = route.request().method();
        if (method === 'PATCH') {
          const body = JSON.parse(route.request().postData() || '{}');
          await route.fulfill(
            noopJson({
              id: disc.id,
              content_hash: disc.content_hash,
              disc_number: disc.disc_number,
              disc_name: body.disc_name ?? disc.disc_name,
              disc_slug: disc.disc_slug,
              format: disc.format,
              finalized: false,
              finalized_at: null,
              titles: [makeTitle()],
            }),
          );
          return;
        }
        await route.fallback();
      },
    );

    await gotoLibrary(page);

    // Expand the release card to reveal disc rows, then open the disc.
    await page.getByRole('button', { name: /Expand disc list/ }).click();
    await page.getByRole('button', { name: /Open Disc 1.*details/ }).click();

    // Drawer's disc-name input.
    const nameInput = page.getByRole('textbox', { name: 'Disc name' });
    await expect(nameInput).toBeVisible();

    await nameInput.fill('Special Edition');

    const reqP = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().includes(`/releases/disc/${disc.id}`),
      { timeout: 5_000 },
    );
    await nameInput.blur();
    const req = await reqP;

    const body = JSON.parse(req.postData() || '{}');
    expect(body.disc_name).toBe('Special Edition');
  });
});
