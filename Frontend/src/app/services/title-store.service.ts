// src/app/services/title-store.service.ts
//
// TitleStore — the single owner of client-side title state machinery.
// Area 5 of the title-state redesign (internal_docs/DESIGN-title-editing-
// state.md, internal_docs/BLAST-RADIUS-title-state-v2.md).
//
// Before this store, WorkflowService carried three parallel shadow copies
// of server title state (seq cache, pending-text cache, version acks) and
// the merge rules were spread across five methods; the editing components
// each carried their own copy of the buffering machinery — which is how
// the mobile modal missed two rounds of fixes. The store gives all of it
// ONE home with exactly three inputs, per the design doc:
//
//   1. a local edit        → enqueuePatch / patchBatch (per-title queue)
//   2. a write ack         → applyPatchResults (success / retry / conflict)
//   3. a delta event       → foldServerRows (titles_changed)
//
// and one merge rule: per row, a newer server version wins; text the user
// is still typing is never overwritten; a stale first write retries once
// with the server's version (user input wins over background churn).
//
// The store deliberately does NOT own the titles array — the active
// WorkflowContext stays the single rendering source. It reaches the
// array through a small bridge WorkflowService attaches at construction,
// which keeps the dependency one-directional (service → store) and the
// store fully unit-testable.

import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subscriber, firstValueFrom, of, throwError } from 'rxjs';
import { catchError, map, switchMap } from 'rxjs/operators';
import { environment } from '../environments/environment';
import { ToastService } from './toast.service';
import type {
  TitlePatchBatchResponse,
  TitlePatchRequest,
  TitlePatchResponse,
  TitlePatchResult,
} from './workflow.service';

/** How the store reaches the active context. Attached by WorkflowService. */
export interface TitleStoreContextBridge {
  /** Disc key of the active context, or null when none. */
  getActiveDiscKey(): string | null;
  /** The active context's titles array (live reference), or null. */
  getActiveTitles(): any[] | null;
  /** Apply a titles update through the context's immutable-update path. */
  applyTitles(update: { titles?: any[]; titlesVersion?: number; titlesVersionAck?: number }): void;
  /** Resolve a row's identity key (title_id / source_file fallback). */
  titleKey(row: any, context: string): string;
}

interface QueuedWrite {
  discId: string;
  fields: Partial<TitlePatchRequest>;
  emitters: Subscriber<TitlePatchResponse>[];
}

@Injectable({ providedIn: 'root' })
export class TitleStore {
  private http = inject(HttpClient);
  private toastSvc = inject(ToastService);
  private readonly apiUrl = environment.apiBase ?? 'http://localhost:8000';

  private bridge: TitleStoreContextBridge | null = null;

  // ── The three caches (moved verbatim from WorkflowService) ──────────
  /** Highest title_seq observed per title — reads, write acks, deltas. */
  private seqByTitleId = new Map<string, number>();
  /** Text with an unresolved write per title. While present, no inbound
   *  row may overwrite the title text (its write cannot be in any server
   *  snapshot yet). */
  private pendingTextByTitleId = new Map<string, string>();
  /** Highest titles_version acknowledged per disc. */
  private versionAckByDisc = new Map<string, number>();

  // ── Per-title write queue ────────────────────────────────────────────
  /** Titles with a PATCH in flight. At most one write per title exists on
   *  the wire at any moment; later edits coalesce into `queuedByTitleId`
   *  and send when the slot frees, carrying the version the ack taught
   *  us. Two same-tick writes to one row carried the same base_seq — one
   *  of them always lost; a queue cannot race itself. */
  private inFlightTitleIds = new Set<string>();
  private queuedByTitleId = new Map<string, QueuedWrite>();

  attach(bridge: TitleStoreContextBridge): void {
    this.bridge = bridge;
  }

  // ── Cache accessors (Workflow service + fetch reconcile consult these) ──

  /** The version we last observed for this title. Read-only on purpose:
   *  the server owns version assignment (#778 stage 2). Falls back to the
   *  active context's row when the cache has no entry. */
  knownSeq(titleId: string): number {
    const cached = this.seqByTitleId.get(titleId);
    if (typeof cached === 'number') return cached;
    const row = this.bridge?.getActiveTitles()?.find(t => t?.title_id === titleId);
    return typeof row?.title_seq === 'number' ? row.title_seq : 0;
  }

  /** Cache-only variant for merge rules that combine with a local row. */
  cachedSeq(titleId: string): number {
    return this.seqByTitleId.get(titleId) ?? 0;
  }

  pendingTextFor(titleId: string): string | undefined {
    return this.pendingTextByTitleId.get(titleId);
  }

  /** Learn seqs from any server-authoritative rows (fetch, set-primary…). */
  learnRowSeqs(rows: any[] | null | undefined): void {
    if (!rows || rows.length === 0) return;
    for (const t of rows) {
      const titleId = t?.title_id;
      const seq = t?.title_seq;
      if (!titleId || typeof seq !== 'number') continue;
      const current = this.seqByTitleId.get(String(titleId)) ?? 0;
      if (seq > current) this.seqByTitleId.set(String(titleId), seq);
    }
  }

  /** Record a titles_version ack for a disc; returns the current ack. */
  ackVersion(discId: string, version: number | null | undefined): number | undefined {
    if (typeof version === 'number') {
      const current = this.versionAckByDisc.get(discId) ?? 0;
      if (version > current) this.versionAckByDisc.set(discId, version);
    }
    return this.versionAckByDisc.get(discId);
  }

  ackFor(discId: string): number | undefined {
    return this.versionAckByDisc.get(discId);
  }

  // ── Input 1: local edits (per-title write queue) ─────────────────────

  /** Persist a single-title patch. Serialized per title: if a write for
   *  this title is already in flight, the fields coalesce into the next
   *  write, which departs when the ack returns — carrying the version the
   *  ack taught us. All callers of a coalesced write receive the same
   *  final response. */
  enqueuePatch(discId: string, patch: TitlePatchRequest): Observable<TitlePatchResponse> {
    const titleId = patch?.title_id ? String(patch.title_id) : null;
    if (!titleId) {
      return this.sendPatchAttempt(discId, patch, false);
    }
    if (this.inFlightTitleIds.has(titleId)) {
      return new Observable<TitlePatchResponse>(sub => {
        const q = this.queuedByTitleId.get(titleId) ?? { discId, fields: {}, emitters: [] };
        const { title_id: _id, base_seq: _b, title_seq: _s, ...content } = patch as any;
        q.fields = { ...q.fields, ...content };
        q.discId = discId;
        q.emitters.push(sub);
        this.queuedByTitleId.set(titleId, q);
        // Keep the typing guard current while the write waits its turn.
        if (typeof patch.title === 'string') {
          this.pendingTextByTitleId.set(titleId, patch.title);
        }
      });
    }
    return this.dispatch(discId, patch, titleId);
  }

  /** Take the slot, send, and on settle flush whatever coalesced. */
  private dispatch(discId: string, patch: TitlePatchRequest, titleId: string): Observable<TitlePatchResponse> {
    this.inFlightTitleIds.add(titleId);
    const release = () => {
      this.inFlightTitleIds.delete(titleId);
      const q = this.queuedByTitleId.get(titleId);
      if (!q) return;
      this.queuedByTitleId.delete(titleId);
      const nextPatch = { title_id: titleId, ...q.fields } as TitlePatchRequest;
      this.dispatch(q.discId, nextPatch, titleId).subscribe({
        next: r => { q.emitters.forEach(e => { e.next(r); e.complete(); }); },
        error: err => { q.emitters.forEach(e => e.error(err)); },
      });
    };
    return new Observable<TitlePatchResponse>(sub => {
      this.sendPatchAttempt(discId, patch, false).subscribe({
        next: r => { release(); sub.next(r); sub.complete(); },
        error: err => { release(); sub.error(err); },
      });
    });
  }

  private sendPatchAttempt(
    discId: string,
    patch: TitlePatchRequest,
    isRetry: boolean,
  ): Observable<TitlePatchResponse> {
    const url = `${this.apiUrl}/discs/${discId}/titles`;
    const patchWithSeq = { ...patch };
    // #778 stage 2: send the version we READ (If-Match). The server compares
    // and assigns the next one. The client never computes a version.
    if (patchWithSeq?.title_id && typeof (patchWithSeq as any).base_seq !== 'number') {
      (patchWithSeq as any).base_seq = this.knownSeq(patchWithSeq.title_id);
    }
    if (patchWithSeq?.title_id && typeof patchWithSeq.title === 'string') {
      this.pendingTextByTitleId.set(patchWithSeq.title_id, patchWithSeq.title);
    }
    return this.http.patch<TitlePatchResponse>(url, patchWithSeq).pipe(
      switchMap(response => {
        // stale_seq on the FIRST attempt is routinely our cache trailing a
        // background bump — not a human racing the user. The rejected
        // fields are what the user just typed; silently discarding them is
        // the "name reverts" data loss. Re-send ONCE against the version
        // the server handed back (user wins — design principle 1). Only if
        // the retry also loses does the conflict flow to the UI (toast +
        // current value), which means a genuine concurrent editor.
        const retryPatch = !isRetry ? this.staleRetryPatch(patch, response?.result) : null;
        if (retryPatch) {
          return this.sendPatchAttempt(discId, retryPatch, true);
        }
        this.applyPatchResults(discId, [response.result], response.titles_version, response.synced_titles);
        return of(response);
      })
    );
  }

  /** Build the one-shot retry patch for a stale_seq result, or null when the
   *  result isn't a retryable conflict. Also advances the seq cache to the
   *  server's version so the retry (and any later write) is aligned. */
  private staleRetryPatch(
    patch: TitlePatchRequest,
    result: TitlePatchResult | undefined,
  ): TitlePatchRequest | null {
    if (!result || result.success || result.error_code !== 'stale_seq') return null;
    if (!patch?.title_id) return null;
    const fresh = (result as any).current_title;
    const freshSeq = typeof fresh?.title_seq === 'number' ? fresh.title_seq : null;
    if (freshSeq === null) return null;
    // Only content writes earn the retry; a patch that carries nothing but
    // versioning keys has nothing of the user's to preserve.
    const hasContent = Object.keys(patch).some(
      k => k !== 'title_id' && k !== 'base_seq' && k !== 'title_seq',
    );
    if (!hasContent) return null;
    const key = String(patch.title_id);
    const cached = this.seqByTitleId.get(key) ?? 0;
    if (freshSeq > cached) this.seqByTitleId.set(key, freshSeq);
    return { ...patch, base_seq: freshSeq } as TitlePatchRequest;
  }

  /** Persist a batch. Batches are group gestures whose members are patched
   *  once each, so they bypass the per-title queue; they still stamp the
   *  caches and get the same per-row one-shot stale retry. */
  patchBatch(discId: string, patches: TitlePatchRequest[]): Observable<TitlePatchBatchResponse> {
    return this.sendBatchAttempt(discId, patches, false);
  }

  private sendBatchAttempt(
    discId: string,
    patches: TitlePatchRequest[],
    isRetry: boolean,
  ): Observable<TitlePatchBatchResponse> {
    const url = `${this.apiUrl}/discs/${discId}/titles/batch`;
    const patched = patches.map(patch => {
      const nextPatch = { ...patch };
      if (nextPatch?.title_id && typeof (nextPatch as any).base_seq !== 'number') {
        (nextPatch as any).base_seq = this.knownSeq(nextPatch.title_id);
      }
      if (nextPatch?.title_id && typeof nextPatch.title === 'string') {
        this.pendingTextByTitleId.set(nextPatch.title_id, nextPatch.title);
      }
      return nextPatch;
    });
    return this.http.patch<TitlePatchBatchResponse>(url, { patches: patched }).pipe(
      switchMap(response => {
        const results = response.results || [];
        if (!isRetry) {
          // Same one-shot user-wins retry as the single path, per row.
          const byId = new Map(patches.map(p => [String(p?.title_id ?? ''), p]));
          const retries: TitlePatchRequest[] = [];
          const settled: TitlePatchResult[] = [];
          for (const r of results) {
            const orig = byId.get(String(r?.title_id ?? ''));
            const retry = orig ? this.staleRetryPatch(orig, r) : null;
            if (retry) retries.push(retry);
            else settled.push(r);
          }
          if (retries.length > 0) {
            this.applyPatchResults(discId, settled, response.titles_version, response.synced_titles);
            return this.sendBatchAttempt(discId, retries, true).pipe(
              map(second => ({
                ...second,
                results: [...settled, ...(second.results || [])],
              }))
            );
          }
        }
        this.applyPatchResults(discId, results, response.titles_version, response.synced_titles);
        return of(response);
      })
    );
  }

  // ── Input 2: write acks (success, synced rows, conflicts) ────────────

  applyPatchResults(discId: string, results: TitlePatchResult[], titlesVersion: number, syncedTitles?: any[]): void {
    if (typeof titlesVersion === 'number') {
      this.ackVersion(discId, titlesVersion);
    }
    // Seqs from a write ack are authoritative no matter which disc is on
    // screen — the write queue departs its next write with these, so they
    // must be learned BEFORE the active-disc gates below. (Content
    // application still applies only to the active context.)
    this.learnRowSeqs(results.map(r => r?.success ? r.updated_title : (r as any)?.current_title).filter(Boolean));
    this.learnRowSeqs(syncedTitles);
    const bridge = this.bridge;
    if (!bridge) return;
    const activeDiscKey = bridge.getActiveDiscKey();
    if (activeDiscKey !== discId) return;
    const currentTitles = bridge.getActiveTitles();
    if (!currentTitles) return;

    // #383: stale_seq conflicts that survived the one-shot retry. Surface
    // the loss and reconcile the conflicted rows in place.
    const staleSeqConflicts = results.filter(r => !r?.success && r?.error_code === 'stale_seq');
    if (staleSeqConflicts.length > 0) {
      this.handleStaleSeqConflicts(discId, staleSeqConflicts);
    }

    // #363 H1: pipeline-guard rejections — toast once.
    const lockedResults = results.filter(
      r => !r?.success && (r?.error_code === 'labels_locked' || r?.error_code === 'type_change_locked'),
    );
    if (lockedResults.length > 0) {
      const msg = (lockedResults[0] as any)?.error || 'Title edits are locked at this pipeline stage';
      this.toastSvc.show(msg, 'error', 5000);
    }

    let changed = false;
    const nextTitles = [...currentTitles];
    results.forEach(result => {
      if (!result?.success || !result.updated_title) return;
      const updated = result.updated_title;
      let updatedKey: string;
      try {
        updatedKey = bridge.titleKey(updated, 'applyPatchResults:updated');
      } catch {
        return;
      }
      const responseSeq = typeof updated.title_seq === 'number' ? updated.title_seq : null;
      const localSeq = updatedKey ? this.seqByTitleId.get(updatedKey) : undefined;
      if (typeof responseSeq === 'number' && typeof localSeq === 'number' && responseSeq < localSeq) {
        return;
      }
      if (typeof responseSeq === 'number' && updatedKey) {
        const currentSeq = this.seqByTitleId.get(updatedKey) ?? 0;
        if (responseSeq > currentSeq) {
          this.seqByTitleId.set(updatedKey, responseSeq);
        }
      }
      const idx = nextTitles.findIndex(t => {
        try {
          return bridge.titleKey(t, 'applyPatchResults:existing') === updatedKey;
        } catch {
          return false;
        }
      });
      if (idx >= 0) {
        const existingTitle = nextTitles[idx];
        const pendingTitle = updatedKey ? this.pendingTextByTitleId.get(updatedKey) : undefined;
        const shouldSkipTitle = typeof pendingTitle === 'string' &&
          typeof updated.title === 'string' &&
          updated.title !== pendingTitle;
        const mergedUpdate = shouldSkipTitle
          ? { ...updated, title: existingTitle?.title }
          : updated;
        if (!shouldSkipTitle && updatedKey) {
          this.pendingTextByTitleId.delete(updatedKey);
        }
        nextTitles[idx] = { ...existingTitle, ...mergedUpdate };
      } else {
        nextTitles.push(updated);
      }
      changed = true;
    });

    // #778 stage 2: fold in the winning row for each conflict. The server
    // hands it back, so a conflict is a merge — not an error path that
    // refetches the disc and overlays every row.
    for (const conflict of results) {
      if (conflict?.success || conflict?.error_code !== 'stale_seq') continue;
      const fresh = (conflict as any).current_title;
      if (!fresh) continue;
      let key: string;
      try {
        key = bridge.titleKey(fresh, 'applyPatchResults:conflict');
      } catch {
        continue;
      }
      if (typeof fresh.title_seq === 'number') {
        this.seqByTitleId.set(key, fresh.title_seq);
      }
      // The user was just told their edit lost; keeping pending text would
      // resurrect it on a later merge.
      this.pendingTextByTitleId.delete(key);
      const idx = nextTitles.findIndex(t => {
        try {
          return bridge.titleKey(t, 'applyPatchResults:conflictExisting') === key;
        } catch {
          return false;
        }
      });
      if (idx < 0) continue;
      nextTitles[idx] = { ...nextTitles[idx], ...fresh };
      changed = true;
    }

    // #775: rows the server's duplicate-group sweep modified as a side
    // effect of this patch (area 2 narrowed WHEN it runs — type writes —
    // but when it runs, its rows still arrive here).
    for (const synced of syncedTitles ?? []) {
      let syncedKey: string;
      try {
        syncedKey = bridge.titleKey(synced, 'applyPatchResults:synced');
      } catch {
        continue;
      }
      if (typeof synced.title_seq === 'number') {
        const cur = this.seqByTitleId.get(syncedKey) ?? 0;
        if (synced.title_seq > cur) {
          this.seqByTitleId.set(syncedKey, synced.title_seq);
        }
      }
      const idx = nextTitles.findIndex(t => {
        try {
          return bridge.titleKey(t, 'applyPatchResults:syncedExisting') === syncedKey;
        } catch {
          return false;
        }
      });
      if (idx < 0) continue;
      const existing = nextTitles[idx];
      const pendingText = this.pendingTextByTitleId.get(syncedKey);
      const protectText = typeof pendingText === 'string' &&
        typeof synced.title === 'string' &&
        synced.title !== pendingText;
      nextTitles[idx] = protectText
        ? { ...existing, ...synced, title: existing?.title }
        : { ...existing, ...synced };
      changed = true;
    }

    if (changed || typeof titlesVersion === 'number') {
      bridge.applyTitles({
        titles: changed ? nextTitles : (currentTitles as any),
        titlesVersion: titlesVersion,
        titlesVersionAck: this.versionAckByDisc.get(discId),
      });
    }
  }

  /** Post-retry conflicts: reconcile the conflicted rows in place and tell
   *  the user. By the time a conflict reaches here it has already earned
   *  it — the one-shot retry means this is a genuine concurrent editor. */
  private handleStaleSeqConflicts(
    discId: string,
    conflicts: TitlePatchResult[],
  ): void {
    // Rows carrying current_title are folded into the caller's single
    // context write (applyPatchResults) — only legacy responses without
    // current_title still need the network.
    const needFetch = conflicts.filter(c => !(c as any)?.current_title);

    const count = conflicts.length;
    const message = count === 1
      ? "Your title edit conflicted with a newer change — showing the current value."
      : `${count} title edits conflicted with newer changes — showing current values.`;
    try {
      this.toastSvc.show(message, 'error', 5000);
    } catch {
      // Toast service failure shouldn't break reconciliation.
    }

    if (needFetch.length > 0) {
      this.refreshTitleSeqsAfterConflict(discId, needFetch).catch((err) => {
        console.warn('[TitleStore] stale_seq refresh failed', err);
      });
    }
  }

  /** Legacy-conflict recovery: refetch the disc's title seqs so the local
   *  cache catches up, replacing CONTENT only for rows that conflicted. */
  private async refreshTitleSeqsAfterConflict(
    discId: string,
    conflicts: TitlePatchResult[],
  ): Promise<void> {
    if (!discId || conflicts.length === 0) return;
    const url = `${this.apiUrl}/discs/${encodeURIComponent(discId)}/titles?limit=500`;
    try {
      const response: any = await firstValueFrom(this.http.get<any>(url));
      const items: any[] = Array.isArray(response?.items) ? response.items : [];
      this.learnRowSeqs(items);

      const bridge = this.bridge;
      if (!bridge || bridge.getActiveDiscKey() !== discId) return;
      const currentTitles = bridge.getActiveTitles();
      if (!Array.isArray(currentTitles)) return;

      const titlesById = new Map<string, any>();
      for (const item of items) {
        if (item?.title_id) titlesById.set(String(item.title_id), item);
      }
      const conflictedIds = new Set(
        conflicts.map(c => String(c?.title_id ?? '')).filter(Boolean),
      );
      let changed = false;
      const nextTitles = currentTitles.map((t: any) => {
        const id = t?.title_id ?? t?.id;
        if (!id || !conflictedIds.has(String(id))) return t;
        const fresh = titlesById.get(String(id));
        if (!fresh) return t;
        changed = true;
        this.pendingTextByTitleId.delete(String(id));
        return { ...t, ...fresh };
      });
      if (changed) {
        bridge.applyTitles({ titles: nextTitles });
      }
    } catch (err) {
      console.warn('[TitleStore] refreshTitleSeqsAfterConflict fetch failed', err);
    }
  }

  // ── Input 3: delta events (titles_changed) ───────────────────────────

  /** Fold server-authoritative rows, one row at a time. Strict per-row seq
   *  gating: self-echoes and reordered deliveries drop out as no-ops; text
   *  mid-typing is never overwritten; rows for a non-active disc update
   *  the seq cache only. */
  foldServerRows(discId: string | undefined, rows: any[], titlesVersion?: number): void {
    if (!discId || !Array.isArray(rows) || rows.length === 0) return;
    if (typeof titlesVersion === 'number') {
      this.ackVersion(discId, titlesVersion);
    }
    const bridge = this.bridge;
    const isActiveDisc = !!bridge && bridge.getActiveDiscKey() === discId;

    const applicable: any[] = [];
    for (const row of rows) {
      const id = row?.title_id != null ? String(row.title_id) : null;
      if (!id) continue;
      const rowSeq = typeof row.title_seq === 'number' ? row.title_seq : null;
      const known = this.seqByTitleId.get(id) ?? 0;
      if (rowSeq !== null && rowSeq <= known) continue; // self-echo / stale delivery
      if (rowSeq !== null) this.seqByTitleId.set(id, rowSeq);
      applicable.push(row);
    }
    const currentTitles = isActiveDisc ? bridge!.getActiveTitles() : null;
    if (!isActiveDisc || applicable.length === 0 || !Array.isArray(currentTitles)) return;

    let changed = false;
    const nextTitles = [...currentTitles];
    for (const row of applicable) {
      const id = String(row.title_id);
      const idx = nextTitles.findIndex(t => {
        try {
          return bridge!.titleKey(t, 'foldServerRows') === id;
        } catch {
          return false;
        }
      });
      if (idx < 0) continue;
      const existing = nextTitles[idx];
      const pendingText = this.pendingTextByTitleId.get(id);
      const protectText = typeof pendingText === 'string' &&
        typeof row.title === 'string' &&
        row.title !== pendingText;
      nextTitles[idx] = protectText
        ? { ...existing, ...row, title: existing?.title }
        : { ...existing, ...row };
      changed = true;
    }
    if (changed) {
      bridge!.applyTitles({
        titles: nextTitles,
        titlesVersion: titlesVersion,
        titlesVersionAck: this.versionAckByDisc.get(discId),
      });
    }
  }
}
