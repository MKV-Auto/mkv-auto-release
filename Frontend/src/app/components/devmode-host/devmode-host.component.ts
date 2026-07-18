import {
  Component,
  ViewContainerRef,
  ViewChild,
  AfterViewInit,
  OnDestroy,
} from '@angular/core';
import { CommonModule } from '@angular/common';

/**
 * Host that lazy-loads the devmode menu chunk and renders DevmodeMenuComponent.
 * Used so the dev menu is in a separate chunk and can be omitted in production-no-devmode builds.
 */
@Component({
  selector: 'app-devmode-host',
  standalone: true,
  imports: [CommonModule],
  template: '<ng-container #host></ng-container>',
})
export class DevmodeHostComponent implements AfterViewInit, OnDestroy {
  @ViewChild('host', { read: ViewContainerRef }) hostRef!: ViewContainerRef;
  private destroyed = false;

  ngAfterViewInit(): void {
    import('../devmode-menu/devmode-menu.component').then((m) => {
      if (this.destroyed || !this.hostRef) return;
      const ref = this.hostRef.createComponent(m.DevmodeMenuComponent);
      ref.changeDetectorRef.detectChanges();
    });
  }

  ngOnDestroy(): void {
    this.destroyed = true;
  }
}
