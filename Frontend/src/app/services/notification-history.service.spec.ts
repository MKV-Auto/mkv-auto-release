import { TestBed } from '@angular/core/testing';
import { take } from 'rxjs/operators';
import { NotificationHistoryService } from './notification-history.service';
import { BackendNotification } from './workflow.service';

function notification(over: Partial<BackendNotification> = {}): BackendNotification {
  return {
    message: 'Drive is not responding',
    kind: 'error',
    level: 'error_drive_unresponsive',
    id: 'sys:error_drive_unresponsive:drive_unresponsive:/dev/sr0',
    timestamp: '2026-07-25T10:00:00+00:00',
    ...over,
  };
}

const STORAGE_KEY = 'mkvauto_notification_history';

describe('NotificationHistoryService', () => {
  let service: NotificationHistoryService;

  beforeEach(() => {
    // Deliberately no spyOn(localStorage, ...): other specs in a full run
    // install their own spies on the same instance methods and re-spying
    // throws "already been spied upon". Clearing the key is enough — every
    // assertion below reads the in-memory BehaviorSubject, so it does not
    // matter whether the write behind it is real or a no-op (CI's headless
    // Chrome no-ops localStorage writes).
    localStorage.removeItem(STORAGE_KEY);
    TestBed.configureTestingModule({});
    service = TestBed.inject(NotificationHistoryService);
  });

  afterEach(() => {
    localStorage.removeItem(STORAGE_KEY);
  });

  function stored(): BackendNotification[] {
    let out: BackendNotification[] = [];
    service.notifications$.pipe(take(1)).subscribe(ns => (out = ns));
    return out;
  }

  it('drops an identical payload delivered twice', () => {
    service.add(notification());
    service.add(notification());

    expect(stored().length).toBe(1);
  });

  it('keeps a re-occurrence of a stable system id with a newer timestamp', () => {
    // A drive faults, the user power-cycles it, and it faults again. The
    // backend id is stable by design (that is what lets Redis dedupe it), so
    // matching on id alone would silently swallow the second fault — the bell
    // would show nothing even though the drive is down again.
    service.add(notification({ timestamp: '2026-07-25T10:00:00+00:00' }));
    service.add(notification({ timestamp: '2026-07-25T14:30:00+00:00' }));

    const all = stored();
    expect(all.length).toBe(2);
    expect(all[0].timestamp).toBe('2026-07-25T14:30:00+00:00');
  });

  it('keeps notifications with different ids', () => {
    service.add(notification({ id: 'sys:error_drive_unresponsive:drive_unresponsive:/dev/sr0' }));
    service.add(notification({ id: 'sys:error_drive_unresponsive:drive_unresponsive:/dev/sr1' }));

    expect(stored().length).toBe(2);
  });

  it('keeps notifications that carry no id', () => {
    service.add(notification({ id: undefined }));
    service.add(notification({ id: undefined }));

    expect(stored().length).toBe(2);
  });

  it('marks every notification read', () => {
    service.add(notification({ id: 'a' }));
    service.add(notification({ id: 'b' }));
    service.markAllRead();

    let unread = -1;
    service.unreadCount$.pipe(take(1)).subscribe(c => (unread = c));
    expect(unread).toBe(0);
  });

  it('clears all notifications', () => {
    service.add(notification());
    service.clearAll();

    expect(stored().length).toBe(0);
  });
});
