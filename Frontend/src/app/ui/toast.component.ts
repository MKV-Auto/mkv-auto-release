import { Component, Input } from "@angular/core"
import { CommonModule } from "@angular/common"
import { Toast, ToastService } from "../services/toast.service"

@Component({
  selector: "app-toast-container",
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="fixed top-20 right-4 flex flex-col gap-2 z-[2000]" *ngIf="toasts && toasts.length">
      <div *ngFor="let t of toasts"
           class="relative border text-white py-3 pr-12 pl-4 rounded-xl shadow-xl backdrop-blur-md cursor-pointer min-w-[220px] max-w-[320px] transition-all duration-150 opacity-95 hover:-translate-y-px hover:opacity-100 break-words"
           [ngClass]="{
             'bg-blue-500/15 border-blue-500/30': t.kind==='info',
             'bg-emerald-500/15 border-emerald-500/30': t.kind==='success',
             'bg-amber-500/15 border-amber-500/30': t.kind==='warning',
             'bg-red-600/15 border-red-600/30': t.kind==='error',
             'bg-white/5 border-white/10': !t.kind || (t.kind !== 'info' && t.kind !== 'success' && t.kind !== 'warning' && t.kind !== 'error')
           }"
           (click)="dismiss(t.id)">
        <div class="break-words">{{ t.message }}</div>
        <button
          class="absolute top-2 right-2 bg-white/10 border border-white/20 rounded-lg p-1.5 text-white cursor-pointer transition-colors flex items-center justify-center min-w-[28px] min-h-[28px] flex-shrink-0 hover:bg-white/15 active:bg-white/20"
          (click)="dismiss(t.id); $event.stopPropagation()"
          aria-label="Close">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
    </div>
  `,
})
export class ToastContainerComponent {
  @Input() toasts: Toast[] = []

  constructor(private svc: ToastService) {}
  dismiss(id: number): void { this.svc.dismiss(id) }
}
