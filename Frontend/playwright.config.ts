import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env['E2E_BASE_URL'] || 'http://localhost:4200';
const useExisting = !!process.env['E2E_USE_EXISTING'];
// Full-stack specs share a single backend with one mock drive; parallel rip
// requests fight over the disc lock and one rip always fails with the other
// still holding the lock. Serialize when E2E_FULL is set. Mocked specs (which
// route every backend call via page.route) stay parallel for speed.
const fullStack = !!process.env['E2E_FULL'];

// Cross-browser coverage policy (see docs/SUPPORT_MATRIX.md):
//   * Chromium runs the full e2e suite — it's our canonical target.
//   * Firefox / WebKit run only smoke.spec.ts (app-shell render). Full-stack
//     specs depend on backend wiring that is browser-agnostic; running them
//     in three engines would triple CI time for no real signal. Bugs in
//     Firefox/WebKit are almost always rendering or layout, which the smoke
//     spec catches.
// Opt-in via PW_CROSS_BROWSER=1. Default runs only chromium.
const crossBrowser = !!process.env['PW_CROSS_BROWSER'];

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  ...(fullStack ? { workers: 1 } : {}),
  use: {
    baseURL,
    headless: true,
  },
  projects: crossBrowser
    ? [
        { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
        {
          name: 'firefox',
          use: { ...devices['Desktop Firefox'] },
          testMatch: /smoke\.spec\.ts$/,
        },
        {
          name: 'webkit',
          use: { ...devices['Desktop Safari'] },
          testMatch: /smoke\.spec\.ts$/,
        },
      ]
    : [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  ...(useExisting
    ? {}
    : {
        webServer: {
          command: 'npm run build && npx http-server dist/disc-ripper-ui/browser -p 4200',
          port: 4200,
          reuseExistingServer: !process.env['CI'],
          timeout: 120_000,
        },
      }),
});
