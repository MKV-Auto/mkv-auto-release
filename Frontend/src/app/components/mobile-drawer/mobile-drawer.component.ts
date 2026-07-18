// src/app/components/mobile-drawer/mobile-drawer.component.ts
import { Component, Input, Output, EventEmitter, HostListener, ChangeDetectionStrategy, ViewEncapsulation, OnInit, OnDestroy, OnChanges, SimpleChanges, ElementRef, ChangeDetectorRef, AfterViewInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Overlay, OverlayRef, OverlayConfig } from '@angular/cdk/overlay';
import { DomPortal } from '@angular/cdk/portal';
import { Subscription } from 'rxjs';

@Component({
  selector: 'app-mobile-drawer',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './mobile-drawer.component.html',
  styleUrls: ['./mobile-drawer.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None, // Allow styles to escape parent stacking contexts
})
export class MobileDrawerComponent implements OnInit, AfterViewInit, OnDestroy, OnChanges {
  @Input() isOpen: boolean = false;
  @Input() title: string = '';
  @Output() close = new EventEmitter<void>();
  
  private overlayRef: OverlayRef | null = null;
  private drawerElement: HTMLElement | null = null;
  private backdropClickSubscription: Subscription | null = null;
  private documentClickCheckHandler: ((event: MouseEvent) => void) | null = null;

  constructor(
    private overlay: Overlay,
    private elementRef: ElementRef,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    /**
     * BACKDROP CLICK HANDLING WORKAROUND
     * 
     * Why we're not using the standard CDK approach:
     * 
     * The standard Angular CDK Overlay approach is to use:
     *   overlayRef.backdropClick().subscribe(() => this.onClose());
     * 
     * However, we encountered a known issue where backdropClick() fires even when clicking
     * inside the overlay content (drawer), causing the drawer to close unexpectedly. This is
     * a documented limitation in Angular CDK when using DomPortal to move elements into overlays.
     * 
     * Our workaround:
     * 
     * 1. Disable pointer-events on the backdrop (see openDrawer() method)
     *    - This prevents the backdrop from capturing clicks
     *    - Clicks pass through to elements behind the backdrop
     * 
     * 2. Manual coordinate-based backdrop detection (documentClickCheckHandler)
     *    - Listen to all document clicks in capture phase
     *    - Check if click coordinates fall within backdrop bounds
     *    - Verify click is NOT inside drawer content before closing
     * 
     * 3. Interactive element handling (paneClickHandler, onDrawerClick)
     *    - Don't stop propagation for buttons/inputs/selects
     *    - Allow their native click handlers to fire normally
     *    - Only stop propagation for non-interactive elements
     * 
     * This approach is more complex than the standard CDK method, but necessary to work
     * around the backdropClick() bug. If CDK fixes this issue in the future, we can simplify
     * by reverting to the standard backdropClick() approach.
     * 
     * References:
     * - https://github.com/angular/components/issues/6071
     * - https://stackoverflow.com/questions/51104706/cannot-close-angular-material-cdkoverlay-from-backdropclick
     */
    
    // Create overlay configuration with explicit positioning
    const positionStrategy = this.overlay.position()
      .global()
      .bottom('0px')
      .left('0px')
      .right('0px');
    
    const overlayConfig: OverlayConfig = {
      hasBackdrop: true,
      backdropClass: 'drawer-backdrop',
      positionStrategy: positionStrategy,
      scrollStrategy: this.overlay.scrollStrategies.noop(),
      panelClass: ['mobile-drawer-overlay-panel'],
      maxHeight: '85vh',
      width: '100%',
      disposeOnNavigation: false,
      // Note: We don't use backdropClick() here due to the bug mentioned above
      // Instead, we handle backdrop clicks manually via documentClickCheckHandler
    };
    
    this.overlayRef = this.overlay.create(overlayConfig);
    
    /**
     * Manual backdrop click detection handler
     * 
     * Since we disabled pointer-events on the backdrop, clicks on it pass through to
     * elements behind it. We need to detect clicks in the backdrop area by checking
     * if the click coordinates fall within the backdrop's bounding rectangle.
     * 
     * This handler runs in the capture phase to catch clicks before they bubble.
     */
    this.documentClickCheckHandler = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      
      if (!target || !this.isOpen || !this.overlayRef?.hasAttached()) {
        return;
      }
      
      // Check if click is inside drawer content first
      const isInsideDrawerElement = this.drawerElement && this.drawerElement.contains(target);
      const isDescendantOfDrawer = target?.closest('.mobile-drawer-wrapper') !== null ||
                                    target?.closest('.mobile-drawer-container') !== null ||
                                    target?.closest('.drawer-content') !== null ||
                                    target?.closest('.drawer-header') !== null;
      
      // Try to find overlay pane
      let overlayPane: HTMLElement | null = null;
      if (this.overlayRef.overlayElement) {
        overlayPane = this.overlayRef.overlayElement.querySelector('.cdk-overlay-pane.mobile-drawer-overlay-panel') as HTMLElement;
      }
      if (!overlayPane && this.drawerElement) {
        const parent = this.drawerElement.parentElement;
        if (parent && parent.classList.contains('cdk-overlay-pane')) {
          overlayPane = parent as HTMLElement;
        }
      }
      const isInsidePane = overlayPane && overlayPane.contains(target);
      
      // If click is inside drawer content, don't close
      if (isInsideDrawerElement || isDescendantOfDrawer || isInsidePane) {
        return;
      }
      
      // Check if click is in the backdrop area (but backdrop has pointer-events: none, so this won't fire)
      // Actually, we need to check if the click coordinates are in the backdrop area
      // Try multiple ways to find backdrop
      let backdrop: HTMLElement | null = null;
      if (this.overlayRef?.overlayElement) {
        backdrop = this.overlayRef.overlayElement.querySelector('.cdk-overlay-backdrop') as HTMLElement;
      }
      if (!backdrop) {
        backdrop = document.querySelector('.cdk-overlay-backdrop.drawer-backdrop') as HTMLElement;
      }
      if (!backdrop) {
        const overlayContainer = document.querySelector('.cdk-overlay-container');
        if (overlayContainer) {
          backdrop = overlayContainer.querySelector('.cdk-overlay-backdrop') as HTMLElement;
        }
      }
      
      if (backdrop) {
        const rect = backdrop.getBoundingClientRect();
        const clickX = (event as MouseEvent).clientX;
        const clickY = (event as MouseEvent).clientY;
        const isInBackdropArea = clickX >= rect.left && clickX <= rect.right && 
                                  clickY >= rect.top && clickY <= rect.bottom;
        
        // If click is in backdrop area and not on drawer content, close
        if (isInBackdropArea) {
          this.onClose();
        }
      }
    };
    
    document.addEventListener('click', this.documentClickCheckHandler, true); // Capture phase
    
    // Handle escape key
    this.overlayRef.keydownEvents().subscribe(event => {
      if (event.key === 'Escape') {
        this.onClose();
      }
    });
  }

  ngAfterViewInit(): void {
    // Get the drawer wrapper element - this will be moved to overlay
    this.drawerElement = this.elementRef.nativeElement.querySelector('.mobile-drawer-wrapper') as HTMLElement;
    if (!this.drawerElement) {
      // Fallback to native element if wrapper not found
      this.drawerElement = this.elementRef.nativeElement;
    }
    this.cdr.detectChanges();
    
    // Re-query wrapper in case DOM wasn't ready at start of ngAfterViewInit (e.g. parent OnPush)
    const wrapper = this.elementRef.nativeElement?.querySelector?.('.mobile-drawer-wrapper') as HTMLElement;
    this.drawerElement = wrapper || this.drawerElement || this.elementRef.nativeElement;
    
    if (this.isOpen && this.overlayRef && this.drawerElement) {
      this.openDrawer();
    }
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['isOpen'] && this.overlayRef && this.drawerElement) {
      if (this.isOpen) {
        this.openDrawer();
      } else {
        this.closeDrawer();
      }
    }
  }

  ngOnDestroy(): void {
    // Remove document click check handler
    if (this.documentClickCheckHandler) {
      document.removeEventListener('click', this.documentClickCheckHandler, true);
      this.documentClickCheckHandler = null;
    }
    
    // Unsubscribe from backdrop clicks (if still subscribed)
    if (this.backdropClickSubscription) {
      this.backdropClickSubscription.unsubscribe();
      this.backdropClickSubscription = null;
    }
    
    if (this.overlayRef) {
      this.overlayRef.dispose();
      this.overlayRef = null;
    }
  }

  private openDrawer(): void {
    if (this.overlayRef && !this.overlayRef.hasAttached() && this.drawerElement) {
      // Use DomPortal to move the existing drawer element to overlay
      const portal = new DomPortal(this.drawerElement);
      this.overlayRef.attach(portal);
      
      // Force position update after attachment
      this.overlayRef.updatePosition();
      
      // Show the element in the overlay
      if (this.drawerElement) {
        this.drawerElement.style.display = '';
        this.drawerElement.style.setProperty('pointer-events', 'auto', 'important');
      }
      
      /**
       * Disable pointer-events on backdrop
       * 
       * This is a critical part of our workaround. By setting pointer-events: none on the
       * backdrop, clicks pass through to elements behind it. This allows us to use
       * coordinate-based detection to determine if a click was in the backdrop area.
       * 
       * Without this, the backdrop would capture all clicks, preventing our manual detection
       * logic from working correctly.
       */
      const disableBackdropPointerEvents = (attempt: number) => {
        // Try multiple ways to find the backdrop
        let backdrop: HTMLElement | null = null;
        
        // Method 1: Query overlayElement
        if (this.overlayRef?.overlayElement) {
          backdrop = this.overlayRef.overlayElement.querySelector('.cdk-overlay-backdrop') as HTMLElement;
        }
        
        // Method 2: Query document (backdrop might be at document level)
        if (!backdrop) {
          backdrop = document.querySelector('.cdk-overlay-backdrop.drawer-backdrop') as HTMLElement;
        }
        
        // Method 3: Query any backdrop in overlay container
        if (!backdrop) {
          const overlayContainer = document.querySelector('.cdk-overlay-container');
          if (overlayContainer) {
            backdrop = overlayContainer.querySelector('.cdk-overlay-backdrop') as HTMLElement;
          }
        }
        
        if (backdrop) {
          backdrop.style.setProperty('pointer-events', 'none', 'important');
          // z-index handled by component styles
        } else if (attempt < 10) {
          // Retry finding backdrop with delays
          setTimeout(() => disableBackdropPointerEvents(attempt + 1), 50 * attempt);
        }
      };
      
      // Start trying to disable backdrop pointer-events
      disableBackdropPointerEvents(1);
      setTimeout(() => disableBackdropPointerEvents(2), 0);
      setTimeout(() => disableBackdropPointerEvents(3), 50);
      setTimeout(() => disableBackdropPointerEvents(4), 100);
      setTimeout(() => disableBackdropPointerEvents(5), 200);
      
      // CRITICAL: Add click handler to overlay pane to stop ALL clicks from reaching backdrop
      // This must be done after the drawer is attached to the overlay
      // Try multiple times with delays to ensure the pane is fully attached
      const attachPaneClickHandler = (attempt: number) => {
        // Try to find the pane - it should be the parent of drawerElement after attach
        const overlayPane = this.drawerElement?.parentElement as HTMLElement;
        
        if (overlayPane && overlayPane.classList.contains('cdk-overlay-pane')) {
          // Remove any existing handler first
          const existingHandler = (overlayPane as any).__drawerPaneClickHandler;
          if (existingHandler) {
            overlayPane.removeEventListener('click', existingHandler, true);
          }
          
          /**
           * Pane click handler - prevents backdrop detection from firing on drawer content clicks
           * 
           * This handler is attached directly to the cdk-overlay-pane element. It stops propagation
           * for non-interactive elements to prevent our document-level backdrop detection handler
           * from incorrectly closing the drawer.
           * 
           * CRITICAL: We must NOT stop propagation for interactive elements (buttons, inputs, etc.),
           * otherwise their click handlers won't fire. This is why we check isInteractive first.
           */
          const paneClickHandler = (e: MouseEvent) => {
            const target = e.target as HTMLElement;
            // Only stop propagation if this is NOT an interactive element (input, button, etc.)
            // Interactive elements need to receive the click to work properly
            const isInteractive = target.tagName === 'INPUT' || 
                                  target.tagName === 'BUTTON' || 
                                  target.tagName === 'TEXTAREA' ||
                                  target.tagName === 'SELECT' ||
                                  target.closest('button') !== null ||
                                  target.closest('input') !== null ||
                                  target.closest('textarea') !== null ||
                                  target.closest('select') !== null;
            
            // For interactive elements, don't stop propagation - let them receive the click
            // For other elements, stop propagation to prevent backdrop from receiving it
            if (!isInteractive) {
              e.stopPropagation(); // Only stop bubbling, not immediate propagation
            }
          };
          overlayPane.addEventListener('click', paneClickHandler, true); // Capture phase - runs before backdrop
          (overlayPane as any).__drawerPaneClickHandler = paneClickHandler; // Store reference for cleanup
        } else if (attempt < 5) {
          // Retry if pane not found yet
          setTimeout(() => attachPaneClickHandler(attempt + 1), 50 * attempt);
        }
      };
      
      // Start trying to attach immediately and with delays
      attachPaneClickHandler(1);
      setTimeout(() => attachPaneClickHandler(2), 0);
      setTimeout(() => attachPaneClickHandler(3), 50);
      setTimeout(() => attachPaneClickHandler(4), 100);
      
      /**
       * Attach click listener to overlay pane
       * 
       * This handler prevents clicks inside the drawer from bubbling up to the document
       * level where our backdrop detection handler would incorrectly close the drawer.
       * 
       * IMPORTANT: We only stop propagation for non-interactive elements. Interactive
       * elements (buttons, inputs, selects) need their click events to propagate normally
       * so their handlers can fire. This is why we check isInteractive before calling
       * stopPropagation().
       * 
       * Try multiple times to ensure it attaches (DOM may not be ready immediately).
       */
      const attachClickListener = () => {
        const overlayPane = this.drawerElement?.parentElement as HTMLElement;
        
        if (overlayPane && overlayPane.classList.contains('cdk-overlay-pane')) {
          // Remove any existing listener by cloning (clean slate)
          const clickHandler = (e: MouseEvent) => {
            const target = e.target as HTMLElement;
            const isDrawerContent = target?.closest('.mobile-drawer-container') !== null || 
                                    target?.closest('.mobile-drawer-wrapper') !== null;
            
            
            if (isDrawerContent) {
              // Check if this is an interactive element that needs to receive the click
              // Interactive elements must NOT have propagation stopped, or their handlers won't fire
              const isInteractive = target.tagName === 'INPUT' || 
                                   target.tagName === 'BUTTON' || 
                                   target.tagName === 'TEXTAREA' ||
                                   target.tagName === 'SELECT' ||
                                   target.closest('button') !== null ||
                                   target.closest('input') !== null ||
                                   target.closest('textarea') !== null ||
                                   target.closest('select') !== null;
              
              // Mark that this was a drawer click, not backdrop
              (window as any).__drawerClickInProgress = true;
              
              // Only stop propagation for non-interactive elements
              // Interactive elements (buttons, inputs) need clicks to reach their handlers
              if (!isInteractive) {
                e.stopPropagation(); // Only stop bubbling, not immediate propagation
              }
              
              // Clear flag after backdrop handler would have run
              setTimeout(() => {
                (window as any).__drawerClickInProgress = false;
              }, 100);
            }
          };
          
          overlayPane.addEventListener('click', clickHandler, true); // Capture phase - before backdrop handler
        } else {
          // Retry if pane not found yet
          setTimeout(attachClickListener, 50);
        }
      };
      
      // Try immediately and with delays
      attachClickListener();
      setTimeout(attachClickListener, 50);
      setTimeout(attachClickListener, 100);
      setTimeout(attachClickListener, 200);
      
      // Explicitly set overlay pane position via DOM manipulation
      // Use multiple attempts to ensure positioning works
      const positionOverlay = (attempt: number) => {
        const overlayElement = this.overlayRef?.overlayElement;
        
        if (!overlayElement || !this.drawerElement) {
          return;
        }
        
        // Set overlay element position - this is the root container (e.g. #cdk-overlay-0).
        // Use pointer-events: none on the host so it does not capture touches; only children
        // (backdrop + pane) are hit. Backdrop has pointer-events: none in CSS; pane has auto.
        // This prevents the full-screen host from blocking taps on the drawer on mobile.
        overlayElement.style.cssText = `
          position: fixed !important;
          top: 0 !important;
          left: 0 !important;
          right: 0 !important;
          bottom: 0 !important;
          pointer-events: none !important;
        `;
        
        // Find the pane - it's the parent of the drawer element after attach
        const overlayPane = this.drawerElement.parentElement as HTMLElement;
        
        // Verify it's actually the pane
        if (!overlayPane || !overlayPane.classList.contains('cdk-overlay-pane')) {
          // Fallback: search in overlayElement
          const foundPane = overlayElement.querySelector('.cdk-overlay-pane.mobile-drawer-overlay-panel') as HTMLElement ||
                          overlayElement.querySelector('.cdk-overlay-pane') as HTMLElement;
          if (foundPane) {
            positionPaneElement(foundPane);
            attachPaneTouchFocus(foundPane);
          }
          return;
        }
        
        positionPaneElement(overlayPane);
        attachPaneTouchFocus(overlayPane);
      };
      
      /** Focus form control only on tap (short, minimal movement); allow scroll when user drags. */
      const attachPaneTouchFocus = (overlayPane: HTMLElement) => {
        if ((overlayPane as any).__drawerTouchFocusAttached) return;
        (overlayPane as any).__drawerTouchFocusAttached = true;
        const TAP_MOVE_THRESHOLD_PX = 10;
        const TAP_MAX_DURATION_MS = 400;
        let state: {
          id: number;
          startX: number;
          startY: number;
          startTime: number;
          control: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null;
          scrolling: boolean;
        } | null = null;

        const getControlUnder = (x: number, y: number): HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null => {
          const el = document.elementFromPoint(x, y) as HTMLElement | null;
          if (!el || !overlayPane.contains(el)) return null;
          const field = el.closest('.title-drawer-field-half') || el.closest('.title-drawer-field');
          if (!field) return null;
          const control = field.querySelector('input, select, textarea') as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null;
          return control && !control.disabled ? control : null;
        };

        const onStart = (e: TouchEvent) => {
          if (!e.touches || e.touches.length === 0) return;
          const t = e.touches[0];
          const c = getControlUnder(t.clientX, t.clientY);
          state = { id: t.identifier, startX: t.clientX, startY: t.clientY, startTime: Date.now(), control: c, scrolling: false };
        };

        const onMove = (e: TouchEvent) => {
          if (!state || !e.changedTouches) return;
          const t = Array.from(e.changedTouches).find(touch => touch.identifier === state!.id);
          if (!t) return;
          const dx = t.clientX - state.startX;
          const dy = t.clientY - state.startY;
          if (dx * dx + dy * dy > TAP_MOVE_THRESHOLD_PX * TAP_MOVE_THRESHOLD_PX) state.scrolling = true;
        };

        const onEnd = (e: TouchEvent) => {
          if (!state || !e.changedTouches) return;
          const t = Array.from(e.changedTouches).find(touch => touch.identifier === state!.id);
          if (!t) return;
          const duration = Date.now() - state.startTime;
          const isTap = !state.scrolling && duration < TAP_MAX_DURATION_MS && state.control;
          if (isTap) {
            e.preventDefault();
            state.control!.focus();
          }
          state = null;
        };

        overlayPane.addEventListener('touchstart', onStart, true);
        overlayPane.addEventListener('touchmove', onMove, true);
        overlayPane.addEventListener('touchend', onEnd, true);
        overlayPane.addEventListener('touchcancel', () => { state = null; }, true);
      };
      
      const positionPaneElement = (overlayPane: HTMLElement) => {
        // Get viewport dimensions
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;
        const targetHeight = Math.floor(viewportHeight * 0.85);
        
        // Force position to bottom of viewport using absolute pixel values
        overlayPane.style.cssText = `
          position: fixed !important;
          bottom: 0px !important;
          left: 0px !important;
          right: 0px !important;
          top: auto !important;
          width: ${viewportWidth}px !important;
          max-width: ${viewportWidth}px !important;
          height: ${targetHeight}px !important;
          max-height: ${targetHeight}px !important;
          margin: 0 !important;
          padding: 0 !important;
          transform: translateY(0) !important;
          pointer-events: auto !important;
        `;
        
        // Also set via individual properties as additional fallback
        overlayPane.style.setProperty('position', 'fixed', 'important');
        overlayPane.style.setProperty('bottom', '0', 'important');
        overlayPane.style.setProperty('left', '0', 'important');
        overlayPane.style.setProperty('right', '0', 'important');
        overlayPane.style.setProperty('top', 'auto', 'important');
        overlayPane.style.setProperty('width', `${viewportWidth}px`, 'important');
        overlayPane.style.setProperty('height', `${targetHeight}px`, 'important');
        overlayPane.style.setProperty('margin', '0', 'important');
        overlayPane.style.setProperty('padding', '0', 'important');
        overlayPane.style.setProperty('transform', 'translateY(0)', 'important');
        // z-index handled by component styles
        overlayPane.style.setProperty('pointer-events', 'auto', 'important'); // CRITICAL: Ensure pointer events are enabled
        
        // Also ensure drawer container has pointer-events enabled
        if (this.drawerElement) {
          this.drawerElement.style.setProperty('pointer-events', 'auto', 'important');
          const drawerContainer = this.drawerElement.querySelector('.mobile-drawer-container') as HTMLElement;
          if (drawerContainer) {
            drawerContainer.style.setProperty('pointer-events', 'auto', 'important');
          }
          
          // Also add listener to drawer content
          const drawerContent = this.drawerElement.querySelector('.drawer-content') as HTMLElement;
          if (drawerContent) {
            drawerContent.style.setProperty('pointer-events', 'auto', 'important');
          }
        }
      };
      
      // Try positioning multiple times to ensure it sticks
      positionOverlay(1);
      requestAnimationFrame(() => {
        positionOverlay(2);
        requestAnimationFrame(() => positionOverlay(3));
      });
      setTimeout(() => positionOverlay(4), 0);
      setTimeout(() => positionOverlay(5), 10);
      setTimeout(() => positionOverlay(6), 50);
    }
  }

  private closeDrawer(): void {
    if (this.overlayRef && this.overlayRef.hasAttached()) {
      this.overlayRef.detach();
      // DomPortal automatically moves element back to original location on detach
    }
  }

  @HostListener('document:keydown.escape', ['$event'])
  onEscapeKey(event: KeyboardEvent): void {
    if (this.isOpen) {
      this.onClose();
    }
  }

  onClose(): void {
    this.close.emit();
  }

  /**
   * Template click handler for drawer container
   * 
   * This handler is attached to the .mobile-drawer-container element in the template.
   * It works in conjunction with the pane click handler to prevent backdrop detection
   * from firing on drawer content clicks.
   * 
   * Like the pane handler, we only stop propagation for non-interactive elements to
   * ensure buttons, inputs, and the close button work correctly.
   */
  onDrawerClick(event: Event): void {
    const target = event.target as HTMLElement;
    const currentTarget = event.currentTarget as HTMLElement;
    
    // Check if this is the close button - if so, don't prevent its default behavior
    const isCloseButton = target.closest('.drawer-close-btn') !== null || 
                         target.closest('button[aria-label="Close drawer"]') !== null;
    
    // Check if this is an interactive element that needs to receive the click
    const isInteractive = target.tagName === 'INPUT' || 
                         target.tagName === 'BUTTON' || 
                         target.tagName === 'TEXTAREA' ||
                         target.tagName === 'SELECT' ||
                         target.closest('button') !== null ||
                         target.closest('input') !== null ||
                         target.closest('textarea') !== null ||
                         target.closest('select') !== null;
    
    // For interactive elements and close button, don't stop propagation at all
    // They need their click handlers to fire normally
    // Only stop propagation for non-interactive elements to prevent backdrop clicks
    const stopPropagationCalled = !isInteractive && !isCloseButton;
    if (stopPropagationCalled) {
      event.stopPropagation(); // Only stop bubbling, not immediate propagation
    }

    // Mark as drawer content click for the backdropClick handler (but don't interfere with button clicks)
    if (!isCloseButton) {
      (window as any).__drawerContentClick = true;
      setTimeout(() => {
        (window as any).__drawerContentClick = false;
      }, 100);
    }
  }
}
