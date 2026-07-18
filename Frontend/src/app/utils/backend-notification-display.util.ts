import { BackendNotification } from '../services/workflow.service';

/** Single-line text shown in toasts for a backend notification (keep in sync with OS notification body). */
export function formatBackendNotificationToastText(n: BackendNotification): string {
  const isDiscReadError = n.level === 'error_disc_read';
  return isDiscReadError
    ? n.title
      ? `${n.title}: ${n.message}`
      : n.message
    : (n.title ?? n.message);
}
