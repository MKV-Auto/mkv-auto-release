/**
 * Library — browse / search / tabs.
 *
 * Confirms the page mounts, the completed-rips filter renders the
 * fixture release, and the search input triggers a re-fetch with the
 * search query (Phase 2 wiring).
 */
import { test, expect } from '@playwright/test';
import {
  installBaseMocks,
  gotoLibrary,
  makeRelease,
  makeDisc,
  noopJson,
} from './library-helpers';

test.describe('Library — browse', () => {
  test('mounts with the canonical release rendered', async ({ page }) => {
    await installBaseMocks(page);
    await gotoLibrary(page);

    await expect(page.getByRole('heading', { name: 'Library' })).toBeVisible();
    await expect(page.getByText('Test Movie (2024)')).toBeVisible();
    await expect(page.getByText('1 disc')).toBeVisible();
  });

  test('filters out pending releases (the stuck cohort)', async ({ page }) => {
    const completed = makeRelease({ id: 'rel-done', name: 'Finished' });
    const pending = makeRelease({ id: 'rel-pending', name: 'Stuck in transfer' });
    await installBaseMocks(page, {
      libraryPage: {
        items: [completed, pending],
        release_discs: {
          [completed.id]: [makeDisc({ id: 'd-1', release_id: completed.id, finalized: true })],
          // Pending: no finalized + transfer_state still pending → filtered out.
          [pending.id]: [makeDisc({
            id: 'd-2', release_id: pending.id,
            finalized: false, transfer_state: 'pending',
          })],
        },
        boxsets: [],
        boxset_details: [],
        next_cursor: null,
        has_more: false,
      },
    });
    await gotoLibrary(page);
    await expect(page.getByText('Finished')).toBeVisible();
    await expect(page.getByText('Stuck in transfer')).toHaveCount(0);
  });

  test('search input refetches the page with the search param', async ({ page }) => {
    await installBaseMocks(page);
    await gotoLibrary(page);

    const search = page.getByPlaceholder('Search releases and boxsets…');
    await search.fill('Wednesday');

    // 300ms debounce on the search input + the refetch lands on the
    // same /api/releases/library/page route with `search=Wednesday`.
    const req = await page.waitForRequest(
      (r) => r.url().includes('/releases/library/page') && r.url().includes('search=Wednesday'),
      { timeout: 5_000 },
    );
    expect(req.method()).toBe('GET');
  });
});
