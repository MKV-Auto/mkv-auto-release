import { test, expect, request } from '@playwright/test';

const baseURL = process.env['E2E_BASE_URL'] || 'http://localhost:4200';

test.describe('Smoke', () => {
  test.beforeAll(async () => {
    const ctx = await request.newContext();
    try {
      const res = await ctx.get(baseURL);
      if (!res.ok()) {
        test.skip(true, `Base URL not reachable (${baseURL})`);
      }
    } catch {
      test.skip(true, `Base URL not reachable (${baseURL})`);
    } finally {
      await ctx.dispose();
    }
  });

  test('app shell renders', async ({ page }) => {
    const url = baseURL.endsWith('/') ? baseURL : baseURL + '/';
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    // Basic sanity: root element (allow time for Angular bootstrap)
    const title = page.locator('app-root');
    await expect(title).toBeVisible({ timeout: 15_000 });
  });
});
