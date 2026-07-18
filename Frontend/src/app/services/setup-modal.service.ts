import { Injectable } from '@angular/core';
import { Subject } from 'rxjs';

export interface SetupModalConfig {
  /** Target step to open (1-6). If not specified, opens at the saved setup step. */
  targetStep?: number;
  /** If true, close the modal after completing the target step (don't walk through remaining steps) */
  closeOnComplete?: boolean;
}

/**
 * Service to open the setup modal from components that don't have direct access to the shell
 * (e.g. the lazy-loaded devmode menu). Shell subscribes to open$ and sets setupModalOpen = true.
 */
@Injectable({ providedIn: 'root' })
export class SetupModalService {
  private readonly openSubject = new Subject<SetupModalConfig | undefined>();
  /** Emit when the setup modal should be opened (e.g. from dev menu "Setup" button). */
  readonly open$ = this.openSubject.asObservable();

  open(config?: SetupModalConfig): void {
    this.openSubject.next(config);
  }
}
