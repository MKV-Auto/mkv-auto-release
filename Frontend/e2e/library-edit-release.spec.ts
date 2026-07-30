/**
 * Library — edit-release.
 *
 * Open a release card, click Edit, change the year, click Save, confirm
 * PATCH `/api/releases/{idOrSlug}` fires with the bumped year and that
 * the card reflects the new year without a reload.
 */
import { test, expect } from '@playwright/test';
import {
  installBaseMocks,
  gotoLibrary,
  makeRelease,
  makeDisc,
  noopJson,
} from './library-helpers';

test.describe('Library — edit release', () => {
  test('Edit → change year → Save fires PATCH /releases/{id}', async ({ page }) => {
    // Release.finalize_state must NOT be completed/finalized, else the Edit
    // button is disabled. The disc stays finalized so the LibraryPage filter
    // still surfaces the card.
    const release = makeRelease({ finalize_state: 'pending', production_year: 2020 });
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

    // PATCH mock — registered after installBaseMocks so the LIFO handler
    // wins. Echo back the release with the bumped year.
    await page.route(`**/api/releases/${encodeURIComponent(release.id)}`, async (route) => {
      if (route.request().method() === 'PATCH') {
        const body = JSON.parse(route.request().postData() || '{}');
        await route.fulfill(
          noopJson({ ...release, production_year: body.release_year ?? release.production_year }),
        );
        return;
      }
      await route.fallback();
    });

    await gotoLibrary(page);

    // Edit/Delete live behind the card's kebab menu since the contribution
    // surface landed (#756). Scope to the card: the library shell can render
    // more than one card, and every kebab shares the same accessible name.
    const card = page.locator('app-library-release-card', { hasText: 'Test Movie' });
    await card.getByRole('button', { name: 'Release actions' }).click();
    // The entries carry role="menuitem", which overrides the implicit
    // button role — getByRole('button') never matches them.
    await card.getByRole('menuitem', { name: 'Edit release' }).click();

    const yearInput = page.getByRole('spinbutton', { name: 'Year' });
    await yearInput.fill('2025');

    const reqP = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().includes(`/releases/${release.id}`),
      { timeout: 5_000 },
    );
    await page.getByRole('button', { name: /^Save/ }).click();
    const req = await reqP;

    const body = JSON.parse(req.postData() || '{}');
    expect(body.release_year).toBe(2025);

    await expect(page.getByText('Test Movie (2025)')).toBeVisible();
  });
});
