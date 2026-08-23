// src/app/services/mobile.service.ts
import { Injectable } from '@angular/core';
import { BehaviorSubject, Observable, fromEvent } from 'rxjs';
import { map, startWith } from 'rxjs/operators';

@Injectable({ providedIn: 'root' })
export class MobileService {
  private mobileBreakpoint = 768; // px
  private _isMobile = new BehaviorSubject<boolean>(this.checkMobile());

  constructor() {
    // Listen for window resize events
    if (typeof window !== 'undefined') {
      fromEvent(window, 'resize')
        .pipe(
          startWith(null),
          map(() => this.checkMobile())
        )
        .subscribe(isMobile => {
          if (isMobile !== this._isMobile.value) {
            this._isMobile.next(isMobile);
          }
        });
    }
  }

  get isMobile$(): Observable<boolean> {
    return this._isMobile.asObservable();
  }

  get isMobile(): boolean {
    return this._isMobile.value;
  }

  private checkMobile(): boolean {
    if (typeof window === 'undefined') {
      return false;
    }
    // documentElement.clientWidth, NOT window.innerWidth. innerWidth reports
    // the layout viewport, which horizontal overflow inflates: one 654px-wide
    // element pushed it from 375 to ~700 on a phone, and a slightly wider
    // element would cross 768 and flip the whole app into desktop layout on
    // a phone — the overflow causing the misdetection that worsens the
    // overflow. clientWidth is the true visible width regardless of what the
    // content does. Falls back to innerWidth only if documentElement is
    // somehow unavailable.
    const de = typeof document !== 'undefined' ? document.documentElement : null;
    const width = de?.clientWidth || window.innerWidth;
    return width < this.mobileBreakpoint;
  }
}

