import { TestBed } from '@angular/core/testing';
import { ActivatedRouteSnapshot, UrlTree, convertToParamMap } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';
import { of, throwError } from 'rxjs';
import { setupPageGuard } from './setup-page.guard';
import { SystemService } from '../services/system.service';

function routeWithForce(force: boolean): ActivatedRouteSnapshot {
  return { queryParamMap: convertToParamMap(force ? { force: '1' } : {}) } as ActivatedRouteSnapshot;
}

describe('setupPageGuard', () => {
  let systemSvc: jasmine.SpyObj<Pick<SystemService, 'getSetupStatus' | 'getDevMode'>>;

  beforeEach(() => {
    systemSvc = jasmine.createSpyObj('SystemService', ['getSetupStatus', 'getDevMode']);
    TestBed.configureTestingModule({
      providers: [{ provide: SystemService, useValue: systemSvc }],
      imports: [
        RouterTestingModule.withRoutes([
          { path: 'setup', component: {} as any },
          { path: 'activity', component: {} as any },
        ]),
      ],
    });
  });

  it('redirects to /activity when setup is already complete', (done) => {
    systemSvc.getSetupStatus.and.returnValue(of({ first_time_setup_complete: true, setup_step: 6 }));
    TestBed.runInInjectionContext(() => {
      const result = setupPageGuard(routeWithForce(false), null!);
      (result as any).subscribe((v: boolean | UrlTree) => {
        expect(v instanceof UrlTree).toBe(true);
        expect((v as UrlTree).toString()).toBe('/activity');
        done();
      });
    });
  });

  it('allows /setup when first_time_setup_complete is false', (done) => {
    systemSvc.getSetupStatus.and.returnValue(of({ first_time_setup_complete: false, setup_step: 1 }));
    TestBed.runInInjectionContext(() => {
      const result = setupPageGuard(routeWithForce(false), null!);
      (result as any).subscribe((v: boolean | UrlTree) => {
        expect(v).toBe(true);
        done();
      });
    });
  });

  it('allows /setup when getSetupStatus errors', (done) => {
    systemSvc.getSetupStatus.and.returnValue(throwError(() => new Error('fail')));
    TestBed.runInInjectionContext(() => {
      const result = setupPageGuard(routeWithForce(false), null!);
      (result as any).subscribe((v: boolean | UrlTree) => {
        expect(v).toBe(true);
        done();
      });
    });
  });

  it('allows /setup when force=1 and dev mode is enabled', (done) => {
    systemSvc.getSetupStatus.and.returnValue(of({ first_time_setup_complete: true, setup_step: 6 }));
    systemSvc.getDevMode.and.returnValue(of({ enabled: true } as any));
    TestBed.runInInjectionContext(() => {
      const result = setupPageGuard(routeWithForce(true), null!);
      (result as any).subscribe((v: boolean | UrlTree) => {
        expect(v).toBe(true);
        done();
      });
    });
  });

  it('still redirects when force=1 but dev mode is disabled', (done) => {
    systemSvc.getSetupStatus.and.returnValue(of({ first_time_setup_complete: true, setup_step: 6 }));
    systemSvc.getDevMode.and.returnValue(of({ enabled: false } as any));
    TestBed.runInInjectionContext(() => {
      const result = setupPageGuard(routeWithForce(true), null!);
      (result as any).subscribe((v: boolean | UrlTree) => {
        expect(v instanceof UrlTree).toBe(true);
        expect((v as UrlTree).toString()).toBe('/activity');
        done();
      });
    });
  });
});
