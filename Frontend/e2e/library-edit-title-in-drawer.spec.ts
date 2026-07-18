/**
 * Library — edit-title-in-drawer.
 *
 * Open the drawer, edit a title's title text on a series-typed release
 * (so the season/episode fields aren't conditional), wait past the 300ms
 * debounce, confirm PATCH `/api/discs/{id}/titles` fires.
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

test.describe('Library — edit title in drawer', () => {
  test('Drawer → change title → 300ms debounce → PATCH /discs/{id}/titles', async ({ page }) => {
    const release = makeRelease({
      type: 'series',
      finalize_state: 'pending',
      name: 'Wednesday',
    });
    const disc = makeDisc({
      release_id: release.id,
      finalized: false,
      transfer_state: 'completed',
    });
    const title = makeTitle({ title_id: 't-1', title: 'Episode 1', season: 1, episode: 1 });

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

    // PATCH mock for title.
    await page.route(`**/api/discs/${encodeURIComponent(disc.id)}/titles`, async (route) => {
      if (route.request().method() === 'PATCH') {
        const body = JSON.parse(route.request().postData() || '{}');
        await route.fulfill(
          noopJson({
            result: { title_id: body.title_id, success: true, conflict: false },
            titles_version: 2,
          }),
        );
        return;
      }
      await route.fallback();
    });

    await gotoLibrary(page);

    await page.getByRole('button', { name: /Expand disc list/ }).click();
    await page.getByRole('button', { name: /Open Disc 1.*details/ }).click();

    // Since the #601 compact-card redesign, title rows default to a
    // display-only view; the Title input only exists after clicking Edit.
    await page.getByRole('button', { name: 'Edit this title' }).first().click();

    // The drawer's title row's "Title" text input (placeholder "Title…").
    const titleInput = page.getByPlaceholder(/^Title/).first();
    await expect(titleInput).toBeVisible();

    const reqP = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().endsWith(`/discs/${disc.id}/titles`),
      { timeout: 5_000 },
    );
    // ngModelChange fires per keystroke; the drawer's 300ms debounce
    // flushes one PATCH for the full edit.
    await titleInput.fill('Wednesday — Pilot');
    const req = await reqP;

    const body = JSON.parse(req.postData() || '{}');
    expect(body.title_id).toBe('t-1');
    expect(body.title).toBe('Wednesday — Pilot');
  });
});
