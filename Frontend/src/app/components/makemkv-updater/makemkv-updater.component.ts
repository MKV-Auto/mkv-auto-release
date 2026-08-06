import { CommonModule } from '@angular/common';
import { Component, OnInit, ViewChild, ElementRef } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize } from 'rxjs/operators';

import {
  SystemService,
  MakeMKVInfo,
  MakeMKVUpdateResponse,
  MakeMKVRegistrationStatus,
} from '../../services/system.service';
import { LoggerService } from '../../services/logger.service';

import { CheckboxComponent } from '../../ui/checkbox/checkbox.component';

@Component({
  selector: 'app-makemkv-updater',
  standalone: true,
  imports: [CommonModule, FormsModule, CheckboxComponent],
  templateUrl: './makemkv-updater.component.html',
  styleUrls: ['./makemkv-updater.component.scss'],
})
export class MakeMKVUpdaterComponent implements OnInit {
  info: MakeMKVInfo | null = null;
  updating = false;
  registering = false;
  message = '';
  error = '';
  logs: string[] = [];
  /** Maps to `ffmpeg_advanced_features` (non-free codecs / libfdk-aac).
   *  NOT "should we build ffmpeg" — /system/makemkv/update/start always
   *  builds it. This used to send `build_ffmpeg`, which that endpoint
   *  ignores, so the checkbox did nothing at all. */
  includeAdvancedFeatures = true;
  latestVersion: string | null = null;
  statusLine = 'Idle';
  @ViewChild('logBox') logBox?: ElementRef<HTMLElement>;
  regStatus: MakeMKVRegistrationStatus | null = null;
  regKey = '';

  get hasUpdate(): boolean {
    return !!(this.latestVersion && this.info?.version && this.latestVersion !== this.info.version);
  }

  constructor(
    private systemSvc: SystemService,
    private logger: LoggerService
  ) {}

  ngOnInit(): void {
    this.refreshInfo();
    this.loadLatest();
    this.loadRegistrationStatus();
  }

  refreshInfo(): void {
    this.systemSvc.getMakeMKVInfo().subscribe({
      next: info => {
        this.info = info;
        this.error = '';
      },
      error: err => {
        this.error = err.message ?? 'Unable to load MakeMKV info';
      },
    });
  }

  loadLatest(): void {
    this.statusLine = 'Checking latest version…';
    this.systemSvc.getLatestMakeMKV().subscribe({
      next: res => { this.latestVersion = res.version; this.statusLine = `Latest available: ${res.version}`; },
      error: err => { this.error = err.message ?? 'Unable to fetch latest version'; this.statusLine = 'Latest check failed'; }
    });
  }

  loadRegistrationStatus(): void {
    this.systemSvc.getRegistrationStatus().subscribe({
      next: res => {
        this.regStatus = res;
        if (res.currentKey) {
          this.regKey = res.currentKey;
        }
      },
      error: err => { this.logger.error('Registration status failed', err); }
    });
  }

  updateToLatest(): void {
    if (this.updating) return;
    this.updating = true;
    this.message = '';
    this.error = '';
    this.logs = ['Starting update…'];
    this.statusLine = 'Starting MakeMKV update…';

    this.systemSvc.startMakeMKVUpdate({ ffmpeg_advanced_features: this.includeAdvancedFeatures }).subscribe({
      next: res => {
        const es = this.systemSvc.streamUpdate(res.jobId);
        es.addEventListener('log', (e: MessageEvent) => {
          try {
            const data = JSON.parse(e.data);
            if (data.line) {
              this.logs = [...this.logs, data.line];
              this.scrollLogs();
            }
          } catch (_) {}
        });
        es.addEventListener('status', (e: MessageEvent) => {
          try {
            const data = JSON.parse(e.data);
            if (data.status === 'completed') {
              this.message = `Updated to ${data.version || 'latest'}`;
              this.statusLine = 'Update finished';
              es.close();
              this.updating = false;
              this.refreshInfo();
              this.loadLatest();
              this.scrollLogs();
            } else if (data.status === 'failed') {
              this.error = data.error || 'Update failed';
              this.statusLine = 'Update failed';
              es.close();
              this.updating = false;
            }
          } catch (_) {}
        });
        es.onerror = () => {
          this.error = 'Update stream disconnected';
          this.statusLine = 'Update stream disconnected';
          es.close();
          this.updating = false;
        };
      },
      error: err => {
        this.error = err.error?.detail ?? err.message ?? 'Update failed';
        this.statusLine = 'Update failed';
        this.updating = false;
      },
    });
  }

  submitRegistration(): void {
    const key = this.regKey.trim();
    if (!key) return;
    this.registering = true;
    this.error = '';
    this.message = '';
    this.systemSvc.registerKey(key).subscribe({
      next: res => {
        this.regStatus = res;
        this.message = res.expired ? 'Registration failed or still expired' : 'Registration updated';
        if (!res.expired) {
          this.regKey = res.currentKey || this.regKey;
          window.location.reload();
        }
        this.registering = false;
      },
      error: err => {
        this.error = err.error?.detail ?? err.message ?? 'Registration failed';
        this.registering = false;
      },
    });
  }

  private scrollLogs(): void {
    setTimeout(() => {
      const el = this.logBox?.nativeElement;
      if (el) {
        el.scrollTop = el.scrollHeight;
      }
    }, 0);
  }
}
