import { Component, OnInit, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SystemService, TransferHealthStatus } from '../../services/system.service';

@Component({
  selector: 'app-transfer-health',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="w-full">
      <div class="flex justify-between items-center mb-4">
        <h3 class="m-0">Health Status</h3>
        <button
          class="secondary"
          (click)="triggerHealthCheck()"
          [disabled]="loading">
          {{ loading ? 'Checking...' : 'Check Health' }}
        </button>
      </div>
      <div class="grid gap-4 grid-cols-[repeat(auto-fill,minmax(250px,1fr))]" *ngIf="healthStatus">
        <div class="p-4 bg-white/5 rounded-lg border border-white/10" *ngIf="healthStatus.overall">
          <div class="flex justify-between items-center mb-2">
            <span class="font-semibold text-sm">Overall</span>
            <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" [ngClass]="getStatusBadgeClass(healthStatus.overall.status)">{{ healthStatus.overall.status }}</span>
          </div>
          <div class="text-xs text-white/70 mb-1" *ngIf="healthStatus.overall.message">{{ healthStatus.overall.message }}</div>
          <div class="text-[11px] text-white/50" *ngIf="healthStatus.overall.response_time_ms">Response time: {{ healthStatus.overall.response_time_ms }}ms</div>
        </div>
        <div class="p-4 bg-white/5 rounded-lg border border-white/10" *ngIf="healthStatus.connectivity">
          <div class="flex justify-between items-center mb-2">
            <span class="font-semibold text-sm">Connectivity</span>
            <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" [ngClass]="getStatusBadgeClass(healthStatus.connectivity.status)">{{ healthStatus.connectivity.status }}</span>
          </div>
          <div class="text-xs text-white/70 mb-1" *ngIf="healthStatus.connectivity.message">{{ healthStatus.connectivity.message }}</div>
          <div class="text-[11px] text-white/50" *ngIf="healthStatus.connectivity.response_time_ms">Response time: {{ healthStatus.connectivity.response_time_ms }}ms</div>
        </div>
        <div class="p-4 bg-white/5 rounded-lg border border-white/10" *ngIf="healthStatus.authentication">
          <div class="flex justify-between items-center mb-2">
            <span class="font-semibold text-sm">Authentication</span>
            <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" [ngClass]="getStatusBadgeClass(healthStatus.authentication.status)">{{ healthStatus.authentication.status }}</span>
          </div>
          <div class="text-xs text-white/70 mb-1" *ngIf="healthStatus.authentication.message">{{ healthStatus.authentication.message }}</div>
          <div class="text-[11px] text-white/50" *ngIf="healthStatus.authentication.response_time_ms">Response time: {{ healthStatus.authentication.response_time_ms }}ms</div>
        </div>
        <div class="p-4 bg-white/5 rounded-lg border border-white/10" *ngIf="healthStatus.permissions">
          <div class="flex justify-between items-center mb-2">
            <span class="font-semibold text-sm">Permissions</span>
            <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" [ngClass]="getStatusBadgeClass(healthStatus.permissions.status)">{{ healthStatus.permissions.status }}</span>
          </div>
          <div class="text-xs text-white/70 mb-1" *ngIf="healthStatus.permissions.message">{{ healthStatus.permissions.message }}</div>
          <div class="text-[11px] text-white/50" *ngIf="healthStatus.permissions.response_time_ms">Response time: {{ healthStatus.permissions.response_time_ms }}ms</div>
        </div>
        <div class="p-4 bg-white/5 rounded-lg border border-white/10" *ngIf="healthStatus.space">
          <div class="flex justify-between items-center mb-2">
            <span class="font-semibold text-sm">Space</span>
            <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" [ngClass]="getStatusBadgeClass(healthStatus.space.status)">{{ healthStatus.space.status }}</span>
          </div>
          <div class="text-xs text-white/70 mb-1" *ngIf="healthStatus.space.message">{{ healthStatus.space.message }}</div>
          <div class="text-[11px] text-white/50" *ngIf="healthStatus.space.response_time_ms">Response time: {{ healthStatus.space.response_time_ms }}ms</div>
        </div>
      </div>
      <div class="py-8 text-center" *ngIf="!healthStatus">
        <p class="muted">No health status available. Click "Check Health" to run a health check.</p>
      </div>
    </div>
  `,
})
export class TransferHealthComponent implements OnInit {
  @Input() configId = '';
  
  healthStatus: TransferHealthStatus | null = null;
  loading = false;

  constructor(private systemService: SystemService) {}

  ngOnInit() {
    if (this.configId) {
      this.loadHealthStatus();
    }
  }

  loadHealthStatus() {
    this.systemService.getTransferHealth(this.configId).subscribe({
      next: (status) => {
        this.healthStatus = status;
      },
      error: () => {}
    });
  }

  triggerHealthCheck() {
    this.loading = true;
    this.systemService.triggerHealthCheck(this.configId).subscribe({
      next: (status) => {
        this.healthStatus = status;
        this.loading = false;
      },
      error: () => {
        this.loading = false;
      }
    });
  }

  getStatusBadgeClass(status: string): string {
    if (status === 'healthy') return 'bg-emerald-500/20 text-emerald-400';
    if (status === 'unhealthy') return 'bg-red-500/20 text-red-400';
    if (status === 'degraded') return 'bg-amber-500/20 text-amber-400';
    return 'bg-white/10 text-white/60';
  }
}











