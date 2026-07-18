import { Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { IconComponent } from '../../../ui/icon/icon.component';

export interface LibraryStepData {
  type: 'plex' | 'jellyfin';
}

interface LibraryOption {
  value: 'plex' | 'jellyfin';
  name: string;
  description: string;
  color: string;
  gradient: string;
  glow: string;
}

const LIBRARY_OPTIONS: LibraryOption[] = [
  {
    value: 'plex',
    name: 'Plex',
    description: 'Popular media server with broad device support and powerful features',
    color: '#e5a00d',
    gradient: 'linear-gradient(135deg, #e5a00d 0%, #d89000 100%)',
    glow: 'rgba(229, 160, 13, 0.3)',
  },
  {
    value: 'jellyfin',
    name: 'Jellyfin',
    description: 'Free and open-source media server with no paywalls or tracking',
    color: '#00a4dc',
    gradient: 'linear-gradient(135deg, #00a4dc 0%, #0090c0 100%)',
    glow: 'rgba(0, 164, 220, 0.3)',
  },
];

@Component({
  selector: 'app-setup-step-library',
  standalone: true,
  imports: [CommonModule, IconComponent],
  template: `
    <div class="setup-step">
      <!-- Header -->
      <div class="setup-step-header">
        <div class="setup-step-icon setup-step-icon-teal">
          <ui-icon name="server" [size]="24"></ui-icon>
        </div>
        <div class="setup-step-header-text">
          <h3 class="setup-step-title">Library Type</h3>
          <p class="setup-step-desc">Select your media server platform. This helps us organize and name your files correctly for optimal compatibility with your library.</p>
        </div>
      </div>

      <!-- Library Selection -->
      <div class="setup-step-library-grid">
        <button 
          *ngFor="let library of libraryOptions" 
          type="button" 
          class="setup-step-library-card"
          [class.active]="data.type === library.value"
          (click)="select(library.value)"
          [style.background]="getCardBackground(library)"
          [style.border]="getCardBorder(library)"
          [style.box-shadow]="getCardShadow(library)"
        >
          <div class="setup-step-library-card-content">
            <!-- Icon -->
            <div
              class="setup-step-library-icon"
              [style.background]="library.gradient"
              [style.box-shadow]="'0 0 15px ' + library.glow"
            >
              <ui-icon name="server" [size]="24"></ui-icon>
            </div>
            
            <!-- Content -->
            <div class="setup-step-library-text">
              <div class="setup-step-library-header">
                <h4 class="setup-step-library-name">{{ library.name }}</h4>
                <div
                  *ngIf="data.type === library.value"
                  class="setup-step-library-check"
                  [style.background]="library.gradient"
                  [style.box-shadow]="'0 0 10px ' + library.glow"
                >
                  <ui-icon name="check" [size]="16"></ui-icon>
                </div>
              </div>
              <p class="setup-step-library-desc">{{ library.description }}</p>
            </div>
          </div>
        </button>
      </div>

      <!-- Info -->
      <div class="setup-step-info setup-step-info-teal">
        <p class="setup-step-info-title">💡 What does this affect?</p>
        <ul>
          <li>File naming conventions (both follow similar patterns)</li>
          <li>Metadata organization for optimal library scanning</li>
          <li>Post-processing paths and folder structures</li>
          <li>You can change this later in Settings if needed</li>
        </ul>
      </div>
    </div>
  `,
  styles: [`
    .setup-step { display: flex; flex-direction: column; gap: 1.5rem; }
    .setup-step-header { display: flex; gap: 1rem; align-items: flex-start; }
    .setup-step-icon { width: 3rem; height: 3rem; border-radius: 0.75rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .setup-step-icon-teal { background: linear-gradient(135deg, #14b8a6 0%, #0d9488 100%); box-shadow: 0 0 20px rgba(20, 184, 166, 0.3); color: #fff; }
    .setup-step-header-text { flex: 1; }
    .setup-step-title { font-size: 1.125rem; font-weight: 700; color: #fff; margin: 0 0 0.5rem 0; }
    .setup-step-desc { font-size: 0.875rem; color: rgba(255,255,255,0.7); margin: 0; line-height: 1.5; }
    
    .setup-step-library-grid { display: grid; gap: 1rem; }
    .setup-step-library-card { position: relative; padding: 1.25rem; border-radius: 0.75rem; text-align: left; cursor: pointer; transition: all 0.2s; border: none; }
    .setup-step-library-card:hover { transform: scale(1.02); }
    .setup-step-library-card-content { display: flex; align-items: flex-start; gap: 1rem; }
    
    .setup-step-library-icon { width: 3rem; height: 3rem; border-radius: 0.5rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; color: #fff; }
    
    .setup-step-library-text { flex: 1; }
    .setup-step-library-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.5rem; }
    .setup-step-library-name { font-size: 1rem; font-weight: 700; color: #fff; margin: 0; }
    .setup-step-library-check { width: 1.5rem; height: 1.5rem; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; }
    .setup-step-library-desc { font-size: 0.875rem; color: rgba(255,255,255,0.6); margin: 0; }
    
    .setup-step-info { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; }
    .setup-step-info-teal { background: rgba(20, 184, 166, 0.08); border: 1px solid rgba(20, 184, 166, 0.2); }
    .setup-step-info-title { color: #5eead4; font-weight: 500; margin: 0 0 0.5rem 0; }
    .setup-step-info ul { margin: 0; padding-left: 1rem; color: rgba(94, 234, 212, 0.8); list-style-type: disc; }
    .setup-step-info ul li { margin-bottom: 0.25rem; }
  `],
})
export class SetupStepLibraryComponent {
  @Input() data!: LibraryStepData;
  @Output() dataChange = new EventEmitter<Partial<LibraryStepData>>();

  readonly libraryOptions = LIBRARY_OPTIONS;

  select(type: 'plex' | 'jellyfin'): void {
    this.dataChange.emit({ type });
  }

  getCardBackground(library: LibraryOption): string {
    return this.data.type === library.value
      ? `${library.color}15`
      : 'rgba(255, 255, 255, 0.04)';
  }

  getCardBorder(library: LibraryOption): string {
    return this.data.type === library.value
      ? `1px solid ${library.color}40`
      : '1px solid rgba(255, 255, 255, 0.08)';
  }

  getCardShadow(library: LibraryOption): string {
    return this.data.type === library.value
      ? `0 0 20px ${library.glow}`
      : 'none';
  }
}
