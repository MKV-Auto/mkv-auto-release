import { Component, OnInit, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SystemService, TransferCapabilities, TransferConfigSummary } from '../../services/system.service';
import { BtnComponent } from '../../ui/btn/btn.component';
import { IconComponent } from '../../ui/icon/icon.component';
import type { IconName } from '../../ui/icon/icon-paths';
import { PillComponent, PillTone } from '../../ui/pill/pill.component';
import { EmptyStateComponent } from '../../ui/empty-state/empty-state.component';

const MODE_TONE: Record<string, PillTone> = {
  local: 'blue',
  rsync: 'purple',
  smb: 'cyan',
  nfs: 'emerald',
};

const HEALTH_TONE: Record<string, PillTone> = {
  healthy: 'emerald',
  degraded: 'amber',
  unhealthy: 'red',
};

const HEALTH_LABEL: Record<string, string> = {
  healthy: 'Healthy',
  degraded: 'Degraded',
  unhealthy: 'Unhealthy',
};

/** Capability-pill semantics (#635 commit C). Reflects the strategy the
 * transfer worker will pick given the config's current
 * `conflict_resolution` and the probed capabilities. */
type CapabilityPillState =
  | { kind: 'not_probed' }
  | { kind: 'probe_error'; error: string }
  | { kind: 'direct'; label: string; tone: PillTone; icon: IconName }
  | { kind: 'delete_then_copy'; label: string; tone: PillTone; icon: IconName }
  | { kind: 'rename'; label: string; tone: PillTone; icon: IconName }
  | { kind: 'unavailable'; label: string; tone: PillTone; icon: IconName };

@Component({
  selector: 'app-transfer-config-list',
  standalone: true,
  imports: [CommonModule, BtnComponent, IconComponent, PillComponent, EmptyStateComponent],
  template: `
    <div class="tcfg-list">
      <!-- Header -->
      <div class="tcfg-list__header">
        <div>
          <h4 class="tcfg-list__title">Transfer configurations</h4>
          <p class="tcfg-list__subtitle">Manage your file transfer destinations.</p>
        </div>
        <ui-btn variant="primary" (click)="onCreate.emit()">
          <ui-icon uiBtnIcon name="plus" [size]="13"></ui-icon>
          New config
        </ui-btn>
      </div>

      <!-- Loading -->
      <div *ngIf="loading" class="tcfg-list__loading">
        <span class="tcfg-list__spin"><ui-icon name="spinner" [size]="20"></ui-icon></span>
      </div>

      <!-- Empty -->
      <ui-empty-state
        *ngIf="!loading && configs.length === 0"
        title="No transfer configs yet"
        body="Create your first transfer configuration to start moving files.">
        <ui-icon uiEmptyIcon name="server" [size]="20"></ui-icon>
        <ui-btn variant="primary" (click)="onCreate.emit()">
          <ui-icon uiBtnIcon name="plus" [size]="13"></ui-icon>
          Create config
        </ui-btn>
      </ui-empty-state>

      <!-- Cards -->
      <div *ngIf="!loading && configs.length > 0" class="tcfg-list__items">
        <div
          *ngFor="let config of configs"
          class="tcfg-card"
          [class.is-active]="config.is_active">
          <div class="tcfg-card__main">
            <div class="tcfg-card__head">
              <h5 class="tcfg-card__name">{{ config.name }}</h5>
              <ui-pill *ngIf="config.is_active" tone="indigo">Active</ui-pill>
              <ui-pill [tone]="getModeTone(config.mode)">{{ config.mode.toUpperCase() }}</ui-pill>
            </div>

            <div class="tcfg-card__path" [attr.title]="config.transfer_dir">
              {{ config.transfer_dir }}
            </div>

            <div class="tcfg-card__options">
              <ui-pill *ngIf="config.conflict_resolution" tone="purple">
                <ui-icon uiPillIcon name="refresh" [size]="11"></ui-icon>
                {{ capitalize(config.conflict_resolution) }}
              </ui-pill>
            </div>

            <ui-pill [tone]="getHealthTone(config.health_status)">
              <ui-icon
                uiPillIcon
                [name]="config.health_status === 'healthy' ? 'check' : 'alert'"
                [size]="11">
              </ui-icon>
              {{ getHealthLabel(config.health_status) }}
            </ui-pill>

            <!-- #635 commit C: destination capability pill. Shows the
                 strategy the transfer worker will pick for the current
                 conflict_resolution based on probed capabilities. A "Probe"
                 button surfaces when no probe has ever run for the config. -->
            <ng-container *ngIf="getCapabilityPill(config) as cap">
              <ui-pill *ngIf="cap.kind !== 'not_probed'" [tone]="cap.kind === 'probe_error' ? 'red' : cap.tone" [attr.title]="cap.kind === 'probe_error' ? cap.error : null">
                <ui-icon uiPillIcon [name]="cap.kind === 'probe_error' ? 'alert' : cap.icon" [size]="11"></ui-icon>
                {{ cap.kind === 'probe_error' ? 'Probe failed' : cap.label }}
              </ui-pill>
              <ui-btn *ngIf="cap.kind === 'not_probed'" variant="ghost" (click)="onProbe.emit(config.id)">
                <ui-icon uiBtnIcon name="refresh" [size]="11"></ui-icon>
                Probe destination
              </ui-btn>
            </ng-container>
          </div>

          <div class="tcfg-card__actions">
            <button
              type="button"
              class="tcfg-icon-btn"
              title="Check health"
              (click)="onHealthCheck.emit(config.id)">
              <ui-icon name="refresh" [size]="14"></ui-icon>
            </button>
            <button
              type="button"
              class="tcfg-icon-btn"
              title="Edit"
              (click)="onEdit.emit(config.id)">
              <ui-icon name="edit" [size]="14"></ui-icon>
            </button>
            <button
              type="button"
              class="tcfg-icon-btn tcfg-icon-btn--success"
              *ngIf="!config.is_active"
              title="Set as active"
              (click)="onActivate.emit(config.id)">
              <ui-icon name="check" [size]="14"></ui-icon>
            </button>
            <button
              type="button"
              class="tcfg-icon-btn tcfg-icon-btn--danger"
              title="Delete"
              (click)="onDelete.emit(config.id)">
              <ui-icon name="trash" [size]="14"></ui-icon>
            </button>
          </div>
        </div>
      </div>
    </div>
  `,
  styles: [`
    .tcfg-list {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .tcfg-list__header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .tcfg-list__title {
      font-size: 15px;
      font-weight: 700;
      color: #fff;
      margin: 0;
    }
    .tcfg-list__subtitle {
      font-size: 13px;
      color: rgba(255, 255, 255, 0.6);
      margin: 2px 0 0;
    }
    .tcfg-list__loading {
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 40px 0;
      color: rgba(255, 255, 255, 0.4);
    }
    .tcfg-list__spin {
      display: inline-flex;
      animation: tcfg-spin 1s linear infinite;
    }
    @keyframes tcfg-spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    @media (prefers-reduced-motion: reduce) {
      .tcfg-list__spin { animation: none; }
    }

    .tcfg-list__items {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    /* Single transfer config card. Active card gets the indigo accent
     * border + ring, matching the rest of the design system's selected
     * surfaces (drive card, label-row, threshold modal action). */
    .tcfg-card {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      padding: 14px 16px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid rgba(255, 255, 255, 0.08);
      transition: background 150ms ease, border-color 150ms ease;
    }
    .tcfg-card:hover {
      background: rgba(255, 255, 255, 0.04);
    }
    .tcfg-card.is-active {
      background: rgba(99, 102, 241, 0.06);
      border-color: rgba(99, 102, 241, 0.35);
      box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.20);
    }

    .tcfg-card__main {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .tcfg-card__head {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .tcfg-card__name {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
      color: #fff;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }
    .tcfg-card__path {
      font-family: var(--ui-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
      font-size: 11.5px;
      color: rgba(255, 255, 255, 0.5);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tcfg-card__options {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }

    /* Tight-stacked icon buttons (Health / Edit / Activate / Delete). Smaller
     * than ui-btn so 3-4 fit alongside the name. */
    .tcfg-card__actions {
      display: flex;
      align-items: flex-start;
      gap: 4px;
      flex-shrink: 0;
    }
    .tcfg-icon-btn {
      all: unset;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 32px;
      height: 32px;
      border-radius: 8px;
      color: rgba(255, 255, 255, 0.6);
      background: transparent;
      transition: background 150ms ease, color 150ms ease;
    }
    .tcfg-icon-btn:hover {
      background: rgba(255, 255, 255, 0.06);
      color: #fff;
    }
    .tcfg-icon-btn--success { color: #6ee7b7; }
    .tcfg-icon-btn--success:hover { color: #34d399; }
    .tcfg-icon-btn--danger { color: #fca5a5; }
    .tcfg-icon-btn--danger:hover { color: #f87171; }
    .tcfg-icon-btn:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.45);
    }
  `],
})
export class TransferConfigListComponent implements OnInit {
  @Input() configs: TransferConfigSummary[] = [];
  @Input() loading = false;
  @Output() onCreate = new EventEmitter<void>();
  @Output() onEdit = new EventEmitter<string>();
  @Output() onActivate = new EventEmitter<string>();
  @Output() onDelete = new EventEmitter<string>();
  @Output() onValidate = new EventEmitter<string>();
  @Output() onHealthCheck = new EventEmitter<string>();
  @Output() onProbe = new EventEmitter<string>();

  ngOnInit() {}

  getHealthTone(status: string | null | undefined): PillTone {
    return HEALTH_TONE[status ?? ''] ?? 'slate';
  }

  getHealthLabel(status: string | null | undefined): string {
    return HEALTH_LABEL[status ?? ''] ?? 'Unknown';
  }

  getModeTone(mode: string): PillTone {
    return MODE_TONE[mode] ?? 'slate';
  }

  /** Compute the capability pill state for a config's current
   * conflict_resolution + probed capabilities. Mirrors the backend's
   * resolve_transfer_plan (#635 commit B) so the user sees what's about
   * to happen before they run a transfer. */
  getCapabilityPill(config: TransferConfigSummary): CapabilityPillState {
    const caps = config.capabilities;
    if (!caps) return { kind: 'not_probed' };
    if (caps.probe_error) return { kind: 'probe_error', error: caps.probe_error };
    const intent = (config.conflict_resolution || 'overwrite').toLowerCase();
    if (intent === 'overwrite') {
      if (caps.can_overwrite_in_place) {
        return { kind: 'direct', label: 'Overwrite: direct', tone: 'emerald', icon: 'check' };
      }
      if (caps.can_delete) {
        return { kind: 'delete_then_copy', label: 'Overwrite: delete+copy', tone: 'cyan', icon: 'refresh' };
      }
      if (caps.can_rename) {
        return { kind: 'rename', label: 'Overwrite: via rename', tone: 'amber', icon: 'edit' };
      }
      return { kind: 'unavailable', label: 'Overwrite: unavailable', tone: 'red', icon: 'alert' };
    }
    if (intent === 'rename') {
      if (caps.can_rename) {
        return { kind: 'rename', label: 'Rename: supported', tone: 'emerald', icon: 'check' };
      }
      return { kind: 'unavailable', label: 'Rename: unavailable', tone: 'red', icon: 'alert' };
    }
    // skip / fail don't depend on capabilities — show a neutral confirmation.
    return { kind: 'direct', label: `${this.capitalize(intent)}: ready`, tone: 'emerald', icon: 'check' };
  }

  capitalize(str: string): string {
    return str.charAt(0).toUpperCase() + str.slice(1);
  }

  formatDate(dateStr: string): string {
    return new Date(dateStr).toLocaleString();
  }
}
