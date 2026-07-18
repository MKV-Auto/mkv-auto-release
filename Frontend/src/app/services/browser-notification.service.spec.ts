import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { Subject, of } from 'rxjs';
import { take } from 'rxjs/operators';
import { BrowserNotificationService } from './browser-notification.service';
import { WorkflowService, BackendNotification } from './workflow.service';
import { SetupModalService } from './setup-modal.service';
import { LoggerService } from './logger.service';

const BANNER_DISMISS_KEY = 'mkvauto_os_notif_banner_dismissed';

describe('BrowserNotificationService', () => {
  let notifications$: Subject<BackendNotification>;
  let setupOpenSpy: jasmine.Spy;
  let navigateSpy: jasmine.Spy;
  let selectDriveSpy: jasmine.Spy;
  let setContextByCardSpy: jasmine.Spy;
  let originalNotification: typeof Notification | undefined;
  let ctorSpy: jasmine.Spy;

  function installNotificationMock(permission: NotificationPermission): void {
    originalNotification = globalThis.Notification;
    ctorSpy = jasmine.createSpy('NotificationCtor').and.returnValue({
      close: jasmine.createSpy('close'),
      onclick: null as unknown as (this: Notification, ev: Event) => unknown,
    });
    const Mock = function (this: unknown, title: string, options?: NotificationOptions) {
      return ctorSpy(title, options);
    } as unknown as typeof Notification;
    const MockCtor = Mock as unknown as {
      permission: NotificationPermission;
      requestPermission: () => Promise<NotificationPermission>;
    };
    MockCtor.permission = permission;
    MockCtor.requestPermission = jasmine
      .createSpy('requestPermission')
      .and.returnValue(Promise.resolve(permission));
    globalThis.Notification = Mock;
  }

  function restoreNotification(): void {
    if (originalNotification !== undefined) {
      globalThis.Notification = originalNotification;
    }
  }

  beforeEach(() => {
    notifications$ = new Subject<BackendNotification>();
    sessionStorage.removeItem(BANNER_DISMISS_KEY);
    navigateSpy = jasmine.createSpy('navigate').and.returnValue(Promise.resolve(true));
    selectDriveSpy = jasmine.createSpy('selectDrive');
    setContextByCardSpy = jasmine.createSpy('setContextByCard').and.returnValue(of(undefined));
    TestBed.configureTestingModule({
      providers: [
        BrowserNotificationService,
        {
          provide: WorkflowService,
          useValue: {
            notifications$: notifications$.asObservable(),
            selectDrive: selectDriveSpy,
            setContextByCard: setContextByCardSpy,
          },
        },
        { provide: Router, useValue: { navigate: navigateSpy } },
        { provide: LoggerService, useValue: { debug: () => {}, warn: () => {} } },
      ],
    });
    setupOpenSpy = spyOn(TestBed.inject(SetupModalService), 'open');
  });

  afterEach(() => {
    sessionStorage.removeItem(BANNER_DISMISS_KEY);
    restoreNotification();
  });

  it('osNotifPromptVisible when permission default and banner not dismissed', (done) => {
    installNotificationMock('default');
    const svc = TestBed.inject(BrowserNotificationService);
    svc.osNotifPromptVisible$.pipe(take(1)).subscribe((v) => {
      expect(v).toBe(true);
      done();
    });
  });

  it('osNotifPromptVisible false when banner dismissed in session', (done) => {
    sessionStorage.setItem(BANNER_DISMISS_KEY, '1');
    installNotificationMock('default');
    const svc = TestBed.inject(BrowserNotificationService);
    svc.osNotifPromptVisible$.pipe(take(1)).subscribe((v) => {
      expect(v).toBe(false);
      done();
    });
  });

  it('approveOsNotifPrompt calls requestPermission and refreshes visibility', async () => {
    installNotificationMock('default');
    const svc = TestBed.inject(BrowserNotificationService);
    await svc.approveOsNotifPrompt();
    expect(globalThis.Notification.requestPermission).toHaveBeenCalled();
  });

  it('dismissOsNotifPrompt sets session and hides prompt', (done) => {
    installNotificationMock('default');
    const svc = TestBed.inject(BrowserNotificationService);
    svc.dismissOsNotifPrompt();
    expect(sessionStorage.getItem(BANNER_DISMISS_KEY)).toBe('1');
    svc.osNotifPromptVisible$.pipe(take(1)).subscribe((v) => {
      expect(v).toBe(false);
      done();
    });
  });

  it('shows OS notification when tab hidden and permission granted', () => {
    installNotificationMock('granted');
    spyOnProperty(document, 'visibilityState').and.returnValue('hidden');
    TestBed.inject(BrowserNotificationService);
    notifications$.next({
      message: 'Done',
      kind: 'success',
      level: 'rip_complete',
      title: 'Rip',
      id: 'job-1:rip_complete',
    } as BackendNotification);
    expect(ctorSpy).toHaveBeenCalledWith('Rip', jasmine.objectContaining({ body: 'Done', tag: 'job-1:rip_complete' }));
  });

  it('does not show OS notification when tab visible', () => {
    installNotificationMock('granted');
    spyOnProperty(document, 'visibilityState').and.returnValue('visible');
    TestBed.inject(BrowserNotificationService);
    notifications$.next({
      message: 'Done',
      kind: 'success',
      level: 'rip_complete',
      title: 'Rip',
    } as BackendNotification);
    expect(ctorSpy).not.toHaveBeenCalled();
  });

  it('onclick focuses and opens transfer setup when action_type set', () => {
    installNotificationMock('granted');
    spyOnProperty(document, 'visibilityState').and.returnValue('hidden');
    const focusSpy = spyOn(window, 'focus').and.stub();
    ctorSpy.and.callFake((_title: string, _opts?: NotificationOptions) => ({
      close: jasmine.createSpy('close'),
      onclick: null as unknown as (this: Notification, ev: Event) => unknown,
    }));
    TestBed.inject(BrowserNotificationService);
    notifications$.next({
      message: 'Configure',
      kind: 'warning',
      level: 'no_transfer_destination',
      title: 'Transfer',
      action_type: 'open_transfer_setup',
    } as BackendNotification);
    expect(ctorSpy).toHaveBeenCalled();
    const inst = ctorSpy.calls.mostRecent().returnValue as {
      onclick: ((this: Notification, ev: Event) => unknown) | null;
    };
    expect(inst.onclick).toEqual(jasmine.any(Function));
    inst.onclick!.call({} as Notification, new Event('click'));
    expect(focusSpy).toHaveBeenCalled();
    expect(setupOpenSpy).toHaveBeenCalledWith({ targetStep: 2 });
  });

  it('onclick selects drive navigates to ripper and loads context when open_ripper_drive', async () => {
    installNotificationMock('granted');
    spyOnProperty(document, 'visibilityState').and.returnValue('hidden');
    const focusSpy = spyOn(window, 'focus').and.stub();
    ctorSpy.and.callFake((_title: string, _opts?: NotificationOptions) => ({
      close: jasmine.createSpy('close'),
      onclick: null as unknown as (this: Notification, ev: Event) => unknown,
    }));
    TestBed.inject(BrowserNotificationService);
    notifications$.next({
      message: 'Disc scan finished — open Ripper to start copying.',
      kind: 'success',
      level: 'scan_completed',
      title: 'Scan complete',
      action_type: 'open_ripper_drive',
      action_payload: { mount_point: '/dev/sr0' },
    } as BackendNotification);
    expect(ctorSpy).toHaveBeenCalled();
    const inst = ctorSpy.calls.mostRecent().returnValue as {
      onclick: ((this: Notification, ev: Event) => unknown) | null;
    };
    inst.onclick!.call({} as Notification, new Event('click'));
    expect(focusSpy).toHaveBeenCalled();
    expect(selectDriveSpy).toHaveBeenCalledWith('/dev/sr0');
    expect(navigateSpy).toHaveBeenCalledWith(['/activity']);
    await navigateSpy.calls.mostRecent().returnValue;
    expect(setContextByCardSpy).toHaveBeenCalledWith({ type: 'drive', id: '/dev/sr0' });
  });
});
