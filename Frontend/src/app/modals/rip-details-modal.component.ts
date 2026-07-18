import { Component, Input, Output, EventEmitter } from "@angular/core"
import { CommonModule } from "@angular/common"

@Component({
  selector: "app-rip-details-modal",
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="fixed inset-0 bg-black/50 flex justify-center items-center z-[1000] backdrop-blur-sm">
      <div class="card w-[700px] max-w-[90%] max-h-[90vh] overflow-y-auto">
        <div class="sticky top-0 bg-surface-2 z-10 py-2 flex justify-between items-center mb-4">
          <h2 class="flex items-center gap-2 m-0 text-text-strong">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <circle cx="12" cy="12" r="3"></circle>
            </svg>
            {{ ripDetails?.discTitle || 'Rip Details' }}
          </h2>
          <button class="bg-transparent border-none text-2xl text-text-primary cursor-pointer" (click)="onClose()">&times;</button>
        </div>
        <div class="modal-body">
          <div class="mb-6">
            <h3 class="text-xl font-semibold text-text-strong border-b border-white/10 pb-2 flex items-center gap-2 mb-4">
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
              </svg>
              General Information
            </h3>
            <div class="grid gap-4 grid-cols-[repeat(auto-fit,minmax(200px,1fr))]">
              <div class="p-3 bg-white/5 rounded border border-white/10">
                <div class="text-xs uppercase tracking-wider text-text-muted mb-1">Drive</div>
                <div class="font-medium text-text-strong">{{ ripDetails?.driveNum || '—' }}</div>
              </div>
              <div class="p-3 bg-white/5 rounded border border-white/10">
                <div class="text-xs uppercase tracking-wider text-text-muted mb-1">Output Folder</div>
                <div class="font-medium text-text-strong">{{ ripDetails?.outputFolder || '—' }}</div>
              </div>
              <div class="p-3 bg-white/5 rounded border border-white/10">
                <div class="text-xs uppercase tracking-wider text-text-muted mb-1">Date</div>
                <div class="font-medium text-text-strong">{{ formatDate(ripDetails?.timestamp) }}</div>
              </div>
              <div class="p-3 bg-white/5 rounded border border-white/10">
                <div class="text-xs uppercase tracking-wider text-text-muted mb-1">Total Titles</div>
                <div class="font-medium text-text-strong">{{ safeTitles.length }}</div>
              </div>
            </div>
          </div>
          <div>
            <h3 class="text-xl font-semibold text-text-strong border-b border-white/10 pb-2 flex items-center gap-2 mb-4">
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path>
              </svg>
              Titles
            </h3>
            <div class="rounded overflow-hidden shadow-sm">
              <table class="w-full border-collapse">
                <thead>
                  <tr class="bg-white/5">
                    <th class="w-20 py-2 px-3 text-left text-xs font-semibold text-text-muted">Title</th>
                    <th class="py-2 px-3 text-left text-xs font-semibold text-text-muted">Title</th>
                    <th class="w-24 py-2 px-3 text-left text-xs font-semibold text-text-muted">Mode</th>
                  </tr>
                </thead>
                <tbody>
                  <tr *ngFor="let title of safeTitles" class="border-b border-white/10" [ngClass]="{'bg-emerald-500/5': title.mode === 'rip'}">
                    <td class="py-2 px-3 text-left">{{ title.number }}</td>
                    <td class="py-2 px-3 text-left">{{ title.title || 'Title ' + title.number }}</td>
                    <td class="py-2 px-3 text-left">
                      <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" [ngClass]="title.mode === 'rip' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-primary/20 text-primary-light'">{{ title.mode }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="flex justify-end mt-4">
          <button (click)="onClose()">Close</button>
        </div>
      </div>
    </div>
  `,
})
export class RipDetailsModal {
  @Input() ripDetails: any | null = null
  @Output() close = new EventEmitter<void>()

  get safeTitles(): any[] {
    return this.ripDetails?.titles || []
  }

  formatDate(dateString?: string): string {
    if (!dateString) return '—'
    const date = new Date(dateString)
    return isNaN(date.getTime()) ? '—' : date.toLocaleString()
  }

  onClose(): void {
    this.close.emit()
  }
}
