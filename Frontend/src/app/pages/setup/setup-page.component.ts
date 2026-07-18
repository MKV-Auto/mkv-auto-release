import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { SetupModalComponent } from '../../components/setup/setup-modal.component';
import { SystemService } from '../../services/system.service';

/**
 * Full-page setup wizard shown at /setup when first-time setup is not complete.
 * Wraps the setup modal in standalone mode: on complete, marks setup complete and navigates to ripper.
 */
@Component({
  selector: 'app-setup-page',
  standalone: true,
  imports: [CommonModule, SetupModalComponent],
  template: `
    <div class="setup-page-root">
      <app-setup-modal
        [standalonePage]="true"
        (complete)="onComplete($event)">
      </app-setup-modal>
    </div>
  `,
  styles: [`
    .setup-page-root {
      min-height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
    }
  `],
})
export class SetupPageComponent {
  constructor(
    private systemSvc: SystemService,
    private router: Router,
  ) {}

  onComplete(showGuide: boolean): void {
    this.systemSvc.markSetupComplete().subscribe({
      next: () => {
        // Navigate with state to signal platform guide should open
        this.router.navigate(['/activity'], { 
          state: { openPlatformGuide: showGuide } 
        });
      },
      error: (err) => {
        console.error('Failed to mark setup complete:', err);
        // Navigate anyway - user completed setup
        this.router.navigate(['/activity'], { 
          state: { openPlatformGuide: showGuide } 
        });
      },
    });
  }
}
