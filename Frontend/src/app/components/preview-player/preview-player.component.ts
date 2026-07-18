import { CommonModule } from '@angular/common';
import { Component, ElementRef, Input, OnChanges, OnDestroy, SimpleChanges, ViewChild } from '@angular/core';
import Hls from 'hls.js';

@Component({
  selector: 'app-preview-player',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="w-40 grid gap-1" [class.w-full]="inline" [class.h-[72px]]="inline">
      <video
        #video
        class="w-full rounded-lg bg-[#0f172a]"
        [class.h-[72px]]="inline"
        [class.object-cover]="inline"
        [controls]="!inline"
        [muted]="inline"
        playsinline
        (click)="onInlinePlay()"
        (error)="onError($event)"
        (playing)="onPlaying()"
        (waiting)="onWaiting()">
        Your browser does not support HTML5 video.
      </video>
      <div class="text-xs text-slate-400" *ngIf="status">{{ status }}</div>
      <div class="text-xs text-red-300" *ngIf="error">{{ error }}</div>
    </div>
  `,
})
export class PreviewPlayerComponent implements OnChanges, OnDestroy {
  @Input() src: string | null = null;
  @Input() inline = false;
  @ViewChild('video') video?: ElementRef<HTMLVideoElement>;

  private hls?: Hls;
  status = '';
  error = '';

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['src']) {
      this.load();
    }
  }

  ngOnDestroy(): void {
    this.detach();
  }

  private load(): void {
    this.status = '';
    this.error = '';
    const video = this.video?.nativeElement;
    if (!video || !this.src) return;
    // Keep inline previews muted so browsers allow autoplay without a user gesture.
    video.muted = this.inline;
    video.autoplay = this.inline;
    this.detach();
    video.pause();
    video.removeAttribute('src');
    video.load();

    const isSafari = this.isSafari();
    const canNative = video.canPlayType('application/vnd.apple.mpegurl') !== '';
    if (isSafari && canNative) {
      this.playNative(video);
      return;
    }
    if (Hls.isSupported()) {
      this.hls = new Hls({ enableWorker: true });
      this.hls.on(Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch(err => {
          this.error = err?.message || 'Playback failed';
        });
      });
      this.hls.on(Hls.Events.ERROR, (_, data) => {
        if (data?.fatal) {
          this.error = `HLS error: ${data.details || 'fatal'}`;
          this.status = '';
          this.detach();
        }
      });
      this.hls.loadSource(this.src);
      this.hls.attachMedia(video);
      this.status = 'Loading…';
      return;
    }
    this.playNative(video);
  }

  private playNative(video: HTMLVideoElement): void {
    video.src = this.src || '';
    video.play().catch(err => {
      this.error = err?.message || 'Playback failed';
    });
  }

  private detach(): void {
    if (this.hls) {
      this.hls.destroy();
      this.hls = undefined;
    }
  }

  onInlinePlay(): void {
    if (!this.inline) return;
    // Restart playback on click to recover from autoplay blocking.
    this.load();
  }

  private isSafari(): boolean {
    if (typeof navigator === 'undefined') return false;
    const ua = navigator.userAgent;
    return /Safari/.test(ua) && !/Chrome|CriOS|Chromium|Edg/.test(ua);
  }

  onError(event: Event): void {
    const target = event.target as HTMLVideoElement | null;
    const mediaError = target?.error?.message || target?.error?.code;
    this.error = `Playback error${mediaError ? `: ${mediaError}` : ''}`;
  }

  onPlaying(): void {
    this.status = '';
  }

  onWaiting(): void {
    this.status = 'Buffering…';
  }
}
