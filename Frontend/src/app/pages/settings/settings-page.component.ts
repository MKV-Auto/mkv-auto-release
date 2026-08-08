import { Component, OnInit, OnDestroy } from '@angular/core';
import { Subscription, interval } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  SystemService,
  PreviewConfig,
  DiscordConfig,
  mergeDiscordConfig,
  defaultNotificationPreferences,
  MediaServerConfig,
  DiscDbLookupConfig,
  AutoRipConfig,
  TransferConfigSummary,
  TransferConfigRecord,
  TransferConfigCreate,
  TransferConfigUpdate,
  StorageSummary,
  ImportSummary,
  DiscDbExportJob,
} from '../../services/system.service';
import { ToastService, formatHttpErrorDetail } from '../../services/toast.service';
import { WorkflowService } from '../../services/workflow.service';
import { TransferConfigListComponent } from '../../components/transfer-config/transfer-config-list.component';
import { TransferConfigFormComponent } from '../../components/transfer-config/transfer-config-form.component';
import { TransferHistoryComponent } from '../../components/transfer-config/transfer-history.component';
import { TransferHealthComponent } from '../../components/transfer-config/transfer-health.component';
import { MakeMKVUpdaterComponent } from '../../components/makemkv-updater/makemkv-updater.component';
import { UsbTopologyComponent } from '../../components/usb-topology/usb-topology.component';
import { CardComponent } from '../../ui/card/card.component';
import { IconComponent } from '../../ui/icon/icon.component';
import { BtnComponent } from '../../ui/btn/btn.component';
import { SectionHeaderComponent } from '../../ui/section-header/section-header.component';
import { PillComponent } from '../../ui/pill/pill.component';
import { ChipComponent } from '../../ui/chip/chip.component';
import { FieldComponent } from '../../ui/field/field.component';
import { CheckboxComponent } from '../../ui/checkbox/checkbox.component';
import { IconName } from '../../ui/icon/icon-paths';

type SettingsSection =
  | 'configs'
  | 'history'
  | 'preview'
  | 'notifications'
  | 'library'
  | 'copy'
  | 'makemkv'
  | 'tmdb'
  | 'export'
  | 'discdb'
  | 'help';

interface SettingsNavItem {
  id: SettingsSection;
  label: string;
  icon: IconName;
}

const SETTINGS_NAV: ReadonlyArray<SettingsNavItem> = [
  { id: 'copy', label: 'Disc handling', icon: 'disc' },
  { id: 'configs', label: 'Destinations', icon: 'server' },
  { id: 'history', label: 'Transfer history', icon: 'history' },
  { id: 'preview', label: 'Previews', icon: 'film' },
  { id: 'notifications', label: 'Notifications', icon: 'bot' },
  { id: 'library', label: 'Media server', icon: 'folder' },
  { id: 'makemkv', label: 'MakeMKV', icon: 'terminal' },
  { id: 'tmdb', label: 'TMDB', icon: 'search' },
  { id: 'export', label: 'Export / Import', icon: 'download' },
  { id: 'discdb', label: 'TheDiscDB', icon: 'upload' },
  { id: 'help', label: 'Help', icon: 'info' },
];

@Component({
  selector: 'app-settings-page',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    TransferConfigListComponent,
    TransferConfigFormComponent,
    TransferHistoryComponent,
    TransferHealthComponent,
    MakeMKVUpdaterComponent,
    UsbTopologyComponent,
    CardComponent,
    IconComponent,
    BtnComponent,
    SectionHeaderComponent,
    PillComponent,
    ChipComponent,
    FieldComponent,
    CheckboxComponent,
  ],
  template: `
    <div class="settings-page-root animate-fade-in">
      <!-- Header (template SettingsPage) -->
      <header class="settings-page-header">
        <div class="settings-page-header-inner">
          <div class="settings-page-header-icon">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/><line x1="10" y1="6" x2="10" y2="6.01"/><line x1="10" y1="18" x2="10" y2="18.01"/><line x1="14" y1="6" x2="14" y2="6.01"/><line x1="14" y1="18" x2="14" y2="18.01"/><line x1="18" y1="6" x2="18.01" y2="6"/><line x1="18" y1="18" x2="18.01" y2="18"/></svg>
          </div>
          <div>
            <h1>Settings</h1>
            <p>Configure your media processing pipeline</p>
          </div>
        </div>
      </header>

      <div class="settings-page-content">
      <!-- Storage card (template StorageCard) -->
      <div class="settings-storage-card">
        <div class="settings-storage-card-header">
          <div class="settings-storage-card-title-row">
            <div class="settings-storage-card-icon">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2" ry="2"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="8" y1="8" x2="8.01" y2="8"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="8" x2="16.01" y2="8"/><line x1="16" y1="16" x2="16.01" y2="16"/></svg>
            </div>
            <div>
              <h3>Storage Usage</h3>
              <p class="settings-storage-card-subtitle">Data root and transfer root capacity</p>
            </div>
          </div>
          <button type="button" class="settings-storage-refresh-btn" [class.loading]="storageLoading" (click)="loadStorage()" [disabled]="storageLoading" aria-label="Refresh storage">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/></svg>
          </button>
        </div>
        <div *ngIf="storageError" class="settings-storage-error">{{ storageError }}</div>
        <div *ngIf="storageSummary" class="settings-storage-grid">
          <div class="settings-storage-block">
            <label>Data Root</label>
            <div class="settings-storage-path">{{ storageSummary.data_root.path }}</div>
            <div class="settings-storage-values">
              <span class="settings-storage-free">{{ formatBytes(storageSummary.data_root.free) }}</span>
              <span class="settings-storage-total">/ {{ formatBytes(storageSummary.data_root.total) }}</span>
            </div>
            <div class="settings-storage-bar">
              <div class="settings-storage-bar-fill" [style.width.%]="storageSummary.data_root.total ? (100 - (storageSummary.data_root.free / storageSummary.data_root.total * 100)) : 0" [style.background]="getStorageBarColor(storageSummary.data_root.free, storageSummary.data_root.total)"></div>
            </div>
            <div class="settings-storage-pct">{{ storageSummary.data_root.total ? (storageSummary.data_root.free / storageSummary.data_root.total * 100 | number:'1.1-1') : 0 }}% free</div>
          </div>
          <div class="settings-storage-block">
            <label>Transfer Root</label>
            <div class="settings-storage-path">{{ storageSummary.transfer_root.path }}</div>
            <div class="settings-storage-values">
              <span class="settings-storage-free">{{ formatBytes(storageSummary.transfer_root.free) }}</span>
              <span class="settings-storage-total">/ {{ formatBytes(storageSummary.transfer_root.total) }}</span>
            </div>
            <div class="settings-storage-bar">
              <div class="settings-storage-bar-fill" [style.width.%]="storageSummary.transfer_root.total ? (100 - (storageSummary.transfer_root.free / storageSummary.transfer_root.total * 100)) : 0" [style.background]="getStorageBarColor(storageSummary.transfer_root.free, storageSummary.transfer_root.total)"></div>
            </div>
            <div class="settings-storage-pct">{{ storageSummary.transfer_root.total ? (storageSummary.transfer_root.free / storageSummary.transfer_root.total * 100 | number:'1.1-1') : 0 }}% free</div>
          </div>
        </div>
        <div *ngIf="!storageSummary && !storageError && !storageLoading" class="settings-storage-pct">Click Refresh to load storage information</div>
        <div *ngIf="storageLoading && !storageSummary" class="settings-storage-loading">
          <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="spinner" style="animation: spin 0.8s linear infinite;"><circle cx="12" cy="12" r="10" opacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10"/></svg>
        </div>
      </div>

      <!-- 2-column shell: left sidebar nav + right content panel.
           Mirrors the prototype's settings layout (research/MKV Auto UI/settings.jsx).
           Storage card above stays untouched. -->
      <div class="settings-shell">
        <ui-card class="settings-shell__sidebar">
          <nav class="settings-shell__nav" aria-label="Settings sections">
            <button
              *ngFor="let s of nav"
              type="button"
              class="settings-shell__nav-btn"
              [class.is-active]="activeTab === s.id"
              [attr.aria-current]="activeTab === s.id ? 'page' : null"
              (click)="selectTab(s.id)">
              <ui-icon [name]="s.icon" [size]="14"></ui-icon>
              {{ s.label }}
            </button>
          </nav>
        </ui-card>

        <div class="settings-shell__content">
        <!-- Transfer Configs Tab -->
        <div *ngIf="activeTab === 'configs'" class="settings-section-stack">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Transfer destinations"
                subtitle="Where processed files go after post-processing. The active config wins.">
                <ui-icon uiSecIcon name="server" [size]="14"></ui-icon>
              </ui-section-header>

              <div *ngIf="!editingConfig && !creatingConfig" class="settings-help-callout">
                <ui-icon name="info" [size]="13"></ui-icon>
                <p>
                  <strong>Local</strong> copies on this machine (or a NAS export mounted into the container) ·
                  <strong>SMB</strong> Windows/Samba share.
                </p>
              </div>

              <app-transfer-config-list
                *ngIf="!editingConfig && !creatingConfig"
                [configs]="transferConfigs"
                [loading]="loadingConfigs"
                (onCreate)="createConfig()"
                (onEdit)="editConfig($event)"
                (onActivate)="activateConfig($event)"
                (onDelete)="deleteConfig($event)"
                (onValidate)="validateConfig($event)"
                (onHealthCheck)="checkHealth($event)"
                (onProbe)="probeCapabilities($event)">
              </app-transfer-config-list>

              <app-transfer-config-form
                *ngIf="editingConfig || creatingConfig"
                [config]="editingConfig"
                [formError]="transferConfigFormError"
                (onSave)="saveConfig($event)"
                (onCancel)="cancelEdit()">
              </app-transfer-config-form>
            </div>
          </ui-card>

          <ui-card *ngIf="selectedConfigId && !editingConfig && !creatingConfig">
            <div class="settings-section-body">
              <ui-section-header
                title="Health check"
                subtitle="Last reachability + speed probe for the selected destination.">
                <ui-icon uiSecIcon name="check-circle" [size]="14"></ui-icon>
              </ui-section-header>
              <app-transfer-health [configId]="selectedConfigId"></app-transfer-health>
            </div>
          </ui-card>
        </div>

        <!-- Transfer History Tab -->
        <div *ngIf="activeTab === 'history'">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Transfer history"
                subtitle="Past transfers across all destinations. Filter by config to scope.">
                <ui-icon uiSecIcon name="history" [size]="14"></ui-icon>
              </ui-section-header>
              <app-transfer-history [configs]="transferConfigs"></app-transfer-history>
            </div>
          </ui-card>
        </div>

        <!-- Preview Settings Tab -->
        <div *ngIf="activeTab === 'preview'">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Previews"
                subtitle="HLS preview clip generation — duration and parallelism.">
                <ui-icon uiSecIcon name="film" [size]="14"></ui-icon>
              </ui-section-header>

              <div class="settings-alert settings-alert--error" *ngIf="previewError">
                <ui-icon name="alert" [size]="14"></ui-icon>
                <span>{{ previewError }}</span>
              </div>
              <div class="settings-alert settings-alert--success" *ngIf="previewSaved">
                <ui-icon name="check-circle" [size]="14"></ui-icon>
                <span>{{ previewSaved }}</span>
              </div>

              <ui-field label="Preview duration (seconds)" hint="Length of each HLS preview clip (30–300s).">
                <input class="settings-input" type="number" min="10" max="600" [(ngModel)]="preview.duration_seconds"
                       [disabled]="isEnvManaged('preview_duration_seconds')"
                       [title]="isEnvManaged('preview_duration_seconds') ? envManagedHint : null">
                <div class="settings-env-note" *ngIf="isEnvManaged('preview_duration_seconds')">
                  <ui-icon name="info" [size]="13"></ui-icon>
                  <span>{{ envManagedHint }}</span>
                </div>
              </ui-field>

              <ui-field label="Max parallel previews" hint="Concurrent preview generations (1–{{ preview.max_parallel_ceiling || 1 }}).">
                <input class="settings-input" type="range" min="1" [max]="preview.max_parallel_ceiling || 1" [(ngModel)]="preview.max_parallel"
                       [disabled]="isEnvManaged('preview_max_parallel')"
                       [title]="isEnvManaged('preview_max_parallel') ? envManagedHint : null">
                <div class="settings-range-labels">
                  <span>1</span>
                  <span>{{ preview.max_parallel }} concurrent</span>
                  <span>{{ preview.max_parallel_ceiling || 1 }}</span>
                </div>
                <div class="settings-env-note" *ngIf="isEnvManaged('preview_max_parallel')">
                  <ui-icon name="info" [size]="13"></ui-icon>
                  <span>{{ envManagedHint }}</span>
                </div>
              </ui-field>

              <div class="settings-help-callout">
                <ui-icon name="info" [size]="13"></ui-icon>
                <p>
                  Previews come from the first minutes of each title.
                  Higher parallel counts use more CPU; longer previews take
                  more wall time to generate.
                </p>
              </div>

              <div class="settings-actions">
                <ui-btn variant="secondary" (click)="loadPreviewConfig()">
                  <ui-icon uiBtnIcon name="refresh" [size]="13"></ui-icon>
                  Reset to defaults
                </ui-btn>
                <ui-btn variant="primary" (click)="savePreviewConfig()" [loading]="previewSaving">
                  Save settings
                </ui-btn>
              </div>
            </div>
          </ui-card>
        </div>

        <!-- Notifications Tab: preferences + Discord webhook -->
        <div *ngIf="activeTab === 'notifications'" class="settings-section-stack">
          <!-- Notification preferences -->
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Notification preferences"
                subtitle="Pick the channel (in-app toast / Discord) per notification type.">
                <ui-icon uiSecIcon name="bot" [size]="14"></ui-icon>
              </ui-section-header>

              <!-- #609 follow-up: order is Errors → Action required → Informative.
                   Errors and Action required are the high-priority categories the
                   user genuinely needs; Informative is opt-in fine-tuning and
                   belongs at the bottom so it doesn't crowd the surface. -->

              <!-- Errors -->
              <div class="settings-notif-block">
                <div class="settings-notif-block__head">
                  <div class="settings-notif-block__title">Errors</div>
                  <p class="settings-notif-block__hint">
                    Rip, transfer, and disk space failures. Pick channels
                    (you can turn both off).
                  </p>
                </div>
                <div class="settings-checkbox-row-group">
                  <ui-checkbox [(ngModel)]="discord.notification_preferences!.errors.in_app" ariaLabel="Errors — in-app">In-app</ui-checkbox>
                  <span [class.is-disabled]="!discordConfigured" [title]="!discordConfigured ? discordDisabledHint : null">
                    <ui-checkbox
                      [(ngModel)]="discord.notification_preferences!.errors.discord"
                      [disabled]="!discordConfigured"
                      ariaLabel="Errors — Discord">Discord</ui-checkbox>
                  </span>
                </div>
              </div>

              <!-- Action required -->
              <div class="settings-notif-block">
                <div class="settings-notif-block__head">
                  <div class="settings-notif-block__title">Action required</div>
                  <p class="settings-notif-block__hint">
                    Labeling, transfer handoffs, and similar. Pick channels
                    (you can turn both off).
                  </p>
                </div>
                <div class="settings-checkbox-row-group">
                  <ui-checkbox [(ngModel)]="discord.notification_preferences!.action_required.in_app" ariaLabel="Action-required — in-app">In-app</ui-checkbox>
                  <span [class.is-disabled]="!discordConfigured" [title]="!discordConfigured ? discordDisabledHint : null">
                    <ui-checkbox
                      [(ngModel)]="discord.notification_preferences!.action_required.discord"
                      [disabled]="!discordConfigured"
                      ariaLabel="Action-required — Discord">Discord</ui-checkbox>
                  </span>
                </div>
              </div>

              <!-- Informative -->
              <div class="settings-notif-block">
                <div class="settings-notif-block__head">
                  <div class="settings-notif-block__title">Informative notifications</div>
                  <p class="settings-notif-block__hint">
                    Progress-style updates (rip start, per-title progress, previews ready, etc.) —
                    nice-to-have but low-priority. Off by default. Enable to receive them and
                    fine-tune which categories deliver to which channels.
                  </p>
                </div>
                <ui-checkbox [(ngModel)]="discord.notification_preferences!.informative.enabled" ariaLabel="Enable informative notifications">Enabled</ui-checkbox>
                <!-- #609: per-category matrix is only meaningful when the master toggle
                     is on. Previously rendered greyed-and-disabled, which read as
                     "enabled for the whole category, but they are all checked" — a
                     confusing state disagreement. Hide entirely when master is off;
                     state preserved in categories[] so values restore when re-enabled. -->
                <div class="settings-notif-table" *ngIf="discord.notification_preferences!.informative.enabled">
                  <table>
                    <thead>
                      <tr>
                        <th>Category</th>
                        <th>In-app</th>
                        <th>Discord</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr *ngFor="let key of informativeCategoryKeys">
                        <td>{{ informativeCategoryLabels[key] }}</td>
                        <td>
                          <ui-checkbox
                            [(ngModel)]="discord.notification_preferences!.informative.categories[key].in_app"
                            [ariaLabel]="informativeCategoryLabels[key] + ' — in-app'"></ui-checkbox>
                        </td>
                        <td [title]="!discordConfigured ? discordDisabledHint : null">
                          <ui-checkbox
                            [(ngModel)]="discord.notification_preferences!.informative.categories[key].discord"
                            [disabled]="!discordConfigured"
                            [ariaLabel]="informativeCategoryLabels[key] + ' — Discord'"></ui-checkbox>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </ui-card>

          <!-- Discord integration -->
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Discord integration"
                subtitle="Hook into a Discord channel to receive selected notifications.">
                <ui-icon uiSecIcon name="link" [size]="14"></ui-icon>
              </ui-section-header>

              <div class="settings-alert settings-alert--success" *ngIf="discordSaved">
                <ui-icon name="check-circle" [size]="14"></ui-icon>
                <span>{{ discordSaved }}</span>
              </div>
              <div class="settings-alert settings-alert--error" *ngIf="discordError">
                <ui-icon name="alert" [size]="14"></ui-icon>
                <span>{{ discordError }}</span>
              </div>

              <ui-field
                label="Enable Discord notifications"
                hint="Receive selected notification types in a Discord channel via webhook."
                [inline]="true">
                <button
                  uiFieldInline
                  type="button"
                  class="settings-toggle"
                  [class.is-on]="discord.enabled"
                  [attr.aria-pressed]="discord.enabled ? 'true' : 'false'"
                  [disabled]="isEnvManaged('discord.enabled')"
                  [title]="isEnvManaged('discord.enabled') ? envManagedHint : null"
                  (click)="discord.enabled = !discord.enabled">
                  <span class="settings-toggle__knob" aria-hidden="true"></span>
                </button>
              </ui-field>

              <ui-field
                label="Webhook URL"
                hint="Create a webhook in your Discord server settings (Integrations → Webhooks).">
                <input
                  type="text"
                  class="settings-input settings-input--mono"
                  [(ngModel)]="discord.webhook_url"
                  placeholder="https://discord.com/api/webhooks/…"
                  [disabled]="!discord.enabled || isEnvManaged('discord.webhook_url')"
                  [title]="isEnvManaged('discord.webhook_url') ? envManagedHint : null">
                <div class="settings-env-note" *ngIf="isEnvManaged('discord.webhook_url') || isEnvManaged('discord.enabled')">
                  <ui-icon name="info" [size]="13"></ui-icon>
                  <span>{{ envManagedHint }}</span>
                </div>
              </ui-field>

              <div class="settings-help-callout">
                <ui-icon name="info" [size]="13"></ui-icon>
                <p>
                  <strong>Setup:</strong>
                  Server settings → Integrations → Webhooks → New webhook →
                  copy URL → paste above → Save.
                  <a
                    class="settings-help-callout__link"
                    href="https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks"
                    target="_blank"
                    rel="noopener noreferrer">
                    Discord docs
                    <ui-icon name="external" [size]="11"></ui-icon>
                  </a>
                </p>
              </div>

              <div class="settings-actions">
                <ui-btn variant="secondary" (click)="loadDiscordConfig()">
                  <ui-icon uiBtnIcon name="refresh" [size]="13"></ui-icon>
                  Reset
                </ui-btn>
                <ui-btn
                  variant="primary"
                  (click)="saveDiscordConfig()"
                  [loading]="discordSaving"
                  [disabled]="(discord.enabled && !discord.webhook_url)">
                  Save settings
                </ui-btn>
              </div>
            </div>
          </ui-card>
        </div>

        <!-- Library / Media server Tab (template: LibrarySettings) -->
        <div *ngIf="activeTab === 'library'">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Media server"
                subtitle="Pick the platform — Plex or Jellyfin — for output formatting hints.">
                <ui-icon uiSecIcon name="folder" [size]="14"></ui-icon>
              </ui-section-header>

              <div class="settings-alert settings-alert--success" *ngIf="mediaServerSaved">
                <ui-icon name="check-circle" [size]="14"></ui-icon>
                <span>{{ mediaServerSaved }}</span>
              </div>
              <div class="settings-alert settings-alert--error" *ngIf="mediaServerError">
                <ui-icon name="alert" [size]="14"></ui-icon>
                <span>{{ mediaServerError }}</span>
              </div>

              <ui-field label="Media server platform" hint="Affects only output formatting; can be changed at any time.">
                <div class="settings-platform-grid">
                  <button
                    type="button"
                    class="settings-platform-card"
                    [class.is-selected]="mediaServer.media_server === 'plex'"
                    [disabled]="isEnvManaged('media_server')"
                    [title]="isEnvManaged('media_server') ? envManagedHint : null"
                    (click)="mediaServer.media_server = 'plex'">
                    <div class="settings-platform-card__head">
                      <div class="settings-platform-card__logo settings-platform-card__logo--plex">P</div>
                      <ui-icon
                        *ngIf="mediaServer.media_server === 'plex'"
                        name="check-circle"
                        [size]="18"
                        class="settings-platform-card__check">
                      </ui-icon>
                    </div>
                    <div class="settings-platform-card__name">Plex</div>
                    <p class="settings-platform-card__desc">Popular media server with rich ecosystem and apps.</p>
                  </button>
                  <button
                    type="button"
                    class="settings-platform-card"
                    [class.is-selected]="mediaServer.media_server === 'jellyfin'"
                    [disabled]="isEnvManaged('media_server')"
                    [title]="isEnvManaged('media_server') ? envManagedHint : null"
                    (click)="mediaServer.media_server = 'jellyfin'">
                    <div class="settings-platform-card__head">
                      <div class="settings-platform-card__logo settings-platform-card__logo--jellyfin">J</div>
                      <ui-icon
                        *ngIf="mediaServer.media_server === 'jellyfin'"
                        name="check-circle"
                        [size]="18"
                        class="settings-platform-card__check">
                      </ui-icon>
                    </div>
                    <div class="settings-platform-card__name">Jellyfin</div>
                    <p class="settings-platform-card__desc">Free and open-source media system with no tracking.</p>
                  </button>
                </div>
                <div class="settings-env-note" *ngIf="isEnvManaged('media_server')">
                  <ui-icon name="info" [size]="13"></ui-icon>
                  <span>{{ envManagedHint }}</span>
                </div>
              </ui-field>

              <div class="settings-help-callout">
                <ui-icon name="info" [size]="13"></ui-icon>
                <p>
                  This optimizes metadata formatting only — both Plex and
                  Jellyfin support standard movie folder structures.
                  Switching later won't touch existing files.
                </p>
              </div>

              <div class="settings-actions">
                <ui-btn variant="secondary" (click)="loadMediaServerConfig()">
                  <ui-icon uiBtnIcon name="refresh" [size]="13"></ui-icon>
                  Reset
                </ui-btn>
                <ui-btn variant="primary" (click)="saveMediaServerConfig()" [loading]="librarySaving">
                  Save settings
                </ui-btn>
              </div>
            </div>
          </ui-card>
        </div>

        <!-- Copy Settings: DiscDB lookup toggle -->
        <div *ngIf="activeTab === 'copy'">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Disc handling"
                subtitle="Online lookups, prefill behavior, and eject-on-finish.">
                <ui-icon uiSecIcon name="disc" [size]="14"></ui-icon>
              </ui-section-header>

              <div class="settings-alert settings-alert--success" *ngIf="discDbLookupSaved">
                <ui-icon name="check-circle" [size]="14"></ui-icon>
                <span>{{ discDbLookupSaved }}</span>
              </div>
              <div class="settings-alert settings-alert--error" *ngIf="discDbLookupError">
                <ui-icon name="alert" [size]="14"></ui-icon>
                <span>{{ discDbLookupError }}</span>
              </div>

              <ui-field
                label="DiscDB prefill with full labeling"
                hint="When on, DiscDB / cache hits prefill metadata but the full labeling workflow still runs (same path as a miss). When off, a hit uses the shorter copy workflow when no manual labeling is needed. Separate from Dev mode overrides."
                [inline]="true">
                <button
                  uiFieldInline
                  type="button"
                  class="settings-toggle"
                  [class.is-on]="discDbLookup.discdb_miss_workflow_with_prefill"
                  [attr.aria-pressed]="discDbLookup.discdb_miss_workflow_with_prefill ? 'true' : 'false'"
                  (click)="discDbLookup.discdb_miss_workflow_with_prefill = !discDbLookup.discdb_miss_workflow_with_prefill">
                  <span class="settings-toggle__knob" aria-hidden="true"></span>
                </button>
              </ui-field>

              <ui-field
                label="Eject disc on finish"
                hint="When on, the tray ejects after the job finish. Requires the eject command in the container."
                [inline]="true">
                <button
                  uiFieldInline
                  type="button"
                  class="settings-toggle"
                  [class.is-on]="discDbLookup.eject_on_finish"
                  [attr.aria-pressed]="discDbLookup.eject_on_finish ? 'true' : 'false'"
                  [disabled]="isEnvManaged('eject_on_finish')"
                  [title]="isEnvManaged('eject_on_finish') ? envManagedHint : null"
                  (click)="discDbLookup.eject_on_finish = !discDbLookup.eject_on_finish">
                  <span class="settings-toggle__knob" aria-hidden="true"></span>
                </button>
                <div class="settings-env-note" *ngIf="isEnvManaged('eject_on_finish')">
                  <ui-icon name="info" [size]="13"></ui-icon>
                  <span>{{ envManagedHint }}</span>
                </div>
              </ui-field>

              <ui-field
                label="Auto-rip on insert"
                hint="When on, copying starts automatically once a disc finishes scanning — for DiscDB hits and misses alike. For misses, link the disc to a movie or series after the copy to continue post-processing."
                [inline]="true">
                <button
                  uiFieldInline
                  type="button"
                  class="settings-toggle"
                  [class.is-on]="autoRip.auto_rip_enabled"
                  [attr.aria-pressed]="autoRip.auto_rip_enabled ? 'true' : 'false'"
                  [disabled]="isEnvManaged('auto_rip_enabled')"
                  [title]="isEnvManaged('auto_rip_enabled') ? envManagedHint : null"
                  (click)="autoRip.auto_rip_enabled = !autoRip.auto_rip_enabled">
                  <span class="settings-toggle__knob" aria-hidden="true"></span>
                </button>
                <div class="settings-env-note" *ngIf="isEnvManaged('auto_rip_enabled')">
                  <ui-icon name="info" [size]="13"></ui-icon>
                  <span>{{ envManagedHint }}</span>
                </div>
              </ui-field>

              <div class="settings-actions">
                <ui-btn variant="secondary" (click)="loadDiscDbLookupConfig()">
                  <ui-icon uiBtnIcon name="refresh" [size]="13"></ui-icon>
                  Reset
                </ui-btn>
                <ui-btn variant="primary" (click)="saveDiscDbLookupConfig()" [loading]="discDbLookupSaving">
                  Save settings
                </ui-btn>
              </div>
            </div>
          </ui-card>

          <!-- #578: USB bus topology / bandwidth contention. Multiple drives on
               a single sub-SuperSpeed bus saturate the controller during
               concurrent rips; this card surfaces the live topology so users
               can see + remediate before kicking off a multi-rip workflow. -->
          <ui-card>
            <div class="settings-section-body">
              <app-usb-topology></app-usb-topology>
            </div>
          </ui-card>
        </div>

        <!-- MakeMKV Tab -->
        <div *ngIf="activeTab === 'makemkv'">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="MakeMKV"
                subtitle="License key, beta updater, and version info.">
                <ui-icon uiSecIcon name="terminal" [size]="14"></ui-icon>
              </ui-section-header>
              <app-makemkv-updater></app-makemkv-updater>
            </div>
          </ui-card>
        </div>

        <!-- TMDB Tab (#369). Provides the API key entry that powers the
             auto-suggestion on disc load (#388) and the override search box
             on the film step (#389). The key itself is never echoed back —
             we just show whether one is configured. -->
        <div *ngIf="activeTab === 'tmdb'">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="TMDB"
                subtitle="Optional TMDB v3 API key for disc title identification and episode catalog lookups.">
                <ui-icon uiSecIcon name="search" [size]="14"></ui-icon>
              </ui-section-header>

              <div class="settings-alert settings-alert--success" *ngIf="tmdbSaved">
                <ui-icon name="check-circle" [size]="14"></ui-icon>
                <span>{{ tmdbSaved }}</span>
              </div>
              <div class="settings-alert settings-alert--error" *ngIf="tmdbError">
                <ui-icon name="alert" [size]="14"></ui-icon>
                <span>{{ tmdbError }}</span>
              </div>

              <ui-field
                label="API key status"
                hint="Configured keys are visible below — matches the MakeMKV registration field. Empty input clears the key."
                [inline]="true">
                <span uiFieldInline class="settings-pill"
                      [class.settings-pill--ok]="tmdbApiKeySet"
                      [class.settings-pill--off]="!tmdbApiKeySet">
                  {{ tmdbApiKeySet ? 'Configured' : 'Not configured' }}
                </span>
              </ui-field>

              <ui-field
                label="TMDB v3 API key"
                hint="Paste your TMDB v3 API key. Submit an empty value to clear an existing key. Stored in MKVAUTO_ROOT/settings.json.">
                <input
                  type="text"
                  class="settings-input settings-input--mono"
                  [(ngModel)]="tmdbApiKey"
                  placeholder="e.g. 1a2b3c4d5e6f7g8h9i0j…"
                  autocomplete="off"
                  spellcheck="false"
                  [disabled]="isEnvManaged('tmdb_api_key')"
                  [title]="isEnvManaged('tmdb_api_key') ? envManagedHint : null">
                <div class="settings-env-note" *ngIf="isEnvManaged('tmdb_api_key')">
                  <ui-icon name="info" [size]="13"></ui-icon>
                  <span>{{ envManagedHint }}</span>
                </div>
              </ui-field>

              <div class="settings-help-callout">
                <ui-icon name="info" [size]="13"></ui-icon>
                <p>
                  <strong>How to get a key:</strong>
                  Sign up at
                  <a
                    class="settings-help-callout__link"
                    href="https://www.themoviedb.org/signup"
                    target="_blank"
                    rel="noopener noreferrer">
                    themoviedb.org
                    <ui-icon name="external" [size]="11"></ui-icon>
                  </a>
                  → Profile → Settings → API → request a v3 key. Without a key, the auto-suggestion
                  on disc load and the TMDB search on the film step stay hidden — your existing
                  manual labeling flow is unaffected.
                </p>
              </div>

              <div class="settings-actions">
                <ui-btn variant="secondary"
                        (click)="clearTmdbKey()"
                        [disabled]="tmdbSaving || !tmdbApiKeySet || isEnvManaged('tmdb_api_key')">
                  <ui-icon uiBtnIcon name="refresh" [size]="13"></ui-icon>
                  Clear key
                </ui-btn>
                <ui-btn
                  variant="primary"
                  (click)="saveTmdbConfig()"
                  [loading]="tmdbSaving"
                  [disabled]="(!tmdbApiKey.trim() && !tmdbApiKeySet) || isEnvManaged('tmdb_api_key')">
                  Save key
                </ui-btn>
              </div>
            </div>
          </ui-card>
        </div>

        <!-- TheDiscDB Tab: contributing to the shared disc database. Its own
             section — sandwiching it inside backup/restore made a contribution
             tool read as part of user-data export, which it is not (#741). -->
        <div *ngIf="activeTab === 'discdb'" class="settings-section-stack">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="TheDiscDB submissions"
                subtitle="Package discs that aren't in TheDiscDB yet so other users get automatic identification.">
                <ui-icon uiSecIcon name="upload" [size]="14"></ui-icon>
              </ui-section-header>

              <div class="settings-iox-block settings-iox-block--export">
                <div class="settings-iox-block__head">
                  <ui-icon name="download" [size]="20"></ui-icon>
                  <div>
                    <h5 class="settings-iox-block__title">Export DiscDB submissions</h5>
                    <p class="settings-iox-block__body">
                      Download every disc that isn't in TheDiscDB yet, laid out like the
                      <a class="settings-help-callout__link"
                         href="https://github.com/TheDiscDb/data"
                         target="_blank" rel="noopener noreferrer">TheDiscDb/data</a>
                      repository. Unzip it over a fork of that repo and open one pull request
                      for the whole set. Needs a finished job and a labelled release per disc.
                    </p>
                  </div>
                </div>
                <div class="settings-alert settings-alert--error" *ngIf="discdbExportError">
                  <ui-icon name="alert" [size]="14"></ui-icon>
                  <span>{{ discdbExportError }}</span>
                </div>
                <div class="settings-alert settings-alert--success" *ngIf="discdbExportResult">
                  <ui-icon name="check-circle" [size]="14"></ui-icon>
                  <span>{{ discdbExportResult }}</span>
                </div>
                <!-- A finished archive nobody collected — offered rather than
                     rebuilt, since these take a while and people navigate away. -->
                <div class="settings-alert settings-alert--success"
                     *ngIf="discdbExportReady && !discdbExporting">
                  <ui-icon name="check-circle" [size]="14"></ui-icon>
                  <span>
                    An export finished while you were away —
                    {{ discdbExportReady.included }} disc{{ discdbExportReady.included === 1 ? '' : 's' }}<span
                      *ngIf="discdbExportReady.skipped">, {{ discdbExportReady.skipped }} skipped</span>.
                    Download it instead of building it again.
                  </span>
                </div>
                <div class="settings-actions" *ngIf="discdbExportReady && !discdbExporting">
                  <ui-btn variant="primary" (click)="downloadReadyDiscDbExport()">
                    <ui-icon uiBtnIcon name="download" [size]="13"></ui-icon>
                    Download last export
                  </ui-btn>
                  <ui-btn variant="secondary" (click)="dismissReadyDiscDbExport()">
                    Dismiss
                  </ui-btn>
                </div>

                <div class="settings-export-progress" *ngIf="discdbExporting">
                  <div class="settings-export-progress__bar">
                    <div class="settings-export-progress__fill"
                         [style.width.%]="discdbExportPercent"></div>
                  </div>
                  <p class="settings-export-progress__label">
                    <ng-container *ngIf="discdbExportTotal">
                      {{ discdbExportDone }} of {{ discdbExportTotal }}
                    </ng-container>
                    <ng-container *ngIf="!discdbExportTotal">Starting…</ng-container>
                    <span *ngIf="discdbExportCurrent"> — {{ discdbExportCurrent }}</span>
                  </p>
                </div>
                <div class="settings-actions">
                  <ui-btn [variant]="discdbExportReady ? 'secondary' : 'primary'"
                          (click)="exportDiscDbSubmissions()"
                          [loading]="discdbExporting" [disabled]="discdbExporting">
                    <ui-icon uiBtnIcon name="download" [size]="13"></ui-icon>
                    {{ discdbExportReady ? 'Build a fresh export' : 'Export DiscDB submissions' }}
                  </ui-btn>
                  <ui-btn variant="secondary" *ngIf="discdbExporting"
                          (click)="cancelDiscDbExport()">
                    Cancel
                  </ui-btn>
                </div>
              </div>
            </div>
          </ui-card>
        </div>

        <!-- Export/Import Tab (template: ExportImport) -->
        <div *ngIf="activeTab === 'export'" class="settings-section-stack">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Export &amp; Import"
                subtitle="Backup and restore your ripping history. Metadata only — no video files.">
                <ui-icon uiSecIcon name="download" [size]="14"></ui-icon>
              </ui-section-header>

              <div class="settings-alert settings-alert--error" *ngIf="importError">
                <ui-icon name="alert" [size]="14"></ui-icon>
                <span>{{ importError }}</span>
              </div>

              <!-- Export -->
              <div class="settings-iox-block settings-iox-block--export">
                <div class="settings-iox-block__head">
                  <ui-icon name="download" [size]="20"></ui-icon>
                  <div>
                    <h5 class="settings-iox-block__title">Export data</h5>
                    <p class="settings-iox-block__body">
                      Download a ZIP archive containing all releases, discs,
                      titles, and transfer history. Use this to back up your
                      data or migrate to a new installation.
                    </p>
                  </div>
                </div>
                <ui-btn variant="primary" (click)="exportHistory()" [loading]="exporting">
                  <ui-icon uiBtnIcon name="download" [size]="13"></ui-icon>
                  Export all data
                </ui-btn>
              </div>

              <!-- Import -->
              <div class="settings-iox-block settings-iox-block--import">
                <div class="settings-iox-block__head">
                  <ui-icon name="upload" [size]="20"></ui-icon>
                  <div>
                    <h5 class="settings-iox-block__title">Import data</h5>
                    <p class="settings-iox-block__body">
                      Restore data from a previous export. Merges imported
                      records with existing data (duplicates skipped).
                    </p>
                  </div>
                </div>

                <label *ngIf="!importFile" class="settings-file-drop">
                  <input type="file" accept=".zip" (change)="onFileSelected($event)" [disabled]="importing">
                  <ui-icon name="file" [size]="20"></ui-icon>
                  <span>Click to select a ZIP file</span>
                </label>

                <div *ngIf="importFile && !importSummary" class="settings-file-row">
                  <div class="settings-file-row__main">
                    <ui-icon name="file" [size]="20"></ui-icon>
                    <div>
                      <div class="settings-file-row__name">{{ importFile.name }}</div>
                      <div class="settings-file-row__size">{{ (importFile.size / 1024).toFixed(1) }} KB</div>
                    </div>
                  </div>
                  <button type="button" class="tcfg-icon-btn" (click)="clearImportFile()" [disabled]="importing" title="Remove file">
                    <ui-icon name="close" [size]="14"></ui-icon>
                  </button>
                </div>

                <ui-btn
                  *ngIf="importFile && !importSummary"
                  variant="primary"
                  [fullWidth]="true"
                  (click)="importHistory()"
                  [loading]="importing">
                  <ui-icon uiBtnIcon name="upload" [size]="13"></ui-icon>
                  Start import
                </ui-btn>

                <div *ngIf="importSummary" class="settings-import-summary">
                  <div class="settings-import-summary__head">
                    <ui-icon name="check-circle" [size]="20"></ui-icon>
                    <span>Import complete</span>
                  </div>
                  <div class="settings-import-summary__grid">
                    <div>
                      <div class="settings-import-summary__label">Releases</div>
                      <div class="settings-import-summary__value">{{ importSummary.releases_imported }}</div>
                    </div>
                    <div>
                      <div class="settings-import-summary__label">Discs</div>
                      <div class="settings-import-summary__value">{{ importSummary.discs_imported }}</div>
                    </div>
                    <div>
                      <div class="settings-import-summary__label">Titles</div>
                      <div class="settings-import-summary__value">{{ importSummary.disc_titles_imported }}</div>
                    </div>
                    <div>
                      <div class="settings-import-summary__label">Jobs</div>
                      <div class="settings-import-summary__value">{{ importSummary.jobs_imported }}</div>
                    </div>
                  </div>
                  <div *ngIf="getTotalSkipped(importSummary) > 0" class="settings-import-summary__skipped">
                    {{ getTotalSkipped(importSummary) }} duplicate records skipped
                  </div>
                  <ui-btn variant="ghost" [fullWidth]="true" (click)="clearImportFile()">
                    Import another file
                  </ui-btn>
                </div>
              </div>

              <div class="settings-help-callout settings-help-callout--amber">
                <ui-icon name="alert" [size]="13"></ui-icon>
                <p>
                  <strong>Heads up:</strong>
                  exports contain metadata only — not the actual video files.
                  Import won't delete anything; it merges with what's there.
                  Make regular exports to protect against data loss.
                </p>
              </div>
            </div>
          </ui-card>
        </div>

        <!-- Help Tab -->
        <div *ngIf="activeTab === 'help'">
          <ui-card>
            <div class="settings-section-body">
              <ui-section-header
                title="Platform guide"
                subtitle="How the media processing workflow works and how to organize your collection.">
                <ui-icon uiSecIcon name="info" [size]="14"></ui-icon>
              </ui-section-header>

              <div class="help-accordion">
            <!-- Section 1: How We Identify Discs -->
            <div class="help-section">
              <button type="button" class="help-section__head" (click)="toggleHelpSection(1)">
                <div class="help-section__head-main">
                  <div class="help-section__avatar" style="background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); box-shadow: 0 0 15px rgba(59, 130, 246, 0.25);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: white;"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="2"/></svg>
                  </div>
                  <div class="help-section__head-text">
                    <h3 class="help-section__title">How We Identify Discs</h3>
                    <p class="help-section__subtitle">Section 1 of 7</p>
                  </div>
                </div>
                <ui-icon class="help-section__chevron" [class.is-open]="expandedHelpSection === 1" name="down" [size]="20"></ui-icon>
              </button>
              <div *ngIf="expandedHelpSection === 1" class="help-section__body">
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.8); line-height: 1.6; margin: 0;">
                    When you insert a disc, we automatically scan it and create a unique fingerprint. We then look this up in databases like <strong style="color: white;">TheDiscDB</strong> to identify what movie or TV show it is.
                  </p>
                  <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(59, 130, 246, 0.1); border: 1px solid rgba(59, 130, 246, 0.3);">
                    <p style="font-size: 0.875rem; color: #93c5fd; font-weight: 500; margin: 0 0 0.5rem 0;">✨ What happens when we find a match:</p>
                    <ul style="color: rgba(147, 197, 253, 0.8); font-size: 0.875rem; margin: 0 0 0 1rem; padding: 0; list-style: disc;">
                      <li>Movie title, year, and metadata are pre-filled</li>
                      <li>Cover art and descriptions appear automatically</li>
                      <li>Title information is suggested based on the disc structure</li>
                      <li>You can still edit or override any information</li>
                    </ul>
                  </div>
              </div>
            </div>

            <!-- Section 2: When We Can't Identify Discs -->
            <div class="help-section">
              <button type="button" class="help-section__head" (click)="toggleHelpSection(2)">
                <div class="help-section__head-main">
                  <div class="help-section__avatar" style="background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); box-shadow: 0 0 15px rgba(139, 92, 246, 0.25);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: white;"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
                  </div>
                  <div class="help-section__head-text">
                    <h3 class="help-section__title">When We Can't Identify Discs</h3>
                    <p class="help-section__subtitle">Section 2 of 7</p>
                  </div>
                </div>
                <ui-icon class="help-section__chevron" [class.is-open]="expandedHelpSection === 2" name="down" [size]="20"></ui-icon>
              </button>
              <div *ngIf="expandedHelpSection === 2" class="help-section__body">
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.8); line-height: 1.6; margin: 0;">
                    Sometimes we can't find a match — this could be a rare release, a custom disc, or a disc that's not in our databases yet.
                  </p>
                  <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(139, 92, 246, 0.1); border: 1px solid rgba(139, 92, 246, 0.3);">
                    <p style="font-size: 0.875rem; color: #c4b5fd; font-weight: 500; margin: 0 0 0.5rem 0;">🔍 Don't worry — you can still proceed:</p>
                    <ul style="color: rgba(196, 181, 253, 0.8); font-size: 0.875rem; margin: 0 0 0 1rem; padding: 0; list-style: disc;">
                      <li>Search for the movie/show manually</li>
                      <li>Enter the title and metadata yourself</li>
                      <li>The workflow still works the same way</li>
                      <li>All labeling, ripping, and transfer features still work</li>
                    </ul>
                  </div>
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.6); margin: 0;">
                    <strong style="color: white;">Pro tip:</strong> If you have the movie name and year, the manual search is usually very quick.
                  </p>
              </div>
            </div>

            <!-- Section 3: Transfer Configuration -->
            <div class="help-section">
              <button type="button" class="help-section__head" (click)="toggleHelpSection(3)">
                <div class="help-section__head-main">
                  <div class="help-section__avatar" style="background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 0 15px rgba(16, 185, 129, 0.25);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: white;"><path d="m3 3 3 3m0 0 3-3M6 6v12"/><path d="m21 21-3-3m0 0-3 3m3-3V9"/></svg>
                  </div>
                  <div class="help-section__head-text">
                    <h3 class="help-section__title">Transfer Configuration</h3>
                    <p class="help-section__subtitle">Section 3 of 7</p>
                  </div>
                </div>
                <ui-icon class="help-section__chevron" [class.is-open]="expandedHelpSection === 3" name="down" [size]="20"></ui-icon>
              </button>
              <div *ngIf="expandedHelpSection === 3" class="help-section__body">
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.8); line-height: 1.6; margin: 0;">
                    Transfer configurations define where your processed media files go after ripping. You can set up multiple destinations with different options and only one can be active at a time.
                  </p>
                  <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); display: flex; flex-direction: column; gap: 0.5rem;">
                    <p style="font-size: 0.875rem; color: #6ee7b7; font-weight: 500; margin: 0;">📁 Transfer Modes:</p>
                    <div><p style="font-size: 0.875rem; color: #6ee7b7; font-weight: 500; margin: 0;">Local</p><p style="font-size: 0.75rem; color: rgba(110, 231, 183, 0.7); margin: 0;">Direct file copy to a local path or mounted drive (mount your NAS export into the container to use it here)</p></div>
                    <div><p style="font-size: 0.875rem; color: #6ee7b7; font-weight: 500; margin: 0;">SMB</p><p style="font-size: 0.75rem; color: rgba(110, 231, 183, 0.7); margin: 0;">Windows file sharing (ideal for NAS devices)</p></div>
                  </div>
              </div>
            </div>

            <!-- Section 4: Movie Selection & Creation -->
            <div class="help-section">
              <button type="button" class="help-section__head" (click)="toggleHelpSection(4)">
                <div class="help-section__head-main">
                  <div class="help-section__avatar" style="background: linear-gradient(135deg, #ec4899 0%, #db2777 100%); box-shadow: 0 0 15px rgba(236, 72, 153, 0.25);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: white;"><rect width="18" height="4" x="3" y="4" rx="2"/><rect width="18" height="4" x="3" y="12" rx="2"/><rect width="18" height="4" x="3" y="20" rx="2"/></svg>
                  </div>
                  <div class="help-section__head-text">
                    <h3 class="help-section__title">Movie Selection & Creation</h3>
                    <p class="help-section__subtitle">Section 4 of 7</p>
                  </div>
                </div>
                <ui-icon class="help-section__chevron" [class.is-open]="expandedHelpSection === 4" name="down" [size]="20"></ui-icon>
              </button>
              <div *ngIf="expandedHelpSection === 4" class="help-section__body">
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.8); line-height: 1.6; margin: 0;">
                    In the <strong style="color: white;">Film</strong> step, you choose which movie or show this disc belongs to. You can pick an existing entry or create a new one.
                  </p>
                  <div style="display: grid; gap: 0.75rem;">
                    <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(236, 72, 153, 0.1); border: 1px solid rgba(236, 72, 153, 0.3);">
                      <p style="font-size: 0.875rem; color: #f9a8d4; font-weight: 500; margin: 0 0 0.5rem 0;">Picking an existing movie:</p>
                      <p style="font-size: 0.875rem; color: rgba(249, 168, 212, 0.8); margin: 0;">
                        If you've already ripped this movie before (different disc, different edition), select the existing movie so all versions are grouped together.
                      </p>
                    </div>
                    <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(236, 72, 153, 0.1); border: 1px solid rgba(236, 72, 153, 0.3);">
                      <p style="font-size: 0.875rem; color: #f9a8d4; font-weight: 500; margin: 0 0 0.5rem 0;">Creating a new movie:</p>
                      <p style="font-size: 0.875rem; color: rgba(249, 168, 212, 0.8); margin: 0;">
                        For a brand new movie you haven't ripped before, create a new entry. We'll store all the metadata and group future editions under it.
                      </p>
                    </div>
                  </div>
              </div>
            </div>

            <!-- Section 5: Boxset vs Release -->
            <div class="help-section">
              <button type="button" class="help-section__head" (click)="toggleHelpSection(5)">
                <div class="help-section__head-main">
                  <div class="help-section__avatar" style="background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); box-shadow: 0 0 15px rgba(245, 158, 11, 0.25);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: white;"><path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>
                  </div>
                  <div class="help-section__head-text">
                    <h3 class="help-section__title">Boxset vs Release</h3>
                    <p class="help-section__subtitle">Section 5 of 7</p>
                  </div>
                </div>
                <ui-icon class="help-section__chevron" [class.is-open]="expandedHelpSection === 5" name="down" [size]="20"></ui-icon>
              </button>
              <div *ngIf="expandedHelpSection === 5" class="help-section__body">
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.8); line-height: 1.6; margin: 0;">
                    Understanding the difference between a <strong style="color: white;">Boxset</strong> and a <strong style="color: white;">Release</strong> helps you organize your collection correctly.
                  </p>
                  <div style="display: flex; flex-direction: column; gap: 0.75rem;">
                    <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3);">
                      <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem;">
                        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #fbbf24; flex-shrink: 0;"><path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>
                        <p style="font-size: 0.875rem; color: #fcd34d; font-weight: 700; margin: 0;">Boxset</p>
                      </div>
                      <p style="font-size: 0.875rem; color: rgba(252, 211, 77, 0.8); margin: 0 0 0.5rem 0;">
                        A single product that contains <strong>multiple movies</strong>
                      </p>
                      <p style="font-size: 0.75rem; color: rgba(252, 211, 77, 0.6); margin: 0;">
                        Examples: Lord of the Rings Trilogy, Marvel Phase One Collection, James Bond 007 Collection
                      </p>
                    </div>
                    <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3);">
                      <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem;">
                        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #fbbf24; flex-shrink: 0;"><rect width="18" height="4" x="3" y="4" rx="2"/><rect width="18" height="4" x="3" y="12" rx="2"/><rect width="18" height="4" x="3" y="20" rx="2"/></svg>
                        <p style="font-size: 0.875rem; color: #fcd34d; font-weight: 700; margin: 0;">Release</p>
                      </div>
                      <p style="font-size: 0.875rem; color: rgba(252, 211, 77, 0.8); margin: 0 0 0.5rem 0;">
                        A single production (one movie or one TV series/season)
                      </p>
                      <p style="font-size: 0.75rem; color: rgba(252, 211, 77, 0.6); margin: 0;">
                        Examples: The Matrix (single film), Breaking Bad Season 1, Avengers: Endgame Steelbook
                      </p>
                    </div>
                  </div>
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.6); margin: 0;">
                    <strong style="color: white;">Think of it this way:</strong> Boxset = multiple productions in one package. Release = one production (even if it spans multiple discs).
                  </p>
              </div>
            </div>

            <!-- Section 6: How Titles Work -->
            <div class="help-section">
              <button type="button" class="help-section__head" (click)="toggleHelpSection(6)">
                <div class="help-section__head-main">
                  <div class="help-section__avatar" style="background: linear-gradient(135deg, #14b8a6 0%, #0d9488 100%); box-shadow: 0 0 15px rgba(20, 184, 166, 0.25);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: white;"><line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/><line x1="3" x2="3.01" y1="6" y2="6"/><line x1="3" x2="3.01" y1="12" y2="12"/><line x1="3" x2="3.01" y1="18" y2="18"/></svg>
                  </div>
                  <div class="help-section__head-text">
                    <h3 class="help-section__title">How Titles Work</h3>
                    <p class="help-section__subtitle">Section 6 of 7</p>
                  </div>
                </div>
                <ui-icon class="help-section__chevron" [class.is-open]="expandedHelpSection === 6" name="down" [size]="20"></ui-icon>
              </button>
              <div *ngIf="expandedHelpSection === 6" class="help-section__body">
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.8); line-height: 1.6; margin: 0;">
                    In the <strong style="color: white;">Titles</strong> step, you see all the tracks (titles) available on the disc and choose which ones to rip.
                  </p>
                  <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(20, 184, 166, 0.1); border: 1px solid rgba(20, 184, 166, 0.3); display: flex; flex-direction: column; gap: 0.75rem;">
                    <div>
                      <p style="font-size: 0.875rem; color: #5eead4; font-weight: 500; margin: 0 0 0.25rem 0;">Main movie titles:</p>
                      <p style="font-size: 0.875rem; color: rgba(94, 234, 212, 0.8); margin: 0;">
                        Usually the longest track — this is the feature film
                      </p>
                    </div>
                    <div>
                      <p style="font-size: 0.875rem; color: #5eead4; font-weight: 500; margin: 0 0 0.25rem 0;">Bonus content:</p>
                      <p style="font-size: 0.875rem; color: rgba(94, 234, 212, 0.8); margin: 0;">
                        Behind-the-scenes, deleted scenes, trailers, etc.
                      </p>
                    </div>
                    <div>
                      <p style="font-size: 0.875rem; color: #5eead4; font-weight: 500; margin: 0 0 0.25rem 0;">Duplicates:</p>
                      <p style="font-size: 0.875rem; color: rgba(94, 234, 212, 0.8); margin: 0;">
                        Sometimes the same cut appears multiple times with different audio/subtitle tracks
                      </p>
                    </div>
                  </div>
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.6); margin: 0;">
                    You can name each title, set its type, and choose which to ignore. Each title becomes a separate output file.
                  </p>
              </div>
            </div>

            <!-- Section 7: What Editions Are For -->
            <div class="help-section">
              <button type="button" class="help-section__head" (click)="toggleHelpSection(7)">
                <div class="help-section__head-main">
                  <div class="help-section__avatar" style="background: linear-gradient(135deg, #eab308 0%, #ca8a04 100%); box-shadow: 0 0 15px rgba(234, 179, 8, 0.25);">
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: white;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                  </div>
                  <div class="help-section__head-text">
                    <h3 class="help-section__title">What Editions Are For</h3>
                    <p class="help-section__subtitle">Section 7 of 7</p>
                  </div>
                </div>
                <ui-icon class="help-section__chevron" [class.is-open]="expandedHelpSection === 7" name="down" [size]="20"></ui-icon>
              </button>
              <div *ngIf="expandedHelpSection === 7" class="help-section__body">
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.8); line-height: 1.6; margin: 0;">
                    <strong style="color: white;">Editions</strong> help you distinguish different cuts or versions of the same movie.
                  </p>
                  <div style="padding: 1rem; border-radius: 0.5rem; background: rgba(234, 179, 8, 0.1); border: 1px solid rgba(234, 179, 8, 0.3);">
                    <p style="font-size: 0.875rem; color: #fde047; font-weight: 500; margin: 0 0 0.5rem 0;">Common edition types:</p>
                    <ul style="color: rgba(253, 224, 71, 0.8); font-size: 0.875rem; margin: 0 0 0 1rem; padding: 0; list-style: disc; display: flex; flex-direction: column; gap: 0.25rem;">
                      <li><strong>Theatrical</strong> — the version shown in cinemas</li>
                      <li><strong>Extended</strong> — longer cut with additional scenes</li>
                      <li><strong>Director's Cut</strong> — the director's preferred version</li>
                      <li><strong>Unrated</strong> — version without rating restrictions</li>
                      <li><strong>4K UHD</strong>, <strong>Blu-ray</strong>, <strong>3D</strong> — format-specific releases</li>
                    </ul>
                  </div>
                  <p style="font-size: 0.875rem; color: rgba(255, 255, 255, 0.6); margin: 0;">
                    <strong style="color: white;">Why it matters:</strong> Editions ensure your library correctly names and organizes different versions, so Plex/Jellyfin can show them as separate options.
                  </p>
                  <div style="padding: 0.75rem; border-radius: 0.5rem; background: rgba(234, 179, 8, 0.08); border: 1px solid rgba(234, 179, 8, 0.2);">
                    <p style="font-size: 0.875rem; color: rgba(253, 224, 71, 0.9); margin: 0;">
                      <strong>Example:</strong> If you rip both the theatrical and extended cuts of The Lord of the Rings, each gets its own edition tag so they're stored separately.
                    </p>
                  </div>
              </div>
            </div>

            <!-- Support bundle (#804). Sits outside the accordion: it is an
                 action, not reading material, and someone whose drives are
                 not detected should not have to expand anything to find it. -->
            <div class="support-bundle">
              <div class="support-bundle__head">
                <h3 class="support-bundle__title">Diagnostics</h3>
                <p class="support-bundle__subtitle">
                  Collects logs and drive information into a single file you can attach to a
                  bug report. Passwords and API keys are removed before it is written.
                </p>
              </div>
              <button
                type="button"
                class="support-bundle__btn"
                [disabled]="bundleState === 'working' || !!bundleBlocked"
                (click)="downloadSupportBundle()">
                {{ bundleState === 'working' ? 'Collecting…' : 'Download support bundle' }}
              </button>
              <p *ngIf="bundleBlocked" class="support-bundle__blocked">{{ bundleBlocked }}</p>
              <p *ngIf="bundleNote" class="support-bundle__note">{{ bundleNote }}</p>
              <p *ngIf="bundleError" class="support-bundle__error">{{ bundleError }}</p>
              <p class="support-bundle__hint">
                This is collected from inside the container, which cannot see how Docker itself
                is configured. If a drive is missing and the bundle does not explain why, run
                <code>scripts/mkv-support-bundle.sh</code> on the Docker host — copy it out of
                this image with
                <code>docker cp mkv-auto:/app/scripts/mkv-support-bundle.sh .</code>
              </p>
            </div>
          </div>
            </div>
          </ui-card>
        </div>
        </div>
      </div>
    </div>
  `,
  styles: [`
    /* Sidebar shell layout — replaces the old top tab bar. Mirrors the
     * prototype's settings page (research/MKV Auto UI/settings.jsx): 220px
     * sidebar nav on the left, scrollable content panel on the right. */
    .settings-shell {
      display: grid;
      grid-template-columns: 220px 1fr;
      gap: 18px;
      align-items: start;
      margin-top: 16px;
    }
    .settings-shell__sidebar {
      position: sticky;
      top: 16px;
      align-self: start;
    }
    .settings-shell__sidebar ::ng-deep .ui-card {
      padding: 8px;
    }
    .settings-shell__nav {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .settings-shell__nav-btn {
      all: unset;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 500;
      color: var(--text-muted, rgba(255, 255, 255, 0.55));
      background: transparent;
      transition: background 150ms ease, color 150ms ease;
      box-sizing: border-box;
    }
    .settings-shell__nav-btn:hover {
      background: rgba(255, 255, 255, 0.04);
      color: #fff;
    }
    .settings-shell__nav-btn.is-active {
      background: rgba(99, 102, 241, 0.18);
      color: #fff;
    }
    .settings-shell__nav-btn:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.45);
    }
    .settings-shell__content {
      min-width: 0;
    }

    /* Section card body — inset content from the ui-card border so the
     * SectionHeader and form fields breathe. Stacks multiple cards within
     * a single tab when needed (e.g. configs has both the config list +
     * the health-check card). */
    .settings-section-body { padding: 20px 24px; }
    .settings-section-stack {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    /* Indigo info callout used to introduce a tab when help copy is needed
     * (e.g. transfer configs explainer). Drops the bespoke inline-styled
     * div in favor of a reusable token-aware block. */
    .settings-help-callout {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      margin: 12px 0 16px;
      padding: 10px 12px;
      border-radius: 8px;
      background: rgba(99, 102, 241, 0.08);
      border: 1px solid rgba(99, 102, 241, 0.20);
      font-size: 13px;
      color: rgba(255, 255, 255, 0.7);
      line-height: 1.5;
    }
    .settings-help-callout ui-icon { color: #a5b4fc; margin-top: 2px; flex-shrink: 0; }
    .settings-help-callout p { margin: 0; }
    .settings-help-callout strong { color: #fff; }

    /* Bulk-export progress. The count matters more than the bar — "3 of 40"
       tells you whether to wait; a bar alone does not. */
    .settings-export-progress { margin: 10px 0; }
    .settings-export-progress__bar {
      height: 6px;
      border-radius: 3px;
      overflow: hidden;
      background: rgba(255, 255, 255, 0.08);
    }
    .settings-export-progress__fill {
      height: 100%;
      background: #6366f1;
      transition: width 0.3s ease;
    }
    .settings-export-progress__label {
      margin: 6px 0 0;
      font-size: 12px;
      color: rgba(255, 255, 255, 0.6);
    }

    /* Shown under a field the environment pins. Amber rather than the indigo
       help colour: this is a constraint on what the user can do here, not a tip. */
    .settings-env-note {
      display: flex;
      align-items: flex-start;
      gap: 6px;
      margin-top: 8px;
      font-size: 12px;
      line-height: 1.45;
      color: rgba(251, 191, 36, 0.85);
    }
    .settings-env-note ui-icon { margin-top: 1px; flex-shrink: 0; }

    /* Inline alerts inside a settings card. Red for errors, emerald for
     * saves. Same shape as the help callout but tone-tinted. */
    .settings-alert {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 8px;
      font-size: 13px;
      line-height: 1.5;
      margin-bottom: 14px;
    }
    .settings-alert--error {
      background: rgba(239, 68, 68, 0.10);
      border: 1px solid rgba(239, 68, 68, 0.30);
      color: #fca5a5;
    }
    .settings-alert--success {
      background: rgba(16, 185, 129, 0.10);
      border: 1px solid rgba(16, 185, 129, 0.30);
      color: #6ee7b7;
    }
    .settings-alert ui-icon { flex-shrink: 0; margin-top: 2px; }

    /* Form inputs that match the prototype's settings inputs:
     * dark-glass background, indigo focus ring, full-width inside ui-field. */
    .settings-input {
      all: unset;
      display: block;
      box-sizing: border-box;
      width: 100%;
      padding: 10px 12px;
      border-radius: 8px;
      background: rgba(0, 0, 0, 0.28);
      border: 1px solid rgba(255, 255, 255, 0.10);
      color: #fff;
      font-size: 13px;
      font-family: inherit;
      line-height: 1.4;
      transition: border-color 150ms ease, background 150ms ease;
    }
    .settings-input::placeholder { color: rgba(255, 255, 255, 0.35); }
    .settings-input:hover:not(:disabled) { border-color: rgba(255, 255, 255, 0.18); }
    .settings-input:focus { border-color: rgba(99, 102, 241, 0.55); background: rgba(0, 0, 0, 0.35); }
    .settings-input[type="range"] {
      padding: 6px 0;
      background: transparent;
      border: none;
      accent-color: #6366f1;
    }

    .settings-range-labels {
      display: flex;
      justify-content: space-between;
      margin-top: 6px;
      font-size: 11px;
      color: rgba(255, 255, 255, 0.55);
    }
    .settings-range-labels span:nth-child(2) {
      color: #a5b4fc;
      font-weight: 600;
    }

    /* Action row at the foot of a settings card. */
    .settings-actions {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid rgba(255, 255, 255, 0.06);
    }

    /* Pill toggle (on/off). Used for boolean fields like DiscDB prefill,
     * eject-on-finish, beta key auto-fetch, etc. Slightly larger than the
     * threshold-modal toggle so it stays tappable on touch devices. */
    .settings-toggle {
      all: unset;
      cursor: pointer;
      position: relative;
      width: 44px;
      height: 24px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.18);
      transition: background 200ms ease;
      flex-shrink: 0;
    }
    .settings-toggle.is-on {
      background: #22c55e;
    }
    .settings-toggle:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.45);
    }
    .settings-toggle__knob {
      position: absolute;
      top: 2px;
      left: 2px;
      width: 20px;
      height: 20px;
      border-radius: 999px;
      background: #fff;
      transition: transform 200ms ease;
    }
    .settings-toggle.is-on .settings-toggle__knob {
      transform: translateX(20px);
    }

    /* Platform select cards (Library tab — Plex / Jellyfin). 2-column grid
     * with the logo, a checkmark when selected, and emerald accent border. */
    .settings-platform-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }
    .settings-platform-card {
      all: unset;
      cursor: pointer;
      display: block;
      padding: 16px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid rgba(255, 255, 255, 0.05);
      transition: background 150ms ease, border-color 150ms ease, box-shadow 150ms ease;
      box-sizing: border-box;
    }
    .settings-platform-card:hover {
      background: rgba(255, 255, 255, 0.04);
      border-color: rgba(255, 255, 255, 0.10);
    }
    .settings-platform-card.is-selected {
      background: rgba(16, 185, 129, 0.10);
      border-color: rgba(16, 185, 129, 0.30);
      box-shadow: 0 0 0 1px rgba(16, 185, 129, 0.30);
    }
    .settings-platform-card:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.45);
    }
    .settings-platform-card__head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      margin-bottom: 12px;
    }
    .settings-platform-card__logo {
      width: 48px;
      height: 48px;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
      font-weight: 700;
      flex-shrink: 0;
    }
    .settings-platform-card__logo--plex {
      background: #e5a00d;
      color: #1a1a1a;
    }
    .settings-platform-card__logo--jellyfin {
      background: linear-gradient(135deg, #00a4dc 0%, #0082b3 100%);
      color: #fff;
    }
    .settings-platform-card__check {
      color: #10b981;
    }
    .settings-platform-card__name {
      font-size: 15px;
      font-weight: 700;
      color: #fff;
      margin-bottom: 4px;
    }
    .settings-platform-card__desc {
      font-size: 13px;
      color: rgba(255, 255, 255, 0.6);
      margin: 0;
      line-height: 1.5;
    }

    /* Mono variant of the shared settings input — used for paths, URLs,
     * webhook URLs, license keys, custom flags. JetBrains Mono via the
     * --ui-font-mono token. */
    .settings-input--mono {
      font-family: var(--ui-font-mono, ui-monospace, SFMono-Regular, Menlo, Monaco, monospace);
      font-size: 12.5px;
    }

    /* Notification preferences blocks (Notifications tab). Each block has
     * a heading + hint + body; the Informative block additionally gets a
     * 3-column checkbox table for per-category in-app / Discord toggles. */
    .settings-notif-block {
      padding-bottom: 16px;
      margin-bottom: 16px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }
    .settings-notif-block:last-child {
      padding-bottom: 0;
      margin-bottom: 0;
      border-bottom: none;
    }
    .settings-notif-block__head {
      margin-bottom: 8px;
    }
    .settings-notif-block__title {
      font-size: 13px;
      font-weight: 600;
      color: rgba(255, 255, 255, 0.9);
      margin-bottom: 4px;
    }
    .settings-notif-block__hint {
      font-size: 12px;
      color: rgba(255, 255, 255, 0.55);
      margin: 0;
      line-height: 1.5;
    }
    .settings-notif-table {
      margin-top: 12px;
      overflow-x: auto;
    }
    .settings-notif-table table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    .settings-notif-table thead th {
      text-align: left;
      padding: 6px 8px;
      color: rgba(255, 255, 255, 0.55);
      border-bottom: 1px solid rgba(255, 255, 255, 0.10);
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .settings-notif-table tbody td {
      padding: 8px;
      color: rgba(255, 255, 255, 0.9);
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }
    .settings-notif-table tbody tr:last-child td {
      border-bottom: none;
    }

    /* Checkbox-with-label row used inside notification blocks. */
    .settings-checkbox-row {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: rgba(255, 255, 255, 0.85);
      cursor: pointer;
      margin-top: 8px;
    }
    .settings-checkbox-row input[type="checkbox"] {
      accent-color: #6366f1;
    }
    .settings-checkbox-row.is-disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
    .settings-checkbox-row-group {
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
    }

    /* Inline link inside a help callout (Discord docs link, etc.). */
    .settings-help-callout__link {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: #a5b4fc;
      text-decoration: underline;
      text-underline-offset: 2px;
      margin-left: 4px;
    }
    .settings-help-callout__link:hover {
      color: #c7d2fe;
    }
    /* Amber tone variant of the help callout — used by Export/Import for the
     * "exports are metadata only" warning. */
    .settings-help-callout--amber {
      background: rgba(245, 158, 11, 0.08);
      border-color: rgba(245, 158, 11, 0.25);
      color: rgba(252, 211, 77, 0.85);
    }
    .settings-help-callout--amber ui-icon { color: #fcd34d; }
    .settings-help-callout--amber strong { color: #fde047; }

    /* Export / Import blocks — tinted callouts that group an icon header,
     * descriptive copy, and the action affordance underneath. */
    .settings-iox-block {
      padding: 18px;
      border-radius: 12px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      margin-bottom: 14px;
    }
    .settings-iox-block:last-of-type { margin-bottom: 0; }
    .settings-iox-block--export {
      background: rgba(6, 182, 212, 0.05);
      border: 1px solid rgba(6, 182, 212, 0.20);
    }
    .settings-iox-block--export .settings-iox-block__head ui-icon { color: #22d3ee; }
    .settings-iox-block--import {
      background: rgba(139, 92, 246, 0.05);
      border: 1px solid rgba(139, 92, 246, 0.20);
    }
    .settings-iox-block--import .settings-iox-block__head ui-icon { color: #a78bfa; }
    .settings-iox-block__head {
      display: flex;
      align-items: flex-start;
      gap: 12px;
    }
    .settings-iox-block__head > ui-icon { margin-top: 1px; flex-shrink: 0; }
    .settings-iox-block__title {
      font-size: 14px;
      font-weight: 700;
      color: #fff;
      margin: 0 0 4px;
    }
    .settings-iox-block__body {
      font-size: 13px;
      color: rgba(255, 255, 255, 0.7);
      margin: 0;
      line-height: 1.5;
    }

    /* File-drop label + selected-file row used in the Import block. */
    .settings-file-drop {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 28px 16px;
      border-radius: 8px;
      border: 2px dashed rgba(139, 92, 246, 0.30);
      cursor: pointer;
      transition: border-color 150ms ease, background 150ms ease;
      color: #a78bfa;
      font-size: 13px;
    }
    .settings-file-drop:hover {
      border-color: rgba(139, 92, 246, 0.50);
      background: rgba(139, 92, 246, 0.05);
    }
    .settings-file-drop input[type="file"] { display: none; }
    .settings-file-drop span { color: rgba(255, 255, 255, 0.7); }
    .settings-file-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 12px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid rgba(255, 255, 255, 0.05);
    }
    .settings-file-row__main {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }
    .settings-file-row__main > ui-icon { color: #a78bfa; flex-shrink: 0; }
    .settings-file-row__name {
      font-size: 13px;
      font-weight: 500;
      color: #fff;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .settings-file-row__size { font-size: 11px; color: rgba(255, 255, 255, 0.5); }

    /* Import summary box (post-success). 2-column metric grid + ghost
     * "import another" button. */
    .settings-import-summary {
      padding: 14px;
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      background: rgba(16, 185, 129, 0.08);
      border: 1px solid rgba(16, 185, 129, 0.30);
    }
    .settings-import-summary__head {
      display: flex;
      align-items: center;
      gap: 8px;
      color: #6ee7b7;
      font-weight: 700;
      font-size: 13px;
    }
    .settings-import-summary__grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
    }
    .settings-import-summary__label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: rgba(255, 255, 255, 0.5);
    }
    .settings-import-summary__value {
      font-size: 17px;
      font-weight: 700;
      color: #fff;
    }
    .settings-import-summary__skipped {
      font-size: 12px;
      color: rgba(255, 255, 255, 0.6);
      padding-top: 8px;
      border-top: 1px solid rgba(255, 255, 255, 0.1);
    }

    /* Help tab accordion (Platform Guide). 7 collapsible sections, each with
     * a gradient avatar tile, title, "Section N of 7" subtitle, and a chevron
     * that rotates 180° when expanded. */
    .help-accordion {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-top: 8px;
    }
    .help-section {
      border-radius: 12px;
      overflow: hidden;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.10);
    }
    .help-section__head {
      all: unset;
      cursor: pointer;
      width: 100%;
      box-sizing: border-box;
      padding: 14px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      transition: background 150ms ease;
    }
    .help-section__head:hover { background: rgba(255, 255, 255, 0.02); }
    .help-section__head:focus-visible {
      outline: none;
      box-shadow: inset 0 0 0 2px rgba(99, 102, 241, 0.45);
    }
    .help-section__head-main {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }
    .help-section__avatar {
      width: 40px;
      height: 40px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      color: #fff;
    }
    .help-section__avatar svg { color: #fff; }
    .help-section__head-text { text-align: left; min-width: 0; }
    .help-section__title {
      font-size: 14px;
      font-weight: 600;
      color: #fff;
      margin: 0;
    }
    .help-section__subtitle {
      font-size: 11px;
      color: rgba(255, 255, 255, 0.5);
      margin: 2px 0 0;
    }
    .help-section__chevron {
      color: rgba(255, 255, 255, 0.6);
      flex-shrink: 0;
      transition: transform 200ms ease;
      display: inline-flex;
    }
    .help-section__chevron.is-open { transform: rotate(180deg); }
    @media (prefers-reduced-motion: reduce) {
      .help-section__chevron { transition: none; }
    }
    .support-bundle {
      margin-top: 1.5rem;
      padding: 1.25rem;
      border-radius: 0.75rem;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.08);
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    .support-bundle__title {
      font-size: 1rem;
      font-weight: 600;
      color: white;
      margin: 0 0 0.25rem 0;
    }
    .support-bundle__subtitle {
      font-size: 0.875rem;
      color: rgba(255, 255, 255, 0.6);
      line-height: 1.5;
      margin: 0;
    }
    .support-bundle__btn {
      align-self: flex-start;
      padding: 0.5rem 1rem;
      border-radius: 0.5rem;
      border: 1px solid rgba(99, 102, 241, 0.4);
      background: rgba(99, 102, 241, 0.15);
      color: #c7d2fe;
      font-size: 0.875rem;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s ease;
    }
    .support-bundle__btn:hover:not(:disabled) { background: rgba(99, 102, 241, 0.25); }
    .support-bundle__btn:disabled { opacity: 0.6; cursor: default; }
    .support-bundle__blocked {
      font-size: 0.8125rem;
      color: rgba(253, 224, 71, 0.9);
      margin: 0;
    }
    .support-bundle__note {
      font-size: 0.8125rem;
      color: rgba(134, 239, 172, 0.9);
      margin: 0;
    }
    .support-bundle__error {
      font-size: 0.8125rem;
      color: rgba(252, 165, 165, 0.95);
      margin: 0;
    }
    .support-bundle__hint {
      font-size: 0.8125rem;
      color: rgba(255, 255, 255, 0.45);
      line-height: 1.6;
      margin: 0;
    }
    .support-bundle__hint code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.78125rem;
      padding: 0.1rem 0.3rem;
      border-radius: 0.25rem;
      background: rgba(255, 255, 255, 0.08);
      color: rgba(255, 255, 255, 0.75);
    }
    .help-section__body {
      padding: 6px 18px 18px;
      border-top: 1px solid rgba(255, 255, 255, 0.10);
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    @media (max-width: 900px) {
      .settings-shell {
        grid-template-columns: 1fr;
      }
      .settings-shell__sidebar {
        position: static;
      }
      .settings-shell__nav {
        flex-direction: row;
        flex-wrap: wrap;
      }
    }
  `],
})
export class SettingsPageComponent implements OnInit, OnDestroy {
  // max_parallel_ceiling is server-derived; default to 1 until the backend
  // config is loaded (loadPreviewConfig populates it). The slider's [max]
  // binds to this field, not to navigator.hardwareConcurrency.
  preview: PreviewConfig = { duration_seconds: 120, max_parallel: 1, max_parallel_ceiling: 1 };
  previewError: string | null = null;
  previewSaved: string | null = null;
  previewSaving = false;
  discord: DiscordConfig = {
    webhook_url: null,
    enabled: false,
    notification_preferences: defaultNotificationPreferences(),
  };
  discordError: string | null = null;
  discordSaved: string | null = null;
  discordSaving = false;
  readonly informativeCategoryKeys = [
    'rip_start',
    'rip_complete',
    'job_completed',
    'per_title',
    'previews_ready',
    'transfer_started',
  ] as const;
  readonly informativeCategoryLabels: Record<string, string> = {
    rip_start: 'Rip started',
    rip_complete: 'Rip complete',
    job_completed: 'Job complete',
    per_title: 'Per-title progress',
    previews_ready: 'Previews ready',
    transfer_started: 'Transfer started',
  };
  readonly discordDisabledHint =
    'Add a Discord webhook URL below and enable Discord to use this channel.';
  mediaServer: MediaServerConfig = { media_server: 'plex' };
  mediaServerError: string | null = null;
  mediaServerSaved: string | null = null;
  librarySaving = false;

  discDbLookup: DiscDbLookupConfig = { discdb_miss_workflow_with_prefill: true, eject_on_finish: false };
  discDbLookupError: string | null = null;
  discDbLookupSaved: string | null = null;
  discDbLookupSaving = false;
  autoRip: AutoRipConfig = { auto_rip_enabled: false };

  // TMDB API key state (#369 / #386). The backend never echoes the key back;
  // tmdbApiKeySet just reflects whether ONE is configured. The text input is
  // empty by default — typing into it and saving will send the new value via
  // POST /system/tmdb/config. Submitting an empty input clears the key.
  tmdbApiKey: string = '';
  tmdbApiKeySet: boolean = false;
  tmdbSaving: boolean = false;
  tmdbLoaded: boolean = false;
  tmdbError: string | null = null;
  tmdbSaved: string | null = null;

  // #741: bulk TheDiscDB submission export. Runs in the background, so the page
  // holds the job id and polls rather than blocking on one request.
  discdbExporting = false;
  discdbExportError: string | null = null;
  discdbExportResult: string | null = null;
  discdbExportJobId: string | null = null;
  discdbExportDone = 0;
  discdbExportTotal = 0;
  discdbExportCurrent = '';
  /** A finished archive still on disk, offered rather than rebuilt. */
  discdbExportReady: DiscDbExportJob | null = null;
  private discdbPollSub?: Subscription;

  get discdbExportPercent(): number {
    if (!this.discdbExportTotal) return 0;
    return Math.round((this.discdbExportDone / this.discdbExportTotal) * 100);
  }

  // Settings pinned by environment variables. The container re-applies the
  // environment on every boot, so editing one of these here would look like it
  // worked and then silently revert on the next restart. Disable instead.
  envManaged: string[] = [];
  readonly envManagedHint =
    'Set by an environment variable on this container. Change it in your Docker/Compose configuration and restart.';

  // Storage
  storageSummary: StorageSummary | null = null;
  storageError: string | null = null;
  storageLoading = false;

  // Settings sidebar navigation — order + icons used by the sidebar template.
  readonly nav = SETTINGS_NAV;

  // Transfer config management
  activeTab: SettingsSection = 'copy';
  expandedHelpSection: number | null = 1;
  
  // Export/Import
  exporting = false;
  importing = false;
  importFile: File | null = null;
  importSummary: ImportSummary | null = null;
  importError: string | null = null;
  transferConfigs: TransferConfigSummary[] = [];
  loadingConfigs = false;
  editingConfig: TransferConfigRecord | null = null;
  creatingConfig = false;
  transferConfigFormError: string | null = null;
  selectedConfigId = '';

  constructor(
    private systemSvc: SystemService,
    private toastSvc: ToastService,
    private workflowSvc: WorkflowService,
  ) {}

  ngOnInit(): void {
    this.loadPreviewConfig();
    this.loadDiscordConfig();
    this.loadMediaServerConfig();
    this.loadDiscDbLookupConfig();
    this.loadTmdbConfig();
    this.loadTransferConfigs();
    this.loadStorage();
    this.loadEnvManaged();
    this.resumeDiscDbExport();
  }

  ngOnDestroy(): void {
    // Leaving the page must not keep polling a job nobody is watching.
    this.discdbPollSub?.unsubscribe();
  }

  private loadEnvManaged(): void {
    // Failure just means no fields get disabled — the settings still work,
    // so this must not block the page.
    this.systemSvc.getEnvManagedSettings().subscribe({
      next: (res) => (this.envManaged = res?.managed ?? []),
      error: () => (this.envManaged = []),
    });
  }

  /** True when this setting comes from the environment and cannot be edited here. */
  isEnvManaged(settingPath: string): boolean {
    return this.envManaged.includes(settingPath);
  }

  loadTmdbConfig(): void {
    this.tmdbError = null;
    this.systemSvc.getTmdbConfig().subscribe({
      next: (cfg) => {
        this.tmdbApiKeySet = !!cfg?.api_key_set;
        this.tmdbLoaded = true;
        // #610: pre-populate the field with the persisted value (parity
        // with MakeMKV registration). Falls back to '' for the unset path.
        this.tmdbApiKey = cfg?.api_key ?? '';
      },
      error: (err) => {
        this.tmdbError = err?.error?.detail ?? err?.message ?? 'Failed to load TMDB config.';
        this.tmdbLoaded = true;
      },
    });
  }

  saveTmdbConfig(): void {
    this.tmdbSaving = true;
    this.tmdbError = null;
    this.tmdbSaved = null;
    // Empty input clears the key; non-empty sets it.
    const keyToSend = this.tmdbApiKey?.trim() ? this.tmdbApiKey.trim() : null;
    this.systemSvc.saveTmdbConfig(keyToSend).subscribe({
      next: (cfg) => {
        this.tmdbApiKeySet = !!cfg?.api_key_set;
        this.tmdbSaving = false;
        // #610: keep the field populated with the persisted value after
        // save (cleared input → empty string echoes back as null/''; new
        // key → input now reflects what's on disk).
        this.tmdbApiKey = cfg?.api_key ?? '';
        if (!keyToSend) {
          this.tmdbSaved = 'TMDB API key cleared.';
        } else {
          // The backend runs a one-shot backfill on the no-key → key
          // transition so existing scanned discs get suggestions without
          // a re-scan. Surface the count so the user knows it happened.
          const bf = cfg?.backfill;
          if (bf && (bf.updated || bf.seeded)) {
            const seededNote = bf.seeded
              ? ` ${bf.seeded} got a pre-filled label.`
              : '';
            this.tmdbSaved =
              `TMDB API key saved. Found suggestions for ${bf.updated} existing disc(s).${seededNote}`;
            // Nudge the workflow service to refetch so the currently-loaded
            // disc surfaces its new suggestion without a manual refresh.
            this.workflowSvc.syncCoordinator();
          } else {
            this.tmdbSaved = 'TMDB API key saved.';
          }
        }
      },
      error: (err) => {
        this.tmdbSaving = false;
        this.tmdbError = err?.error?.detail ?? err?.message ?? 'Failed to save TMDB API key.';
      },
    });
  }

  clearTmdbKey(): void {
    this.tmdbApiKey = '';
    // Persist the cleared state immediately so the user doesn't have to
    // also click Save.
    this.saveTmdbConfig();
  }

  loadTransferConfigs(): void {
    this.loadingConfigs = true;
    this.systemSvc.getTransferConfigs().subscribe({
      next: (configs) => {
        this.transferConfigs = configs;
        this.loadingConfigs = false;
      },
      error: () => {
        this.loadingConfigs = false;
      }
    });
  }

  editConfig(configId: string): void {
    this.systemSvc.getTransferConfigById(configId).subscribe({
      next: (config) => {
        this.editingConfig = config;
        this.selectedConfigId = configId;
      },
      error: () => {}
    });
  }

  createConfig(): void {
    this.creatingConfig = true;
    this.editingConfig = null;
    this.selectedConfigId = '';
  }

  saveConfig(payload: TransferConfigCreate | TransferConfigUpdate): void {
    this.transferConfigFormError = null;
    if (this.editingConfig) {
      this.systemSvc.updateTransferConfig(this.editingConfig.id, payload as TransferConfigUpdate).subscribe({
        next: () => {
          this.cancelEdit();
          this.loadTransferConfigs();
        },
        error: (err) => {
          this.transferConfigFormError = err?.error?.detail ?? err?.message ?? 'Failed to save configuration.';
        }
      });
    } else {
      this.systemSvc.createTransferConfig(payload as TransferConfigCreate).subscribe({
        next: () => {
          this.cancelEdit();
          this.loadTransferConfigs();
        },
        error: (err) => {
          this.transferConfigFormError = err?.error?.detail ?? err?.message ?? 'Failed to save configuration.';
        }
      });
    }
  }

  cancelEdit(): void {
    this.editingConfig = null;
    this.creatingConfig = false;
    this.selectedConfigId = '';
    this.transferConfigFormError = null;
  }

  activateConfig(configId: string): void {
    this.systemSvc.activateTransferConfig(configId).subscribe({
      next: () => {
        this.loadTransferConfigs();
      },
      error: () => {}
    });
  }

  deleteConfig(configId: string): void {
    if (confirm('Are you sure you want to delete this transfer configuration?')) {
      this.systemSvc.deleteTransferConfig(configId).subscribe({
        next: () => {
          this.loadTransferConfigs();
        },
        error: err => {
          const raw = err.error?.detail ?? err.message ?? 'Failed to delete transfer configuration.';
          const message = typeof raw === 'string' ? raw : Array.isArray(raw) ? (raw[0]?.msg ?? raw[0] ?? String(raw)) : String(raw);
          const toastMessage = message || 'Failed to delete transfer configuration.';
          this.transferConfigFormError = message;
          this.loadTransferConfigs();
          setTimeout(() => this.toastSvc.show(toastMessage, 'error', 5000), 0);
        }
      });
    }
  }

  validateConfig(configId: string): void {
    this.systemSvc.validateTransferConfig(configId).subscribe({
      next: (result) => {
        if (result.success) {
          alert('Connection validated successfully!');
        } else {
          alert(`Validation failed: ${result.message}`);
        }
      },
      error: () => {
        alert('Failed to validate connection');
      }
    });
  }

  checkHealth(configId: string): void {
    this.selectedConfigId = configId;
  }

  /** #635 commit C: kick off a destination capability probe. Fire-and-forget
   * — result comes back via the `transfer_config_capabilities_updated` WS
   * event, which the systemSvc-subscribed observers will pick up on the
   * next config-list refresh. For now, refetch the list a few seconds
   * after enqueue as a fallback until the WS wiring lands. */
  probeCapabilities(configId: string): void {
    this.systemSvc.probeTransferCapabilities(configId).subscribe({
      next: () => {
        this.toastSvc.show('Probing destination…', 'info', 3000);
        setTimeout(() => this.loadTransferConfigs(), 3000);
      },
      error: (err) => {
        console.error('Failed to enqueue capability probe:', err);
        // #204: surface the failure so the user knows the click didn't take.
        this.toastSvc.show(`Probe failed: ${formatHttpErrorDetail(err)}`, 'error', 6000);
      },
    });
  }

  loadStorage(): void {
    this.storageError = null;
    this.storageLoading = true;
    this.systemSvc.getStorageSummary().subscribe({
      next: summary => {
        this.storageSummary = summary;
        this.storageLoading = false;
      },
      error: err => {
        this.storageError = err.error?.detail || err.message || 'Failed to load storage information';
        this.storageLoading = false;
      },
    });
  }

  /** Color for storage bar (free %): green above 25%, amber 10–25%, red below 10%. */
  getStorageBarColor(free: number, total: number): string {
    if (!total) return 'rgba(255,255,255,0.2)';
    const pct = (free / total) * 100;
    if (pct < 10) return '#ef4444';
    if (pct < 25) return '#f59e0b';
    return '#22c55e';
  }

  formatBytes(bytes: number): string {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
  }

  /** True when Discord webhook is enabled and a URL is set (Discord column available). */
  get discordConfigured(): boolean {
    return !!(this.discord?.enabled && (this.discord?.webhook_url || '').trim());
  }

  loadPreviewConfig(): void {
    this.previewError = null;
    this.previewSaved = null;
    this.systemSvc.getPreviewConfig().subscribe({
      next: cfg => {
        this.preview = { ...cfg };
      },
      error: err => {
        this.previewError = err.error?.detail || err.message || 'Failed to load preview settings';
      },
    });
  }

  savePreviewConfig(): void {
    this.previewError = null;
    this.previewSaved = null;
    this.previewSaving = true;
    this.systemSvc.savePreviewConfig(this.preview).subscribe({
      next: cfg => {
        this.preview = { ...cfg };
        this.previewSaved = 'Preview settings saved successfully!';
        this.previewSaving = false;
      },
      error: err => {
        this.previewError = err.error?.detail || err.message || 'Failed to save preview settings';
        this.previewSaving = false;
      },
    });
  }

  loadDiscordConfig(): void {
    this.discordError = null;
    this.discordSaved = null;
    this.systemSvc.getDiscordConfig().subscribe({
      next: cfg => {
        this.discord = mergeDiscordConfig(cfg);
      },
      error: err => {
        this.discordError = err.error?.detail || err.message || 'Failed to load notification settings';
      },
    });
  }

  saveDiscordConfig(): void {
    this.discordError = null;
    this.discordSaved = null;
    this.discordSaving = true;
    this.systemSvc.saveDiscordConfig(this.discord).subscribe({
      next: cfg => {
        this.discord = mergeDiscordConfig(cfg);
        this.discordSaved = 'Notification settings saved successfully!';
        this.discordSaving = false;
      },
      error: err => {
        this.discordError = err.error?.detail || err.message || 'Failed to save notification settings';
        this.discordSaving = false;
      },
    });
  }

  loadMediaServerConfig(): void {
    this.mediaServerError = null;
    this.mediaServerSaved = null;
    this.systemSvc.getMediaServerConfig().subscribe({
      next: cfg => {
        this.mediaServer = { ...cfg };
      },
      error: err => {
        this.mediaServerError = err.error?.detail || err.message || 'Failed to load library settings';
      },
    });
  }

  saveMediaServerConfig(): void {
    this.mediaServerError = null;
    this.mediaServerSaved = null;
    this.librarySaving = true;
    this.systemSvc.saveMediaServerConfig(this.mediaServer).subscribe({
      next: cfg => {
        this.mediaServer = { ...cfg };
        this.mediaServerSaved = 'Library settings saved successfully!';
        this.librarySaving = false;
      },
      error: err => {
        this.mediaServerError = err.error?.detail || err.message || 'Failed to save library settings';
        this.librarySaving = false;
      },
    });
  }

  loadDiscDbLookupConfig(): void {
    this.discDbLookupError = null;
    this.discDbLookupSaved = null;
    this.systemSvc.getDiscdbLookupConfig().subscribe({
      next: cfg => {
        this.discDbLookup = { ...cfg };
      },
      error: err => {
        this.discDbLookupError = err.error?.detail || err.message || 'Failed to load copy settings';
      },
    });
    this.systemSvc.getAutoRipConfig().subscribe({
      next: cfg => {
        this.autoRip = { ...cfg };
      },
      error: err => {
        this.discDbLookupError = err.error?.detail || err.message || 'Failed to load auto-rip setting';
      },
    });
  }

  saveDiscDbLookupConfig(): void {
    this.discDbLookupError = null;
    this.discDbLookupSaved = null;
    this.discDbLookupSaving = true;
    this.systemSvc.saveDiscdbLookupConfig(this.discDbLookup).subscribe({
      next: cfg => {
        this.discDbLookup = { ...cfg };
        // Same card, same Save button: persist the auto-rip toggle too.
        this.systemSvc.saveAutoRipConfig(this.autoRip).subscribe({
          next: arCfg => {
            this.autoRip = { ...arCfg };
            this.discDbLookupSaved = 'Copy settings saved successfully!';
            this.discDbLookupSaving = false;
          },
          error: err => {
            this.discDbLookupError = err.error?.detail || err.message || 'Failed to save auto-rip setting';
            this.discDbLookupSaving = false;
          },
        });
      },
      error: err => {
        this.discDbLookupError = err.error?.detail || err.message || 'Failed to save copy settings';
        this.discDbLookupSaving = false;
      },
    });
  }

  exportHistory(): void {
    this.exporting = true;
    this.systemSvc.exportHistory().subscribe({
      next: (blob: Blob) => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5);
        a.download = `mkv-auto-export-${timestamp}.zip`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        this.exporting = false;
        this.toastSvc.show('Export completed successfully', 'success');
      },
      error: err => {
        this.exporting = false;
        this.toastSvc.show(err.error?.detail || err.message || 'Export failed', 'error');
      },
    });
  }

  /** #741 — start the bulk export and follow it to completion. */
  exportDiscDbSubmissions(): void {
    this.discdbExporting = true;
    this.discdbExportError = null;
    this.discdbExportResult = null;
    this.discdbExportDone = 0;
    this.discdbExportTotal = 0;
    this.systemSvc.startDiscDbExport().subscribe({
      next: job => {
        this.discdbExportJobId = job.job_id;
        this.applyDiscDbExportStatus(job);
        this.pollDiscDbExport(job.job_id);
      },
      error: err => this.failDiscDbExport(err),
    });
  }

  cancelDiscDbExport(): void {
    if (!this.discdbExportJobId) return;
    this.systemSvc.cancelDiscDbExport(this.discdbExportJobId).subscribe({
      // The worker stops between discs, so the terminal state still arrives
      // through the poll — nothing to do here but let it.
      next: () => {},
      error: () => {},
    });
  }

  /**
   * Reattach to whatever the server has, so leaving the page never costs work.
   *
   * Two distinct cases: an export still running (rejoin and keep polling), or
   * one that finished while nobody was watching (offer the archive). The second
   * is the common one — these take a while, which is why people navigate away —
   * and without it the finished zip would sit out its retention window
   * unreachable and have to be rebuilt from scratch.
   */
  private resumeDiscDbExport(): void {
    this.systemSvc.getActiveDiscDbExport().subscribe({
      next: job => {
        if (!job || job.status === 'idle') return;
        const active = job as DiscDbExportJob;
        this.discdbExportJobId = active.job_id;
        this.applyDiscDbExportStatus(active);

        if (active.status === 'pending' || active.status === 'running') {
          this.discdbExporting = true;
          this.pollDiscDbExport(active.job_id);
        } else if (active.download_ready) {
          // Offered, not auto-downloaded: a file landing unprompted every time
          // you open Settings would be obnoxious.
          this.discdbExportReady = active;
        }
      },
      error: () => {},
    });
  }

  /** Collect a finished archive without rebuilding it. */
  downloadReadyDiscDbExport(): void {
    const job = this.discdbExportReady;
    if (!job) return;
    this.discdbExportError = null;
    this.systemSvc.downloadDiscDbExport(job.job_id).subscribe({
      next: file => this.saveDiscDbExport(file, job),
      error: err => {
        // 410: retention swept it, or the tmp volume was cleared.
        this.discdbExportReady = null;
        this.failDiscDbExport(err);
      },
    });
  }

  dismissReadyDiscDbExport(): void {
    this.discdbExportReady = null;
  }

  private pollDiscDbExport(jobId: string): void {
    this.discdbPollSub?.unsubscribe();
    this.discdbPollSub = interval(1000)
      .pipe(switchMap(() => this.systemSvc.getDiscDbExportStatus(jobId)))
      .subscribe({
        next: job => {
          this.applyDiscDbExportStatus(job);
          if (job.status === 'completed') {
            this.discdbPollSub?.unsubscribe();
            this.downloadDiscDbExport(jobId, job);
          } else if (job.status === 'failed') {
            this.discdbPollSub?.unsubscribe();
            this.discdbExporting = false;
            this.discdbExportError = job.error || 'DiscDB submission export failed';
          }
        },
        error: err => {
          this.discdbPollSub?.unsubscribe();
          this.failDiscDbExport(err);
        },
      });
  }

  private applyDiscDbExportStatus(job: DiscDbExportJob): void {
    this.discdbExportDone = job.done;
    this.discdbExportTotal = job.total;
    this.discdbExportCurrent = job.current;
  }

  private downloadDiscDbExport(jobId: string, job: DiscDbExportJob): void {
    this.systemSvc.downloadDiscDbExport(jobId).subscribe({
      next: file => this.saveDiscDbExport(file, job),
      error: err => {
        // The archive exists even though this attempt failed, so keep offering
        // it rather than making the user rebuild over a transient hiccup.
        this.discdbExportReady = job.download_ready ? job : null;
        this.failDiscDbExport(err);
      },
    });
  }

  private saveDiscDbExport(file: { blob: Blob; filename: string }, job: DiscDbExportJob): void {
    const url = window.URL.createObjectURL(file.blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = file.filename;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    document.body.removeChild(a);
    this.discdbExporting = false;
    this.discdbExportReady = null;
    // Say what was left out as well as what went in — a silent partial export
    // reads as "everything", and the user would submit thinking so.
    this.discdbExportResult =
      `${job.included} disc${job.included === 1 ? '' : 's'} exported` +
      (job.skipped ? ` — ${job.skipped} skipped, see README.txt in the zip` : '') +
      (job.cancelled ? ' (cancelled early)' : '') +
      '. Unzip it over your fork of TheDiscDb/data and open a pull request.';
  }

  private failDiscDbExport(err: any): void {
    this.discdbExporting = false;
    this.discdbExportError =
      err?.error?.detail || err?.message || 'DiscDB submission export failed';
  }

  onFileSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (input.files && input.files.length > 0) {
      this.importFile = input.files[0];
      this.importError = null;
      this.importSummary = null;
    }
  }

  clearImportFile(): void {
    this.importFile = null;
    this.importSummary = null;
    this.importError = null;
  }

  getTotalSkipped(summary: ImportSummary): number {
    return summary.movies_skipped + summary.releases_skipped + summary.discs_skipped + 
           summary.jobs_skipped + summary.disc_titles_skipped + summary.title_streams_skipped +
           summary.boxsets_skipped + summary.boxset_releases_skipped;
  }

  // ── Support bundle (#804) ───────────────────────────────────────────
  bundleState: 'idle' | 'working' = 'idle';
  bundleNote = '';
  bundleError = '';
  bundleBlocked = '';

  /** Ask before showing the button whether collection is possible.
   *
   *  Collecting takes MakeMKV's drive lock, so it is refused during a rip —
   *  a bundle missing the drive enumeration is not worth interrupting a rip
   *  for. Checking up front means the user sees "come back after your rip"
   *  instead of clicking into an error.
   */
  refreshBundleAvailability(): void {
    this.systemSvc.supportBundleAvailability().subscribe({
      next: a => (this.bundleBlocked = a.available ? '' : a.message ?? 'Unavailable right now.'),
      // A failed check must not disable the button — the POST enforces this
      // anyway, so fall back to letting them try.
      error: () => (this.bundleBlocked = ''),
    });
  }

  downloadSupportBundle(): void {
    this.bundleState = 'working';
    this.bundleNote = '';
    this.bundleError = '';

    this.systemSvc.downloadSupportBundle().subscribe({
      next: res => {
        const blob = res.body;
        if (!blob || blob.size === 0) {
          // A 200 with nothing attached is worse than an error: the user sends
          // an empty file and we both waste a round trip working out why.
          this.bundleState = 'idle';
          this.bundleError = 'The server returned an empty bundle. Please report this.';
          return;
        }

        const name =
          this.filenameFromDisposition(res.headers.get('content-disposition')) ??
          `mkv-auto-support-${new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5)}.tar.gz`;

        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);

        this.bundleState = 'idle';
        this.bundleNote =
          res.headers.get('X-Support-Bundle-Makemkv-Probe') === 'skipped'
            ? 'Saved. The drive scan was skipped because a rip is in progress — ' +
              'running it would have stalled the rip. Everything else is included.'
            : 'Saved.';
        this.toastSvc.show('Support bundle downloaded', 'success');
      },
      error: async err => {
        this.bundleState = 'idle';
        // responseType 'blob' means an error body arrives as a Blob too, so the
        // usual err.error?.detail is undefined and the user would see a bare
        // "Http failure response". Read it back as text to recover the reason.
        this.bundleError = await this.errorDetailFromBlob(err);
        this.toastSvc.show(this.bundleError, 'error');
        this.refreshBundleAvailability();
      },
    });
  }

  private filenameFromDisposition(header: string | null): string | null {
    const match = header?.match(/filename="?([^";]+)"?/i);
    return match ? match[1] : null;
  }

  private async errorDetailFromBlob(err: unknown): Promise<string> {
    const e = err as { error?: unknown; message?: string; status?: number };
    if (e?.error instanceof Blob) {
      try {
        const parsed = JSON.parse(await e.error.text());
        if (parsed?.detail) return String(parsed.detail);
      } catch {
        /* not JSON — fall through to the generic message */
      }
    }
    const detail = (e?.error as { detail?: string } | undefined)?.detail;
    return detail || e?.message || 'Could not collect the support bundle.';
  }

  selectTab(id: SettingsSection): void {
    this.activeTab = id;
    // Check on entry rather than on a timer: whether a rip is running can
    // change while the page is open, and this is the moment it matters.
    if (id === 'help') this.refreshBundleAvailability();
  }

  toggleHelpSection(sectionId: number): void {
    this.expandedHelpSection = this.expandedHelpSection === sectionId ? null : sectionId;
  }

  importHistory(): void {
    if (!this.importFile) {
      return;
    }

    this.importing = true;
    this.importError = null;
    this.importSummary = null;

    this.systemSvc.importHistory(this.importFile).subscribe({
      next: summary => {
        this.importSummary = summary;
        this.importing = false;
        this.importFile = null;
        this.toastSvc.show('Import completed successfully', 'success');
      },
      error: err => {
        this.importing = false;
        this.importError = err.error?.detail || err.message || 'Import failed';
        this.toastSvc.show(this.importError || 'Import failed', 'error');
      },
    });
  }
}
