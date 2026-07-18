import { inject } from '@angular/core';
import { ActivatedRouteSnapshot, CanActivateFn, Router } from '@angular/router';
import { catchError, map, retry, switchMap } from 'rxjs/operators';
import { of, timer } from 'rxjs';
import { SystemService } from '../services/system.service';

/**
 * On /setup: if first-time setup is already done, send the user to the ripper.
 * Avoids showing the wizard after restarts or when /setup was opened manually.
 *
 * Dev-mode escape hatch: `/setup?force=1` skips the redirect when dev mode is
 * on (ENABLE_DEVMODE=1), so the wizard can be previewed without flipping the
 * persisted setup-complete flag. In production builds the dev-mode endpoint
 * returns enabled=false, so the bypass is a no-op.
 */
export const setupPageGuard: CanActivateFn = (route: ActivatedRouteSnapshot) => {
  const systemSvc = inject(SystemService);
  const router = inject(Router);
  const force = route.queryParamMap.get('force') === '1';

  return systemSvc.getSetupStatus().pipe(
    retry({ count: 3, delay: () => timer(400) }),
    switchMap((status) => {
      if (!status.first_time_setup_complete) return of(true as const);
      if (!force) return of(router.createUrlTree(['/activity']));
      return systemSvc.getDevMode().pipe(
        map(dev => dev?.enabled ? true as const : router.createUrlTree(['/activity'])),
        catchError(() => of(router.createUrlTree(['/activity']))),
      );
    }),
    catchError(() => of(true)),
  );
};
