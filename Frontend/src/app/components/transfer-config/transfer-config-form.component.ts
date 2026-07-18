import { Component, Input, Output, EventEmitter, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SystemService, TransferConfigCreate, TransferConfigUpdate, TransferConfigRecord } from '../../services/system.service';
import { LoggerService } from '../../services/logger.service';
import { PathTemplateEditorComponent } from './path-template-editor.component';

@Component({
  selector: 'app-transfer-config-form',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <form (ngSubmit)="onSubmit()" class="space-y-6">
      <!-- Header -->
      <div class="flex items-center justify-between">
        <div>
          <h4 class="text-base font-bold text-white">
            {{ config ? 'Edit Transfer Config' : 'New Transfer Config' }}
          </h4>
          <p class="text-sm text-white/60">
            {{ config ? 'Update your transfer configuration' : 'Create a new transfer destination' }}
          </p>
        </div>
        <div class="flex items-center gap-2">
          <button
            type="button"
            (click)="onCancel.emit()"
            class="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-white/70 hover:text-white hover:bg-white/5 transition-all"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            Cancel
          </button>
          <button
            type="submit"
            [disabled]="saving || !requiredFieldsValid"
            class="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-white transition-all"
            [class.opacity-40]="saving || !requiredFieldsValid"
            [class.cursor-not-allowed]="saving || !requiredFieldsValid"
            [class.hover:scale-105]="!(saving || !requiredFieldsValid)"
            [style.background]="'linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%)'"
            [style.boxShadow]="'0 0 20px rgba(59, 130, 246, 0.3)'"
          >
            <svg *ngIf="saving" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="animate-spin"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
            <svg *ngIf="!saving" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
            {{ saving ? 'Saving...' : 'Save Config' }}
          </button>
        </div>
      </div>

      <!-- Error Message -->
      <div
        *ngIf="formError || validationError"
        class="flex items-start gap-2 p-3 rounded-lg text-sm"
        [style.background]="'rgba(239, 68, 68, 0.1)'"
        [style.border]="'1px solid rgba(239, 68, 68, 0.3)'"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="flex-shrink-0 mt-0.5" style="color: #f87171;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span class="text-red-400">{{ formError || validationError }}</span>
      </div>

      <!-- Basic Fields -->
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label class="block text-sm font-medium text-white/80 mb-2">
            Config Name <span class="text-red-400">*</span>
          </label>
          <input
            type="text"
            [(ngModel)]="formData.name"
            name="name"
            placeholder="My NAS"
            class="w-full px-4 py-2.5 text-sm text-white rounded-lg transition-all focus:outline-none focus:ring-2 focus:ring-blue-500/50"
            [style.background]="'rgba(255, 255, 255, 0.06)'"
            [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
          />
        </div>

        <div>
          <label class="block text-sm font-medium text-white/80 mb-2">
            Transfer Mode <span class="text-red-400">*</span>
          </label>
          <select
            [(ngModel)]="formData.mode"
            name="mode"
            [disabled]="!!config"
            class="w-full px-4 py-2.5 text-sm text-white rounded-lg transition-all focus:outline-none focus:ring-2 focus:ring-blue-500/50"
            [style.background]="'rgba(255, 255, 255, 0.06)'"
            [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
          >
            <option value="local">Local</option>
            <!-- rsync + NFS hidden for v1. rsync: no SSH-key upload control yet
                 (#664). NFS: fragile in-container mount (rpc.statd/nolock) and
                 redundant with mounting the export as a Docker volume + Local
                 (#666). Backends untouched; re-enable in v1.0.1. -->
            <option value="smb">SMB/CIFS</option>
          </select>
        </div>
      </div>

      <div>
        <label class="block text-sm font-medium text-white/80 mb-2">
          Transfer Path <span class="text-red-400">*</span>
        </label>
        <input
          type="text"
          [(ngModel)]="formData.transfer_dir"
          name="transfer_dir"
          required
          [placeholder]="formData.mode === 'local' ? '/mnt/nas/media' : '/path/to/destination'"
          class="w-full px-4 py-2.5 text-sm font-mono text-white rounded-lg transition-all focus:outline-none focus:ring-2 focus:ring-blue-500/50"
          [style.background]="'rgba(255, 255, 255, 0.06)'"
          [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
        />
      </div>

      <!-- Mode-Specific Fields: Rsync -->
      <div
        *ngIf="formData.mode === 'rsync'"
        class="p-4 rounded-lg space-y-4"
        [style.background]="'rgba(139, 92, 246, 0.05)'"
        [style.border]="'1px solid rgba(139, 92, 246, 0.2)'"
      >
        <h5 class="text-sm font-bold text-white">Rsync Configuration</h5>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Host</label>
            <input
              type="text"
              [(ngModel)]="rsyncConfig.host"
              name="rsync_host"
              placeholder="backup.server.com"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-purple-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">User</label>
            <input
              type="text"
              [(ngModel)]="rsyncConfig.user"
              name="rsync_user"
              placeholder="username"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-purple-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Port</label>
            <input
              type="number"
              [(ngModel)]="rsyncConfig.port"
              name="rsync_port"
              min="1"
              max="65535"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-purple-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Bandwidth Limit (Mbps)</label>
            <input
              type="number"
              [(ngModel)]="rsyncConfig.bwlimit"
              name="rsync_bwlimit"
              min="0"
              placeholder="0 = unlimited"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-purple-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
        </div>
      </div>

      <!-- Mode-Specific Fields: SMB -->
      <div
        *ngIf="formData.mode === 'smb'"
        class="p-4 rounded-lg space-y-4"
        [style.background]="'rgba(6, 182, 212, 0.05)'"
        [style.border]="'1px solid rgba(6, 182, 212, 0.2)'"
      >
        <h5 class="text-sm font-bold text-white">SMB/CIFS Configuration</h5>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Host</label>
            <input
              type="text"
              [(ngModel)]="smbConfig.host"
              name="smb_host"
              placeholder="nas.local"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-cyan-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Share</label>
            <input
              type="text"
              [(ngModel)]="smbConfig.share"
              name="smb_share"
              placeholder="media"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-cyan-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Port</label>
            <input
              type="number"
              [(ngModel)]="smbConfig.port"
              name="smb_port"
              min="1"
              max="65535"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-cyan-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Username</label>
            <input
              type="text"
              [(ngModel)]="smbConfig.username"
              name="smb_username"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-cyan-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Password</label>
            <input
              type="password"
              [(ngModel)]="smbConfig.password"
              name="smb_password"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-cyan-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
        </div>
      </div>

      <!-- Mode-Specific Fields: NFS -->
      <div
        *ngIf="formData.mode === 'nfs'"
        class="p-4 rounded-lg space-y-4"
        [style.background]="'rgba(16, 185, 129, 0.05)'"
        [style.border]="'1px solid rgba(16, 185, 129, 0.2)'"
      >
        <h5 class="text-sm font-bold text-white">NFS Configuration</h5>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Server</label>
            <input
              type="text"
              [(ngModel)]="nfsConfig.server"
              name="nfs_server"
              placeholder="nfs.server.com"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-white/70 mb-2">Export Path</label>
            <input
              type="text"
              [(ngModel)]="nfsConfig.export_path"
              name="nfs_export_path"
              placeholder="/export/media"
              class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
              [style.background]="'rgba(255, 255, 255, 0.06)'"
              [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
            />
          </div>
        </div>
      </div>

      <!-- Advanced Settings -->
      <div
        class="p-4 rounded-lg space-y-4"
        [style.background]="'rgba(255, 255, 255, 0.02)'"
        [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
      >
        <div class="flex items-center gap-2 mb-2">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: rgba(255,255,255,0.6);"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
          <h5 class="text-sm font-bold text-white">Advanced Options</h5>
        </div>

        <!-- Conflict Resolution -->
        <div>
          <label class="block text-xs font-medium text-white/70 mb-2">
            Conflict Resolution
          </label>
          <p class="text-xs text-white/50 mb-2">
            How to handle files that already exist at the destination
          </p>
          <select
            [(ngModel)]="formData.conflict_resolution"
            name="conflict_resolution"
            class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-gray-500/50"
            [style.background]="'rgba(255, 255, 255, 0.06)'"
            [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
          >
            <option value="rename">Rename - Add suffix to new file</option>
            <option value="skip">Skip - Keep existing file</option>
            <option value="overwrite">Overwrite - Replace existing file</option>
            <option value="fail">Fail - Stop transfer on conflict</option>
          </select>
        </div>

        <!-- Health Check Interval -->
        <div>
          <label class="block text-xs font-medium text-white/70 mb-2">
            Health Check Interval (minutes)
          </label>
          <p class="text-xs text-white/50 mb-2">
            How often to check destination connectivity and storage space
          </p>
          <input
            type="number"
            min="5"
            max="1440"
            [(ngModel)]="formData.health_check_interval_minutes"
            name="health_check_interval"
            class="w-full px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-gray-500/50"
            [style.background]="'rgba(255, 255, 255, 0.06)'"
            [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
          />
        </div>
      </div>
    </form>
  `,
})
export class TransferConfigFormComponent implements OnInit {
  @Input() config: TransferConfigRecord | null = null;
  @Input() formError: string | null = null;
  @Output() onSave = new EventEmitter<TransferConfigCreate | TransferConfigUpdate>();
  @Output() onCancel = new EventEmitter<void>();

  formData: (TransferConfigCreate | TransferConfigUpdate) & { mode?: 'local' | 'rsync' | 'smb' | 'nfs' } = {
    mode: 'local',
    name: null,
    transfer_dir: null,
    path_template: null,
    conflict_resolution: 'overwrite',
    health_check_interval_minutes: 60,
  };

  rsyncConfig: any = {
    host: '',
    user: '',
    port: 22,
    path: '',
    bwlimit: 0,
  };

  smbConfig: any = {
    host: '',
    share: '',
    port: 445,
    path: '',
    username: '',
    password: '',
    domain: '',
  };

  nfsConfig: any = {
    server: '',
    export_path: '',
    path: '',
  };

  rsyncKeyFile: File | null = null;
  saving = false;
  uploadingKey = false;
  keyUploadSuccess = false;
  /** Client-side validation error (required fields missing). */
  validationError: string | null = null;

  /** True when name, Transfer Path, and mode-specific required fields are set. */
  get requiredFieldsValid(): boolean {
    if (!this.formData?.name || !String(this.formData.name).trim()) return false;
    const pathOk = this.formData.transfer_dir != null && String(this.formData.transfer_dir).trim();
    if (!pathOk) return false;
    const mode = this.formData.mode;
    if (mode === 'local') return true;
    if (mode === 'rsync') {
      return !!(this.rsyncConfig?.host && String(this.rsyncConfig.host).trim() &&
        this.rsyncConfig?.user && String(this.rsyncConfig.user).trim());
    }
    if (mode === 'smb') {
      return !!(this.smbConfig?.host && String(this.smbConfig.host).trim() &&
        this.smbConfig?.share && String(this.smbConfig.share).trim());
    }
    if (mode === 'nfs') {
      return !!(this.nfsConfig?.server && String(this.nfsConfig.server).trim() &&
        this.nfsConfig?.export_path != null && String(this.nfsConfig.export_path).trim());
    }
    return false;
  }

  constructor(
    private systemSvc: SystemService,
    private cdr: ChangeDetectorRef,
    private logger: LoggerService
  ) {}

  sampleData: Record<string, any> = {
    movie_name: 'Example Movie',
    movie_year: 2024,
    year: 2024,
    release_name: 'Collectors Edition',
    release_year: 2024,
    release_slug: 'example-movie-2024',
    disc_number: 1,
    disc_name: 'Disc 1',
    type: 'movie',
    format: 'UHD',
  };

  ngOnInit() {
    if (this.config) {
      // Transfer Path: single field for all modes (transfer_dir or config_data.path for remote)
      const transferPath = this.config.transfer_dir ?? this.config.config_data?.['path'] ?? null;
      this.formData = {
        mode: this.config.mode as 'local' | 'rsync' | 'smb' | 'nfs',
        name: this.config.name,
        transfer_dir: transferPath,
        output_dir: this.config.output_dir,
        path_template: this.config.path_template,
        conflict_resolution: this.config.conflict_resolution as any,
        health_check_interval_minutes: this.config.health_check_interval_minutes,
      };

      if (this.config.config_data) {
        if (this.config.mode === 'rsync') {
          this.rsyncConfig = { ...this.config.config_data };
          delete this.rsyncConfig.path; // use formData.transfer_dir
        } else if (this.config.mode === 'smb') {
          this.smbConfig = { ...this.config.config_data };
          delete this.smbConfig.path;
        } else if (this.config.mode === 'nfs') {
          this.nfsConfig = { ...this.config.config_data };
          delete this.nfsConfig.path;
        }
      }
    }
  }

  onSubmit() {
    this.validationError = null;
    if (!this.requiredFieldsValid) {
      this.validationError = this.formData.mode === 'local'
        ? 'Transfer Path is required.'
        : 'Please fill in all required fields for the selected transfer mode.';
      return;
    }
    this.saving = true;
    
    const configData: any = {};
    const credentials: Record<string, string> = {};
    
    if (this.formData.mode === 'rsync') {
      Object.assign(configData, this.rsyncConfig);
      configData.path = this.formData.transfer_dir ?? '';
      if (this.rsyncKeyFile) {
        // Key will be uploaded separately
      }
    } else if (this.formData.mode === 'smb') {
      // Extract credentials from smbConfig
      const { username, password, domain, ...smbConfigData } = this.smbConfig;
      Object.assign(configData, smbConfigData);
      configData.path = this.formData.transfer_dir ?? '';
      if (username) credentials['smb_username'] = username;
      if (password) credentials['smb_password'] = password;
      if (domain) credentials['smb_domain'] = domain;
    } else if (this.formData.mode === 'nfs') {
      Object.assign(configData, this.nfsConfig);
      configData.path = this.formData.transfer_dir ?? '';
    }
    
    const payload: TransferConfigCreate | TransferConfigUpdate = {
      ...this.formData,
      config_data: Object.keys(configData).length > 0 ? configData : null,
      credentials: Object.keys(credentials).length > 0 ? credentials : null,
    };
    
    this.onSave.emit(payload);
  }

  onRsyncKeySelected(event: Event) {
    const input = event.target as HTMLInputElement;
    if (input.files && input.files.length > 0) {
      const file = input.files[0];
      this.rsyncKeyFile = file;
      this.uploadKey(file);
    }
  }

  uploadKey(file: File): void {
    this.uploadingKey = true;
    this.keyUploadSuccess = false;
    this.cdr.detectChanges();
    this.systemSvc.uploadRsyncKey(file).subscribe({
      next: res => {
        this.uploadingKey = false;
        this.keyUploadSuccess = true;
        setTimeout(() => {
          this.keyUploadSuccess = false;
        }, 3000);
      },
      error: err => {
        this.uploadingKey = false;
        this.keyUploadSuccess = false;
        this.logger.error('Failed to upload SSH key:', err);
      }
    });
  }
}
