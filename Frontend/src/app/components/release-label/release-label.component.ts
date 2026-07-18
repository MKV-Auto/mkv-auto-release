import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, ViewEncapsulation } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-release-label',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './release-label.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
})
export class ReleaseLabelComponent {
  @Input() labelForm: any;
  @Input() labelSaving = false;
  @Input() lastAutosaveOk = true;
  @Input() hasLabelContent = false;
  @Input() groupOptions: any[] = [];
  @Input() lastReleaseDetails: any = null;
  @Input() releaseNameHint = '';
  @Input() releaseSlugHint = '';
  @Input() isLinkedToBoxset = false; // Whether the release is linked to a boxset

  groupOpen = false;
  groupSearch = '';

  @Output() labelChanged = new EventEmitter<void>();
  @Output() nameChanged = new EventEmitter<void>();
  @Output() nameBlur = new EventEmitter<void>();
  @Output() slugEdited = new EventEmitter<void>();
  @Output() coverChange = new EventEmitter<void>();
  @Output() fieldBlur = new EventEmitter<void>();
  @Output() clearSelection = new EventEmitter<void>();
  @Output() groupSelected = new EventEmitter<any>();
  @Output() groupDeleted = new EventEmitter<any>();
  @Output() groupSearchChanged = new EventEmitter<string>();
  @Output() groupOpenChanged = new EventEmitter<boolean>();

  private focusDepth = 0;
  isActive = false;

  get showSpinner(): boolean {
    return this.labelSaving || this.isActive;
  }

  private isEmpty(val: any): boolean {
    return val === null || val === undefined || `${val}`.trim() === '';
  }

  get missingReleaseYear(): boolean {
    return this.isEmpty(this.labelForm?.release_year);
  }

  get invalidReleaseYear(): boolean {
    const year = this.labelForm?.release_year;
    if (this.isEmpty(year)) return true;
    const yearNum = parseInt(year, 10);
    return !Number.isInteger(yearNum) || yearNum < 1000 || yearNum > 9999;
  }

  get missingReleaseSlug(): boolean {
    return this.isEmpty(this.labelForm?.release_slug);
  }

  get missingFrontCover(): boolean {
    return this.isEmpty(this.labelForm?.cover_front_url);
  }

  get invalidFrontCover(): boolean {
    const url = this.labelForm?.cover_front_url;
    if (this.isEmpty(url)) return true;
    const trimmed = String(url).trim();
    return !trimmed.startsWith('http://') && !trimmed.startsWith('https://');
  }

  get missingBackCover(): boolean {
    return this.isEmpty(this.labelForm?.cover_back_url);
  }

  get missingUPC(): boolean {
    return this.isEmpty(this.labelForm?.upc);
  }

  /** Invalid if not a valid GTIN (8, 12, 13, or 14 digits). */
  get invalidUPC(): boolean {
    const upc = this.labelForm?.upc;
    if (this.isEmpty(upc)) return true;
    const s = String(upc).trim();
    if (!/^\d+$/.test(s)) return true;
    const len = s.length;
    return len !== 8 && len !== 12 && len !== 13 && len !== 14;
  }

  // Combined validation for invalid class
  get invalidReleaseYearField(): boolean {
    return this.missingReleaseYear || this.invalidReleaseYear;
  }

  get invalidFrontCoverField(): boolean {
    return this.missingFrontCover || this.invalidFrontCover;
  }

  get invalidUPCField(): boolean {
    return this.missingUPC || this.invalidUPC;
  }

  toggleCombobox(): void {
    this.groupOpen = !this.groupOpen;
    this.groupOpenChanged.emit(this.groupOpen);
    if (!this.groupOpen) {
      this.groupSearch = '';
      this.groupSearchChanged.emit(this.groupSearch);
    }
  }

  closeCombobox(): void {
    this.groupOpen = false;
    this.groupOpenChanged.emit(false);
  }

  onSearchChange(val: string): void {
    this.groupSearch = val || '';
    this.groupSearchChanged.emit(this.groupSearch);
  }

  onApplyGroup(group: any): void {
    this.groupSelected.emit(group);
    this.closeCombobox();
  }

  onDeleteGroup(group: any, ev: Event): void {
    ev.stopPropagation();
    this.groupDeleted.emit(group);
  }

  yearLabelForSelection(): string {
    const labelRel = (this.labelForm?.release_year as any) || null;
    const labelOrig = (this.labelForm?.production_year as any) || null;
    const lastRel = this.lastReleaseDetails?.release_year || null;
    const lastOrig = this.lastReleaseDetails?.production_year || null;
    return this.formatYearLabel(null, labelRel || lastRel || labelOrig || lastOrig || null);
  }

  optionYearLabel(group: any): string {
    if (!group) return '—';
    const baseRel = group.release_year || null;
    const baseOrig = group.production_year || null;
    return this.formatYearLabel(null, baseRel || baseOrig);
  }

  coverImageForSelection(): string | null {
    return this.labelForm?.cover_front_url || this.lastReleaseDetails?.cover_front_url || null;
  }

  filteredGroupOptions(): any[] {
    return (this.groupOptions || [])
      .filter(g => {
        return this.matchesGroupOption(g);
      })
      .slice(0, 50);
  }

  private matchesGroupOption(group: any): boolean {
    if (!group || !this.labelForm) return false;
    
    // Filter by movie_id if available
    const formMovieId = this.labelForm.movie_id;
    const groupMovieId = group.movie_id;
    if (formMovieId && groupMovieId && formMovieId !== groupMovieId) {
      return false; // Different movies - don't show this release
    }
    
    const search = (this.groupSearch || '').toLowerCase();
    const slug = (group.disc_group || '').toLowerCase();
    const releaseSlug = (group.release_slug || '').toLowerCase();
    const name = (group.release_name || '').toLowerCase();
    const matchesSearch = !search || slug.includes(search) || releaseSlug.includes(search) || name.includes(search);
    const targetType = this.labelForm.group_type || null;
    const groupType = group.group_type || 'movie';
    const typeMatches = !targetType || groupType === targetType || (!group.group_type && targetType === 'movie');
    return matchesSearch && typeMatches;
  }

  private formatYearLabel(_original?: any, release?: any): string {
    const rel = release != null && `${release}`.trim() ? `${release}` : null;
    return rel || '—';
  }

  /**
   * Get display name for a release group option.
   * Returns release_name if available, otherwise returns release_year as string.
   */
  getReleaseDisplayName(group: any): string {
    if (!group) return '—';
    
    // If release has an edition name, use it
    if (group.release_name && group.release_name.trim()) {
      return group.release_name.trim();
    }
    
    // Fallback: Release Year
    const releaseYear = group.release_year;
    if (releaseYear != null) {
      return String(releaseYear);
    }
    
    // Final fallback
    return '—';
  }

  /**
   * Get display name for the currently selected release (trigger button).
   * Returns release_name if available, otherwise returns release_year as string.
   */
  getSelectedReleaseDisplayName(): string {
    // Check if we have a release name (edition)
    const releaseName = this.labelForm?.release_name || this.lastReleaseDetails?.release_name;
    if (releaseName && releaseName.trim()) {
      return releaseName.trim();
    }
    
    // Fallback: Release Year
    const releaseYear = this.labelForm?.release_year || this.lastReleaseDetails?.release_year;
    if (releaseYear != null) {
      return String(releaseYear);
    }
    
    // Final fallback
    return 'Select release';
  }

  /**
   * Get metadata string for a release group option.
   * Format: "Highest Resolution. Type" (e.g., "UHD. Movie" or "Blu-Ray. Series")
   */
  getReleaseMetadata(group: any): string {
    if (!group) return '—';
    
    const resolution = this.getHighestResolution(group);
    const type = (group.group_type || group.type || 'Movie').charAt(0).toUpperCase() + 
                 (group.group_type || group.type || 'Movie').slice(1).toLowerCase();
    
    if (resolution) {
      return `${resolution}. ${type}`;
    }
    
    // If no resolution, just show type
    return type;
  }

  /**
   * Get metadata string for the currently selected release.
   * Format: "Highest Resolution. Type" (e.g., "UHD. Movie" or "Blu-Ray. Series")
   */
  getSelectedReleaseMetadata(): string {
    // Try to find the matching release in groupOptions to get metadata
    const releaseId = this.labelForm?.release_id || this.lastReleaseDetails?.release_id;
    const releaseSlug = this.labelForm?.disc_group || this.labelForm?.release_slug || 
                       this.lastReleaseDetails?.disc_group || this.lastReleaseDetails?.release_slug;
    let matchedGroup: any = null;
    
    if (releaseId || releaseSlug) {
      matchedGroup = (this.groupOptions || []).find((g: any) => 
        (releaseId && g.release_id === releaseId) || 
        (releaseSlug && (g.disc_group === releaseSlug || g.release_slug === releaseSlug))
      );
    }
    
    const resolution = this.getSelectedReleaseResolution(matchedGroup);
    const type = (this.labelForm?.group_type || 
                  matchedGroup?.group_type || 
                  this.lastReleaseDetails?.group_type || 
                  'Movie').charAt(0).toUpperCase() + 
                 (this.labelForm?.group_type || 
                  matchedGroup?.group_type || 
                  this.lastReleaseDetails?.group_type || 
                  'Movie').slice(1).toLowerCase();
    
    if (resolution) {
      return `${resolution}. ${type}`;
    }
    
    // If no resolution, just show type
    return type;
  }

  /**
   * Get the highest resolution for the currently selected release.
   */
  private getSelectedReleaseResolution(matchedGroup?: any): string | null {
    // Check labelForm first
    const labelFormat = this.labelForm?.disc_format;
    if (labelFormat) {
      const fmtStr = String(labelFormat).toLowerCase();
      if (fmtStr.includes('uhd') || fmtStr.includes('4k')) return 'UHD';
      if (fmtStr.includes('blu')) return 'Blu-Ray';
      if (fmtStr.includes('dvd')) return 'DVD';
    }
    
    // Check matched group from groupOptions
    if (matchedGroup) {
      const groupResolution = matchedGroup.resolution;
      if (groupResolution) {
        const resStr = String(groupResolution).toLowerCase();
        if (resStr.includes('2160') || resStr.includes('4k') || resStr.includes('uhd')) return 'UHD';
        if (resStr.includes('1080') || resStr.includes('blu')) return 'Blu-Ray';
        if (resStr.includes('480') || resStr.includes('dvd')) return 'DVD';
      }
      // Check format in matched group
      const groupFormat = matchedGroup.format || matchedGroup.disc_format;
      if (groupFormat) {
        const fmtStr = String(groupFormat).toLowerCase();
        if (fmtStr.includes('uhd') || fmtStr.includes('4k')) return 'UHD';
        if (fmtStr.includes('blu')) return 'Blu-Ray';
        if (fmtStr.includes('dvd')) return 'DVD';
      }
      // Check slug in matched group
      const groupSlug = matchedGroup.disc_group || matchedGroup.release_slug || '';
      if (groupSlug.toLowerCase().includes('uhd') || groupSlug.toLowerCase().includes('4k')) return 'UHD';
      if (groupSlug.toLowerCase().includes('blu')) return 'Blu-Ray';
      if (groupSlug.toLowerCase().includes('dvd')) return 'DVD';
    }
    
    // Check lastReleaseDetails
    const lastResolution = (this.lastReleaseDetails as any)?.resolution;
    if (lastResolution) {
      const resStr = String(lastResolution).toLowerCase();
      if (resStr.includes('2160') || resStr.includes('4k') || resStr.includes('uhd')) return 'UHD';
      if (resStr.includes('1080') || resStr.includes('blu')) return 'Blu-Ray';
      if (resStr.includes('480') || resStr.includes('dvd')) return 'DVD';
    }
    
    // Check slug for format hints
    const slug = this.labelForm?.disc_group || this.labelForm?.release_slug || 
                 this.lastReleaseDetails?.disc_group || this.lastReleaseDetails?.release_slug || '';
    if (slug.toLowerCase().includes('uhd') || slug.toLowerCase().includes('4k')) return 'UHD';
    if (slug.toLowerCase().includes('blu')) return 'Blu-Ray';
    if (slug.toLowerCase().includes('dvd')) return 'DVD';
    
    return null;
  }

  /**
   * Get the highest resolution from a release group.
   * Returns UHD, Blu-Ray, or DVD based on resolution or format.
   */
  private getHighestResolution(group: any): string | null {
    if (!group) return null;
    
    // Check resolution field first
    const resolution = group.resolution || (group as any)?.resolution;
    if (resolution) {
      const resStr = String(resolution).toLowerCase();
      if (resStr.includes('2160') || resStr.includes('4k') || resStr.includes('uhd')) {
        return 'UHD';
      }
      if (resStr.includes('1080') || resStr.includes('blu')) {
        return 'Blu-Ray';
      }
      if (resStr.includes('480') || resStr.includes('dvd')) {
        return 'DVD';
      }
    }
    
    // Check format field if available
    const format = group.format || (group as any)?.disc_format;
    if (format) {
      const fmtStr = String(format).toLowerCase();
      if (fmtStr.includes('uhd') || fmtStr.includes('4k')) {
        return 'UHD';
      }
      if (fmtStr.includes('blu')) {
        return 'Blu-Ray';
      }
      if (fmtStr.includes('dvd')) {
        return 'DVD';
      }
    }
    
    // Check disc_group slug for format hints (e.g., "2020-uhd")
    const slug = group.disc_group || group.release_slug || '';
    if (slug.toLowerCase().includes('uhd') || slug.toLowerCase().includes('4k')) {
      return 'UHD';
    }
    if (slug.toLowerCase().includes('blu')) {
      return 'Blu-Ray';
    }
    if (slug.toLowerCase().includes('dvd')) {
      return 'DVD';
    }
    
    return null;
  }

  onFocusIn(): void {
    this.focusDepth += 1;
    this.isActive = true;
  }

  onFocusOut(): void {
    this.focusDepth = Math.max(0, this.focusDepth - 1);
    this.isActive = this.focusDepth > 0;
  }

}
