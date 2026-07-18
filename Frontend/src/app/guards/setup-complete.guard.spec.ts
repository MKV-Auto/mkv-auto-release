import { TestBed } from '@angular/core/testing';
import { UrlTree } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';
import { of, throwError } from 'rxjs';
import { setupCompleteGuard } from './setup-complete.guard';
import { SystemService } from '../services/system.service';

describe('setupCompleteGuard', () => {
  let systemSvc: jasmine.SpyObj<Pick<SystemService, 'getSetupStatus'>>;

  beforeEach(() => {
    systemSvc = jasmine.createSpyObj('SystemService', ['getSetupStatus']);
    TestBed.configureTestingModule({
      providers: [
        { provide: SystemService, useValue: systemSvc },
      ],
      imports: [
        RouterTestingModule.withRoutes([
          { path: 'setup', component: {} as any },
          { path: 'ripper', component: {} as any },
        ]),
      ],
    });
  });

  it('returns true when first_time_setup_complete is true', (done) => {
    systemSvc.getSetupStatus.and.returnValue(of({ first_time_setup_complete: true, setup_step: 6 }));
    TestBed.runInInjectionContext(() => {
      const result = setupCompleteGuard(null!, null!);
      (result as any).subscribe((v: boolean | UrlTree) => {
        expect(v).toBe(true);
        done();
      });
    });
  });

  it('redirects to /setup when first_time_setup_complete is false', (done) => {
    systemSvc.getSetupStatus.and.returnValue(of({ first_time_setup_complete: false, setup_step: 1 }));
    TestBed.runInInjectionContext(() => {
      const result = setupCompleteGuard(null!, null!);
      (result as any).subscribe((v: boolean | UrlTree) => {
        expect(v instanceof UrlTree).toBe(true);
        expect((v as UrlTree).toString()).toBe('/setup');
        done();
      });
    });
  });

  it('allows access when getSetupStatus fails (transient API; avoid false setup redirect)', (done) => {
    systemSvc.getSetupStatus.and.returnValue(throwError(() => new Error('fail')));
    TestBed.runInInjectionContext(() => {
      const result = setupCompleteGuard(null!, null!);
      (result as any).subscribe((v: boolean | UrlTree) => {
        expect(v).toBe(true);
        done();
      });
    });
  });
});
