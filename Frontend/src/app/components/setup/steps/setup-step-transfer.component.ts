import { Component, EventEmitter, Input, Output, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { IconComponent } from '../../../ui/icon/icon.component';
import { SystemService } from '../../../services/system.service';

export interface TransferStepData {
  configured: boolean;
  configId?: string;
  configName?: string;
  configMode?: string;
  configPath?: string;
}

@Component({
  selector: 'app-setup-step-transfer',
  standalone: true,
  imports: [CommonModule, FormsModule, IconComponent],
  template: `
    <div class="setup-step">
      <!-- Header -->
      <div class="setup-step-header">
        <div class="setup-step-icon setup-step-icon-purple">
          <ui-icon name="folder" [size]="24"></ui-icon>
        </div>
        <div class="setup-step-header-text">
          <h3 class="setup-step-title">Transfer Destination</h3>
          <p class="setup-step-desc">Configure where your processed media files will be transferred. This can be a local folder or an SMB share your media server (Plex/Jellyfin) can access — other network storage works too if you mount it into the container and use local mode.</p>
        </div>
      </div>

      <!-- Configuration Form -->
      <div *ngIf="showForm" class="setup-step-body">
        <!-- Basic Fields -->
        <div class="setup-step-grid">
          <!-- Name -->
          <div>
            <label class="setup-step-label">
              Configuration Name <span class="text-red-400">*</span>
            </label>
            <input
              type="text"
              [(ngModel)]="formData.name"
              placeholder="e.g., Plex Library, Main Storage"
              class="setup-step-input"
            />
          </div>

          <!-- Mode -->
          <div>
            <label class="setup-step-label">
              Transfer Mode <span class="text-red-400">*</span>
            </label>
            <select [(ngModel)]="formData.mode" class="setup-step-input">
              <option value="local">Local</option>
              <!-- rsync + NFS hidden for v1 (#664 / #666); backends untouched. -->
              <option value="smb">SMB/CIFS</option>
            </select>
          </div>
        </div>

        <div>
          <label class="setup-step-label">
            Transfer Path <span class="text-red-400">*</span>
          </label>
          <input
            type="text"
            [(ngModel)]="formData.transfer_dir"
            required
            [placeholder]="formData.mode === 'local' ? '/mnt/nas/media' : '/path/to/destination'"
            class="setup-step-input setup-step-input-mono"
          />
        </div>

        <!-- Mode-Specific Fields - Rsync -->
        <div *ngIf="formData.mode === 'rsync'" class="setup-step-mode-config setup-step-mode-rsync">
          <h5 class="setup-step-mode-title">Rsync Configuration</h5>
          <div class="setup-step-grid">
            <div>
              <label class="setup-step-label-sm">Host *</label>
              <input
                type="text"
                [(ngModel)]="rsyncConfig.host"
                placeholder="backup.server.com"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">User *</label>
              <input
                type="text"
                [(ngModel)]="rsyncConfig.user"
                placeholder="username"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">Port</label>
              <input
                type="number"
                [(ngModel)]="rsyncConfig.port"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">Bandwidth Limit (Mbps)</label>
              <input
                type="number"
                [(ngModel)]="rsyncConfig.bwlimit"
                placeholder="0 = unlimited"
                class="setup-step-input-sm"
              />
            </div>
          </div>
        </div>

        <!-- Mode-Specific Fields - SMB -->
        <div *ngIf="formData.mode === 'smb'" class="setup-step-mode-config setup-step-mode-smb">
          <h5 class="setup-step-mode-title">SMB/CIFS Configuration</h5>
          <div class="setup-step-grid">
            <div>
              <label class="setup-step-label-sm">Host *</label>
              <input
                type="text"
                [(ngModel)]="smbConfig.host"
                placeholder="nas.local"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">Share *</label>
              <input
                type="text"
                [(ngModel)]="smbConfig.share"
                placeholder="media"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">Port</label>
              <input
                type="number"
                [(ngModel)]="smbConfig.port"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">Username</label>
              <input
                type="text"
                [(ngModel)]="smbConfig.username"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">Password</label>
              <input
                type="password"
                [(ngModel)]="smbConfig.password"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">Domain (optional)</label>
              <input
                type="text"
                [(ngModel)]="smbConfig.domain"
                placeholder="WORKGROUP"
                class="setup-step-input-sm"
              />
            </div>
          </div>
        </div>

        <!-- Mode-Specific Fields - NFS -->
        <div *ngIf="formData.mode === 'nfs'" class="setup-step-mode-config setup-step-mode-nfs">
          <h5 class="setup-step-mode-title">NFS Configuration</h5>
          <div class="setup-step-grid">
            <div>
              <label class="setup-step-label-sm">Server *</label>
              <input
                type="text"
                [(ngModel)]="nfsConfig.server"
                placeholder="nfs.server.local"
                class="setup-step-input-sm"
              />
            </div>
            <div>
              <label class="setup-step-label-sm">Export Path *</label>
              <input
                type="text"
                [(ngModel)]="nfsConfig.export_path"
                placeholder="/export/media"
                class="setup-step-input-sm"
              />
            </div>
          </div>
        </div>

        <!-- Advanced Options Toggle -->
        <button
          type="button"
          (click)="showAdvanced = !showAdvanced"
          class="setup-step-advanced-toggle"
        >
          <ui-icon name="settings" [size]="16"></ui-icon>
          <span>{{ showAdvanced ? 'Hide' : 'Show' }} Advanced Options</span>
          <ui-icon name="down" [size]="16" [class.rotate-180]="showAdvanced" style="transition: transform 0.2s; display: inline-flex;"></ui-icon>
        </button>

        <!-- Advanced Options -->
        <div *ngIf="showAdvanced" class="setup-step-advanced">
          <!-- Conflict Resolution -->
          <div>
            <label class="setup-step-label">Conflict Resolution</label>
            <p class="setup-step-help-text">How to handle files that already exist at the destination</p>
            <select [(ngModel)]="formData.conflict_resolution" class="setup-step-input">
              <option value="rename">Rename - Add suffix to new file</option>
              <option value="skip">Skip - Keep existing file</option>
              <option value="overwrite">Overwrite - Replace existing file</option>
              <option value="fail">Fail - Stop transfer on conflict</option>
            </select>
          </div>

          <!-- Health Check Interval -->
          <div>
            <label class="setup-step-label">Health Check Interval (minutes)</label>
            <p class="setup-step-help-text">How often to check destination connectivity and storage space</p>
            <input
              type="number"
              min="5"
              max="1440"
              [(ngModel)]="formData.health_check_interval_minutes"
              class="setup-step-input"
            />
          </div>
        </div>

        <div *ngIf="createError" class="setup-step-error">
          <span class="text-red-400">{{ createError }}</span>
        </div>

        <!-- Create Button -->
        <button
          type="button"
          (click)="handleCreate()"
          [disabled]="!isFormValid()"
          class="setup-step-btn setup-step-btn-create"
          [class.setup-step-btn-disabled]="!isFormValid()"
        >
          <ui-icon name="plus" [size]="16"></ui-icon>
          Create Transfer Configuration
        </button>
      </div>

      <!-- Success State -->
      <div *ngIf="!showForm" class="setup-step-success">
        <div class="setup-step-success-inner">
          <div class="setup-step-success-icon">
            <ui-icon name="check" [size]="20"></ui-icon>
          </div>
          <div class="setup-step-success-content">
            <h4 class="setup-step-success-title">Transfer destination configured</h4>
            <p class="setup-step-success-desc">
              {{ formData.name }} ({{ formData.mode.toUpperCase() }}) → <span class="setup-step-success-path">{{ formData.transfer_dir }}</span>
            </p>
          </div>
        </div>
        <button
          type="button"
          (click)="showForm = true"
          class="setup-step-success-edit"
        >
          Change configuration
        </button>
      </div>

      <!-- Info Box -->
      <div class="setup-step-info setup-step-info-purple">
        <p class="setup-step-info-title">💡 Transfer tips</p>
        <ul class="setup-step-info-list">
          <li>Local mode works best for directly attached storage (including a NAS export mounted into the container)</li>
          <li>SMB is perfect for Windows shares and NAS devices</li>
          <li>You can add more destinations later in Settings</li>
        </ul>
      </div>
    </div>
  `,
  styles: [`
    .setup-step-header { display: flex; gap: 1rem; align-items: flex-start; margin-bottom: 1.5rem; }
    .setup-step-icon { width: 3rem; height: 3rem; border-radius: 0.75rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .setup-step-icon-purple { background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); box-shadow: 0 0 20px rgba(139, 92, 246, 0.3); color: #fff; }
    .setup-step-header-text { flex: 1; }
    .setup-step-title { font-size: 1.125rem; font-weight: 700; color: #fff; margin: 0 0 0.5rem 0; }
    .setup-step-desc { font-size: 0.875rem; color: rgba(255,255,255,0.7); margin: 0; line-height: 1.5; }
    .setup-step-body { display: flex; flex-direction: column; gap: 1rem; }
    .setup-step-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }
    .setup-step-label { display: block; font-size: 0.875rem; font-weight: 500; color: rgba(255,255,255,0.8); margin-bottom: 0.5rem; }
    .setup-step-label-sm { display: block; font-size: 0.75rem; font-weight: 500; color: rgba(255,255,255,0.7); margin-bottom: 0.5rem; }
    .setup-step-input { width: 100%; padding: 0.625rem 1rem; font-size: 0.875rem; color: #fff; border-radius: 0.5rem; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); transition: all 0.2s; }
    .setup-step-input:focus { outline: none; ring: 2px solid rgba(139, 92, 246, 0.5); }
    .setup-step-input-mono { font-family: monospace; }
    .setup-step-input-sm { width: 100%; padding: 0.5rem 0.75rem; font-size: 0.875rem; color: #fff; border-radius: 0.5rem; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); }
    .setup-step-input-sm:focus { outline: none; ring: 2px solid rgba(139, 92, 246, 0.5); }
    .setup-step-mode-config { padding: 1rem; border-radius: 0.5rem; display: flex; flex-direction: column; gap: 1rem; }
    .setup-step-mode-rsync { background: rgba(139, 92, 246, 0.05); border: 1px solid rgba(139, 92, 246, 0.2); }
    .setup-step-mode-smb { background: rgba(6, 182, 212, 0.05); border: 1px solid rgba(6, 182, 212, 0.2); }
    .setup-step-mode-nfs { background: rgba(16, 185, 129, 0.05); border: 1px solid rgba(16, 185, 129, 0.2); }
    .setup-step-mode-title { font-size: 0.875rem; font-weight: 700; color: #fff; margin: 0; }
    .setup-step-advanced-toggle { display: flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; color: rgba(196, 181, 253, 0.8); background: none; border: none; cursor: pointer; transition: color 0.2s; }
    .setup-step-advanced-toggle:hover { color: #c4b5fd; }
    .setup-step-advanced { padding: 1rem; border-radius: 0.5rem; background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.1); display: flex; flex-direction: column; gap: 1rem; }
    .setup-step-help-text { font-size: 0.75rem; color: rgba(255,255,255,0.5); margin-bottom: 0.5rem; }
    .setup-step-checkboxes { display: flex; flex-direction: column; gap: 0.75rem; }
    .setup-step-checkbox { display: flex; align-items: start; gap: 0.75rem; padding: 0.75rem; border-radius: 0.5rem; cursor: pointer; transition: background 0.2s; }
    .setup-step-checkbox:hover { background: rgba(255,255,255,0.05); }
    .setup-step-checkbox input { margin-top: 0.125rem; width: 1rem; height: 1rem; cursor: pointer; }
    .setup-step-checkbox-content { flex: 1; }
    .setup-step-checkbox-label { font-size: 0.875rem; font-weight: 500; color: #fff; }
    .setup-step-checkbox-desc { font-size: 0.75rem; color: rgba(255,255,255,0.5); margin-top: 0.25rem; }
    .setup-step-btn { width: 100%; padding: 0.75rem 1rem; border-radius: 0.5rem; font-size: 0.875rem; font-weight: 500; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 0.5rem; transition: all 0.2s; }
    .setup-step-btn-create { background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); color: #fff; box-shadow: 0 0 15px rgba(139, 92, 246, 0.3); }
    .setup-step-btn-disabled { opacity: 0.4; cursor: not-allowed; background: rgba(255,255,255,0.1); box-shadow: none; }
    .setup-step-success { padding: 1.5rem; border-radius: 0.5rem; background: rgba(34, 197, 94, 0.08); border: 1px solid rgba(34, 197, 94, 0.3); display: flex; flex-direction: column; gap: 1rem; }
    .setup-step-success-inner { display: flex; align-items: center; gap: 0.75rem; }
    .setup-step-success-icon { width: 2.5rem; height: 2.5rem; border-radius: 0.5rem; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%); box-shadow: 0 0 15px rgba(34, 197, 94, 0.3); color: #fff; flex-shrink: 0; }
    .setup-step-success-content { flex: 1; }
    .setup-step-success-title { font-weight: 500; color: #86efac; margin: 0 0 0.25rem 0; }
    .setup-step-success-desc { font-size: 0.875rem; color: rgba(134, 239, 172, 0.7); margin: 0; }
    .setup-step-success-path { font-family: monospace; }
    .setup-step-success-edit { font-size: 0.875rem; color: rgba(134, 239, 172, 0.8); background: none; border: none; cursor: pointer; transition: color 0.2s; padding: 0; text-align: left; }
    .setup-step-success-edit:hover { color: #86efac; }
    .setup-step-info { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; }
    .setup-step-info-purple { background: rgba(139, 92, 246, 0.08); border: 1px solid rgba(139, 92, 246, 0.2); }
    .setup-step-info-title { color: #c4b5fd; font-weight: 500; margin: 0 0 0.5rem 0; }
    .setup-step-info-list { margin: 0; padding-left: 1rem; color: rgba(196, 181, 253, 0.8); font-size: 0.75rem; line-height: 1.6; }
    .setup-step-error { margin-top: 0.5rem; margin-bottom: 0.5rem; padding: 0.75rem 1rem; border-radius: 0.5rem; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); font-size: 0.875rem; }
    @media (min-width: 640px) {
      .setup-step-info-list { font-size: 0.875rem; }
    }
  `],
})
export class SetupStepTransferComponent implements OnInit {
  @Input() data!: TransferStepData;
  @Output() dataChange = new EventEmitter<Partial<TransferStepData>>();

  showForm = true;
  showAdvanced = false;

  formData = {
    name: '',
    mode: 'local' as 'local' | 'rsync' | 'smb' | 'nfs',
    transfer_dir: '',
    conflict_resolution: 'overwrite' as 'overwrite' | 'skip' | 'rename' | 'fail',
    health_check_interval_minutes: 60,
  };

  rsyncConfig = {
    host: '',
    user: '',
    port: 22,
    path: '',
    bwlimit: 0,
  };

  smbConfig = {
    host: '',
    share: '',
    port: 445,
    path: '',
    username: '',
    password: '',
    domain: '',
  };

  nfsConfig = {
    server: '',
    export_path: '',
    path: '',
  };

  /** API validation or create error message. */
  createError: string | null = null;

  constructor(private system: SystemService) {}

  ngOnInit(): void {
    // Check if already configured via parent data
    if (this.data.configured) {
      this.showForm = false;
      // Restore form data if available
      if (this.data.configName) {
        this.formData.name = this.data.configName;
        this.formData.mode = this.data.configMode as any || 'local';
        this.formData.transfer_dir = this.data.configPath || '';
      }
      return;
    }
    
    // Check for existing transfer configs from API
    this.system.getTransferConfigs().subscribe({
      next: (configs) => {
        if (configs && configs.length > 0) {
          // Found existing config - mark as configured
          const activeConfig = configs.find(c => c.is_active) || configs[0];
          this.formData.name = activeConfig.name || '';
          this.formData.mode = activeConfig.mode as any;
          this.formData.transfer_dir = activeConfig.transfer_dir || (activeConfig as any).config_data?.path || '';
          this.showForm = false;
          this.dataChange.emit({
            configured: true,
            configId: activeConfig.id,
            configName: activeConfig.name || undefined,
            configMode: activeConfig.mode,
            configPath: activeConfig.transfer_dir || (activeConfig as any).config_data?.path || undefined,
          });
        }
      },
      error: (err) => {
        console.error('Failed to load transfer configs:', err);
      },
    });
  }

  isFormValid(): boolean {
    if (!this.formData.name) return false;
    // Transfer Path required for all modes
    if (!this.formData.transfer_dir || !String(this.formData.transfer_dir).trim()) return false;
    if (this.formData.mode === 'rsync') {
      return !!(this.rsyncConfig.host && this.rsyncConfig.user);
    }
    if (this.formData.mode === 'smb') {
      return !!(this.smbConfig.host && this.smbConfig.share);
    }
    if (this.formData.mode === 'nfs') {
      return !!(this.nfsConfig.server && this.nfsConfig.export_path);
    }
    return true;
  }

  handleCreate(): void {
    if (!this.isFormValid()) return;
    
    const payload: any = {
      name: this.formData.name,
      mode: this.formData.mode,
      transfer_dir: this.formData.transfer_dir,
      conflict_resolution: this.formData.conflict_resolution,
      health_check_interval_minutes: this.formData.health_check_interval_minutes,
    };

    // Add mode-specific config_data; path comes from Transfer Path (transfer_dir)
    if (this.formData.mode === 'rsync') {
      payload.config_data = { ...this.rsyncConfig, path: this.formData.transfer_dir ?? '' };
    } else if (this.formData.mode === 'smb') {
      payload.config_data = { ...this.smbConfig, path: this.formData.transfer_dir ?? '' };
    } else if (this.formData.mode === 'nfs') {
      payload.config_data = { ...this.nfsConfig, path: this.formData.transfer_dir ?? '' };
    }

    // Call API to create config
    this.createError = null;
    this.system.createTransferConfig(payload).subscribe({
      next: () => {
        this.showForm = false;
        this.dataChange.emit({
          configured: true,
          configName: this.formData.name,
          configMode: this.formData.mode,
          configPath: this.formData.transfer_dir,
        });
      },
      error: (err) => {
        this.createError = err?.error?.detail ?? err?.message ?? 'Failed to create transfer configuration.';
      },
    });
  }
}
