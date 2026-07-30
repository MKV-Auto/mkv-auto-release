/**
 * Shared helpers for the Library Playwright suite (#508 / #500 Phase 7).
 *
 * Every spec mocks the same handful of endpoints so we don't depend on
 * backend state. Per-spec route handlers can override these via
 * `page.route` higher in the test (Playwright respects later-registered
 * handlers first).
 */
import { Page, Route } from '@playwright/test';

export const noopJson = (data: unknown = {}) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(data),
});

/** Bare-minimum release the LibraryPage accepts as "completed" (has at
 * least one finalized OR transfer_state=completed disc). */
export function makeRelease(over: Partial<any> = {}) {
  return {
    id: 'rel-1',
    slug: 'rel-1',
    type: 'movie',
    name: 'Test Movie',
    production_year: 2024,
    cover_front_url: null,
    boxset_id: null,
    finalize_state: 'completed',
    ...over,
  };
}

export function makeDisc(over: Partial<any> = {}) {
  return {
    id: 'd-1',
    content_hash: 'h-1',
    release_id: 'rel-1',
    disc_number: 1,
    disc_name: 'Theatrical',
    disc_slug: 'theatrical',
    format: 'Blu-Ray',
    label_present: true,
    finalized: true,                 // ⇒ release is "completed" per the LibraryPage filter
    finalized_at: '2024-01-01',
    transfer_state: 'completed',
    titles_completed: 2,
    total_titles: 2,
    ...over,
  };
}

export function makeTitle(over: Partial<any> = {}) {
  return {
    title_id: 't-1',
    title: 'Feature',
    type: 'Main Movie',
    season: null,
    episode: null,
    edition: '',
    description: '',
    duration: 7200,
    size: 4_000_000_000,
    mkv_size: 3_900_000_000,
    file_path: '/library/Movies/Test Movie (2024)/Test Movie.1080p.mkv',
    file_path_stage: 'transfer' as const,
    title_seq: 5,
    active: true,
    ...over,
  };
}

/**
 * Install the baseline mocks every Library spec relies on. Spec-level
 * overrides on the same URL pattern take precedence (Playwright runs
 * later-registered handlers first).
 *
 * Optional `libraryPage` lets a spec swap the canonical fixture for
 * something exotic (multi-release, with boxset, no completed discs, etc.).
 */
export async function installBaseMocks(
  page: Page,
  opts: { libraryPage?: any; discRecord?: any } = {},
): Promise<void> {
  const release = makeRelease();
  const disc = makeDisc();
  const title = makeTitle();

  const libraryPage = opts.libraryPage ?? {
    items: [release],
    release_discs: { [release.id]: [disc] },
    boxsets: [],
    boxset_details: [],
    next_cursor: null,
    has_more: false,
  };
  const discRecord = opts.discRecord ?? {
    id: disc.id,
    content_hash: disc.content_hash,
    disc_number: disc.disc_number,
    disc_name: disc.disc_name,
    disc_slug: disc.disc_slug,
    format: disc.format,
    finalized: disc.finalized,
    finalized_at: disc.finalized_at,
    titles: [title],
  };

  // Background chatter that the shell makes on bootstrap. Playwright route
  // matching is LIFO — the *most recently registered* handler that matches a
  // URL wins. We register the broad catch-alls FIRST so the specific routes
  // that come after them win when both could match. (Prior version had this
  // inverted, which silently shadowed setup/status and transfer/configs and
  // bounced the page to /setup.)

  // ---- broad catch-alls (registered first → shadowed by specifics below) ----
  await page.route('**/api/system/**', (route) => route.fulfill(noopJson({})));
  await page.route('**/api/jobs/**', (route) => route.fulfill(noopJson([])));
  await page.route('**/api/drives**', (route) => route.fulfill(noopJson([])));
  await page.route('**/api/events/**', (route) => route.abort());
  // WebSocket: accept the handshake and go silent. Aborting it (the old
  // behavior) threw WorkflowService into a ~2s reconnect loop — every retry
  // churned the page and collapsed open card menus, making menu clicks a
  // race that slower CI runners reliably lost. (The old bare /ws|websocket/
  // regex also matched Vite's @angular_platform-BROWSER dep chunk under
  // `ng serve` and aborted the app bundle itself at boot.)
  await page.routeWebSocket(/\/(ws|websocket)([/?#]|$)/, () => {
    // Connected, never speaks — the app idles with no events.
  });

  // ---- specific endpoints (registered last → win over the catch-alls) ----
  // /readyz: blocks the APP_INITIALIZER. Without this the static
  // "Setting Up…" overlay never goes away. Match `**/readyz` (not
  // `**/api/readyz`): ReadinessService uses window.MKVAUTO_API_BASE, which
  // the e2e:full harness injects as http://127.0.0.1:<port> — so the real
  // URL is `.../readyz` with no `/api` segment. Broad match keeps these
  // specs self-contained regardless of the runtime API base.
  await page.route('**/readyz', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
  );
  // setup/status: setupCompleteGuard checks `first_time_setup_complete` —
  // anything but `true` redirects to /setup.
  await page.route('**/api/system/setup/status', (route) =>
    route.fulfill(noopJson({ first_time_setup_complete: true, setup_step: 6 })),
  );
  await page.route('**/api/system/devmode', (route) =>
    route.fulfill(noopJson({ devMode: false })),
  );
  // Shell-level consumers expect an array for these. The catch-all would
  // return `{}` which trips `.find is not a function` etc.
  //
  // The shell also calls `checkTransferDestination` on bootstrap — if no
  // active config has a transfer_dir it opens a Setup modal after 1.5s,
  // which would later intercept clicks on the Library card. Hand back a
  // valid active local config so the check returns early.
  await page.route('**/api/system/transfer/configs', (route) =>
    route.fulfill(
      noopJson([
        {
          id: 'cfg-1',
          name: 'Local library',
          mode: 'local',
          is_active: true,
          transfer_dir: '/library',
        },
      ]),
    ),
  );
  // Shell's checkMakeMKVHealth opens the Setup modal at MakeMKV step if
  // !valid || !can_rip. Give it a clean bill of health.
  await page.route('**/api/system/makemkv/health', (route) =>
    route.fulfill(noopJson({ valid: true, can_rip: true })),
  );
  await page.route('**/api/coordinator/initial-state', (route) =>
    route.fulfill(noopJson({ type: 'initial_state', discs: [] })),
  );

  // Library endpoints.
  await page.route('**/api/releases/library/page**', (route) =>
    route.fulfill(noopJson(libraryPage)),
  );
  // Per-disc record (drawer load).
  await page.route(`**/api/releases/disc/${encodeURIComponent(disc.id)}`, (route) =>
    route.fulfill(noopJson(discRecord)),
  );
}

/** Wait for the Library page to fully bootstrap. The library-page
 * component renders inside ShellComponent; we wait for the page-level
 * landmark so subsequent locator queries are stable. */
export async function gotoLibrary(
  page: Page,
  // Default to the harness/base URL (E2E_BASE_URL) exactly like
  // playwright.config.ts and smoke.spec.ts. The previous hardcoded
  // `http://localhost` (:80) was connection-refused under `e2e:full`,
  // which serves the frontend on :4200.
  base = process.env['E2E_BASE_URL'] || 'http://localhost:4200',
): Promise<void> {
  await page.goto(`${base}/library`, { waitUntil: 'domcontentloaded' });
  // app-library-page is the page-component selector; wait for it to
  // mount before the spec issues any locator queries.
  await page.waitForSelector('app-library-page', { timeout: 15_000 });
}

/** Capture a request to a URL pattern. Resolves when the request fires. */
export async function expectRequest(
  page: Page,
  predicate: (req: { url: () => string; method: () => string }) => boolean,
  timeoutMs = 5_000,
): Promise<{ url: string; method: string; postData: string | null }> {
  const req = await page.waitForRequest(
    (r) => predicate({ url: () => r.url(), method: () => r.method() }),
    { timeout: timeoutMs },
  );
  return { url: req.url(), method: req.method(), postData: req.postData() };
}
