import { Component, OnInit, OnDestroy, HostListener, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { combineLatest, Observable, Subscription, BehaviorSubject, of } from 'rxjs';
import { map, switchMap, startWith, distinctUntilChanged } from 'rxjs/operators';
import { WorkflowService, DiscMetadata, ProgressUpdateMessage } from '../../../../services/workflow.service';
// RipperStateService removed - using WorkflowService
import { MetadataService } from '../../../../services/metadata.service';
import { LoggerService } from '../../../../services/logger.service';
import { filterSupersededFailedCarouselDiscs } from '../../../../utils/filter-superseded-failed-carousel-discs.util';
import { sortCarouselDiscsForDisplay } from '../../../../utils/sort-carousel-discs.util';
import { IconComponent } from '../../../../ui/icon/icon.component';
import { PillComponent } from '../../../../ui/pill/pill.component';
import { ProgressRingComponent } from '../../../../ui/progress-ring/progress-ring.component';

export type CardType = { type: 'drive' | 'job', id: string, data: DiscMetadata };

@Component({
  selector: 'app-card-carousel',
  standalone: true,
  imports: [CommonModule, IconComponent, PillComponent, ProgressRingComponent],
  templateUrl: './card-carousel.component.html',
  styleUrls: ['./card-carousel.component.scss']
})
export class CardCarouselComponent implements OnInit, OnDestroy {
  private subscriptions = new Subscription();
  /** Emits true after a drive has been scanning for more than 30s (reassurance pill). Driven by Observable so async pipe triggers view update. */
  showScanLongMessage$ = new BehaviorSubject<boolean>(false);
  /** True when the scan-long tooltip panel is visible (hover or tap). */
  showScanTooltip = false;
  /** Tooltip copy for the "taking longer" pill (desktop hover, mobile tap). */
  readonly scanTooltipText = 'Large disc - scanning may take a few minutes';
  private scanLongMessageTimeout: ReturnType<typeof setTimeout> | null = null;

  // Observables from services (initialized in constructor)
  discs$!: Observable<DiscMetadata[]>;
  selectedCard$!: Observable<{ type: 'drive' | 'job', id: string } | null>;
  driveLoadingStates$!: Observable<Map<string, boolean>>;
  driveScanState$!: Observable<string>;
  movieOptions$!: Observable<any[]>;

  allCards$!: Observable<CardType[]>;
  /** Set of job_id that are currently processing (rip/postprocess/transfer) for spinner on job cards. */
  jobIdsProcessing$!: Observable<Set<string>>;
  /** Map of mount_point → rip_progress (0–100) for in-drive cards with an active rip. */
  driveRipProgress$!: Observable<Map<string, number>>;

  constructor(
    private workflowService: WorkflowService,
    // ripperStateService removed - using workflowService
    private metadataService: MetadataService,
    private logger: LoggerService,
    private cdr: ChangeDetectorRef
  ) {
    // Initialize observables after services are injected
    this.discs$ = this.workflowService.discs$;

    this.selectedCard$ = this.workflowService.getSelectedCard$();

    this.driveLoadingStates$ = this.workflowService.getUIOrchestrationState$().pipe(
      map((state: any) => state.driveLoadingStates)
    );

    this.driveScanState$ = this.workflowService.getUIOrchestrationState$().pipe(
      map((state: any) => state.driveScanState)
    );

    this.movieOptions$ = this.metadataService.getMovieOptions();

    // Initialize allCards$ AFTER observables are set up
    this.allCards$ = combineLatest([
      this.discs$,
      this.selectedCard$
    ]).pipe(
      map(([discs]) => {
        const sortedDiscs = sortCarouselDiscsForDisplay(discs);
        const sortedDiscsFiltered = filterSupersededFailedCarouselDiscs(sortedDiscs);

        const cards = sortedDiscsFiltered.map(disc => ({
          type: disc.disc_state === 'in_drive' ? 'drive' as const : 'job' as const,
          id: disc.disc_state === 'in_drive'
            ? (disc.mount_point || disc.disc_id || '')
            : (disc.job_id || disc.disc_id || ''),
          data: disc
        }));
        // #603: when an inserted disc is finalized in the Library, the drive
        // card is the one clear surface; drop any job card for the same
        // disc_id so the user doesn't see two competing entries.
        const finalizedDiscIds = new Set(
          cards
            .filter(c => c.type === 'drive' && c.data.finalized === true && c.data.disc_id)
            .map(c => c.data.disc_id as string)
        );
        if (finalizedDiscIds.size === 0) return cards;
        return cards.filter(c => c.type !== 'job' || !c.data.disc_id || !finalizedDiscIds.has(c.data.disc_id));
      })
    );

    this.jobIdsProcessing$ = this.allCards$.pipe(
      switchMap((cards) => {
        const jobCards = cards.filter((c): c is CardType & { type: 'job' } => c.type === 'job');
        if (jobCards.length === 0) {
          return of(new Set<string>());
        }
        return combineLatest(
          jobCards.map((c) =>
            this.workflowService.getJobProgress(c.data.job_id!).pipe(
              map((progress) => ({ jobId: c.data.job_id!, isProcessing: this.isProgressProcessing(progress) })),
              startWith({ jobId: c.data.job_id!, isProcessing: false })
            )
          )
        ).pipe(
          map((arr) => new Set(arr.filter((x) => x.isProcessing).map((x) => x.jobId)))
        );
      })
    );

    // Drive cards: rip progress (0–100) keyed by mount_point for determinate ring display
    this.driveRipProgress$ = this.allCards$.pipe(
      switchMap((cards) => {
        const driveCards = cards.filter(
          (c) => c.type === 'drive' && c.data.job_id && c.data.mount_point
        );
        if (driveCards.length === 0) {
          return of(new Map<string, number>());
        }
        return combineLatest(
          driveCards.map((c) =>
            this.workflowService.getJobProgress(c.data.job_id!).pipe(
              map((progress) => ({
                mount: c.data.mount_point!,
                ripProgress: progress?.rip_progress ?? 0,
              })),
              startWith({ mount: c.data.mount_point!, ripProgress: 0 })
            )
          )
        ).pipe(
          map((arr) => {
            const m = new Map<string, number>();
            for (const item of arr) {
              m.set(item.mount, item.ripProgress);
            }
            return m;
          })
        );
      })
    );
  }

  /**
   * True when a stage is *actively* working (ripping, validating, postprocessing,
   * transferring) — not when the job is sitting between stages awaiting user
   * input. A stage at 0 means "not started"; at 100 means "done". Only the
   * strictly-in-between window counts as processing. `rip_phase` covers the
   * pre-heartbeat moment when rip just started but rip_progress hasn't ticked.
   *
   * Progress messages come from a BehaviorSubject that holds the LAST value
   * seen. Rip_complete on the backend does not emit a fresh progress_update
   * (only a context_changed for jobStatus), so the last rip-time progress
   * lingers — commonly with `rip_phase='verification'` at rip_progress=100.
   * Without the terminal-state guard below the card stays "busy" forever
   * after rip finishes, even though the job is idle awaiting labeling
   * (the exact symptom: user ejects disc mid-labeling and sees a spinner
   * on the unfinished-job card).
   *
   * Progress messages have carried `rip_state`/`post_state`/`transfer_state`
   * since #604/#605, so we can lean on them as the authoritative terminal
   * signal. When a stage's state is terminal, ignore its phase/progress
   * for the purposes of the busy indicator.
   */
  isProgressProcessing(progress: ProgressUpdateMessage | null): boolean {
    if (!progress) return false;
    const isTerminal = (s: string | null | undefined): boolean =>
      s === 'completed' || s === 'skipped' || s === 'failed';
    if (!isTerminal(progress.rip_state)) {
      if (progress.rip_phase) return true;
      if (progress.rip_progress > 0 && progress.rip_progress < 100) return true;
    }
    if (!isTerminal(progress.post_state)) {
      if (progress.post_progress > 0 && progress.post_progress < 100) return true;
    }
    if (!isTerminal(progress.transfer_state)) {
      if (
        progress.transfer_progress != null &&
        progress.transfer_progress > 0 &&
        progress.transfer_progress < 100
      ) {
        return true;
      }
    }
    return false;
  }

  isJobCardProcessing(card: CardType, jobIdsProcessing: Set<string> | null | undefined): boolean {
    if (card.type !== 'job' || !card.data.job_id) return false;
    // Terminal job states never spin — progress alone can't distinguish a
    // stalled / failed job (last known progress sticks) from a live one.
    const status = card.data.job_status;
    if (status === 'failed' || status === 'completed' || status === 'cancelled') return false;
    return jobIdsProcessing != null && jobIdsProcessing.has(card.data.job_id);
  }

  /** In-drive card with a failed job attached (joined from unfinished / coordinator / job_finished). */
  isDriveCardJobFailed(disc: DiscMetadata): boolean {
    return (
      disc.disc_state === 'in_drive' &&
      disc.scan_state === 'ready' &&
      !!disc.job_id &&
      disc.job_status === 'failed'
    );
  }

  /** TheDiscDB matched — carousel pill (uses discdb_result / discdb_hit, not workflow stage_profile). */
  showDiscdbSuggestedOnCard(data: DiscMetadata): boolean {
    if (data.discdb_result === 'hit') return true;
    return data.discdb_hit === true;
  }

  ngOnInit(): void {
    this.subscriptions.add(
      combineLatest([this.driveScanState$, this.discs$]).pipe(
        map(([driveScanState, discs]) =>
          driveScanState === 'scanning' ||
          discs.some(
            (d) =>
              d.disc_state === 'in_drive' &&
              (d.scan_state === 'scanning' || d.scan_state === 'pending')
          )
        )
      ).subscribe((anyScanning) => {
        if (this.scanLongMessageTimeout) {
          clearTimeout(this.scanLongMessageTimeout);
          this.scanLongMessageTimeout = null;
        }
        if (!anyScanning) {
          this.showScanLongMessage$.next(false);
          this.showScanTooltip = false;
          this.cdr.detectChanges();
          return;
        }
        this.scanLongMessageTimeout = setTimeout(() => {
          this.showScanLongMessage$.next(true);
          this.scanLongMessageTimeout = null;
        }, 30000);
      })
    );
  }

  ngOnDestroy(): void {
    if (this.scanLongMessageTimeout) {
      clearTimeout(this.scanLongMessageTimeout);
      this.scanLongMessageTimeout = null;
    }
    this.subscriptions.unsubscribe();
  }

  trackByCardId(index: number, card: CardType): string {
    return `${card.type}-${card.id}`;
  }

  isCardActive(card: CardType, selectedCard: { type: 'drive' | 'job', id: string } | null): boolean {
    if (!selectedCard) return false;
    return selectedCard.type === card.type && selectedCard.id === card.id;
  }

  isCardLoading(card: CardType, driveScanState: string | null, driveLoadingStates: Map<string, boolean> | null): boolean {
    if (card.type === 'drive') {
      // Empty drive (ejected / no disc): never show loading — just "Insert Disc"
      if (card.data.disc_id?.startsWith('empty-') || (!card.data.disc_hash && !card.data.scan_state)) {
        return false;
      }

      // If disc has a hash or real disc_id, the scan completed successfully —
      // never show loading regardless of stale scan_state or driveLoadingStates.
      const hasRealData = card.data.disc_hash ||
        (card.data.disc_id && !card.data.disc_id.startsWith('pending-') &&
         !card.data.disc_id.startsWith('scanning-') && !card.data.disc_id.startsWith('empty-'));
      if (hasRealData && card.data.scan_state !== 'scanning') {
        return false;
      }

      // Check scan_state from coordinator - show loading if pending or scanning
      const scanState = card.data.scan_state;
      if (scanState === 'pending' || scanState === 'scanning') {
        return true;
      }
      return false;
    }
    return false;
  }

  onCardSelected(card: CardType): void {
    // Prevent clicks during scanning for drive cards
    if (card.type === 'drive') {
      const scanState = card.data.scan_state;
      if (scanState === 'pending' || scanState === 'scanning') {
        return; // Don't allow selection during scanning
      }
    }
    
    // Update selected card in WorkflowService
    this.workflowService.setSelectedCard({ type: card.type, id: card.id });

    // Load workflow context for the selected card
    this.workflowService.setContextByCard({ type: card.type, id: card.id })
      .subscribe({
        next: () => {
          // Context loaded and set as active
        },
        error: (err: any) => {
          this.logger.error('Failed to load context for card:', err);
        }
      });
  }

  /** Eyebrow text per backend card_state (#839); null → legacy wording. */
  private static readonly CARD_STATE_EYEBROWS: Record<string, string> = {
    queued: 'Queued',
    copying: 'Copying',
    awaiting_label: 'Needs labeling',
    ready_to_process: 'Ready to process',
    postprocessing: 'Post-processing',
    needs_destination: 'Ready to transfer',
    ready_to_transfer: 'Ready to transfer',
    transferring: 'Transferring',
    verifying: 'Verifying transfer',
    ready_to_finish: 'Ready to finish',
    completed: 'Finished',
    failed_copy: 'Copy failed',
    failed_post: 'Processing failed',
    failed_transfer: 'Transfer failed',
    failed: 'Failed disc',
  };

  /** The visual family for a job card (#839). Falls back to legacy signals
   * (job_status failed) when the backend payload predates card_state. */
  cardFamily(disc: DiscMetadata): 'your_turn' | 'working' | 'done' | 'fix' | null {
    if (disc.card_family) return disc.card_family;
    if (disc.job_status === 'failed') return 'fix';
    return null;
  }

  cardEyebrow(disc: DiscMetadata): string {
    const mapped = disc.card_state
      ? CardCarouselComponent.CARD_STATE_EYEBROWS[disc.card_state]
      : null;
    if (mapped) return mapped;
    return disc.job_status === 'failed' ? 'Failed disc' : 'Unfinished disc';
  }

  /** Footer pill text: the backend's verb/stage, else legacy Unfinished/Failed. */
  cardPill(disc: DiscMetadata): string {
    if (disc.card_pill) {
      const p = disc.card_progress;
      const showPct = (disc.card_state === 'copying' || disc.card_state === 'transferring')
        && typeof p === 'number' && p > 0 && p < 100;
      return showPct ? `${disc.card_pill} ${p}%` : disc.card_pill;
    }
    return disc.job_status === 'failed' ? 'Failed' : 'Unfinished';
  }

  /** ui-pill tone by family. */
  cardPillTone(disc: DiscMetadata): 'amber' | 'slate' | 'emerald' | 'red' {
    switch (this.cardFamily(disc)) {
      case 'your_turn': return 'amber';
      case 'working': return 'slate';
      case 'done': return 'emerald';
      case 'fix': return 'red';
      default: return disc.job_status === 'failed' ? 'red' : 'amber';
    }
  }

  /** Arrow suffix on actionable pills — the pill is a verb, clicking does it. */
  cardPillActionable(disc: DiscMetadata): boolean {
    const f = this.cardFamily(disc);
    return f === 'your_turn' || f === 'fix';
  }

  /** 0–100 for the thin card progress bar; null hides it. */
  cardProgress(disc: DiscMetadata): number | null {
    const f = this.cardFamily(disc);
    if (f !== 'working' && f !== 'fix') return null;
    const p = disc.card_progress;
    return typeof p === 'number' && p >= 0 ? Math.min(100, p) : null;
  }

  /** Card title: movie/series name (movie_name then info_title only, no release_name). Disc number is shown in meta. */
  getDiscTitle(disc: DiscMetadata): string {
    const name = disc.movie_name || disc.info_title || '';

    if (disc.disc_state === 'in_drive') {
      if (name) {
        return name;
      }
      // A failed scan has no identity to show. "Insert Disc" would be a lie
      // (the drive is loaded, it just would not answer) and "Drive 0" hides
      // the problem entirely — name the fault instead (#724).
      if (disc.scan_state === 'failed') {
        return 'Drive Error';
      }
      if (!disc.disc_hash) {
        return 'Insert Disc';
      }
      return `Drive ${disc.disc_num || '?'}`;
    }
    if (name) {
      return name;
    }
    return 'Unknown Disc';
  }

  /**
   * User-facing drive/scan error for a card, or null when healthy.
   * Rendered in place of the meta line so the message (and its remedy, e.g.
   * "Try power cycling the drive") is visible without hovering (#724).
   */
  getDiscErrorMessage(disc: DiscMetadata): string | null {
    if (disc.scan_state !== 'failed') {
      return null;
    }
    return disc.scan_error || 'Disc scan failed';
  }

  /** Meta line: (year) · release name · format · Disc N. Format is disc
   * format only (Blu-Ray, UHD, or DVD). Disc number when set.
   *
   * The release name (a season for series — "Season Two"; an edition for
   * movies — "Director's Cut") is what tells four Rebels cards apart; it
   * was dropped from the card *title* in 069d580 and lives here instead
   * (#833). Omitted when it merely repeats the show/movie name. */
  getDiscMeta(disc: DiscMetadata): string {
    const parts: string[] = [];
    const year = disc.production_year || disc.release_year;
    if (year) {
      parts.push(`(${year})`);
    }
    const releaseName = this.releaseNameForCard(disc);
    if (releaseName) {
      parts.push(releaseName);
    }
    // Only set when the release spans multiple seasons (#846) — tells the
    // four discs of a "Season 1-5" box apart at a glance. The within-season
    // position joins it ("S5 Disc 4") so the chip matches the number the
    // disc NAME counts by, while the release-wide disc_number keeps the
    // boxset position at the end ("Disc 14").
    if (disc.disc_season != null) {
      parts.push(disc.disc_season_ordinal != null
        ? `S${disc.disc_season} Disc ${disc.disc_season_ordinal}`
        : `S${disc.disc_season}`);
    }
    if (disc.disc_format) {
      parts.push(disc.disc_format);
    }
    // Skip the boxset position when it's the same number the season chip
    // already shows (season 1 discs, where the counts coincide).
    if (disc.disc_number != null &&
        !(disc.disc_season != null && disc.disc_season_ordinal === disc.disc_number)) {
      parts.push(`Disc ${disc.disc_number}`);
    }
    return parts.length > 0 ? parts.join(' · ') : '—';
  }

  /**
   * The release name as the card should show it (#837): without a leading
   * repeat of the show/movie name. The card title already says
   * "Star Wars: The Clone Wars", so its release "Star Wars: The Clone Wars -
   * Season 1-5 Collector's Edition" reads "Season 1-5 Collector's Edition"
   * and "Star Wars Rebels: Complete Season Two" reads "Complete Season Two".
   * Empty when the release name is missing or is nothing but the show name.
   */
  releaseNameForCard(disc: DiscMetadata): string {
    const releaseName = (disc.release_name || '').toString().trim();
    if (!releaseName) return '';
    const showName = (disc.movie_name || disc.info_title || '').toString().trim();
    if (!showName) return releaseName;
    const lower = releaseName.toLowerCase();
    const showLower = showName.toLowerCase();
    if (lower === showLower) return '';
    if (lower.startsWith(showLower)) {
      // Strip the show name plus whatever separator follows it (": ", " - ", " – ", " — ").
      const rest = releaseName.slice(showName.length).replace(/^\s*[:\-–—]?\s*/, '').trim();
      return rest || '';
    }
    return releaseName;
  }

  getDriveMount(disc: DiscMetadata): string {
    return disc.mount_point || '';
  }

  getJobStage(disc: DiscMetadata): string {
    return 'Unfinished';
  }

  /** Formatted creation date for job card footer (template: text-xs text-white/40). */
  getJobCreatedDate(disc: DiscMetadata): string {
    if (!disc.created_at) return '';
    return new Date(disc.created_at).toLocaleDateString();
  }

  /**
   * Get rip progress (0–100) for a drive card, or -1 if not ripping.
   * Only returns > 0 when the drive has an active job with rip in progress.
   */
  getDriveRipProgress(card: CardType, driveRipProgress: Map<string, number> | null): number {
    if (card.type !== 'drive' || !card.data.mount_point || !card.data.job_id) return -1;
    if (!driveRipProgress) return -1;
    const progress = driveRipProgress.get(card.data.mount_point);
    if (progress == null || progress <= 0) return -1;
    return progress;
  }

  /**
   * SVG stroke-dashoffset for a determinate progress ring.
   * Ring: radius 10, circumference ~62.83. Full offset = hidden, 0 = full circle.
   */
  getRipRingOffset(progress: number): number {
    const circumference = 2 * Math.PI * 10; // ~62.83
    const clamped = Math.max(0, Math.min(100, progress));
    return circumference * (1 - clamped / 100);
  }

  /** Toggle tooltip visibility (mobile tap). */
  toggleScanTooltip(): void {
    this.showScanTooltip = !this.showScanTooltip;
  }

  /** Close scan tooltip when clicking outside the pill/tooltip host. */
  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent): void {
    const target = event.target as HTMLElement;
    if (target?.closest?.('.disc-card-scan-tooltip-host')) return;
    this.showScanTooltip = false;
  }
}