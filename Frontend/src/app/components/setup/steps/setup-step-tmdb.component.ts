import { Component, EventEmitter, Input, OnChanges, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { SystemService } from '../../../services/system.service';
import { IconComponent } from '../../../ui/icon/icon.component';
import { BtnComponent } from '../../../ui/btn/btn.component';

/** #614: TMDB API key step in the first-boot setup assistant. Optional — the
 *  user can skip it and configure later via Settings → TMDB. */
export interface TmdbStepData {
  /** Backend-confirmed: a key is currently persisted. */
  apiKeySet: boolean;
  /** User-edited value, pre-populated from /system/tmdb/config on load. */
  apiKey: string;
  /** User clicked "skip" — counts as step complete even without a key. */
  dismissed: boolean;
}

type SaveResult = 'success' | 'error' | null;

@Component({
  selector: 'app-setup-step-tmdb',
  standalone: true,
  imports: [CommonModule, FormsModule, IconComponent, BtnComponent],
  template: `
    <div class="setup-step">
      <!-- Header -->
      <div class="setup-step-header">
        <div class="setup-step-icon setup-step-icon-tmdb">
          <ui-icon name="film" [size]="24"></ui-icon>
        </div>
        <div class="setup-step-header-text">
          <h3 class="setup-step-title">TheMovieDB (TMDB)</h3>
          <p class="setup-step-desc">A free TMDB v3 API key unlocks automatic movie / series suggestions on disc insert and the TMDB search on the labeling step. Optional — you can skip and configure later in Settings → TMDB.</p>
        </div>
      </div>

      <!-- API key input -->
      <div class="setup-step-tmdb-section">
        <label class="setup-step-label">TMDB v3 API key</label>
        <input
          type="text"
          class="setup-step-input"
          [class.error]="saveResult === 'error'"
          [class.success]="saveResult === 'success'"
          [(ngModel)]="localApiKey"
          (ngModelChange)="onKeyChange($event)"
          placeholder="e.g. 1a2b3c4d5e6f7g8h9i0j…"
          autocomplete="off"
          spellcheck="false"
        />

        <div *ngIf="saveResult === 'success'" class="setup-step-message success">
          <ui-icon name="check-circle" [size]="16"></ui-icon>
          <span>Key saved. TMDB-backed features are now enabled.</span>
        </div>

        <div *ngIf="saveResult === 'error'" class="setup-step-message error">
          <ui-icon name="alert" [size]="16"></ui-icon>
          <span>{{ saveError || 'Failed to save the key. Please try again.' }}</span>
        </div>

        <ui-btn
          variant="primary"
          [disabled]="!localApiKey?.trim()"
          [loading]="saving"
          (click)="saveKey()">
          Save key
        </ui-btn>
      </div>

      <!-- How to get a key -->
      <div class="setup-step-info setup-step-info-tmdb">
        <p class="setup-step-info-title">💡 How to get a TMDB API key</p>
        <ol class="setup-step-info-list">
          <li>Sign up free at <a href="https://www.themoviedb.org/signup" target="_blank" rel="noopener noreferrer" class="setup-step-info-link">themoviedb.org <ui-icon name="external" [size]="11"></ui-icon></a></li>
          <li>Open your profile → Settings → API</li>
          <li>Request a v3 API key (instant approval for personal / hobby use)</li>
          <li>Copy the key and paste it above</li>
        </ol>
      </div>

      <!-- Optional reminder -->
      <div *ngIf="!data.apiKeySet" class="setup-step-reminder">
        <p><strong>Note:</strong> Without a key, the auto-suggestion on disc insert and the TMDB search on the film step stay hidden. Your existing manual labeling flow is unaffected.</p>
      </div>
    </div>
  `,
  styles: [`
    .setup-step { display: flex; flex-direction: column; gap: 1.5rem; }
    .setup-step-header { display: flex; gap: 1rem; align-items: flex-start; }
    .setup-step-icon { width: 3rem; height: 3rem; border-radius: 0.75rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .setup-step-icon-tmdb { background: linear-gradient(135deg, #01b4e4 0%, #0d8eb9 100%); box-shadow: 0 0 20px rgba(1, 180, 228, 0.3); color: #fff; }
    .setup-step-header-text { flex: 1; }
    .setup-step-title { font-size: 1.125rem; font-weight: 700; color: #fff; margin: 0 0 0.5rem 0; }
    .setup-step-desc { font-size: 0.875rem; color: rgba(255,255,255,0.7); margin: 0; line-height: 1.5; }

    .setup-step-tmdb-section { display: flex; flex-direction: column; gap: 0.75rem; }
    .setup-step-label { display: block; font-size: 0.875rem; font-weight: 500; color: rgba(255,255,255,0.8); }
    .setup-step-input { width: 100%; padding: 0.625rem 1rem; font-size: 0.875rem; font-family: monospace; color: #fff; border-radius: 0.5rem; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); transition: all 0.2s; }
    .setup-step-input:focus { outline: none; box-shadow: 0 0 0 2px rgba(1, 180, 228, 0.5); }
    .setup-step-input.error { border-color: rgba(239, 68, 68, 0.5); }
    .setup-step-input.success { border-color: rgba(34, 197, 94, 0.5); }

    .setup-step-message { display: flex; align-items: flex-start; gap: 0.5rem; padding: 0.75rem; border-radius: 0.5rem; font-size: 0.875rem; }
    .setup-step-message svg { flex-shrink: 0; margin-top: 0.125rem; }
    .setup-step-message.success { background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.3); color: #4ade80; }
    .setup-step-message.error { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); color: #f87171; }

    .setup-step-info { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; }
    .setup-step-info-tmdb { background: rgba(1, 180, 228, 0.08); border: 1px solid rgba(1, 180, 228, 0.2); }
    .setup-step-info-title { color: #7dd3fc; font-weight: 500; margin: 0 0 0.75rem 0; }
    .setup-step-info-list { margin: 0; padding-left: 1.25rem; color: rgba(125, 211, 252, 0.85); list-style-type: decimal; }
    .setup-step-info-list li { margin-bottom: 0.5rem; }
    .setup-step-info-link { display: inline-flex; align-items: center; gap: 0.25rem; color: #7dd3fc; text-decoration: none; transition: color 0.2s; }
    .setup-step-info-link:hover { color: #bae6fd; }
    .setup-step-info-link svg { flex-shrink: 0; }

    .setup-step-reminder { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; background: rgba(251, 146, 60, 0.08); border: 1px solid rgba(251, 146, 60, 0.2); color: #fdba74; }
    .setup-step-reminder strong { color: #fb923c; }
  `],
})
export class SetupStepTmdbComponent implements OnChanges {
  @Input() data!: TmdbStepData;
  @Output() dataChange = new EventEmitter<Partial<TmdbStepData>>();

  localApiKey = '';
  saving = false;
  saveResult: SaveResult = null;
  saveError: string | null = null;

  constructor(private system: SystemService) {}

  ngOnChanges(): void {
    this.localApiKey = this.data?.apiKey ?? '';
  }

  onKeyChange(value: string): void {
    this.localApiKey = value;
    this.dataChange.emit({ apiKey: value, dismissed: false });
    // Reset the save indicator when the user starts editing again.
    if (this.saveResult !== null) {
      this.saveResult = null;
      this.saveError = null;
    }
  }

  saveKey(): void {
    const trimmed = this.localApiKey?.trim();
    if (!trimmed) return;
    this.saving = true;
    this.saveResult = null;
    this.saveError = null;
    this.system.saveTmdbConfig(trimmed).subscribe({
      next: (cfg) => {
        this.saving = false;
        this.saveResult = 'success';
        this.dataChange.emit({
          apiKey: cfg?.api_key ?? trimmed,
          apiKeySet: !!cfg?.api_key_set,
          dismissed: false,
        });
      },
      error: (err) => {
        this.saving = false;
        this.saveResult = 'error';
        this.saveError = err?.error?.detail ?? err?.message ?? null;
      },
    });
  }
}
