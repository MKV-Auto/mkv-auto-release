import { Injectable } from "@angular/core"
import { BehaviorSubject } from "rxjs"

export type ToastKind = "info" | "success" | "warning" | "error"

/**
 * Build a short user-visible message from an HttpClient / FastAPI error body.
 */
export function formatHttpErrorDetail(err: unknown): string {
  const e = err as { message?: string; error?: { detail?: unknown } }
  const d = e?.error?.detail
  if (d == null || d === "") {
    if (typeof e?.message === "string" && e.message.trim()) return e.message
    return "Request failed"
  }
  if (typeof d === "string") return d
  if (typeof d === "object" && d !== null) {
    const o = d as { error?: string; missing?: string[]; message?: string }
    if (o.error === "release_not_link_ready" && Array.isArray(o.missing) && o.missing.length) {
      return `Release is not ready to link (missing: ${o.missing.join(", ")})`
    }
    if (typeof o.message === "string" && o.message.trim()) return o.message
    if (typeof o.error === "string" && o.error.trim()) return o.error
  }
  try {
    return JSON.stringify(d)
  } catch {
    return "Request failed"
  }
}

export interface Toast {
  id: number
  message: string
  kind: ToastKind
  timeout: number
}

/**
 * Frontend only displays toasts. Backend owns all Discord and push sends.
 */
@Injectable({ providedIn: "root" })
export class ToastService {
  private _toasts = new BehaviorSubject<Toast[]>([])
  toasts$ = this._toasts.asObservable()
  private nextId = 1

  show(message: string, kind: ToastKind = "info", timeout = 3500): void {
    const toast: Toast = { id: this.nextId++, message, kind, timeout }
    const list = [...this._toasts.value, toast]
    this._toasts.next(list)
    setTimeout(() => this.dismiss(toast.id), timeout)
  }

  dismiss(id: number): void {
    this._toasts.next(this._toasts.value.filter(t => t.id !== id))
  }
}
