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
    return window.innerWidth < this.mobileBreakpoint;
  }
}

