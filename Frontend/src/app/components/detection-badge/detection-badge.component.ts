import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import type { TitleInfo } from '../../services/drive.service';

/** Amber badge showing padding/junk detection reason and metrics tooltip. */
@Component({
  selector: 'app-detection-badge',
  standalone: true,
  imports: [CommonModule],
  template: `
    <ng-container *ngIf="title?.detection_warning">
      <div class="detection-badge-wrap">
        <button
          type="button"
          class="detection-badge"
          (mouseenter)="showTooltip = true"
          (mouseleave)="showTooltip = false"
          [attr.aria-label]="'Detection: ' + detectionReason"
          [title]="detectionReason"
        >
          <span class="detection-icon" [innerHTML]="detectionIconSvg"></span>
          <span class="detection-reason">{{ detectionReason }}</span>
          <svg class="info-icon" xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="10"></circle>
            <path d="M12 16v-4"></path>
            <path d="M12 8h.01"></path>
          </svg>
        </button>
        <div *ngIf="showTooltip" class="detection-tooltip" (mouseenter)="showTooltip = true" (mouseleave)="showTooltip = false">
          <div class="detection-tooltip-header">Detection Metrics</div>
          <div *ngFor="let m of detectionMetrics" class="detection-tooltip-line">{{ m }}</div>
          <div *ngIf="!detectionMetrics.length" class="detection-tooltip-line muted">No metrics available</div>
        </div>
      </div>
    </ng-container>
  `,
  styles: [
    `
      :host {
        display: inline-block;
      }
      .detection-badge-wrap {
        position: relative;
        display: inline-flex;
      }
      .detection-badge {
        display: inline-flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.375rem 0.75rem;
        border-radius: 0.5rem;
        font-size: 0.75rem;
        font-weight: 500;
        cursor: pointer;
        border: 1px solid rgba(251, 191, 36, 0.3);
        background: rgba(251, 191, 36, 0.1);
        color: rgb(253 224 71);
        transition: background 0.15s;
      }
      .detection-badge:hover {
        background: rgba(251, 191, 36, 0.15);
      }
      .detection-icon {
        display: inline-flex;
        flex-shrink: 0;
        width: 0.875rem;
        height: 0.875rem;
      }
      .detection-icon ::ng-deep svg {
        width: 100%;
        height: 100%;
        stroke: rgb(253 224 71);
      }
      .detection-reason {
        flex: 0 1 auto;
      }
      .info-icon {
        flex-shrink: 0;
        opacity: 0.6;
      }
      .detection-tooltip {
        position: absolute;
        z-index: 50;
        left: 0;
        top: 100%;
        margin-top: 0.5rem;
        min-width: 220px;
        padding: 0.75rem;
        border-radius: 0.5rem;
        border: 2px solid rgba(251, 191, 36, 0.6);
        background: #19202e;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.9);
        font-size: 0.75rem;
      }
      .detection-tooltip-header {
        font-weight: 600;
        color: rgb(253 224 71);
        margin-bottom: 0.5rem;
        display: flex;
        align-items: center;
        gap: 0.375rem;
      }
      .detection-tooltip-line {
        color: #fff;
      }
      .detection-tooltip-line.muted {
        color: rgba(255, 255, 255, 0.5);
        font-style: italic;
      }
    `,
  ],
})
export class DetectionBadgeComponent {
  @Input() title: TitleInfo | null = null;
  showTooltip = false;

  get detectionReason(): string {
    const t = this.title;
    const flags = t?.detection_flags;
    if (!t?.detection_warning) return 'Suspicious';
    if (flags?.freeze_detected && (flags.freeze_duration ?? 0) > 0) return 'Freeze detected';
    if ((flags?.black_frame_duration ?? 0) > 0) return 'Black frames';
    if ((flags?.silence_duration ?? 0) > 0) return 'Silence';
    if (flags?.signal_entropy != null && flags.signal_entropy < 0.5) return 'Low signal variance';
    if (flags?.is_suspicious_bitrate) return 'Low bitrate';
    return 'Likely padding/junk';
  }

  get detectionIconSvg(): string {
    const flags = this.title?.detection_flags;
    if (!flags) return this.svgEyeOff;
    if (flags.freeze_detected && (flags.freeze_duration ?? 0) > 0) return this.svgPause;
    if ((flags.black_frame_duration ?? 0) > 0) return this.svgFilm;
    if ((flags.silence_duration ?? 0) > 0) return this.svgVolumeX;
    if (flags.signal_entropy != null && flags.signal_entropy < 0.5) return this.svgActivity;
    if (flags.is_suspicious_bitrate) return this.svgZap;
    return this.svgEyeOff;
  }

  get detectionMetrics(): string[] {
    const t = this.title;
    const flags = t?.detection_flags;
    const lines: string[] = [];
    if (t?.duration != null) {
      const m = Math.floor(t.duration / 60);
      const s = Math.round(t.duration % 60);
      lines.push(`Duration: ${m}m ${s}s`);
    }
    if (flags?.bitrate_mbps != null) {
      const susp = flags.is_suspicious_bitrate ? ' (suspicious)' : '';
      lines.push(`Bitrate: ${flags.bitrate_mbps.toFixed(1)} Mbps${susp}`);
    }
    if (flags && (flags.black_frame_duration ?? 0) > 0) {
      lines.push(`Black frames: ${flags.black_frame_duration}s`);
    }
    if (flags && (flags.silence_duration ?? 0) > 0) {
      lines.push(`Silence: ${flags.silence_duration}s`);
    }
    if (flags?.freeze_detected && (flags?.freeze_duration ?? 0) > 0) {
      lines.push(`Freeze: ${flags?.freeze_duration}s`);
    }
    if (flags?.signal_entropy != null) {
      lines.push(`Signal entropy: ${flags.signal_entropy.toFixed(2)}`);
    }
    if (t?.detection_confidence != null) {
      lines.push(`Confidence: ${(t.detection_confidence * 100).toFixed(0)}%`);
    }
    return lines;
  }

  private get svgPause(): string {
    return `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>`;
  }
  private get svgFilm(): string {
    return `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="2" y1="7" x2="7" y2="7"/><line x1="2" y1="17" x2="7" y2="17"/><line x1="17" y1="17" x2="22" y2="17"/><line x1="17" y1="7" x2="22" y2="7"/></svg>`;
  }
  private get svgVolumeX(): string {
    return `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="16" y2="16"/><line x1="16" y1="9" x2="23" y2="16"/></svg>`;
  }
  private get svgActivity(): string {
    return `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>`;
  }
  private get svgZap(): string {
    return `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>`;
  }
  private get svgEyeOff(): string {
    return `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/><path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/><path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 22 20-20"/></svg>`;
  }
}
