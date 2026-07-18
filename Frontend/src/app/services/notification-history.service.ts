import { Injectable } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';
import { map } from 'rxjs/operators';
import { BackendNotification } from './workflow.service';

export interface StoredNotification extends BackendNotification {
  /** When the notification was received (ISO string). Falls back to BackendNotification.timestamp. */
  receivedAt: string;
  /** Whether the user has seen this notification. */
  read: boolean;
}

const STORAGE_KEY = 'mkvauto_notification_history';
const MAX_NOTIFICATIONS = 100;

@Injectable({ providedIn: 'root' })
export class NotificationHistoryService {
  private _notifications$ = new BehaviorSubject<StoredNotification[]>([]);

  /** All stored notifications (newest first). */
  notifications$: Observable<StoredNotification[]> = this._notifications$.asObservable();

  /** Count of unread notifications. */
  unreadCount$: Observable<number> = this._notifications$.pipe(
    map(ns => ns.filter(n => !n.read).length)
  );

  constructor() {
    this._loadFromStorage();
  }

  /** Add a new notification from the WebSocket stream. Deduplicates by id. */
  add(notification: BackendNotification): void {
    const current = this._notifications$.value;
    // Deduplicate by id
    if (notification.id && current.some(n => n.id === notification.id)) {
      return;
    }
    const stored: StoredNotification = {
      ...notification,
      receivedAt: notification.timestamp || new Date().toISOString(),
      read: false,
    };
    const updated = [stored, ...current].slice(0, MAX_NOTIFICATIONS);
    this._notifications$.next(updated);
    this._saveToStorage(updated);
  }

  /** Mark all notifications as read. */
  markAllRead(): void {
    const updated = this._notifications$.value.map(n => ({ ...n, read: true }));
    this._notifications$.next(updated);
    this._saveToStorage(updated);
  }

  /** Mark a single notification as read. */
  markRead(id: string): void {
    const updated = this._notifications$.value.map(n =>
      n.id === id ? { ...n, read: true } : n
    );
    this._notifications$.next(updated);
    this._saveToStorage(updated);
  }

  /** Clear all notifications. */
  clearAll(): void {
    this._notifications$.next([]);
    this._saveToStorage([]);
  }

  private _loadFromStorage(): void {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          this._notifications$.next(parsed.slice(0, MAX_NOTIFICATIONS));
        }
      }
    } catch {
      // Ignore corrupt storage
    }
  }

  private _saveToStorage(notifications: StoredNotification[]): void {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(notifications));
    } catch {
      // Ignore storage errors (e.g. quota exceeded)
    }
  }
}
