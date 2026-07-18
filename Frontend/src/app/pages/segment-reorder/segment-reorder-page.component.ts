import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { CdkDragDrop, DragDropModule, moveItemInArray } from '@angular/cdk/drag-drop';
import { HttpClient } from '@angular/common/http';
import { Subscription } from 'rxjs';
import {
  JobService,
  RemainingPlaylistSizeResponse,
  SupersetCandidate,
} from '../../services/job.service';
import { ToastService } from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';
import { WorkflowService } from '../../services/workflow.service';
import { environment } from '../../environments/environment';
import { BtnComponent } from '../../ui/btn/btn.component';
import { IconComponent } from '../../ui/icon/icon.component';
import { KbdComponent } from '../../ui/kbd/kbd.component';
import { PillComponent } from '../../ui/pill/pill.component';
import {
  SegmentSupersetPickerComponent,
  SegmentSupersetCluster,
} from '../../components/segment-superset-picker/segment-superset-picker.component';

/**
 * Path A — segment-reorder UI.
 *
 * Once the exploratory rip completes and previews are extracted, the user
 * drags these cards into story order. On submit we POST the ordering to
 * `/jobs/{id}/segment-order`. Three response shapes:
 *
 *   - matched=true with a unique title_index → backend kicks off the
 *     canonical re-rip via the selective-rip path; this page navigates
 *     back to the ripper.
 *   - matched=false with candidates → multiple exact matches; show
 *     them and let the user pick (TODO: secondary modal; for now the
 *     toast directs them to retry ordering).
 *   - matched=false with no candidates → no exact match; user re-orders
 *     and resubmits.
 *
 * Partial-order persistence is handled server-side via
 * `jobs.segment_reorder_state.submitted_order`; on revisit we hydrate
 * from that field. (The plan calls for a per-segment partial_order
 * draft as well; this is a follow-up.)
 */

interface PreviewSpec {
  index: number;
  path: string;            // relative to previews_dir, e.g. "seg_00.mp4"
  cum_start_s: number;
  mode: 'full' | 'stitch';
  src_dur_s: number;
  /** MPLS clip identifier (e.g. "00504"); the matching half compares
   * user-submitted orderings of these clip names against `segment_map`. */
  clip_name?: string;
  head_s?: number;
  tail_s?: number;
}

@Component({
  selector: 'app-segment-reorder-page',
  standalone: true,
  imports: [
    CommonModule,
    DragDropModule,
    BtnComponent,
    PillComponent,
    KbdComponent,
    IconComponent,
    SegmentSupersetPickerComponent,
  ],
  templateUrl: './segment-reorder-page.component.html',
  styleUrls: ['./segment-reorder-page.component.scss'],
})
export class SegmentReorderPageComponent implements OnInit, OnChanges, OnDestroy {
  /** Optional @Input — when set, the component runs in embedded mode
   * (inside path-a-workspace) and skips the route paramMap subscription.
   * When unset, the component reads `:jobId` from the route. */
  @Input() jobIdInput: string | null = null;

  /** Resolved job id — populated either from `jobIdInput` or the route. */
  jobId: string | null = null;

  /** Bound to the disc the job is currently working — required for the
   * per-clip flag endpoints (PATCH /discs/{id}/segment-flags). Pulled
   * from `jobStatus.disc_id` on context load. */
  discId: string | null = null;

  /** MakeMKV title index of the mpls we exploratory-ripped to produce
   * the previews. Required for `flag-decoys` so the backend can mark
   * the exploratory mpls + its sibling permutations as type='ignore'. */
  exploratoryTitleIndex: number | null = null;

  /** Preview specs in the user's current ordering. */
  segments: PreviewSpec[] = [];

  /** Live region announcement string for keyboard-driven reorder. */
  announce = '';

  /** Set true while submit/cancel HTTP calls are in flight. */
  busy = false;

  /** Set when no exact match was found; user should re-order. */
  noMatchHint: string | null = null;

  /** True while the "Are you sure your order is right?" confirmation
   * modal is open. Set after a no-match submit response. */
  showConfirmationGate = false;

  /** True while the "Previous order had decoys" escape-hatch confirmation
   * is open. Distinct from the order-confirmation gate above. */
  showDecoyConfirmation = false;

  /** Most-recent submitted order — held for re-submission via the
   * confirm endpoint if the user reaffirms. */
  private lastSubmittedOrder: string[] = [];

  /** Subsequence-superset clusters from the most recent confirm response.
   * `[]` means the matcher found no candidates; the picker stays hidden
   * and the noMatchHint shows the hard-fail message. */
  supersetClusters: SegmentSupersetCluster[] = [];

  /** Which candidate's rip is currently being kicked off (after the
   * user picks from the superset modal). Drives the in-modal pending
   * state so the user can't double-click. */
  pendingPickerTitleIndex: number | null = null;

  /** Per-clip obfuscation flags (`{clip_id → 'potentially' | 'definitely'}`)
   * hydrated from the disc on load. Drives the three-state toggle on
   * each preview tile. */
  clipFlags: Record<string, 'potentially' | 'definitely'> = {};

  /** MakeMKV title indexes already ruled out via flag-decoys this
   * session. Drives the eliminated-count badge in the sidebar. */
  eliminatedTitleIndexes: number[] = [];

  /** Disk-pressure snapshot for the "Rip the rest" CTA + remaining-size
   * gauge. Loaded on mount and refreshed after every elimination. */
  remainingSizeInfo: RemainingPlaylistSizeResponse | null = null;

  /** True while the "Rip the rest?" confirmation modal is open. */
  showRipTheRestConfirmation = false;

  /** Job's bound mount_point — pulled from workflow-context. Required to
   * pair the job with a disc card and detect ejection. */
  jobMountPoint: string | null = null;

  /** True when no disc is currently in the bound drive (ejected, or not
   * yet scanned after reinsertion). Disables all destructive actions
   * (submit / confirm / flag-decoys / rip-superset / rip-the-rest) and
   * surfaces the "Please reinsert disc" banner. */
  discNotReady = false;

  /** Human-readable reason for the disc-not-ready banner — varies based
   * on whether the disc is ejected vs scanning vs failed. */
  discNotReadyReason: string | null = null;

  /** Latest disc-list snapshot from WorkflowService; cached locally so
   * the readiness check is synchronous. */
  private latestDiscs: any[] = [];

  private subs = new Subscription();

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private http: HttpClient,
    private jobSvc: JobService,
    private toast: ToastService,
    private logger: LoggerService,
    private workflowService: WorkflowService,
  ) {}

  ngOnInit(): void {
    // Embedded mode: jobIdInput is set by the parent (path-a-workspace).
    // Route mode: read :jobId from ActivatedRoute.
    if (this.jobIdInput) {
      this.jobId = this.jobIdInput;
      this.loadSegments(this.jobId);
    } else {
      this.subs.add(
        this.route.paramMap.subscribe((params) => {
          this.jobId = params.get('jobId');
          if (this.jobId) {
            this.loadSegments(this.jobId);
          }
        }),
      );
    }
    // Disc-presence guard: subscribe to the workflow service's disc list
    // so we know the moment the disc is ejected (or hasn't finished a
    // post-reinsertion scan). Recomputes `discNotReady` on every change.
    this.subs.add(
      this.workflowService.getDiscs().subscribe((discs) => {
        this.latestDiscs = discs || [];
        this.refreshDiscReadiness();
      }),
    );
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['jobIdInput'] && changes['jobIdInput'].currentValue) {
      const next = changes['jobIdInput'].currentValue as string;
      if (next !== this.jobId) {
        this.jobId = next;
        this.loadSegments(this.jobId);
      }
    }
  }

  ngOnDestroy(): void {
    this.subs.unsubscribe();
  }

  /** Recompute `discNotReady` from the disc list snapshot — true when
   * the bound mount_point has no in-drive ready disc behind it. */
  private refreshDiscReadiness(): void {
    if (!this.jobMountPoint) {
      // No mount yet (still loading workflow-context); treat as ready
      // so the page can render — the gate fires once mount is known.
      this.discNotReady = false;
      this.discNotReadyReason = null;
      return;
    }
    const match = this.latestDiscs.find((d) => d?.mount_point === this.jobMountPoint);
    if (!match) {
      this.discNotReady = true;
      this.discNotReadyReason =
        'No disc detected in the drive. Please reinsert the disc to continue.';
      return;
    }
    if (match.disc_state !== 'in_drive') {
      this.discNotReady = true;
      this.discNotReadyReason =
        'The disc was ejected. Please reinsert it and wait for the scan to complete.';
      return;
    }
    if (match.scan_state && match.scan_state !== 'ready') {
      this.discNotReady = true;
      const stateLabel =
        match.scan_state === 'pending' ? 'queued for scan' :
        match.scan_state === 'scanning' ? 'still scanning' :
        match.scan_state === 'failed' ? 'failed to scan' :
        match.scan_state;
      this.discNotReadyReason =
        `Disc is ${stateLabel}. Please wait for the scan to complete before continuing.`;
      return;
    }
    this.discNotReady = false;
    this.discNotReadyReason = null;
  }

  // ── Segment loading ──────────────────────────────────────────────────────

  private loadSegments(jobId: string): void {
    // Previews live in the job's raw/previews directory served by the API.
    // Manifest is at GET /jobs/{id}/segment-reorder/manifest (a thin endpoint
    // we'll add in the next commit; for now we read it from the job's
    // workflow context which carries segment_reorder_state).
    this.http.get<any>(`${environment.apiBase}/jobs/${jobId}/workflow-context`).subscribe({
      next: (ctx) => {
        const js = ctx?.jobStatus || {};
        const state = js.segment_reorder_state || {};
        this.discId = js.disc_id || null;
        // Mount point may live on jobStatus.mount_point OR on discInfo
        // depending on how the workflow-context endpoint built the
        // response — check both.
        this.jobMountPoint =
          (ctx?.jobStatus as any)?.mount_point ||
          (ctx?.discInfo as any)?.mount_point ||
          null;
        this.refreshDiscReadiness();
        this.exploratoryTitleIndex =
          typeof state.exploratory_title_index === 'number'
            ? state.exploratory_title_index
            : null;
        this.eliminatedTitleIndexes = Array.isArray(state.eliminated_title_indexes)
          ? [...state.eliminated_title_indexes]
          : [];
        const manifest: PreviewSpec[] = state.previews_manifest || [];
        if (manifest.length > 0) {
          // If the user already submitted an order before, restore it.
          // submitted_order holds stripped clip names ("00503" → "503"),
          // matching what onSubmit() sends to the backend. The manifest
          // carries the padded clip_name and a positional index; build
          // a lookup keyed by the stripped clip name so the restore
          // round-trips correctly.
          const submitted = state.submitted_order || null;
          const byClip = new Map<string, PreviewSpec>();
          for (const s of manifest) {
            const key = this.normalizeClipId(s.clip_name ?? String(s.index));
            if (key) byClip.set(key, s);
          }
          let restored: PreviewSpec[] | null = null;
          if (Array.isArray(submitted) && submitted.length === manifest.length) {
            const lookup = submitted
              .map((k: any) => byClip.get(this.normalizeClipId(String(k))))
              .filter((s: PreviewSpec | undefined): s is PreviewSpec => !!s);
            // Only accept the restore if every submitted entry matched a
            // manifest preview — partial matches mean the manifest changed
            // (e.g. after a re-exploratory rip) and we should fall back to
            // manifest order instead of showing a half-empty list.
            if (lookup.length === manifest.length) {
              restored = lookup;
            }
          }
          this.segments = restored ?? [...manifest];
        }
        if (this.discId) {
          this.loadClipFlags(this.discId);
          this.refreshRemainingSize(this.discId);
        }
      },
      error: (err) => {
        this.logger.error('Failed to load segment-reorder context', err);
        this.toast.show('Failed to load segments. Please try again.', 'error', 6000);
      },
    });
  }

  /** Hydrate the per-clip flag dictionary from the disc on load. */
  private loadClipFlags(discId: string): void {
    this.jobSvc.getSegmentFlags(discId).subscribe({
      next: (resp) => {
        this.clipFlags = { ...resp.flags };
      },
      error: (err) => {
        // Non-fatal — without flags the page still works, the matcher
        // just won't filter/boost candidates.
        this.logger.error('Failed to load clip flags', err);
      },
    });
  }

  /** Refresh the disk-pressure snapshot. Called on mount and after any
   * elimination so the "Rip the rest" CTA appears the moment the
   * remaining size drops under the threshold. */
  private refreshRemainingSize(discId: string): void {
    this.jobSvc.getRemainingPlaylistSize(discId).subscribe({
      next: (resp) => { this.remainingSizeInfo = resp; },
      error: (err) => {
        this.logger.error('Failed to load remaining playlist size', err);
        this.remainingSizeInfo = null;
      },
    });
  }

  /** Friendly size formatter for the gauge — GB if ≥1, MB otherwise. */
  formatBytes(bytes: number | null | undefined): string {
    if (bytes == null) return 'unknown';
    const gb = bytes / (1024 ** 3);
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    const mb = bytes / (1024 ** 2);
    return `${mb.toFixed(0)} MB`;
  }

  /** Width percentage for the disk-pressure gauge fill (0–100). */
  gaugeFillPercent(): number {
    if (!this.remainingSizeInfo) return 0;
    const { remaining_size_b, threshold_b } = this.remainingSizeInfo;
    if (threshold_b <= 0) return 100;
    return Math.min(100, (remaining_size_b / threshold_b) * 100);
  }

  /** Normalize a clip name to its backend-side key — backend's
   * segment_map strips leading zeros ("00504" → "504"). */
  private normalizeClipId(clip: string | undefined): string {
    if (!clip) return '';
    const trimmed = clip.replace(/^0+/, '');
    return trimmed || clip;
  }

  /** Return the current flag for a preview tile (or 'clear' for none). */
  clipFlagFor(seg: PreviewSpec): 'potentially' | 'definitely' | 'clear' {
    const cid = this.normalizeClipId(seg.clip_name ?? String(seg.index));
    return this.clipFlags[cid] || 'clear';
  }

  /** Cycle through the three flag states on a preview tile:
   *  clear → potentially → definitely → clear. */
  onCycleClipFlag(seg: PreviewSpec): void {
    if (!this.discId) {
      this.toast.show('Disc not bound to this job yet — cannot flag clips', 'warning');
      return;
    }
    const cid = this.normalizeClipId(seg.clip_name ?? String(seg.index));
    if (!cid) return;
    const current = this.clipFlagFor(seg);
    const next: 'potentially' | 'definitely' | null =
      current === 'clear' ? 'potentially' :
      current === 'potentially' ? 'definitely' :
      null;
    // Optimistic update; rollback on error.
    const prev = this.clipFlags[cid];
    if (next === null) {
      delete this.clipFlags[cid];
    } else {
      this.clipFlags[cid] = next;
    }
    this.jobSvc.setSegmentFlag(this.discId, cid, next).subscribe({
      next: (resp) => {
        // Trust the server's view in case other tabs raced us.
        this.clipFlags = { ...resp.flags };
      },
      error: (err) => {
        this.logger.error('Failed to set clip flag', err);
        if (prev) {
          this.clipFlags[cid] = prev;
        } else {
          delete this.clipFlags[cid];
        }
        this.toast.show('Failed to update clip flag', 'error', 4000);
      },
    });
  }

  /** Label for the per-tile flag indicator. */
  clipFlagLabel(state: 'potentially' | 'definitely' | 'clear'): string {
    if (state === 'definitely') return 'Definite decoy';
    if (state === 'potentially') return 'Possible decoy';
    return 'Flag this clip';
  }

  // ── "Previous order had decoys" escape hatch ─────────────────────────────

  /** Open the confirmation modal for the "previous order had decoys"
   * action. Available always during preview review — not gated on a
   * no-match response. */
  onFlagDecoysClick(): void {
    if (this.busy) return;
    if (this.discNotReady) {
      this.toast.show(this.discNotReadyReason || 'Disc not ready', 'warning', 5000);
      return;
    }
    if (this.exploratoryTitleIndex === null) {
      this.toast.show(
        'This job has no exploratory title to mark — nothing to flag.',
        'warning', 5000,
      );
      return;
    }
    this.showDecoyConfirmation = true;
  }

  onFlagDecoysCancel(): void {
    this.showDecoyConfirmation = false;
  }

  /** Confirm the "previous order had decoys" action. Marks the
   * exploratory mpls + every sibling sharing its sorted-segment-set
   * as type='ignore' on the backend; sends the user back to pick a
   * fresh exploratory rip. */
  onFlagDecoysConfirm(): void {
    if (!this.jobId || this.exploratoryTitleIndex === null) return;
    this.busy = true;
    this.jobSvc.flagSegmentDecoys(this.jobId, this.exploratoryTitleIndex).subscribe({
      next: (resp) => {
        this.busy = false;
        this.showDecoyConfirmation = false;
        this.eliminatedTitleIndexes = [...resp.eliminated_title_indexes];
        // Refresh disk-pressure snapshot so the "Rip the rest" CTA can
        // unlock immediately if remaining-size dropped under threshold.
        if (this.discId) this.refreshRemainingSize(this.discId);
        this.toast.show(
          `Marked ${resp.newly_eliminated_count} playlist${resp.newly_eliminated_count === 1 ? '' : 's'} ` +
          `as decoy. Pick a new exploratory rip from the ripper.`,
          'success', 5000,
        );
        this.router.navigate(['/activity']);
      },
      error: (err) => {
        this.busy = false;
        this.showDecoyConfirmation = false;
        this.logger.error('Failed to flag decoys', err);
        this.toast.show('Failed to flag decoys', 'error', 6000);
      },
    });
  }

  // ── "Rip the rest" final escape hatch ────────────────────────────────────

  /** True when the backend has greenlit the rip-the-rest CTA — remaining
   * size fits under the threshold AND there are titles left to rip. */
  get canRipTheRest(): boolean {
    return !!this.remainingSizeInfo?.allows_rip_rest;
  }

  /** Open the rip-the-rest confirmation modal. */
  onRipTheRestClick(): void {
    if (this.busy || !this.canRipTheRest) return;
    if (this.discNotReady) {
      this.toast.show(this.discNotReadyReason || 'Disc not ready', 'warning', 5000);
      return;
    }
    this.showRipTheRestConfirmation = true;
  }

  onRipTheRestCancel(): void {
    this.showRipTheRestConfirmation = false;
  }

  /** Confirm: fire the rip-the-rest endpoint and route back to /ripper. */
  onRipTheRestConfirm(): void {
    if (!this.jobId) return;
    this.busy = true;
    this.jobSvc.ripTheRest(this.jobId).subscribe({
      next: (resp) => {
        this.busy = false;
        this.showRipTheRestConfirmation = false;
        this.toast.show(
          `Ripping ${resp.rip_set_size} remaining playlist${resp.rip_set_size === 1 ? '' : 's'} ` +
          `(${this.formatBytes(resp.remaining_size_b)}). Review them from the titles step once complete.`,
          'success', 7000,
        );
        this.router.navigate(['/activity']);
      },
      error: (err) => {
        this.busy = false;
        this.showRipTheRestConfirmation = false;
        this.logger.error('Failed to dispatch rip-the-rest', err);
        const detail = err?.error?.detail;
        const msg =
          detail?.error === 'remaining_size_exceeds_threshold'
            ? 'Remaining playlists are larger than the disk-pressure threshold; eliminate more decoys first.'
            : 'Failed to start rip-the-rest.';
        this.toast.show(msg, 'error', 8000);
      },
    });
  }

  /** Build a URL for the segment preview .mp4 served from the job's raw dir. */
  previewUrl(seg: PreviewSpec): string {
    if (!this.jobId) return '';
    return `${environment.apiBase}/jobs/${this.jobId}/segment-reorder/preview/${seg.path}`;
  }

  formatDuration(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  // ── Reordering ───────────────────────────────────────────────────────────

  onDrop(event: CdkDragDrop<PreviewSpec[]>): void {
    moveItemInArray(this.segments, event.previousIndex, event.currentIndex);
    const moved = this.segments[event.currentIndex];
    this.announce = `Segment ${moved.index} moved to position ${event.currentIndex + 1} of ${this.segments.length}`;
  }

  /** Accessible up/down keyboard reorder (alternative to drag for keyboard users). */
  move(segIndex: number, direction: -1 | 1): void {
    const target = segIndex + direction;
    if (target < 0 || target >= this.segments.length) return;
    moveItemInArray(this.segments, segIndex, target);
    this.announce = `Segment ${this.segments[target].index} moved to position ${target + 1} of ${this.segments.length}`;
  }

  // ── Submit / cancel ──────────────────────────────────────────────────────

  onSubmit(): void {
    if (!this.jobId || this.busy) return;
    if (this.discNotReady) {
      this.toast.show(this.discNotReadyReason || 'Disc not ready', 'warning', 5000);
      return;
    }
    if (this.segments.length === 0) {
      this.toast.show('No segments to submit', 'warning');
      return;
    }
    this.busy = true;
    this.noMatchHint = null;
    this.supersetClusters = [];
    this.showConfirmationGate = false;
    // Backend's match_user_order_to_playlists compares against `segment_map`
    // which holds MPLS clip names ("504,510,501,..."). MakeMKV's segment_map
    // strips the leading "00" of the 5-char clip name (e.g. "00504" → "504"),
    // so we strip our padding too before submitting.
    const order = this.segments.map((s) => {
      const clip = s.clip_name ?? String(s.index);
      // "00504" → "504", but "3113" stays "3113".
      return clip.replace(/^0+/, '') || clip;
    });
    this.lastSubmittedOrder = order;
    this.jobSvc.submitSegmentOrder(this.jobId, order).subscribe({
      next: (result) => {
        this.busy = false;
        if (result.matched) {
          this.toast.show(
            `Matched playlist title ${result.title_index} — starting final rip.`,
            'success',
          );
          this.router.navigate(['/activity']);
          return;
        }
        if ((result.exact_count ?? 0) > 1) {
          this.noMatchHint = `Multiple playlists match this order (${result.exact_count}). Pick one or re-order.`;
          return;
        }
        // No exact match. Open the confirmation gate so the user
        // re-affirms their order before we surface superset candidates.
        // The submit response may already carry supersets but per the
        // gated-UX design we don't show them until the user confirms.
        this.showConfirmationGate = true;
      },
      error: (err) => {
        this.busy = false;
        this.logger.error('Failed to submit segment order', err);
        this.toast.show('Failed to submit segment order', 'error', 6000);
      },
    });
  }

  /** User clicked "Yes, my order is right" on the confirmation gate.
   * Re-submits via /segment-order/confirm to mark confirmed_segment_order
   * and pull back subsequence_supersets filtered by the disc's flags. */
  onConfirmOrder(): void {
    if (!this.jobId || this.busy || this.lastSubmittedOrder.length === 0) return;
    if (this.discNotReady) {
      this.toast.show(this.discNotReadyReason || 'Disc not ready', 'warning', 5000);
      return;
    }
    this.busy = true;
    this.jobSvc.confirmSegmentOrder(this.jobId, this.lastSubmittedOrder).subscribe({
      next: (result) => {
        this.busy = false;
        this.showConfirmationGate = false;
        const supersets = result.subsequence_supersets || [];
        if (supersets.length === 0) {
          // The matcher found nothing even after confirmation. Surface
          // the hard-fail message so the user can re-order or flag decoys.
          this.noMatchHint =
            "Even after confirming your order, no playlist on disc matches. " +
            "The previous exploratory rip may have hit a decoy — try the " +
            "'Previous order had decoys' option to skip it.";
          return;
        }
        this.supersetClusters = this.clusterSupersets(supersets);
      },
      error: (err) => {
        this.busy = false;
        this.showConfirmationGate = false;
        this.logger.error('Failed to confirm segment order', err);
        this.toast.show('Failed to check for matching playlists', 'error', 6000);
      },
    });
  }

  /** User clicked "Let me re-order" on the confirmation gate. */
  onRejectConfirmation(): void {
    this.showConfirmationGate = false;
  }

  /** User picked a superset candidate from the picker. Fires an
   * exploratory rip on the picked title and navigates the user back
   * to the ripper where the rip-progress UI takes over. */
  onSupersetSelect(candidate: SupersetCandidate): void {
    if (!this.jobId || this.pendingPickerTitleIndex !== null) return;
    if (this.discNotReady) {
      this.toast.show(this.discNotReadyReason || 'Disc not ready', 'warning', 5000);
      return;
    }
    this.pendingPickerTitleIndex = candidate.title_index;
    this.jobSvc.ripSupersetCandidate(this.jobId, candidate.title_index).subscribe({
      next: () => {
        this.pendingPickerTitleIndex = null;
        this.supersetClusters = [];
        this.toast.show(
          `Ripping ${candidate.source_file || 'title ' + candidate.title_index} ` +
          `for verification — return here once the rip completes.`,
          'success', 6000,
        );
        this.router.navigate(['/activity']);
      },
      error: (err) => {
        this.pendingPickerTitleIndex = null;
        this.logger.error('Failed to dispatch superset rip', err);
        this.toast.show('Failed to start verification rip', 'error', 6000);
      },
    });
  }

  /** User dismissed the picker without choosing. */
  onSupersetDismiss(): void {
    if (this.pendingPickerTitleIndex !== null) return;
    this.supersetClusters = [];
  }

  /** Group supersets by sorted_set_key. Backend already returns them in
   * the right within-cluster order (fewest extras first); we just bucket. */
  private clusterSupersets(supersets: SupersetCandidate[]): SegmentSupersetCluster[] {
    const buckets = new Map<string, SupersetCandidate[]>();
    for (const c of supersets) {
      const key = c.sorted_set_key || `__${c.title_index}`;
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key)!.push(c);
    }
    // Order clusters by member count desc (largest cluster = most likely
    // to contain the real movie), then by smallest sum-of-extras.
    return Array.from(buckets.values()).sort((a, b) => {
      if (b.length !== a.length) return b.length - a.length;
      const extrasA = a.reduce((sum, c) => sum + c.extras_clips.length, 0);
      const extrasB = b.reduce((sum, c) => sum + c.extras_clips.length, 0);
      return extrasA - extrasB;
    });
  }

  onCancel(): void {
    if (!this.jobId || this.busy) return;
    this.busy = true;
    this.jobSvc.cancelSegmentReorder(this.jobId).subscribe({
      next: () => {
        this.busy = false;
        this.toast.show('Segment reorder cancelled.', 'info');
        this.router.navigate(['/activity']);
      },
      error: (err) => {
        this.busy = false;
        this.logger.error('Failed to cancel segment-reorder', err);
        this.toast.show('Failed to cancel.', 'error', 6000);
      },
    });
  }
}
