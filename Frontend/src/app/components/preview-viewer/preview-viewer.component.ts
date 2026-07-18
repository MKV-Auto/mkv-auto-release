import { CommonModule } from '@angular/common';
import { AfterViewInit, Component, ElementRef, EventEmitter, HostListener, Input, OnChanges, OnDestroy, Output, SimpleChanges, TemplateRef, ViewChild, ViewContainerRef } from '@angular/core';
import { Overlay, OverlayConfig, OverlayRef } from '@angular/cdk/overlay';
import { TemplatePortal } from '@angular/cdk/portal';
import Hls from 'hls.js';

@Component({
  selector: 'app-preview-viewer',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './preview-viewer.component.html',
})
export class PreviewViewerComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() src: string | null = null;
  @Input() autoLoad = true;
  @Input() autoplay = true;
  @Input() poster: string | null = null;
  @Input() preload: 'auto' | 'metadata' | 'none' = 'metadata';
  @Input() muted = false;
  @Input() inline = false;
  @Input() fill = false;
  @Input() showFullscreen = false;
  @Input() hideFullscreenOnMobile = false;
  @Input() disablePictureInPicture = false;
  @Output() statusChange = new EventEmitter<string>();
  @Output() errorChange = new EventEmitter<string>();
  /** Emits video intrinsic dimensions when metadata is loaded so the container can match aspect ratio. */
  @Output() aspectRatioChange = new EventEmitter<{ width: number; height: number } | null>();
  @ViewChild('video') video?: ElementRef<HTMLVideoElement>;
  @ViewChild('overlayVideo') overlayVideo?: ElementRef<HTMLVideoElement>;
  @ViewChild('overlayTemplate') overlayTemplate?: TemplateRef<unknown>;

  private hls?: Hls;
  private overlayRef?: OverlayRef;
  private overlayHls?: Hls;
  private initialized = false;
  private overlayStartTime = 0;
  private overlayShouldPlay = false;
  private suppressInlineExpandUntil = 0;
  private documentClickHandler?: (event: MouseEvent) => void;
  playbackStarted = false;
  overlayOpen = false;
  status = '';
  error = '';

  ngAfterViewInit(): void {
    this.initialized = true;
    if (this.autoLoad && this.src) {
      this.reload();
    }
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (!this.initialized) return;
    if (changes['src'] && this.autoLoad) {
      this.playbackStarted = false;
      this.reload();
    }
  }

  constructor(
    private overlay: Overlay,
    private viewContainerRef: ViewContainerRef
  ) {}

  ngOnDestroy(): void {
    // Remove document click listener if still attached
    if (this.documentClickHandler) {
      document.removeEventListener('click', this.documentClickHandler, true);
      this.documentClickHandler = undefined;
    }
    
    this.detachHls();
    this.detachOverlayHls();
    if (this.overlayRef) {
      this.overlayRef.dispose();
      this.overlayRef = undefined;
    }
  }

  /** Public reload so parents can manually trigger playback */
  reload(): void {
    this.setStatus('Loading stream…');
    this.setError('');
    const video = this.video?.nativeElement;
    if (!video || !this.src) {
      this.setStatus(video ? 'No preview available' : 'Player not ready');
      return;
    }
    this.detachHls();
    video.pause();
    video.removeAttribute('src');
    video.load();
    video.muted = this.muted;
    video.autoplay = this.autoplay;

    const isSafari = this.isSafari();
    const canPlayNative = video.canPlayType('application/vnd.apple.mpegurl') !== '';

    if (isSafari && canPlayNative) {
      this.playNative(video, 'Playing stream (Safari native HLS)');
      return;
    }
    if (this.startHls(video)) {
      return;
    }
    this.playNative(video, 'Playing stream (native fallback)');
  }

  onLoaded(): void {
    if (!this.status) {
      this.setStatus('Ready');
    }
  }

  onVideoMetadata(event: Event): void {
    const v = event.target as HTMLVideoElement | null;
    if (v?.videoWidth && v?.videoHeight) {
      this.aspectRatioChange.emit({ width: v.videoWidth, height: v.videoHeight });
    } else {
      this.aspectRatioChange.emit(null);
    }
  }

  onWaiting(): void {
    this.setStatus('Buffering…');
  }

  onPlaying(): void {
    this.setStatus('Playing stream');
    this.playbackStarted = true;
  }

  onVideoError(event: Event): void {
    const target = event.target as HTMLVideoElement | null;
    const mediaError = target?.error?.message || target?.error?.code;
    this.setError(`Playback error${mediaError ? `: ${mediaError}` : ''}`);
    this.setStatus('');
  }

  requestFullscreen(): void {
    const video = this.video?.nativeElement;
    if (!video) return;
    const anyVideo = video as any;
    if (video.requestFullscreen) {
      video.requestFullscreen().catch(() => undefined);
      return;
    }
    if (anyVideo.webkitEnterFullscreen) {
      anyVideo.webkitEnterFullscreen();
      return;
    }
    if (anyVideo.webkitRequestFullscreen) {
      anyVideo.webkitRequestFullscreen();
    }
  }

  openOverlay(): void {
    if (this.overlayOpen) return;
    const inline = this.video?.nativeElement;
    const currentTime = inline?.currentTime || 0;
    const isPaused = inline?.paused ?? true;
    if (inline) {
      inline.pause();
    }
    this.overlayStartTime = currentTime;
    this.overlayShouldPlay = !isPaused;
    this.overlayOpen = true;
    if (!this.overlayRef) {
      const positionStrategy = this.overlay.position()
        .global()
        .centerHorizontally()
        .centerVertically();
      const overlayConfig: OverlayConfig = {
        hasBackdrop: true,
        backdropClass: 'preview-overlay-backdrop',
        panelClass: 'preview-overlay-panel',
        positionStrategy,
        scrollStrategy: this.overlay.scrollStrategies.block(),
      };
      this.overlayRef = this.overlay.create(overlayConfig);
      // Don't use backdropClick() - it intercepts clicks before they reach the video
      // Instead, use a document-level click listener that only closes when clicking outside video area
    }
    if (this.overlayTemplate && this.overlayRef) {
      this.overlayRef.attach(new TemplatePortal(this.overlayTemplate, this.viewContainerRef));
      requestAnimationFrame(() => this.loadOverlay());
    }

    // Explicitly disable pointer-events on backdrop using JavaScript (like mobile-drawer)
    // CSS alone may not be sufficient due to timing/z-index issues
    const disableBackdropPointerEvents = (attempt: number) => {
      let backdrop: HTMLElement | null = null;
      
      // Method 1: Find backdrop via overlayRef
      if (this.overlayRef?.overlayElement) {
        backdrop = this.overlayRef.overlayElement.querySelector('.cdk-overlay-backdrop.preview-overlay-backdrop') as HTMLElement;
      }
      
      // Method 2: Find backdrop via document query
      if (!backdrop) {
        backdrop = document.querySelector('.cdk-overlay-backdrop.preview-overlay-backdrop') as HTMLElement;
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

    // Add pane click handler to stop propagation for non-interactive elements
    // This prevents backdrop from receiving clicks on overlay content
    // (like mobile-drawer approach)
    const attachPaneClickHandler = (attempt: number) => {
      let overlayPane: HTMLElement | null = null;
      if (this.overlayRef?.overlayElement) {
        overlayPane = this.overlayRef.overlayElement.querySelector('.cdk-overlay-pane.preview-overlay-panel') as HTMLElement;
      }
      if (!overlayPane) {
        overlayPane = document.querySelector('.cdk-overlay-pane.preview-overlay-panel') as HTMLElement;
      }
      
      if (overlayPane) {
        // Remove any existing handler first
        const existingHandler = (overlayPane as any).__previewPaneClickHandler;
        if (existingHandler) {
          overlayPane.removeEventListener('click', existingHandler, true);
        }
        
        const paneClickHandler = (e: MouseEvent) => {
          const target = e.target as HTMLElement;
          // Check if this is an interactive element (video, button, input, etc.)
          const isInteractive = target.tagName === 'VIDEO' || 
                                target.tagName === 'INPUT' || 
                                target.tagName === 'BUTTON' || 
                                target.tagName === 'TEXTAREA' ||
                                target.tagName === 'SELECT' ||
                                target.closest('video') !== null ||
                                target.closest('button') !== null ||
                                target.closest('input') !== null ||
                                target.closest('textarea') !== null ||
                                target.closest('select') !== null;
          
          // For interactive elements (video, buttons, etc.), don't stop propagation - let them receive the click
          // For other elements, stop propagation to prevent backdrop from receiving it
          if (!isInteractive) {
            e.stopPropagation();
          }
        };
        overlayPane.addEventListener('click', paneClickHandler, true);
        (overlayPane as any).__previewPaneClickHandler = paneClickHandler;
      } else if (attempt < 10) {
        setTimeout(() => attachPaneClickHandler(attempt + 1), 50 * attempt);
      }
    };
    
    // Start trying to attach pane click handler
    attachPaneClickHandler(1);
    setTimeout(() => attachPaneClickHandler(2), 0);
    setTimeout(() => attachPaneClickHandler(3), 50);
    setTimeout(() => attachPaneClickHandler(4), 100);
    setTimeout(() => attachPaneClickHandler(5), 200);

    // Set up document click handler to detect clicks outside preview content
    // This approach allows clicks on video/controls to work normally
    this.documentClickHandler = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      const isBackdrop = target?.classList.contains('cdk-overlay-backdrop') || 
                         target?.classList.contains('preview-overlay-backdrop');
      
      if (!this.overlayOpen || !this.overlayRef?.hasAttached()) {
        return;
      }

      // Find the video element and check if click coordinates are inside it
      let overlayPane: HTMLElement | null = null;
      if (this.overlayRef?.overlayElement) {
        overlayPane = this.overlayRef.overlayElement.querySelector('.cdk-overlay-pane.preview-overlay-panel') as HTMLElement;
      }
      if (!overlayPane) {
        overlayPane = document.querySelector('.cdk-overlay-pane.preview-overlay-panel') as HTMLElement;
      }
      
      const previewOverlayElement = overlayPane?.querySelector('.preview-overlay') as HTMLElement || 
                                    document.querySelector('.preview-overlay') as HTMLElement;
      const videoElement = previewOverlayElement?.querySelector('video') as HTMLVideoElement;
      
      const clickX = event.clientX;
      const clickY = event.clientY;
      
      // Check if click is inside video element using coordinates
      let isInsideVideo = false;
      if (videoElement) {
        const videoRect = videoElement.getBoundingClientRect();
        isInsideVideo = clickX >= videoRect.left && clickX <= videoRect.right && 
                        clickY >= videoRect.top && clickY <= videoRect.bottom;
      } else {
        // Fallback: check if click is inside preview overlay element
        if (previewOverlayElement) {
          const previewRect = previewOverlayElement.getBoundingClientRect();
          isInsideVideo = clickX >= previewRect.left && clickX <= previewRect.right && 
                          clickY >= previewRect.top && clickY <= previewRect.bottom;
        }
      }
      
      // Also check if click target is inside preview overlay (for close button, etc.)
      const isInsidePreviewOverlay = target?.closest('.preview-overlay') !== null;
      const isInsidePane = overlayPane && overlayPane.contains(target);
      
      // If click is on backdrop but inside video area, ignore it (backdrop shouldn't receive clicks due to pointer-events: none)
      // Only close if click is NOT inside video/preview content AND is actually on the backdrop
      if (isBackdrop && isInsideVideo) {
        return; // Don't close - this shouldn't happen if pointer-events: none is working
      }
      
      // Only close if click is NOT inside video/preview content
      if (!isInsideVideo && !isInsidePreviewOverlay && !isInsidePane) {
        this.closeOverlay();
      }
    };
    
    document.addEventListener('click', this.documentClickHandler, true); // Capture phase
  }

  onInlinePlay(): void {
    if (Date.now() < this.suppressInlineExpandUntil) {
      return;
    }
    if (this.showFullscreen) {
      this.openOverlay();
    }
  }

  onInlinePause(): void {
    // Keep overlay state unchanged on inline pause.
  }

  @HostListener('document:keydown.escape', ['$event'])
  onEscape(event: KeyboardEvent): void {
    if (!this.overlayOpen) return;
    event.preventDefault();
    event.stopPropagation();
    this.closeOverlay();
  }

  closeOverlay(): void {
    // Remove document click listener
    if (this.documentClickHandler) {
      document.removeEventListener('click', this.documentClickHandler, true);
      this.documentClickHandler = undefined;
    }
    
    // Remove pane click handler
    let overlayPane: HTMLElement | null = null;
    if (this.overlayRef?.overlayElement) {
      overlayPane = this.overlayRef.overlayElement.querySelector('.cdk-overlay-pane.preview-overlay-panel') as HTMLElement;
    }
    if (!overlayPane) {
      overlayPane = document.querySelector('.cdk-overlay-pane.preview-overlay-panel') as HTMLElement;
    }
    if (overlayPane) {
      const existingHandler = (overlayPane as any).__previewPaneClickHandler;
      if (existingHandler) {
        overlayPane.removeEventListener('click', existingHandler, true);
        (overlayPane as any).__previewPaneClickHandler = undefined;
      }
    }
    
    const overlay = this.overlayVideo?.nativeElement;
    const inline = this.video?.nativeElement;
    const currentTime = overlay?.currentTime || 0;
    const wasPlaying = overlay ? !overlay.paused : this.overlayShouldPlay;
    this.detachOverlayHls();
    if (this.overlayRef?.hasAttached()) {
      this.overlayRef.detach();
    }
    this.overlayOpen = false;
    if (overlay) {
      overlay.pause();
      overlay.removeAttribute('src');
      overlay.load();
    }
    if (inline && this.src) {
      inline.currentTime = currentTime;
      // Always pause inline video when overlay closes — user dismissed the preview
      inline.pause();
    }
  }

  onOverlayLoaded(): void {
    const overlay = this.overlayVideo?.nativeElement;
    if (!overlay) return;
    if (this.overlayStartTime > 0) {
      overlay.currentTime = this.overlayStartTime;
    }
    if (this.overlayShouldPlay) {
      overlay.play().catch(() => undefined);
    }
  }

  private loadOverlay(): void {
    const overlay = this.overlayVideo?.nativeElement;
    if (!overlay || !this.src) return;
    this.detachOverlayHls();
    overlay.pause();
    overlay.removeAttribute('src');
    overlay.load();
    overlay.muted = this.muted;

    const isSafari = this.isSafari();
    const canPlayNative = overlay.canPlayType('application/vnd.apple.mpegurl') !== '';

    if (isSafari && canPlayNative) {
      this.playNative(overlay, 'Playing stream (Safari native HLS)');
      return;
    }
    if (this.startHls(overlay, true)) {
      return;
    }
    this.playNative(overlay, 'Playing stream (native fallback)');
  }

  private startHls(video: HTMLVideoElement, isOverlay = false): boolean {
    if (!Hls.isSupported()) {
      this.setError('HLS not supported in this browser.');
      this.setStatus('');
      return false;
    }
    const hls = new Hls({ enableWorker: true });
    const shouldAutoplay = isOverlay ? this.overlayShouldPlay : this.autoplay;
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (!shouldAutoplay) {
        this.setStatus('Ready');
        return;
      }
      video
        .play()
        .then(() => this.setStatus('Playing stream (HLS.js)'))
        .catch(err => {
          this.setError(err?.message || 'Unable to start playback');
          this.setStatus('');
        });
    });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data?.fatal) {
        this.setError(`HLS fatal error: ${data.details || 'unknown'}`);
        this.setStatus('');
        if (isOverlay) {
          this.detachOverlayHls();
        } else {
          this.detachHls();
        }
      } else if (data?.details) {
        this.setError(`HLS error: ${data.details}`);
      }
    });
    hls.loadSource(this.src || '');
    hls.attachMedia(video);
    if (isOverlay) {
      this.overlayHls = hls;
    } else {
      this.hls = hls;
    }
    return true;
  }

  private playNative(video: HTMLVideoElement, okStatus: string): void {
    video.src = this.src || '';
    if (!this.autoplay) {
      this.setStatus('Ready');
      return;
    }
    video
      .play()
      .then(() => this.setStatus(okStatus))
      .catch(err => {
        this.setError(err?.message || 'Unable to start playback (native)');
        this.setStatus('');
      });
  }

  private detachHls(): void {
    if (this.hls) {
      this.hls.destroy();
      this.hls = undefined;
    }
  }

  private detachOverlayHls(): void {
    if (this.overlayHls) {
      this.overlayHls.destroy();
      this.overlayHls = undefined;
    }
  }

  private isSafari(): boolean {
    if (typeof navigator === 'undefined') return false;
    const ua = navigator.userAgent;
    const isIos = /iP(ad|hone|od)/.test(ua);
    const isSafari = /Safari/.test(ua) && !/Chrome|CriOS|Chromium|Edg/.test(ua);
    return isSafari || isIos;
  }

  private setStatus(val: string): void {
    this.status = val;
    this.statusChange.emit(val);
  }

  private setError(val: string): void {
    this.error = val;
    this.errorChange.emit(val);
  }
}
