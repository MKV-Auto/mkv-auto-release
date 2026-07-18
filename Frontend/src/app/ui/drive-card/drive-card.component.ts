import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { IconComponent } from '../icon/icon.component';
import { PillComponent } from '../pill/pill.component';
import { ProgressRingComponent } from '../progress-ring/progress-ring.component';

export type DriveState =
  | 'idle'
  | 'scanning'
  | 'awaiting_user_choice'
  | 'exploratory_ripping'
  | 'awaiting_segment_order'
  | 'matching_playlists'
  | 'canonical_ripping';

export interface DriveCardData {
  id: string;
  title: string;
  meta: string;
  mount: string;
  state: DriveState;
  progress?: number;
  attention?: boolean;
}

const SUB_LABELS: Record<DriveState, (progress: number) => string> = {
  idle: () => 'Ready',
  scanning: () => 'Scanning...',
  awaiting_user_choice: () => 'Choose how to rip',
  exploratory_ripping: (p) => `Exploring disc... ${p}%`,
  awaiting_segment_order: () => 'Order segments to find main feature',
  matching_playlists: () => 'Matching...',
  canonical_ripping: (p) => `Ripping main feature... ${p}%`,
};

@Component({
  selector: 'ui-drive-card',
  standalone: true,
  imports: [IconComponent, PillComponent, ProgressRingComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="ui-drivecard"
      [class.ui-drivecard--active]="active"
      [attr.data-state]="drive.state"
      (click)="clicked.emit(drive)"
    >
      @if (drive.attention) {
        <span class="ui-drivecard__dot" aria-hidden="true"></span>
      }
      @if (showRing()) {
        <span class="ui-drivecard__indicator">
          <ui-progress-ring [value]="drive.progress ?? 0" [size]="22"></ui-progress-ring>
        </span>
      } @else if (showSpinner()) {
        <span class="ui-drivecard__indicator ui-drivecard__indicator--spin">
          <ui-icon name="spinner" [size]="16"></ui-icon>
        </span>
      }

      <div class="ui-drivecard__head">
        <div class="ui-drivecard__eyebrow">
          <span class="ui-drivecard__eyebrow-icon"><ui-icon name="disc" [size]="14"></ui-icon></span>
          <span>Now Reading</span>
        </div>
        <div class="ui-drivecard__title">{{ drive.title }}</div>
        <div class="ui-drivecard__meta">{{ drive.meta }}</div>
      </div>

      <div class="ui-drivecard__foot">
        <div class="ui-drivecard__mount">{{ drive.mount }}</div>
        @if (attentionTone()) {
          <ui-pill [tone]="attentionTone()!">{{ attentionLabel() }}</ui-pill>
        } @else {
          <span class="ui-drivecard__sub">{{ subLabel() }}</span>
        }
      </div>
    </button>
  `,
  styles: [`
    .ui-drivecard {
      all: unset;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      position: relative;
      padding: 14px 16px;
      box-sizing: border-box;
      width: 280px;
      aspect-ratio: 2 / 1;
      flex-shrink: 0;

      background: var(--ui-card-bg, rgba(255,255,255,0.04));
      border: 1px solid var(--ui-card-border, rgba(255,255,255,0.10));
      border-radius: var(--ui-card-radius, 14px);
      backdrop-filter: blur(var(--ui-card-blur, 12px));
      -webkit-backdrop-filter: blur(var(--ui-card-blur, 12px));
      box-shadow: 0 4px 16px rgba(0,0,0,0.18);
      transition: transform 200ms ease, background 200ms ease, border-color 200ms ease;
    }
    .ui-drivecard:hover { transform: translateY(-2px); }
    .ui-drivecard:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99,102,241,0.45);
    }
    .ui-drivecard--active {
      background: rgba(255,255,255,0.08);
      border-color: var(--ui-accent, #6366f1);
      box-shadow: var(--ui-accent-ring, 0 0 0 2px rgba(99,102,241,0.3));
    }

    .ui-drivecard__dot {
      position: absolute; top: 10px; left: 10px;
      width: 8px; height: 8px; border-radius: 999px;
      background: #fcd34d;
      box-shadow: 0 0 0 4px rgba(251,191,36,0.18);
      animation: ui-drivecard-pulse 1.4s ease-in-out infinite;
    }
    @keyframes ui-drivecard-pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
    .ui-drivecard__indicator { position: absolute; top: 10px; right: 10px; color: #60a5fa; display: inline-flex; }
    .ui-drivecard__indicator--spin { animation: ui-drivecard-spin 1s linear infinite; }
    @keyframes ui-drivecard-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

    .ui-drivecard__head { width: 100%; min-width: 0; text-align: left; }
    .ui-drivecard__eyebrow {
      display: flex; align-items: center; gap: 6px; margin-bottom: 8px;
      font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
      text-transform: uppercase; color: rgba(255,255,255,0.7);
    }
    .ui-drivecard__eyebrow-icon { color: #60a5fa; display: inline-flex; }
    .ui-drivecard__title {
      font-size: 15px; font-weight: 600; color: #fff; line-height: 1.3;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .ui-drivecard__meta { font-size: 12px; color: rgba(255,255,255,0.6); margin-top: 2px; }

    .ui-drivecard__foot {
      margin-top: 10px; padding-top: 8px;
      border-top: 1px solid rgba(255,255,255,0.08);
      width: 100%;
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
    }
    .ui-drivecard__mount {
      font-family: var(--ui-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
      font-size: 11px; color: rgba(255,255,255,0.5);
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
    }
    .ui-drivecard__sub { font-size: 11px; color: #60a5fa; flex-shrink: 0; font-weight: 500; }
  `],
})
export class DriveCardComponent {
  @Input({ required: true }) drive!: DriveCardData;
  @Input() active = false;
  @Output() clicked = new EventEmitter<DriveCardData>();

  showRing(): boolean {
    return this.drive.state === 'exploratory_ripping' || this.drive.state === 'canonical_ripping';
  }
  showSpinner(): boolean {
    return this.drive.state === 'matching_playlists' || this.drive.state === 'scanning';
  }
  subLabel(): string {
    const fn = SUB_LABELS[this.drive.state] ?? SUB_LABELS.idle;
    return fn(this.drive.progress ?? 0);
  }
  attentionTone(): 'amber' | null {
    if (this.drive.state === 'awaiting_user_choice') return 'amber';
    if (this.drive.state === 'awaiting_segment_order') return 'amber';
    return null;
  }
  attentionLabel(): string {
    if (this.drive.state === 'awaiting_user_choice') return 'Action needed';
    if (this.drive.state === 'awaiting_segment_order') return 'Order segments';
    return '';
  }
}
