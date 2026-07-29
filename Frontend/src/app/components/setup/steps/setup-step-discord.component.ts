import { Component, EventEmitter, Input, OnChanges, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SystemService } from '../../../services/system.service';
import { IconComponent } from '../../../ui/icon/icon.component';
import { BtnComponent } from '../../../ui/btn/btn.component';
import { EnvManagedNoteComponent } from './env-managed-note.component';

export interface DiscordStepData {
  enabled: boolean;
  webhookUrl: string;
  dismissed: boolean;
}

type TestResult = 'success' | 'error' | null;

@Component({
  selector: 'app-setup-step-discord',
  standalone: true,
  imports: [EnvManagedNoteComponent, CommonModule, FormsModule, IconComponent, BtnComponent],
  template: `
    <div class="setup-step">
      <!-- Header -->
      <div class="setup-step-header">
        <div class="setup-step-icon setup-step-icon-discord">
          <ui-icon name="bot" [size]="24"></ui-icon>
        </div>
        <div class="setup-step-header-text">
          <h3 class="setup-step-title">Discord Notifications</h3>
          <p class="setup-step-desc">Get notified in Discord when your rips complete, encounter errors, or need attention. This step is optional — you can skip it and configure later.</p>
        </div>
      </div>

      <!-- Enable Toggle -->
      <div class="setup-step-toggle-card">
        <div class="setup-step-toggle-content">
          <span class="setup-step-toggle-icon" style="display: inline-flex;">
            <ui-icon name="bot" [size]="20"></ui-icon>
          </span>
          <div>
            <p class="setup-step-toggle-title">Enable Discord notifications</p>
            <p class="setup-step-toggle-subtitle">Receive updates about your ripping jobs</p>
          </div>
        </div>
        <button type="button" class="setup-step-toggle" [class.on]="data.enabled" (click)="toggleEnabled()" role="switch" [attr.aria-checked]="data.enabled" [disabled]="isEnvManaged('discord.enabled')">
          <span class="setup-step-toggle-thumb"></span>
        </button>
      </div>

      <!-- Webhook URL (only if enabled) -->
      <div *ngIf="data.enabled" class="setup-step-webhook-section">
        <label class="setup-step-label">Webhook URL</label>
        <input 
          type="text" 
          class="setup-step-input" 
          [class.error]="testResult === 'error'"
          [class.success]="testResult === 'success'"
          [(ngModel)]="localWebhookUrl" 
          (ngModelChange)="onWebhookChange($event)" 
          placeholder="https://discord.com/api/webhooks/..." 
          [disabled]="isEnvManaged('discord.webhook_url')"
        />
        <app-env-managed-note *ngIf="isEnvManaged('discord.webhook_url')"
                              variable="MKVAUTO_DISCORD_WEBHOOK_URL"></app-env-managed-note>

        <!-- Test Result Messages -->
        <div *ngIf="testResult === 'success'" class="setup-step-message success">
          <ui-icon name="check" [size]="16"></ui-icon>
          <span>Webhook validated! Test message sent successfully.</span>
        </div>

        <div *ngIf="testResult === 'error'" class="setup-step-message error">
          <ui-icon name="alert" [size]="16"></ui-icon>
          <span>Invalid webhook URL. Please check and try again.</span>
        </div>

        <!-- Test Button -->
        <ui-btn
          variant="primary"
          [disabled]="!data.webhookUrl"
          [loading]="testing"
          (click)="testWebhook()">
          Test webhook
        </ui-btn>
      </div>

      <!-- How to get webhook -->
      <div class="setup-step-info setup-step-info-discord">
        <p class="setup-step-info-title">💡 How to get a webhook URL</p>
        <ol class="setup-step-info-list">
          <li>Open your Discord server and go to Server Settings</li>
          <li>Navigate to Integrations → Webhooks</li>
          <li>Click "New Webhook" and choose a channel</li>
          <li>Copy the webhook URL and paste it above</li>
        </ol>
        <a
          href="https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks"
          target="_blank"
          rel="noopener noreferrer"
          class="setup-step-info-link"
        >
          Learn more about Discord webhooks
          <ui-icon name="external" [size]="12"></ui-icon>
        </a>
      </div>

      <!-- Optional reminder (only if not enabled) -->
      <div *ngIf="!data.enabled" class="setup-step-reminder">
        <p><strong>Note:</strong> You can always set this up later in Settings → Notifications</p>
      </div>
    </div>
  `,
  styles: [`
    .setup-step { display: flex; flex-direction: column; gap: 1.5rem; }
    .setup-step-header { display: flex; gap: 1rem; align-items: flex-start; }
    .setup-step-icon { width: 3rem; height: 3rem; border-radius: 0.75rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .setup-step-icon-discord { background: linear-gradient(135deg, #5865f2 0%, #4752c4 100%); box-shadow: 0 0 20px rgba(88, 101, 242, 0.3); color: #fff; }
    .setup-step-header-text { flex: 1; }
    .setup-step-title { font-size: 1.125rem; font-weight: 700; color: #fff; margin: 0 0 0.5rem 0; }
    .setup-step-desc { font-size: 0.875rem; color: rgba(255,255,255,0.7); margin: 0; line-height: 1.5; }
    
    .setup-step-toggle-card { padding: 1rem; border-radius: 0.5rem; display: flex; align-items: center; justify-content: space-between; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); }
    .setup-step-toggle-content { display: flex; align-items: center; gap: 0.75rem; }
    .setup-step-toggle-icon { color: #60a5fa; flex-shrink: 0; }
    .setup-step-toggle-title { font-size: 0.875rem; font-weight: 500; color: #fff; margin: 0; }
    .setup-step-toggle-subtitle { font-size: 0.75rem; color: rgba(255,255,255,0.5); margin: 0; }
    .setup-step-toggle { width: 2.75rem; height: 1.5rem; border-radius: 9999px; background: rgba(255,255,255,0.2); border: none; cursor: pointer; position: relative; transition: all 0.2s; }
    .setup-step-toggle.on { background: linear-gradient(135deg, #5865f2 0%, #4752c4 100%); }
    .setup-step-toggle-thumb { position: absolute; top: 0.25rem; left: 0.25rem; width: 1rem; height: 1rem; background: #fff; border-radius: 50%; transition: transform 0.2s; }
    .setup-step-toggle.on .setup-step-toggle-thumb { transform: translateX(1.25rem); }
    
    .setup-step-webhook-section { display: flex; flex-direction: column; gap: 0.75rem; }
    .setup-step-label { display: block; font-size: 0.875rem; font-weight: 500; color: rgba(255,255,255,0.8); }
    .setup-step-input { width: 100%; padding: 0.625rem 1rem; font-size: 0.875rem; font-family: monospace; color: #fff; border-radius: 0.5rem; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); transition: all 0.2s; }
    .setup-step-input:focus { outline: none; box-shadow: 0 0 0 2px rgba(88, 101, 242, 0.5); }
    .setup-step-input.error { border-color: rgba(239, 68, 68, 0.5); }
    .setup-step-input.success { border-color: rgba(34, 197, 94, 0.5); }
    
    .setup-step-message { display: flex; align-items: flex-start; gap: 0.5rem; padding: 0.75rem; border-radius: 0.5rem; font-size: 0.875rem; }
    .setup-step-message svg { flex-shrink: 0; margin-top: 0.125rem; }
    .setup-step-message.success { background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.3); color: #4ade80; }
    .setup-step-message.error { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); color: #f87171; }
    
    .setup-step-btn { padding: 0.5rem 1rem; border-radius: 0.5rem; font-size: 0.875rem; font-weight: 500; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; gap: 0.5rem; }
    .setup-step-btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .setup-step-btn-discord { background: rgba(88, 101, 242, 0.2); color: #fff; border: 1px solid rgba(88, 101, 242, 0.3); }
    .setup-step-btn-discord:hover:not(:disabled) { background: rgba(88, 101, 242, 0.3); }
    
    .setup-step-info { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; }
    .setup-step-info-discord { background: rgba(88, 101, 242, 0.08); border: 1px solid rgba(88, 101, 242, 0.2); }
    .setup-step-info-title { color: #a5b4fc; font-weight: 500; margin: 0 0 0.75rem 0; }
    .setup-step-info-list { margin: 0 0 0.75rem 0; padding-left: 1.25rem; color: rgba(165, 180, 252, 0.8); list-style-type: decimal; }
    .setup-step-info-list li { margin-bottom: 0.5rem; }
    .setup-step-info-link { display: inline-flex; align-items: center; gap: 0.25rem; color: #a5b4fc; text-decoration: none; transition: color 0.2s; }
    .setup-step-info-link:hover { color: #c7d2fe; }
    .setup-step-info-link svg { flex-shrink: 0; }
    
    .setup-step-reminder { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; background: rgba(251, 146, 60, 0.08); border: 1px solid rgba(251, 146, 60, 0.2); color: #fdba74; }
    .setup-step-reminder strong { color: #fb923c; }
  `],
})
export class SetupStepDiscordComponent implements OnChanges {
  /** Dotted setting paths the container's environment pins (see the modal). */
  @Input() envManaged: string[] = [];

  isEnvManaged(settingPath: string): boolean {
    return this.envManaged.includes(settingPath);
  }

  @Input() data!: DiscordStepData;
  @Output() dataChange = new EventEmitter<Partial<DiscordStepData>>();

  localWebhookUrl = '';
  testing = false;
  testResult: TestResult = null;

  constructor(private system: SystemService) {}

  ngOnChanges(): void {
    this.localWebhookUrl = this.data?.webhookUrl ?? '';
  }

  toggleEnabled(): void {
    const newEnabled = !this.data.enabled;
    this.dataChange.emit({ enabled: newEnabled });
    
    // Save to backend
    if (newEnabled && this.data.webhookUrl) {
      this.system.saveDiscordConfig({
        enabled: true,
        webhook_url: this.data.webhookUrl,
      }).subscribe({
        next: () => {},
        error: () => {},
      });
    } else if (!newEnabled) {
      this.system.saveDiscordConfig({
        enabled: false,
        webhook_url: this.data.webhookUrl || '',
      }).subscribe({
        next: () => {},
        error: () => {},
      });
    }
  }

  onWebhookChange(url: string): void {
    this.localWebhookUrl = url;
    this.dataChange.emit({ webhookUrl: url });
    this.testResult = null; // Reset test result when URL changes
    
    // Save to backend if enabled
    if (this.data.enabled) {
      this.system.saveDiscordConfig({
        enabled: true,
        webhook_url: url,
      }).subscribe({
        next: () => {},
        error: () => {},
      });
    }
  }

  testWebhook(): void {
    if (!this.data.webhookUrl) return;
    
    this.testing = true;
    this.testResult = null;
    
    this.system.sendDiscordTest().subscribe({
      next: (res) => {
        this.testing = false;
        this.testResult = res?.status === 'sent' ? 'success' : 'error';
        if (res?.status === 'sent') this.dataChange.emit({ enabled: true });
      },
      error: () => {
        this.testing = false;
        this.testResult = 'error';
      },
    });
  }
}
