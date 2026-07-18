import { fakeAsync, TestBed, tick } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { FrontendVersionService, RELOAD_PAGE } from './frontend-version.service';
import { SystemService } from './system.service';
import { ToastService } from './toast.service';

describe('FrontendVersionService', () => {
  let systemMock: jasmine.SpyObj<SystemService>;
  let toastMock: jasmine.SpyObj<ToastService>;
  let reloadSpy: jasmine.Spy;

  beforeEach(() => {
    systemMock = jasmine.createSpyObj('SystemService', ['getDevMode', 'getFrontendVersion']);
    toastMock = jasmine.createSpyObj('ToastService', ['show']);
    reloadSpy = jasmine.createSpy('reload');
    TestBed.configureTestingModule({
      providers: [
        FrontendVersionService,
        { provide: SystemService, useValue: systemMock },
        { provide: ToastService, useValue: toastMock },
        { provide: RELOAD_PAGE, useValue: reloadSpy },
      ],
    });
  });

  function setup(opts: { devMode: boolean; initialVersion: string }): FrontendVersionService {
    systemMock.getDevMode.and.returnValue(of({ enabled: opts.devMode } as any));
    systemMock.getFrontendVersion.and.returnValue(of({ version: opts.initialVersion }));
    return TestBed.inject(FrontendVersionService);
  }

  it('reloads the page when the hash changes in dev mode', fakeAsync(() => {
    const svc = setup({ devMode: true, initialVersion: 'aaaa1111' });
    svc.start();
    tick();  // resolve devMode + initial version fetches
    // Now flip the poll response and tick past the poll interval.
    systemMock.getFrontendVersion.and.returnValue(of({ version: 'bbbb2222' }));
    tick(30_001);
    expect(reloadSpy).toHaveBeenCalled();
    expect(toastMock.show).not.toHaveBeenCalled();
    svc.ngOnDestroy();
  }));

  it('shows a toast (no reload) when the hash changes in production', fakeAsync(() => {
    const svc = setup({ devMode: false, initialVersion: 'aaaa1111' });
    svc.start();
    tick();
    systemMock.getFrontendVersion.and.returnValue(of({ version: 'bbbb2222' }));
    tick(30_001);
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(toastMock.show).toHaveBeenCalledWith(
      jasmine.stringMatching(/new version/i),
      'info',
      jasmine.any(Number),
    );
    svc.ngOnDestroy();
  }));

  it('does nothing on the first poll if the hash is unchanged', fakeAsync(() => {
    const svc = setup({ devMode: true, initialVersion: 'aaaa1111' });
    svc.start();
    tick();
    systemMock.getFrontendVersion.and.returnValue(of({ version: 'aaaa1111' }));
    tick(30_001);
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(toastMock.show).not.toHaveBeenCalled();
    svc.ngOnDestroy();
  }));

  it('reloads only once even if multiple polls observe the new hash', fakeAsync(() => {
    const svc = setup({ devMode: true, initialVersion: 'aaaa1111' });
    svc.start();
    tick();
    systemMock.getFrontendVersion.and.returnValue(of({ version: 'bbbb2222' }));
    tick(30_001);
    tick(30_001);
    tick(30_001);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
    svc.ngOnDestroy();
  }));

  it('does not start polling when the baseline fetch fails', fakeAsync(() => {
    systemMock.getDevMode.and.returnValue(of({ enabled: true } as any));
    systemMock.getFrontendVersion.and.returnValue(throwError(() => new Error('network')));
    const svc = TestBed.inject(FrontendVersionService);
    svc.start();
    tick();
    expect(systemMock.getFrontendVersion).toHaveBeenCalledTimes(1);  // only the baseline call
    tick(60_001);  // would-be 2 polls
    expect(systemMock.getFrontendVersion).toHaveBeenCalledTimes(1);  // still 1
    svc.ngOnDestroy();
  }));

  it('does not poll when backend returns empty version (frontend not on disk)', fakeAsync(() => {
    const svc = setup({ devMode: true, initialVersion: '' });
    svc.start();
    tick();
    expect(systemMock.getFrontendVersion).toHaveBeenCalledTimes(1);
    tick(60_001);
    expect(systemMock.getFrontendVersion).toHaveBeenCalledTimes(1);
    svc.ngOnDestroy();
  }));
});
