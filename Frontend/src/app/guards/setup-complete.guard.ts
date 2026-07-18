import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map, catchError, retry } from 'rxjs/operators';
import { of, timer } from 'rxjs';
import { SystemService } from '../services/system.service';

/**
 * Guard that blocks access to protected routes until first-time setup is complete.
 * If setup is not complete, redirects to /setup and returns false.
 *
 * Transient API failures (e.g. backend still starting after container restart) must not
 * send users to the setup wizard; we retry briefly then allow access so an existing
 * install is not mistaken for a fresh setup.
 */
export const setupCompleteGuard: CanActivateFn = () => {
  const systemSvc = inject(SystemService);
  const router = inject(Router);

  return systemSvc.getSetupStatus().pipe(
    retry({ count: 3, delay: () => timer(400) }),
    map((status) => {
      if (status.first_time_setup_complete) {
        return true;
      }
      return router.createUrlTree(['/setup']);
    }),
    catchError(() => of(true)),
  );
};
