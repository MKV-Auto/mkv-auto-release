import { Routes } from '@angular/router';
import { ShellComponent } from './layout/shell.component';
import { RipperPageComponent } from './pages/ripper/ripper-page.component';
import { LibraryPageComponent } from './pages/library/library-page.component';
import { SettingsPageComponent } from './pages/settings/settings-page.component';
import { DiscdbSearchComponent } from './pages/discdb-search/discdb-search.component';
import { SetupPageComponent } from './pages/setup/setup-page.component';
import { setupCompleteGuard } from './guards/setup-complete.guard';
import { setupPageGuard } from './guards/setup-page.guard';

export const routes: Routes = [
  {
    path: '',
    component: ShellComponent,
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'activity' },
      { path: 'setup', component: SetupPageComponent, canActivate: [setupPageGuard] },
      // #618: 'Ripper' → 'Activity'. The RipperPageComponent class + the
      // pages/ripper/ folder stay (internal-only); only the URL slug + nav
      // label change. /ripper redirects so any in-flight bookmarks survive.
      { path: 'activity', component: RipperPageComponent, canActivate: [setupCompleteGuard] },
      { path: 'ripper', pathMatch: 'full', redirectTo: 'activity' },
      // #500 Phase 6: legacy History page retired. /library is the only
      // mount point; /history kept as a redirect alias so external
      // bookmarks survive the rename — eventually removed once the
      // bookmark cohort has migrated.
      { path: 'library', component: LibraryPageComponent, canActivate: [setupCompleteGuard] },
      { path: 'history', pathMatch: 'full', redirectTo: 'library' },
      { path: 'settings', component: SettingsPageComponent, canActivate: [setupCompleteGuard] },
      { path: 'search', component: DiscdbSearchComponent, canActivate: [setupCompleteGuard] },
      {
        path: 'preview-test',
        loadComponent: () =>
          import('./pages/preview-test/preview-test.component').then(
            (m) => m.PreviewTestComponent
          ),
        canActivate: [setupCompleteGuard],
      },
      {
        // UI design-system demo — every primitive + composite in one place
        // for visual diffing against the prototype screenshots. Lazy so it
        // doesn't ship in production navigation; reach it by URL only.
        path: 'preview-test/ui-kit',
        loadComponent: () =>
          import('./pages/preview-test/ui-kit/ui-kit.component').then(
            (m) => m.UiKitDemoComponent
          ),
        canActivate: [setupCompleteGuard],
      },
      {
        // Path A — segment-reorder UI for the user to put exploratory-rip
        // PlayItem previews in story order. The job's segment_reorder_state
        // drives the manifest + persistence; we navigate here from the
        // ripper page when a job advances to awaiting_segment_order.
        path: 'segment-reorder/:jobId',
        loadComponent: () =>
          import('./pages/segment-reorder/segment-reorder-page.component').then(
            (m) => m.SegmentReorderPageComponent
          ),
        canActivate: [setupCompleteGuard],
      },
    ],
  },
  { path: '**', redirectTo: 'activity' },
];
