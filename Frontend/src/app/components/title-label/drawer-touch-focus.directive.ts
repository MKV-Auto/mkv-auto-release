import { Directive, ElementRef, OnInit, OnDestroy } from '@angular/core';

/**
 * Capture-phase touchstart so the first touch in a drawer form field focuses the control.
 * Use on the drawer content container; runs before bubble phase so we get the touch even
 * when something else would receive it.
 */
@Directive({ selector: '[appDrawerTouchFocus]', standalone: true })
export class DrawerTouchFocusDirective implements OnInit, OnDestroy {
  private handler = (event: TouchEvent): void => {
    const target = event.target as HTMLElement | null;
    if (!target || !this.el.nativeElement.contains(target)) return;
    const field = target.closest('.title-drawer-field-half') || target.closest('.title-drawer-field');
    if (!field) return;
    const control = field.querySelector('input, select, textarea') as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null;
    if (!control || control.disabled) return;
    event.preventDefault();
    event.stopPropagation();
    control.focus();
  };

  constructor(private el: ElementRef<HTMLElement>) {}

  ngOnInit(): void {
    this.el.nativeElement.addEventListener('touchstart', this.handler, true);
  }

  ngOnDestroy(): void {
    this.el.nativeElement.removeEventListener('touchstart', this.handler, true);
  }
}
