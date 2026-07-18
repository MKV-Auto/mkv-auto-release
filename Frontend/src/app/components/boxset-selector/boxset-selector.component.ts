// src/app/components/boxset-selector/boxset-selector.component.ts
import { Component, Input, Output, EventEmitter, OnInit, OnDestroy, ChangeDetectionStrategy, ChangeDetectorRef, HostListener, ElementRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subject, takeUntil, debounceTime, switchMap, of } from 'rxjs';
import { BoxsetSummary, MetadataService } from '../../services/metadata.service';
import { LoggerService } from '../../services/logger.service';
import { ToastService, formatHttpErrorDetail } from '../../services/toast.service';
import { MobileService } from '../../services/mobile.service';
import { MobileDrawerComponent } from '../mobile-drawer/mobile-drawer.component';
import {
  EditionMetadataFormComponent,
  EditionFormValue,
} from '../edition-metadata-form/edition-metadata-form.component';

@Component({
  selector: 'app-boxset-selector',
  standalone: true,
  imports: [CommonModule, FormsModule, MobileDrawerComponent, EditionMetadataFormComponent],
  templateUrl: './boxset-selector.component.html',
  styleUrls: ['./boxset-selector.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BoxsetSelectorComponent implements OnInit, OnDestroy {
  private _boxsetOptions: BoxsetSummary[] = [];

  @Input() set boxsetOptions(value: BoxsetSummary[]) {
    this._boxsetOptions = value || [];
    this.cdr.markForCheck();
  }

  get boxsetOptions(): BoxsetSummary[] {
    return this._boxsetOptions;
  }
  @Input() selectedBoxsetId: string | null = null;
  @Input() loading: boolean = false;

  @Output() boxsetSelected = new EventEmitter<BoxsetSummary>();
  @Output() boxsetToggled = new EventEmitter<boolean>();
  @Output() boxsetCleared = new EventEmitter<void>();
  @Output() boxsetCreated = new EventEmitter<BoxsetSummary>();
  @Output() boxsetUpdated = new EventEmitter<BoxsetSummary>();
  /** Optional row edit (link-ready boxset): PATCH only, no re-selection. */
  @Output() boxsetMetadataPatched = new EventEmitter<BoxsetSummary>();
  @Output() boxsetDeleted = new EventEmitter<string>();

  private destroy$ = new Subject<void>();

  /** Selected boxset when selectedBoxsetId matches an option (exclude __pending__). Template: selected card. */
  get selectedBoxset(): BoxsetSummary | null {
    if (!this.selectedBoxsetId || this.selectedBoxsetId === '__pending__') return null;
    return this.boxsetOptions.find(b => b.id === this.selectedBoxsetId) ?? null;
  }

  trackByBoxsetId(_index: number, boxset: BoxsetSummary): string {
    return boxset.id ?? boxset.slug ?? '';
  }

  /** Panel/drawer open state (same pattern as release selector). */
  isOpen = false;
  /** When true, show create form inside panel/drawer instead of list. */
  showCreateForm = false;
  /** Incomplete boxset: same edition form as create, PATCH on submit. */
  showPendingEdit = false;
  pendingEditPurpose: 'complete-to-link' | 'metadata-only' | null = null;
  pendingEditBoxset: BoxsetSummary | null = null;
  pendingEditErrors: string[] = [];
  pendingEditSaving = false;
  editionFormResetVersion = 0;
  readonly emptyEditionPrefill: Partial<EditionFormValue> = {};
  /** Stable prefill for complete-metadata (not a per-CD new object). */
  pendingBoxsetEditionPrefill: Partial<EditionFormValue> = {};

  isMobile = false;

  /** Search-as-you-type state */
  searchTerm = '';
  searchResults: BoxsetSummary[] | null = null;
  searching = false;
  private _searchInput$ = new Subject<string>();

  private setPendingBoxsetPrefillFrom(b: BoxsetSummary): void {
    this.pendingBoxsetEditionPrefill = {
      name: (b.name || '').trim(),
      year: b.year ?? null,
      upc: b.upc || '',
      asin: b.asin || '',
      cover_front_url: b.cover_front_url || '',
      cover_back_url: b.cover_back_url || '',
    };
  }

  constructor(
    private metadataSvc: MetadataService,
    private cdr: ChangeDetectorRef,
    private logger: LoggerService,
    private mobileService: MobileService,
    private elementRef: ElementRef,
    private toastSvc: ToastService
  ) {}

  ngOnInit(): void {
    this.isMobile = this.mobileService.isMobile;
    this.mobileService.isMobile$.pipe(
      takeUntil(this.destroy$)
    ).subscribe(isMobile => {
      this.isMobile = isMobile;
      this.cdr.markForCheck();
    });

    if (this.boxsetOptions.length === 0) {
      this.metadataSvc.listBoxsets().pipe(
        takeUntil(this.destroy$)
      ).subscribe({
        next: (boxsets) => {
          this._boxsetOptions = boxsets || [];
          this.cdr.markForCheck();
        },
        error: (err) => {
          this.logger.error('[BoxsetSelector] Failed to load boxsets:', err);
        },
      });
    }

    this._searchInput$.pipe(
      debounceTime(300),
      switchMap(term => {
        if (!term || term.length < 3) {
          return of(null as BoxsetSummary[] | null);
        }
        this.searching = true;
        this.cdr.markForCheck();
        return this.metadataSvc.searchBoxsetsBackend(term, 20);
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
    this.pendingEditBoxset = null;
    this.pendingBoxsetEditionPrefill = {};
    this.pendingEditErrors = [];
    this.cdr.markForCheck();
  }

  openCreateForm(): void {
    this.showCreateForm = true;
    this.showPendingEdit = false;
    this.pendingEditPurpose = null;
    this.pendingEditBoxset = null;
    this.pendingBoxsetEditionPrefill = {};
    this.editionFormResetVersion++;
    this.cdr.markForCheck();
  }

  closePanel(): void {
    this.isOpen = false;
    this.showCreateForm = false;
    this.showPendingEdit = false;
    this.pendingEditPurpose = null;
    this.pendingEditBoxset = null;
    this.pendingBoxsetEditionPrefill = {};
    this.pendingEditErrors = [];
    this.searchTerm = '';
    this.searchResults = null;
    this.cdr.markForCheck();
  }

  onSearchInput(term: string): void {
    this.searchTerm = term;
    this._searchInput$.next(term);
  }

  get displayBoxsets(): BoxsetSummary[] {
    return this.searchResults !== null ? this.searchResults : this.boxsetOptions;
  }

  isNeedsInfo(boxset: BoxsetSummary): boolean {
    if (boxset.boxset_link_ready === true) return false;
    if (boxset.boxset_missing_required_fields && boxset.boxset_missing_required_fields.length > 0) {
      return true;
    }
    return boxset.boxset_link_ready === false;
  }

  selectBoxset(boxset: BoxsetSummary): void {
    const incomplete =
      boxset.boxset_link_ready === false ||
      (!!boxset.boxset_missing_required_fields && boxset.boxset_missing_required_fields.length > 0);
    if (incomplete && boxset.id) {
      this.pendingEditPurpose = 'complete-to-link';
      this.pendingEditBoxset = boxset;
      this.setPendingBoxsetPrefillFrom(boxset);
      this.showPendingEdit = true;
      this.showCreateForm = false;
      this.pendingEditErrors = [];
      this.editionFormResetVersion++;
      this.cdr.markForCheck();
      return;
    }
    this.boxsetSelected.emit(boxset);
    this.closePanel();
  }

  cancelPendingEdit(): void {
    this.showPendingEdit = false;
    this.pendingEditPurpose = null;
    this.pendingEditBoxset = null;
    this.pendingBoxsetEditionPrefill = {};
    this.pendingEditErrors = [];
    this.cdr.markForCheck();
  }

  openBoxsetRowEdit(boxset: BoxsetSummary, event: Event): void {
    event.stopPropagation();
    const incomplete =
      boxset.boxset_link_ready === false ||
      (!!boxset.boxset_missing_required_fields && boxset.boxset_missing_required_fields.length > 0);
    if (incomplete && boxset.id) {
      this.pendingEditPurpose = 'complete-to-link';
      this.pendingEditBoxset = boxset;
      this.setPendingBoxsetPrefillFrom(boxset);
      this.showPendingEdit = true;
      this.showCreateForm = false;
      this.pendingEditErrors = [];
      this.editionFormResetVersion++;
      this.cdr.markForCheck();
      return;
    }
    if (!boxset.id) return;
    this.pendingEditPurpose = 'metadata-only';
    this.pendingEditBoxset = boxset;
    this.setPendingBoxsetPrefillFrom(boxset);
    this.showPendingEdit = true;
    this.showCreateForm = false;
    this.pendingEditErrors = [];
    this.editionFormResetVersion++;
    this.cdr.markForCheck();
  }

  confirmDeleteBoxset(boxset: BoxsetSummary, event: Event): void {
    event.stopPropagation();
    if (!boxset.id) return;
    if (
      !confirm(
        'Delete this boxset? This will also delete all linked releases, discs, titles, and tracks.'
      )
    ) {
      return;
    }
    this.metadataSvc
      .deleteBoxset(boxset.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: () => {
          this.toastSvc.show('Boxset deleted', 'success', 2500);
          this.boxsetDeleted.emit(boxset.id);
          this.closePanel();
        },
        error: (err) => {
          this.logger.error('[BoxsetSelector] delete failed', err);
          this.toastSvc.show(formatHttpErrorDetail(err), 'error', 5000);
          this.cdr.markForCheck();
        },
      });
  }

  onBoxsetEditionPending(v: EditionFormValue): void {
    const b = this.pendingEditBoxset;
    if (!b?.id) return;
    this.pendingEditErrors = [];
    this.pendingEditSaving = true;
    this.cdr.markForCheck();
    const backRaw = v.cover_back_url?.trim();
    const payload: Record<string, string | number | undefined> = {
      name: v.name.trim(),
      year: v.year ?? undefined,
      upc: v.upc?.trim(),
      cover_front_url: v.cover_front_url?.trim(),
    };
    const asinTrim = v.asin?.trim();
    if (asinTrim) payload['asin'] = asinTrim;
    if (backRaw) payload['cover_back_url'] = backRaw;
    this.metadataSvc
      .updateBoxset(b.id, payload)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (updated) => {
          this.pendingEditSaving = false;
          this.pendingEditBoxset = updated;
          if (this.pendingEditPurpose === 'metadata-only') {
            this.boxsetMetadataPatched.emit(updated);
            this.cancelPendingEdit();
            this.cdr.markForCheck();
            return;
          }
          if (updated.boxset_link_ready) {
            this.boxsetUpdated.emit(updated);
            this.boxsetSelected.emit(updated);
            this.closePanel();
          } else {
            this.setPendingBoxsetPrefillFrom(updated);
            this.editionFormResetVersion++;
            this.pendingEditErrors = updated.boxset_missing_required_fields?.length
              ? [`Still missing: ${updated.boxset_missing_required_fields.join(', ')}`]
              : ['Boxset is still incomplete'];
            this.cdr.markForCheck();
          }
        },
        error: (err) => {
          this.pendingEditSaving = false;
          const d = err?.error?.detail;
          if (d?.missing?.length) {
            this.pendingEditErrors = [`Missing: ${d.missing.join(', ')}`];
          } else {
            this.pendingEditErrors = [typeof d === 'string' ? d : 'Failed to save boxset'];
          }
          this.logger.error('[BoxsetSelector] pending edit save failed', err);
          this.cdr.markForCheck();
        },
      });
  }

  onBoxsetEditionCreate(v: EditionFormValue): void {
    // Parent createBoxsetFromData reads name/year/upc/asin/cover_* only.
    this.boxsetCreated.emit({
      name: v.name,
      year: v.year,
      upc: v.upc,
      asin: v.asin || undefined,
      cover_front_url: v.cover_front_url,
      cover_back_url: v.cover_back_url || undefined,
    } as unknown as BoxsetSummary);
    this.showCreateForm = false;
    this.closePanel();
  }

  onBoxsetCleared(): void {
    this.boxsetCleared.emit();
    this.boxsetToggled.emit(false);
    this.closePanel();
  }

  cancelCreateForm(): void {
    this.showCreateForm = false;
    this.cdr.markForCheck();
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: Event): void {
    const target = event.target as HTMLElement;
    const isClickInside = this.elementRef.nativeElement.contains(target);
    const closestContainer = target?.closest('.mobile-drawer-container');
    const closestWrapper = target?.closest('.mobile-drawer-wrapper');
    const closestPane = target?.closest('.cdk-overlay-pane.mobile-drawer-overlay-panel');
    const closestDrawerContent = target?.closest('.drawer-content');
    const isClickInDrawer = closestContainer !== null ||
                            closestWrapper !== null ||
                            closestPane !== null ||
                            closestDrawerContent !== null ||
                            target?.classList?.contains('mobile-drawer-container') ||
                            target?.classList?.contains('mobile-drawer-wrapper') ||
                            target?.classList?.contains('drawer-content');

    if (isClickInDrawer) {
      event.stopPropagation();
      return;
    }
    if (!isClickInside && this.isOpen) {
      this.closePanel();
    }
  }
}
