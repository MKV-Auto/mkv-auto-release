// src/app/components/combobox/combobox.component.ts
import { Component, Input, Output, EventEmitter, OnInit, OnDestroy, OnChanges, SimpleChanges, ChangeDetectionStrategy, ChangeDetectorRef, TemplateRef, HostListener, ElementRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subject, takeUntil } from 'rxjs';
import { MobileService } from '../../services/mobile.service';
import { MobileDrawerComponent } from '../mobile-drawer/mobile-drawer.component';

export interface ComboboxItem {
  id: string | null;
  name?: string;
  title?: string;
  slug?: string;
  cover_url?: string | null;
  cover_path?: string | null;
  cover_front_url?: string | null;
  [key: string]: any; // Allow additional properties
}

export type AddMode = 'tmdb-url' | 'modal';

@Component({
  selector: 'app-combobox',
  standalone: true,
  imports: [CommonModule, FormsModule, MobileDrawerComponent],
  templateUrl: './combobox.component.html',
  styleUrls: ['./combobox.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ComboboxComponent implements OnInit, OnDestroy, OnChanges {
  @Input() label: string = '';
  @Input() items: ComboboxItem[] = [];
  @Input() selectedItemId: string | null = null;
  @Input() placeholder: string = 'Select...';
  @Input() searchPlaceholder: string = 'Search...';
  @Input() addMode: AddMode = 'modal';
  /** When true, show a "Create New" button at top of panel/drawer (template: release/boxset selectors). */
  @Input() showCreateNewAtTop: boolean = false;
  /** Label for the Create New button when showCreateNewAtTop is true (e.g. "Create New Release"). */
  @Input() createNewLabel: string = 'Create New';
  @Input() loading: boolean = false;
  @Input() error: string | null = null;
  
  // For TMDB URL mode
  @Input() tmdbUrl: string = '';
  @Input() tmdbUrlPlaceholder: string = 'https://www.themoviedb.org/movie/414906';
  
  // Custom templates for display
  @Input() triggerTemplate?: TemplateRef<any>;
  @Input() optionTemplate?: TemplateRef<any>;
  
  // Functions for custom display
  @Input() getItemDisplayName?: (item: ComboboxItem) => string;
  @Input() getItemMetadata?: (item: ComboboxItem) => string;
  @Input() getItemCover?: (item: ComboboxItem) => string | null;
  @Input() filterItems?: (items: ComboboxItem[], search: string) => ComboboxItem[];
  
  @Output() itemSelected = new EventEmitter<ComboboxItem>();
  @Output() itemCleared = new EventEmitter<void>();
  @Output() addClicked = new EventEmitter<void>();
  @Output() tmdbUrlLookup = new EventEmitter<string>();
  @Output() searchChanged = new EventEmitter<string>();
  @Output() panelOpened = new EventEmitter<void>();

  isOpen = false;
  searchTerm = '';
  private destroy$ = new Subject<void>();
  private _filteredCache: ComboboxItem[] | null = null;
  private _searchCache: string = '';
  
  // Internal tmdbUrl state (synced with @Input for two-way binding)
  internalTmdbUrl: string = '';
  showTmdbInput = false;
  tmdbUrlInvalid: boolean = false;
  
  // Mobile state
  isMobile: boolean = false;

  constructor(
    private cdr: ChangeDetectorRef,
    private elementRef: ElementRef,
    private mobileService: MobileService
  ) {}

  ngOnInit(): void {
    // Sync internal tmdbUrl when input changes
    this.internalTmdbUrl = this.tmdbUrl || '';
    
    // Subscribe to mobile service
    this.mobileService.isMobile$.pipe(
      takeUntil(this.destroy$)
    ).subscribe(isMobile => {
      this.isMobile = isMobile;
      this.cdr.markForCheck();
    });
  }

  ngOnChanges(changes: SimpleChanges): void {
    // Close TMDB dropdown when selectedItemId changes (movie was selected/updated)
    if (changes['selectedItemId'] && changes['selectedItemId'].currentValue !== changes['selectedItemId'].previousValue) {
      if (this.addMode === 'tmdb-url' && this.showTmdbInput) {
        this.showTmdbInput = false;
      }
      // Trigger change detection to update selectedItem display
      this.cdr.markForCheck();
    }
    // Also trigger change detection when items change (in case selectedItemId matches a new item)
    if (changes['items']) {
      this._filteredCache = null; // Clear cache when items change
      this.cdr.markForCheck();
    }
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  get selectedItem(): ComboboxItem | null {
    if (!this.selectedItemId) return null;
    return this.items.find(item => item.id === this.selectedItemId) || null;
  }

  get filteredItems(): ComboboxItem[] {
    if (this._filteredCache && this._searchCache === this.searchTerm) {
      return this._filteredCache;
    }

    if (this.items.length === 0) {
      this._filteredCache = [];
      this._searchCache = this.searchTerm;
      return this._filteredCache;
    }

    if (this.filterItems) {
      this._filteredCache = this.filterItems(this.items, this.searchTerm);
    } else {
      // Default filtering
      if (!this.searchTerm) {
        this._filteredCache = this.items.slice(0, 50);
      } else {
        const search = this.searchTerm.toLowerCase();
        this._filteredCache = this.items.filter(item => {
          const name = this.getItemDisplayName?.(item) || item.name || item.title || item.slug || '';
          return name.toLowerCase().includes(search);
        }).slice(0, 50);
      }
    }
    
    this._searchCache = this.searchTerm;
    return this._filteredCache;
  }

  getDisplayName(item: ComboboxItem | null): string {
    if (!item) return this.placeholder;
    return this.getItemDisplayName?.(item) || item.name || item.title || item.slug || this.placeholder;
  }

  getMetadata(item: ComboboxItem | null): string {
    if (!item) return '';
    return this.getItemMetadata?.(item) || '';
  }

  getCover(item: ComboboxItem | null): string | null {
    if (!item) return null;
    return this.getItemCover?.(item) || item.cover_url || item.cover_path || item.cover_front_url || null;
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: Event): void {
    const target = event.target as HTMLElement;
    
    // Check if click is inside the combobox component
    const isClickInside = this.elementRef.nativeElement.contains(target);
    
    // Also check if click is inside the mobile drawer (which is in CDK overlay, outside component DOM)
    const closestContainer = target?.closest('.mobile-drawer-container');
    const closestWrapper = target?.closest('.mobile-drawer-wrapper');
    const closestPane = target?.closest('.cdk-overlay-pane.mobile-drawer-overlay-panel');
    const closestDrawerContent = target?.closest('.drawer-content');
    const closestOverlay = target?.closest('.cdk-overlay-container');
    const hasContainerClass = target?.classList?.contains('mobile-drawer-container');
    const hasWrapperClass = target?.classList?.contains('mobile-drawer-wrapper');
    const hasDrawerContentClass = target?.classList?.contains('drawer-content');
    
    const isClickInDrawer = closestContainer !== null ||
                            closestWrapper !== null ||
                            closestPane !== null ||
                            closestDrawerContent !== null ||
                            hasContainerClass ||
                            hasWrapperClass ||
                            hasDrawerContentClass;
    
    // If click is in drawer, stop propagation to prevent other handlers from closing it
    if (isClickInDrawer) {
      event.stopPropagation();
      event.stopImmediatePropagation();
      return; // Don't close combobox if clicking in drawer
    }
    
    if (!isClickInside) {
      // Click outside - close both dropdowns
      if (this.isOpen) {
        this.isOpen = false;
        this.cdr.markForCheck();
      }
      if (this.showTmdbInput) {
        this.showTmdbInput = false;
        this.cdr.markForCheck();
      }
    }
  }

  onToggle(): void {
    const wasOpen = this.isOpen;
    this.isOpen = !this.isOpen;
    // Close TMDB dropdown when opening selection dropdown
    if (this.isOpen && !wasOpen) {
      this.showTmdbInput = false;
      // Emit event that panel opened
      this.panelOpened.emit();
    }
    this.cdr.markForCheck();
  }

  onClose(): void {
    this.isOpen = false;
    // Don't close TMDB dropdown when combobox closes - they're independent
    this.cdr.markForCheck();
  }

  onSearchChange(search: string): void {
    this.searchTerm = search || '';
    this._filteredCache = null;
    this.searchChanged.emit(this.searchTerm);
    this.cdr.markForCheck();
  }

  onSelectItem(item: ComboboxItem): void {
    this.itemSelected.emit(item);
    this.isOpen = false;
    // Close TMDB dropdown when item is selected
    this.showTmdbInput = false;
    this.cdr.markForCheck();
  }

  onClear(): void {
    this.itemCleared.emit();
    this.isOpen = false;
    this.showTmdbInput = false;
    this.cdr.markForCheck();
  }

  onAddClick(): void {
    if (this.addMode === 'tmdb-url') {
      const wasOpen = this.showTmdbInput;
      this.showTmdbInput = !this.showTmdbInput;
      // Close selection dropdown when opening TMDB dropdown
      if (this.showTmdbInput && !wasOpen) {
        this.isOpen = false;
      }
    } else {
      // Close selection dropdown when add button is clicked (for modal mode)
      // This ensures creation dropdowns (boxset/release) can open without selection dropdown being open
      if (this.isOpen) {
        this.isOpen = false;
      }
      this.addClicked.emit();
    }
    this.cdr.markForCheck();
  }


  _validateTMDBURL(url: string): boolean {
    if (!url || !url.trim()) return false;
    const trimmed = url.trim();
    return trimmed.startsWith('https://www.themoviedb.org/') || trimmed.startsWith('https://themoviedb.org/');
  }

  validateTmdbUrl(): void {
    this.tmdbUrlInvalid = !this._validateTMDBURL(this.internalTmdbUrl);
    this.cdr.markForCheck();
  }

  onTmdbLookup(): void {
    const trimmed = this.internalTmdbUrl.trim();
    if (trimmed) {
      // Validate before emitting
      if (!this._validateTMDBURL(trimmed)) {
        this.tmdbUrlInvalid = true;
        this.cdr.markForCheck();
        return;
      }
      this.tmdbUrlInvalid = false;
      this.tmdbUrlLookup.emit(trimmed);
    }
  }

  onTmdbUrlKeyup(event: KeyboardEvent): void {
    this.validateTmdbUrl();
    if (event.key === 'Enter') {
      this.onTmdbLookup();
    }
  }

  onTmdbUrlBlur(): void {
    this.validateTmdbUrl();
  }
}
