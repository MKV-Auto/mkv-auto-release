import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, ViewEncapsulation } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ReleaseLabelComponent } from '../release-label/release-label.component';
import { DiscLabelComponent } from '../disc-label/disc-label.component';
import { TitleLabelComponent } from '../title-label/title-label.component';
import { canonicalTrackTitle } from '../../utils/canonical-track-title.util';

@Component({
  selector: 'app-label-shell',
  standalone: true,
  imports: [CommonModule, ReleaseLabelComponent, DiscLabelComponent, TitleLabelComponent],
  templateUrl: './label-shell.component.html',
  styleUrls: ['./label-shell.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
})
export class LabelShellComponent {
  @Input() labelForm: any;
  @Input() labelSaving = false; // default for backward compat
  @Input() lastAutosaveOk = true; // default for backward compat
  @Input() releaseSaving = false;
  @Input() releaseAutosaveOk = true;
  @Input() discSaving = false;
  @Input() discAutosaveOk = true;
  @Input() titleSaving = false;
  @Input() titleAutosaveOk = true;
  @Input() hasLabelContent = false;
  @Input() groupOptions: any[] = [];
  @Input() lastReleaseDetails: any = null;
  @Input() releaseNameHint = '';
  @Input() releaseSlugHint = '';
  @Input() titles: any[] = [];
  @Input() isSeries = false;
  @Input() titleStatusFn: (id: string | null | undefined) => string = () => 'pending';
  @Input() titleProgressValueFn: (id: string | null | undefined) => number = () => 0;
  @Input() titleActiveFn: (id: string | null | undefined) => boolean = () => false;
  @Input() showTitleStatus = true;
  @Input() previewUrlFn: (t: any) => string | null = () => null;
  @Input() titlePathFn: (t: any) => string | null = () => null;
  @Input() previewStateFn: (t: any) => { status: string; error?: string | null } | null = () => null;
  @Input() titleProgress: Record<string, number> = {};
  @Input() devMode = false;
  @Input() discdbHit = false; // Hide release metadata card for DiscDB hits
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
  @Output() releaseChanged = new EventEmitter<void>();
  @Output() releaseBlur = new EventEmitter<void>();
  @Output() discChanged = new EventEmitter<void>();
  @Output() discBlur = new EventEmitter<void>();
  @Output() titleChanged = new EventEmitter<void>();
  @Output() titleBlur = new EventEmitter<void>();

  private isFilled(v: any): boolean {
    return v !== null && v !== undefined && `${v}`.trim().length > 0;
  }

  get isMovieComplete(): boolean {
    const f = this.labelForm || {};
    return !!f.movie_id;
  }

  get isReleaseComplete(): boolean {
    if (!this.isMovieComplete) return false;
    const f = this.labelForm || {};
    
    // If linked to a boxset, consider release complete if boxset_id is set
    // The boxset owns these fields, so they don't need to be in the form
    if (f.boxset_id) {
      return true;
    }
    
    // Otherwise, require all release fields to be filled
    return (
      this.isFilled(f.release_year) &&
      this.isFilled(f.cover_front_url) &&
      this.isFilled(f.cover_back_url) &&
      this.isFilled(f.upc)
    );
  }

  get isDiscComplete(): boolean {
    if (!this.isReleaseComplete) return false;
    const f = this.labelForm || {};
    return (
      this.isFilled(f.disc_format) &&
      this.isFilled(f.disc_name) &&
      this.isFilled(f.disc_number)
    );
  }

  get missingFields(): string[] {
    const missing: string[] = [];
    const f = this.labelForm || {};
    
    // Movie is required - only check if movie_id is not set
    // If movie_id is set, the movie is already loaded and TMDB URL is not needed
    if (!f.movie_id) {
      missing.push('Movie: Movie ID (lookup from TMDB URL)');
    }
    
    // If linked to a boxset, boxset owns the release fields, so don't check them
    if (!f.boxset_id) {
      const releaseFields: Array<[string, any]> = [
        ['Release Year', f.release_year],
        ['Front Cover URL', f.cover_front_url],
        ['Back Cover URL', f.cover_back_url],
        ['UPC', f.upc],
        // Release Slug is auto-generated, not counted in missing fields
        // Release Name is optional (edition name), not counted in missing fields
      ];
      releaseFields.forEach(([label, val]) => {
        if (!this.isFilled(val)) missing.push(`Release: ${label}`);
      });
    }

    const discFields: Array<[string, any]> = [
      ['Disc Format', f.disc_format],
      ['Disc Name', f.disc_name],
      ['Disc Number', f.disc_number],
    ];
    discFields.forEach(([label, val]) => {
      if (!this.isFilled(val)) missing.push(`Disc: ${label}`);
    });

    const tracks = Array.isArray(this.titles) ? this.titles : Array.isArray(f.tracks) ? f.tracks : [];
    tracks
      .filter((t: any) => {
        const rawType = (t?.type ?? '').toString().toLowerCase();
        const ignored = rawType === 'ignore';
        return !ignored;
      })
      .forEach((t: any, idx: number) => {
        const display = canonicalTrackTitle(t) || t?.source_file || `Track ${idx + 1}`;
        const type = (t?.type ?? '').toString().toLowerCase();
        const titleLike = canonicalTrackTitle(t) || (t?.description ?? t?.note ?? null);
        if (!this.isFilled(titleLike)) {
          missing.push(`Track ${idx + 1} (${display}): title/description`);
        }
        if (type === 'episode') {
          if (!this.isFilled(t?.season)) missing.push(`Track ${idx + 1} (${display}): season`);
          if (!this.isFilled(t?.episode)) missing.push(`Track ${idx + 1} (${display}): episode`);
        }
      });

    return missing;
  }

}
