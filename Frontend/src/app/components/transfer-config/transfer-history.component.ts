import { Component, OnInit, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SystemService, TransferHistorySummary } from '../../services/system.service';

/** Aggregates rendered in the four KPI tiles. Derived from the loaded
 * `history` rows so the totals stay in lockstep with whatever filter
 * is active (the previous server-side aggregator used a fixed 30-day
 * window that disagreed with the un-windowed row list — #592). */
interface TransferKpis {
  total: number;
  successful: number;
  failed: number;
  avgSpeed: number;
}

@Component({
  selector: 'app-transfer-history',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="space-y-6">
      <!-- Header with Filters -->
      <div class="flex items-start justify-between gap-4">
        <div>
          <h4 class="text-base font-bold text-white">Transfer History</h4>
          <p class="text-sm text-white/60">View past transfer operations and statistics</p>
        </div>
        <div class="flex items-center gap-2">
          <select
            [(ngModel)]="selectedConfigId"
            (change)="loadHistory()"
            name="config_filter"
            class="px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/50"
            [style.background]="'rgba(255, 255, 255, 0.06)'"
            [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
          >
            <option value="">All Configs</option>
            <option *ngFor="let config of configs" [value]="config.id">
              {{ config.name }}
            </option>
          </select>

          <select
            [(ngModel)]="selectedStatus"
            (change)="applyStatusFilter()"
            name="status_filter"
            class="px-3 py-2 text-sm text-white rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/50"
            [style.background]="'rgba(255, 255, 255, 0.06)'"
            [style.border]="'1px solid rgba(255, 255, 255, 0.1)'"
          >
            <option value="">All Statuses</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="in_progress">In Progress</option>
          </select>
        </div>
      </div>

      <!-- KPI Cards (derived from filteredHistory so they always agree with the visible rows — #592) -->
      <div *ngIf="history.length > 0" class="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div
          class="p-4 rounded-xl"
          [style.background]="'rgba(59, 130, 246, 0.08)'"
          [style.border]="'1px solid rgba(59, 130, 246, 0.2)'"
        >
          <div class="text-xs text-blue-400 font-medium mb-1">Total Transfers</div>
          <div class="text-2xl font-bold text-white">{{ kpis.total }}</div>
        </div>

        <div
          class="p-4 rounded-xl"
          [style.background]="'rgba(34, 197, 94, 0.08)'"
          [style.border]="'1px solid rgba(34, 197, 94, 0.2)'"
        >
          <div class="text-xs text-green-400 font-medium mb-1">Successful</div>
          <div class="text-2xl font-bold text-white">{{ kpis.successful }}</div>
        </div>

        <div
          class="p-4 rounded-xl"
          [style.background]="'rgba(239, 68, 68, 0.08)'"
          [style.border]="'1px solid rgba(239, 68, 68, 0.2)'"
        >
          <div class="text-xs text-red-400 font-medium mb-1">Failed</div>
          <div class="text-2xl font-bold text-white">{{ kpis.failed }}</div>
        </div>

        <div
          class="p-4 rounded-xl"
          [style.background]="'rgba(139, 92, 246, 0.08)'"
          [style.border]="'1px solid rgba(139, 92, 246, 0.2)'"
        >
          <div class="text-xs text-purple-400 font-medium mb-1">Avg Speed</div>
          <div class="text-2xl font-bold text-white">{{ kpis.avgSpeed.toFixed(0) }} Mbps</div>
        </div>
      </div>
      <p *ngIf="history.length > 0" class="text-xs text-white/40">
        Stats based on the {{ history.length }} most recent transfers loaded.
      </p>

      <!-- Loading State -->
      <div *ngIf="loading" class="flex items-center justify-center py-12">
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="animate-spin" style="color: rgba(255,255,255,0.4);"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
      </div>

      <!-- Empty State -->
      <div
        *ngIf="!loading && filteredHistory.length === 0"
        class="text-center py-12 rounded-xl"
        [style.background]="'rgba(255, 255, 255, 0.02)'"
        [style.border]="'1px solid rgba(255, 255, 255, 0.05)'"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="mx-auto mb-3" style="color: rgba(255,255,255,0.2);"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        <p class="text-sm text-white/60">No transfer history found</p>
      </div>

      <!-- History Cards -->
      <div *ngIf="!loading && filteredHistory.length > 0" class="space-y-2">
        <div
          *ngFor="let entry of filteredHistory"
          class="p-4 rounded-lg hover:bg-white/[0.04] transition-all"
          [style.background]="'rgba(255, 255, 255, 0.02)'"
          [style.border]="'1px solid rgba(255, 255, 255, 0.05)'"
        >
          <div class="flex items-start justify-between gap-4">
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 mb-1">
                <h6 class="text-sm font-bold text-white truncate">
                  {{ getEntryTitle(entry) }}
                </h6>
                <span
                  class="px-2 py-0.5 rounded text-xs font-medium text-white"
                  [style.background]="getStatusBadge(entry.status).color"
                >
                  {{ getStatusBadge(entry.status).label }}
                </span>
                <span *ngIf="isOrphan(entry)" class="px-2 py-0.5 rounded text-xs font-medium text-white/60" [style.background]="'rgba(255,255,255,0.06)'" [style.border]="'1px solid rgba(255,255,255,0.1)'">
                  orphaned
                </span>
              </div>
              <div class="text-xs text-white/50 mb-2">
                {{ getEntrySubtitle(entry) }}
              </div>
              <div class="text-xs text-white/40 flex items-center gap-2">
                <span>{{ formatDate(entry.created_at) }}</span>
                <span *ngIf="entry.job_id" class="font-mono text-white/30" [title]="entry.job_id">job {{ entry.job_id.substring(0, 8) }}…</span>
              </div>
            </div>
            <div *ngIf="entry.average_speed_mbps" class="text-right">
              <div class="text-sm font-bold text-white">{{ entry.average_speed_mbps.toFixed(0) }} Mbps</div>
              <div class="text-xs text-white/50">Transfer speed</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class TransferHistoryComponent implements OnInit {
  @Input() configs: any[] = [];
  
  history: TransferHistorySummary[] = [];
  filteredHistory: TransferHistorySummary[] = [];
  selectedConfigId = '';
  selectedStatus = '';
  loading = false;

  constructor(private systemService: SystemService) {}

  ngOnInit() {
    this.loadHistory();
  }

  /** KPIs derived from the loaded history with the active filters applied.
   *  Previously a separate /transfer/statistics call ran with a hardcoded
   *  30-day window that disagreed with the un-windowed row list — when the
   *  user's most recent transfer was >30 days old, the KPIs read zero while
   *  rows still rendered (#592). Now the same `filteredHistory` array powers
   *  both. Avg speed excludes failed rows and zero-byte rows. */
  get kpis(): TransferKpis {
    const rows = this.filteredHistory;
    const completed = rows.filter((r) => r.status === 'completed');
    const failed = rows.filter((r) => r.status === 'failed');
    const speeds = completed
      .map((r) => r.average_speed_mbps)
      .filter((x): x is number => typeof x === 'number' && x > 0);
    const avgSpeed = speeds.length ? speeds.reduce((a, b) => a + b, 0) / speeds.length : 0;
    return {
      total: rows.length,
      successful: completed.length,
      failed: failed.length,
      avgSpeed,
    };
  }

  loadHistory() {
    this.loading = true;
    this.systemService.getTransferHistory(
      undefined,
      this.selectedConfigId || undefined,
      100
    ).subscribe({
      next: (history) => {
        this.history = history;
        this.applyStatusFilter();
        this.loading = false;
      },
      error: () => {
        this.loading = false;
      }
    });
  }

  applyStatusFilter() {
    if (this.selectedStatus) {
      this.filteredHistory = this.history.filter(h => h.status === this.selectedStatus);
    } else {
      this.filteredHistory = this.history;
    }
  }

  getStatusBadge(status: string): { color: string; label: string } {
    switch (status) {
      case 'completed':
        return { color: '#22c55e', label: 'Completed' };
      case 'failed':
        return { color: '#ef4444', label: 'Failed' };
      case 'in_progress':
        return { color: '#3b82f6', label: 'In Progress' };
      default:
        return { color: '#6b7280', label: 'Unknown' };
    }
  }

  /** True when the transfer's job FK was cleared (job deleted with
   *  ON DELETE SET NULL) — server returns NULL identity fields and we
   *  fall through to source-path parsing for the row title. */
  isOrphan(entry: TransferHistorySummary): boolean {
    return !entry.job_id || (
      !entry.movie_name && !entry.release_name && !entry.disc_name
    );
  }

  getEntryTitle(entry: TransferHistorySummary): string {
    // Preferred: server-resolved identity via Job → Disc → Release → Movie (#593).
    // "Movie (Year) — Disc Name" reads cleanly for a single line; gracefully
    // drops any piece that resolves to null.
    if (entry.movie_name) {
      const year = entry.release_year ? ` (${entry.release_year})` : '';
      const disc = entry.disc_name ? ` — ${entry.disc_name}` : '';
      return `${entry.movie_name}${year}${disc}`;
    }
    if (entry.release_name) {
      const year = entry.release_year ? ` (${entry.release_year})` : '';
      const disc = entry.disc_name ? ` — ${entry.disc_name}` : '';
      return `${entry.release_name}${year}${disc}`;
    }
    if (entry.disc_name) {
      return entry.disc_name;
    }
    // Fallback: parse the filesystem source path (legacy behavior).
    const path = entry.source_path || '';
    const parts = path.split('/').filter(Boolean);
    return parts[parts.length - 2] || parts[parts.length - 1] || 'Transfer';
  }

  getEntrySubtitle(entry: TransferHistorySummary): string {
    const configName = this.configs.find(c => c.id === entry.transfer_config_id)?.name || 'Unknown';
    const sizeGB = ((entry.bytes_transferred || 0) / 1024 / 1024 / 1024).toFixed(1);
    return `${configName} • ${sizeGB} GB`;
  }

  formatDate(dateStr: string | null | undefined): string {
    if (!dateStr) return '—';
    return new Date(dateStr).toLocaleString();
  }
}
