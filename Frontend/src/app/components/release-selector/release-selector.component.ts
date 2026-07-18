// src/app/components/release-selector/release-selector.component.ts
import { Component, Input, Output, EventEmitter, OnInit, OnDestroy, ChangeDetectionStrategy, ChangeDetectorRef, HostListener, ElementRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subject, takeUntil, debounceTime, switchMap, of } from 'rxjs';
import { ReleaseSummary, MetadataService } from '../../services/metadata.service';
import { MobileService } from '../../services/mobile.service';
import { MobileDrawerComponent } from '../mobile-drawer/mobile-drawer.component';
import { LoggerService } from '../../services/logger.service';
import { ToastService, formatHttpErrorDetail } from '../../services/toast.service';
import {
  EditionMetadataFormComponent,
  EditionFormValue,
} from '../edition-metadata-form/edition-metadata-form.component';

@Component({
  selector: 'app-release-selector',
  standalone: true,
  imports: [CommonModule, FormsModule, MobileDrawerComponent, EditionMetadataFormComponent],
  templateUrl: './release-selector.component.html',
  styleUrls: ['./release-selector.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReleaseSelectorComponent implements OnInit, OnDestroy {
  @Input() releaseOptions: ReleaseSummary[] = [];
  /** DiscDB proposal: show in list with needs-info until linked */
  @Input() pendingRelease: ReleaseSummary | null = null;
  @Input() selectedReleaseId: string | null = null;
  /** From labelForm: overrides list row for selected card (list can be stale after PATCH). */
  @Input() labelFormReleaseName: string | null = null;
  @Input() labelFormCoverFrontUrl: string | null = null;
  @Input() searchTerm: string = '';
  @Input() movieId: string | null = null;
  @Input() loading: boolean = false;
  @Input() error: string | null = null;

  @Output() releaseSelected = new EventEmitter<ReleaseSummary>();
  @Output() releaseCleared = new EventEmitter<void>();
  @Output() releaseCreated = new EventEmitter<any>();
  /** After optional row edit (link-ready release) PATCH succeeds. */
  @Output() releaseMetadataPatched = new EventEmitter<ReleaseSummary>();
  @Output() releaseDeleted = new EventEmitter<string>();

  /** Panel/drawer open state (template: single panel or drawer with list or create form). */
  isOpen = false;
  /** When true, show create form inside panel/drawer instead of list (template: toggle inside same panel). */
  showCreateForm = false;
  /** Editing an incomplete (pending) release before linking */
  showPendingEdit = false;
  /** complete-to-link: must fix fields then select; metadata-only: edit already link-ready row */
  pendingEditPurpose: 'complete-to-link' | 'metadata-only' | null = null;
  pendingEditRelease: ReleaseSummary | null = null;
  pendingEditErrors: string[] = [];
  pendingEditSaving = false;
  /** Bump when opening create or pending form so shared form reapplies prefill. */
  editionFormResetVersion = 0;
  /** Stable reference for create flow (avoid re-applying prefill every CD cycle). */
  readonly emptyEditionPrefill: Partial<EditionFormValue> = {};
  /**
   * Stable prefill for complete-metadata; must NOT be a getter returning a new object each CD
   * (that would re-trigger child ngOnChanges and wipe user input).
   */
  pendingReleaseEditionPrefill: Partial<EditionFormValue> = {};
  isMobile = false;
  
  /** Search-as-you-type state */
  localSearchTerm = '';
  searchResults: ReleaseSummary[] | null = null;
  searching = false;
  private _searchInput$ = new Subject<string>();

  private destroy$ = new Subject<void>();

  private setPendingReleasePrefillFrom(r: ReleaseSummary): void {
    // #633: DiscDB-auto-created release stubs land with ``r.name = null``
    // (only ``movie.name`` gets populated). Falling back through the
    // labelForm and the summary's movie-derived name keeps the "Save & link"
    // save flow from submitting an empty release_name (which the backend used
    // to interpret as "blank the field", stranding the Library at "(untitled)").
    const summaryMovieName =
      (r as any).movie?.name ??
      (r as any).movie_name ??
      null;
    const name =
      (r.name || '').trim() ||
      (this.labelFormReleaseName || '').trim() ||
      (summaryMovieName || '').trim() ||
      (r.slug || '').trim();
    this.pendingReleaseEditionPrefill = {
      name,
      year: r.release_year ?? null,
      upc: r.upc || '',
      asin: r.asin || '',
      cover_front_url: r.cover_front_url || '',
      cover_back_url: r.cover_back_url || '',
    };
  }

  get selectedRelease(): ReleaseSummary | null {
    if (!this.selectedReleaseId) return null;
    const found = this.releaseOptions.find(r => r.id === this.selectedReleaseId) ?? null;
    if (!found) return null;
    const nameFromForm = (this.labelFormReleaseName ?? '').trim();
    const coverFromForm = (this.labelFormCoverFrontUrl ?? '').trim();
    if (!nameFromForm && !coverFromForm) return found;
    return {
      ...found,
      ...(nameFromForm ? { name: nameFromForm } : {}),
      ...(coverFromForm ? { cover_front_url: coverFromForm } : {}),
    };
  }

  trackByReleaseId(_index: number, release: ReleaseSummary): string {
    return (release as { id?: string })?.id ?? release.slug ?? '';
  }

  constructor(
    private metadataSvc: MetadataService,
    private cdr: ChangeDetectorRef,
    private mobileService: MobileService,
    private elementRef: ElementRef,
    private logger: LoggerService,
    private toastSvc: ToastService
  ) {}

  ngOnInit(): void {
    this.mobileService.isMobile$.pipe(takeUntil(this.destroy$)).subscribe(isMobile => {
      this.isMobile = isMobile;
      this.cdr.markForCheck();
    });

    if (this.releaseOptions.length === 0 && this.movieId) {
      this.metadataSvc.listReleases({ movie_id: this.movieId }).pipe(takeUntil(this.destroy$)).subscribe({
        next: (releases) => {
          this.releaseOptions = (releases || []).filter(r =>
            !r.boxset_id && r.slug !== 'pending' && !r.slug?.startsWith('pending-')
          );
          this.cdr.markForCheck();
        },
        error: (err) => {
          this.logger.error('[ReleaseSelector] Failed to load releases:', err);
        },
      });
    }
    
    // Backend search-as-you-type: debounce 300ms, search after 3+ chars
    this._searchInput$.pipe(
      debounceTime(300),
      switchMap(term => {
        if (!term || term.length < 3) {
          return of(null as ReleaseSummary[] | null);
        }
        this.searching = true;
        this.cdr.markForCheck();
        return this.metadataSvc.searchReleasesBackend(term, this.movieId || undefined, 20);
      }),
      takeUntil(this.destroy$),
    ).subscribe(results => {
      this.searchResults = results;
      this.searching = false;
      this.cdr.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  openPanel(): void {
    this.isOpen = true;
    this.showCreateForm = false;
    this.showPendingEdit = false;
    this.pendingEditPurpose = null;
    this.pendingEditRelease = null;
    this.pendingReleaseEditionPrefill = {};
    this.pendingEditErrors = [];
    this.cdr.markForCheck();
  }

  /** Switch to create form inside panel/drawer. Ensures OnPush runs so the form is visible immediately. */
  openCreateForm(): void {
    this.showCreateForm = true;
    this.showPendingEdit = false;
    this.pendingEditPurpose = null;
    this.pendingEditRelease = null;
    this.pendingReleaseEditionPrefill = {};
    this.editionFormResetVersion++;
    this.cdr.markForCheck();
  }

  /** Called when user types in the search input */
  onSearchInput(term: string): void {
    this.localSearchTerm = term;
    this._searchInput$.next(term);
  }

  /** Releases to display: search results if searching, otherwise pre-loaded options */
  get displayReleases(): ReleaseSummary[] {
    const base = this.searchResults !== null ? this.searchResults : this.releaseOptions;
    if (this.searchResults !== null || !this.pendingRelease?.id) {
      return base;
    }
    if (base.some(r => r.id === this.pendingRelease?.id)) {
      return base;
    }
    return [this.pendingRelease, ...base];
  }

  isNeedsInfo(release: ReleaseSummary): boolean {
    if (release.release_link_ready === true) return false;
    if (release.release_missing_required_fields && release.release_missing_required_fields.length > 0) {
      return true;
    }
    return release.release_link_ready === false;
  }

  /**
   * Compact detail line for a release option — production year, resolution, and slug,
   * omitting any that are blank. Surfaces the resolution so users can tell releases
   * apart in the picker (e.g. a 2160p 4K release vs a 1080p Blu-ray of the same title),
   * which otherwise only differed by slug.
   */
  releaseMetaLine(release: ReleaseSummary): string {
    return [
      release?.production_year != null ? String(release.production_year) : '',
      release?.resolution ?? '',
      release?.slug ?? '',
    ]
      .map((v) => (v ?? '').toString().trim())
      .filter((v) => v !== '')
      .join(' • ');
  }

  closePanel(): void {
    this.isOpen = false;
    this.localSearchTerm = '';
    this.searchResults = null;
    this.showCreateForm = false;
    this.showPendingEdit = false;
    this.pendingEditPurpose = null;
    this.pendingEditRelease = null;
    this.pendingReleaseEditionPrefill = {};
    this.pendingEditErrors = [];
    this.cdr.markForCheck();
  }

  selectRelease(release: ReleaseSummary): void {
    const incomplete =
      release.release_link_ready === false ||
      (!!release.release_missing_required_fields && release.release_missing_required_fields.length > 0);
    if (incomplete && release.id) {
      this.pendingEditPurpose = 'complete-to-link';
      this.pendingEditRelease = release;
      this.setPendingReleasePrefillFrom(release);
      this.showPendingEdit = true;
      this.showCreateForm = false;
      this.pendingEditErrors = [];
      this.editionFormResetVersion++;
      this.cdr.markForCheck();
      return;
    }
    this.releaseSelected.emit(release);
    this.isOpen = false;
    this.showCreateForm = false;
    this.showPendingEdit = false;
    this.cdr.markForCheck();
  }

  cancelPendingEdit(): void {
    this.showPendingEdit = false;
    this.pendingEditPurpose = null;
    this.pendingEditRelease = null;
    this.pendingReleaseEditionPrefill = {};
    this.pendingEditErrors = [];
    this.cdr.markForCheck();
  }

  /** Pencil on row: incomplete → same as select; link-ready → edit without selecting. */
  openReleaseRowEdit(release: ReleaseSummary, event: Event): void {
    event.stopPropagation();
    const incomplete =
      release.release_link_ready === false ||
      (!!release.release_missing_required_fields && release.release_missing_required_fields.length > 0);
    if (incomplete && release.id) {
      this.pendingEditPurpose = 'complete-to-link';
      this.pendingEditRelease = release;
      this.setPendingReleasePrefillFrom(release);
      this.showPendingEdit = true;
      this.showCreateForm = false;
      this.pendingEditErrors = [];
      this.editionFormResetVersion++;
      this.cdr.markForCheck();
      return;
    }
    if (!release.id) return;
    this.pendingEditPurpose = 'metadata-only';
    this.pendingEditRelease = release;
    this.setPendingReleasePrefillFrom(release);
    this.showPendingEdit = true;
    this.showCreateForm = false;
    this.pendingEditErrors = [];
    this.editionFormResetVersion++;
    this.cdr.markForCheck();
  }

  confirmDeleteRelease(release: ReleaseSummary, event: Event): void {
    event.stopPropagation();
    const idOrSlug = release.slug || release.id;
    if (!idOrSlug) return;
    const idForParentEmit = release.id ?? undefined;
    if (!confirm('Delete this release? This may affect linked discs and metadata.')) return;
    this.metadataSvc
      .deleteRelease(idOrSlug)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: () => {
          this.toastSvc.show('Release deleted', 'success', 2500);
          if (idForParentEmit) {
            this.releaseDeleted.emit(idForParentEmit);
          }
          this.closePanel();
        },
        error: (err) => {
          this.logger.error('[ReleaseSelector] delete failed', err);
          this.toastSvc.show(formatHttpErrorDetail(err), 'error', 5000);
          this.cdr.markForCheck();
        },
      });
  }

  onReleaseEditionPending(v: EditionFormValue): void {
    const rel = this.pendingEditRelease;
    if (!rel?.id) return;
    this.pendingEditErrors = [];
    this.pendingEditSaving = true;
    this.cdr.markForCheck();
    const backRaw = v.cover_back_url?.trim();
    const payload: Record<string, string | number | null | undefined> = {
      release_name: v.name.trim(),
      release_year: v.year ?? undefined,
      upc: v.upc?.trim(),
      cover_front_url: v.cover_front_url?.trim(),
    };
    const asinTrim = v.asin?.trim();
    if (asinTrim) payload['asin'] = asinTrim;
    if (backRaw) payload['cover_back_url'] = backRaw;
    this.metadataSvc
      .updateRelease(rel.id, payload)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (updated) => {
          this.pendingEditSaving = false;
          this.pendingEditRelease = updated;
          if (this.pendingEditPurpose === 'metadata-only') {
            this.releaseMetadataPatched.emit(updated);
            this.cancelPendingEdit();
            this.cdr.markForCheck();
            return;
          }
          if (updated.release_link_ready) {
            this.releaseSelected.emit(updated);
            this.closePanel();
          } else {
            this.setPendingReleasePrefillFrom(updated);
            this.editionFormResetVersion++;
            this.pendingEditErrors = updated.release_missing_required_fields?.length
              ? [`Still missing: ${updated.release_missing_required_fields.join(', ')}`]
              : ['Release is still incomplete (check boxset metadata if this disc is in a boxset)'];
            this.cdr.markForCheck();
          }
        },
        error: (err) => {
          this.pendingEditSaving = false;
          const d = err?.error?.detail;
          if (d?.missing?.length) {
            this.pendingEditErrors = [`Missing: ${d.missing.join(', ')}`];
          } else {
            this.pendingEditErrors = [typeof d === 'string' ? d : 'Failed to save release'];
          }
          this.logger.error('[ReleaseSelector] pending edit save failed', err);
          this.cdr.markForCheck();
        },
      });
  }

  onReleaseEditionCreate(v: EditionFormValue): void {
    this.releaseCreated.emit({
      name: v.name,
      release_year: v.year,
      upc: v.upc,
      asin: v.asin || undefined,
      cover_front_url: v.cover_front_url,
      cover_back_url: v.cover_back_url || undefined,
    });
    this.showCreateForm = false;
    this.isOpen = false;
    this.cdr.markForCheck();
  }

  cancelCreateForm(): void {
    this.showCreateForm = false;
    this.cdr.markForCheck();
  }

  onReleaseCleared(): void {
    this.releaseCleared.emit();
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: Event): void {
    const target = event.target as HTMLElement;
    const isClickInside = this.elementRef.nativeElement.contains(target);
    const isClickInDrawer =
      target?.closest('.mobile-drawer-container') != null ||
      target?.closest('.mobile-drawer-wrapper') != null ||
      target?.closest('.cdk-overlay-pane.mobile-drawer-overlay-panel') != null ||
      target?.closest('.drawer-content') != null;
    if (isClickInDrawer) return;
    if (!isClickInside && this.isOpen && !this.isMobile) {
      this.closePanel();
    }
  }

  // Expose for tests (template used _validateReleaseYear for optional validation)
  _validateReleaseYear(year: number | null): boolean {
    if (year === null || year === undefined) return true;
    return Number.isInteger(year) && year >= 1000 && year <= 9999;
  }
}
