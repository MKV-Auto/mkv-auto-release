/**
 * ReadinessService — polls the backend `/readyz` endpoint during app bootstrap and
 * blocks Angular until the backend is ready to serve real traffic. Used by the
 * APP_INITIALIZER in app.config.ts so the app never renders against a backend that's
 * still in WAL recovery and would 500 every endpoint.
 *
 * The static "Setting Up" overlay in index.html is visible by default and is removed
 * (via #app-loading-overlay element id) once readiness succeeds.
 */
import { Injectable } from '@angular/core';
import { environment } from '../environments/environment';

// Runtime override (set by e2e-full.js into the built index.html) takes
// precedence over the baked environment.apiBase. Lets the same bundle
// run against ad-hoc test backends without rebuilding for each port.
const RUNTIME_API_BASE =
  typeof window !== 'undefined' ? (window as any)?.MKVAUTO_API_BASE : undefined;
const API_BASE =
  RUNTIME_API_BASE ?? (environment as any)?.apiBase ?? 'http://localhost:8000';
const POLL_INTERVAL_MS = 750;
const MAX_WAIT_MS = 90_000;

@Injectable({ providedIn: 'root' })
export class ReadinessService {
  /** Resolves once `/readyz` returns 200, or after MAX_WAIT_MS even on failure
   * (better to let the app boot and surface real errors than to hang on startup). */
  async waitUntilReady(updateMessage?: (msg: string) => void): Promise<{ ready: boolean; waitedMs: number }> {
    const start = performance.now();
    let attempt = 0;
    while (true) {
      attempt += 1;
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 3000);
        const res = await fetch(`${API_BASE}/readyz`, {
          method: 'GET',
          signal: ctrl.signal,
          cache: 'no-store',
        });
        clearTimeout(timer);
        if (res.ok) {
          return { ready: true, waitedMs: performance.now() - start };
        }
        // 503 from the readiness gate; fall through to the retry path.
        if (updateMessage) {
          updateMessage(`Backend warming up… (attempt ${attempt})`);
        }
      } catch (_err) {
        // Network error / fetch abort — backend may not be listening yet.
        if (updateMessage) {
          updateMessage(`Waiting for backend… (attempt ${attempt})`);
        }
      }
      const elapsed = performance.now() - start;
      if (elapsed > MAX_WAIT_MS) {
        return { ready: false, waitedMs: elapsed };
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  }
}
