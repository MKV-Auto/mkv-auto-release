import { BackendNotification } from '../services/workflow.service';
import { formatBackendNotificationToastText } from './backend-notification-display.util';

describe('formatBackendNotificationToastText', () => {
  it('uses title or message for normal levels', () => {
    const n = { message: 'body', kind: 'info' as const, level: 'rip_complete' } as BackendNotification;
    expect(formatBackendNotificationToastText(n)).toBe('body');
    expect(
      formatBackendNotificationToastText({ ...n, title: 'T' } as BackendNotification),
    ).toBe('T');
  });

  it('combines title and message for error_disc_read', () => {
    const n = {
      message: 'read failed',
      kind: 'error' as const,
      level: 'error_disc_read',
      title: 'Disc',
    } as BackendNotification;
    expect(formatBackendNotificationToastText(n)).toBe('Disc: read failed');
    expect(
      formatBackendNotificationToastText({
        message: 'read failed',
        kind: 'error',
        level: 'error_disc_read',
      } as BackendNotification),
    ).toBe('read failed');
  });
});
