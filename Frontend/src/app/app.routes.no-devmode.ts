import { Routes } from '@angular/router';
import { ShellComponent } from './layout/shell.component';
import { RipperPageComponent } from './pages/ripper/ripper-page.component';
import { LibraryPageComponent } from './pages/library/library-page.component';
import { SettingsPageComponent } from './pages/settings/settings-page.component';
import { DiscdbSearchComponent } from './pages/discdb-search/discdb-search.component';

/**
 * Routes used for production-no-devmode build. preview-test redirects to ripper
 * so the dev chunk is never referenced and not emitted.
 */
export const routes: Routes = [
  {
    path: '',
    component: ShellComponent,
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'activity' },
      // #618: 'Ripper' → 'Activity'. Component class + path stay internal.
      { path: 'activity', component: RipperPageComponent },
      { path: 'ripper', pathMatch: 'full', redirectTo: 'activity' },
      // #500 Phase 6: History page retired; /library is the only mount,
      // /history kept as a redirect alias for bookmark migration.
      { path: 'library', component: LibraryPageComponent },
      { path: 'history', pathMatch: 'full', redirectTo: 'library' },
      { path: 'settings', component: SettingsPageComponent },
      { path: 'search', component: DiscdbSearchComponent },
      { path: 'preview-test', redirectTo: 'activity', pathMatch: 'full' },
    ],
  },
  { path: '**', redirectTo: 'activity' },
];
