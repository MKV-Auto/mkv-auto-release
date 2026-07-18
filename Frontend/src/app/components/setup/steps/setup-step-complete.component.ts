import { Component, EventEmitter, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { IconComponent } from '../../../ui/icon/icon.component';
import { BtnComponent } from '../../../ui/btn/btn.component';

@Component({
  selector: 'app-setup-step-complete',
  standalone: true,
  imports: [CommonModule, IconComponent, BtnComponent],
  template: `
    <div class="setup-step setup-step-complete">
      <!-- Success Icon -->
      <div class="setup-step-complete-icon-wrapper">
        <div class="setup-step-complete-icon">
          <ui-icon name="check-circle" [size]="40"></ui-icon>
        </div>
      </div>

      <!-- Header -->
      <div class="setup-step-complete-header">
        <h3 class="setup-step-complete-title">You're all set!</h3>
        <p class="setup-step-complete-desc">Your media ripper is configured and ready to go. Here's what happens next:</p>
      </div>

      <!-- Steps -->
      <div class="setup-step-complete-steps">
        <div class="setup-step-complete-step">
          <div class="setup-step-complete-step-badge badge-blue">1</div>
          <div class="setup-step-complete-step-content">
            <h4 class="setup-step-complete-step-title">Insert a disc</h4>
            <p class="setup-step-complete-step-desc">We'll automatically scan it and look up metadata to identify the content</p>
          </div>
        </div>

        <div class="setup-step-complete-step">
          <div class="setup-step-complete-step-badge badge-purple">2</div>
          <div class="setup-step-complete-step-content">
            <h4 class="setup-step-complete-step-title">Pick or create your movie/release</h4>
            <p class="setup-step-complete-step-desc">Choose which titles to rip and configure any settings</p>
          </div>
        </div>

        <div class="setup-step-complete-step">
          <div class="setup-step-complete-step-badge badge-pink">3</div>
          <div class="setup-step-complete-step-content">
            <h4 class="setup-step-complete-step-title">Start the rip</h4>
            <p class="setup-step-complete-step-desc">We'll copy, post-process, and transfer files to your library automatically</p>
          </div>
        </div>

        <div class="setup-step-complete-step">
          <div class="setup-step-complete-step-badge badge-green">✓</div>
          <div class="setup-step-complete-step-content">
            <h4 class="setup-step-complete-step-title">Enjoy your media</h4>
            <p class="setup-step-complete-step-desc">Files appear in your Plex or Jellyfin library, ready to watch</p>
          </div>
        </div>
      </div>

      <!-- What's Next -->
      <div class="setup-step-complete-whats-next">
        <div class="setup-step-complete-whats-next-header">
          <ui-icon name="book" [size]="20"></ui-icon>
          <h4>What's next?</h4>
        </div>
        <p class="setup-step-complete-whats-next-text">
          Want a quick tour of how the platform works? We'll show you how disc identification,
          title selection, boxsets vs releases, and editions work — it only takes a minute.
        </p>
        <div class="setup-step-complete-actions">
          <ui-btn variant="primary" [fullWidth]="true" (click)="onComplete(true)">
            <ui-icon uiBtnIcon name="book" [size]="14"></ui-icon>
            Take a quick tour
          </ui-btn>
          <ui-btn variant="ghost" [fullWidth]="true" (click)="onComplete(false)">
            <ui-icon uiBtnIcon name="disc" [size]="14"></ui-icon>
            Start ripping
          </ui-btn>
        </div>
      </div>

      <!-- Quick Tip -->
      <div class="setup-step-complete-tip">
        <p>💡 You can change any of these settings later in <span class="highlight">Settings</span></p>
      </div>
    </div>
  `,
  styles: [`
    .setup-step { display: flex; flex-direction: column; gap: 1.5rem; }
    .setup-step-complete { text-align: center; }
    
    .setup-step-complete-icon-wrapper { display: flex; justify-content: center; }
    .setup-step-complete-icon { width: 5rem; height: 5rem; border-radius: 1rem; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%); box-shadow: 0 0 40px rgba(34, 197, 94, 0.4); color: #fff; }
    
    .setup-step-complete-header { text-align: center; }
    .setup-step-complete-title { font-size: 1.5rem; font-weight: 700; color: #fff; margin: 0 0 0.5rem 0; }
    .setup-step-complete-desc { font-size: 0.875rem; color: rgba(255,255,255,0.7); margin: 0; max-width: 28rem; margin-left: auto; margin-right: auto; }
    
    .setup-step-complete-steps { padding: 1.25rem; border-radius: 0.75rem; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); display: flex; flex-direction: column; gap: 1rem; }
    .setup-step-complete-step { display: flex; gap: 1rem; text-align: left; }
    .setup-step-complete-step-badge { width: 2rem; height: 2rem; border-radius: 0.5rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-size: 0.875rem; font-weight: 700; color: #fff; }
    .badge-blue { background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); }
    .badge-purple { background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); }
    .badge-pink { background: linear-gradient(135deg, #ec4899 0%, #db2777 100%); }
    .badge-green { background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%); }
    .setup-step-complete-step-content { flex: 1; }
    .setup-step-complete-step-title { font-size: 0.875rem; font-weight: 500; color: #fff; margin: 0 0 0.25rem 0; }
    .setup-step-complete-step-desc { font-size: 0.875rem; color: rgba(255,255,255,0.6); margin: 0; }
    
    .setup-step-complete-whats-next { padding: 1.25rem; border-radius: 0.75rem; background: rgba(59, 130, 246, 0.08); border: 1px solid rgba(59, 130, 246, 0.2); display: flex; flex-direction: column; gap: 1rem; }
    .setup-step-complete-whats-next-header { display: flex; align-items: center; gap: 0.5rem; color: #93c5fd; }
    .setup-step-complete-whats-next-header h4 { font-size: 0.875rem; font-weight: 500; margin: 0; }
    .setup-step-complete-whats-next-text { font-size: 0.875rem; color: rgba(147, 197, 253, 0.8); margin: 0; text-align: left; }
    
    .setup-step-complete-actions { display: flex; flex-direction: column; gap: 0.75rem; }
    @media (min-width: 640px) {
      .setup-step-complete-actions { flex-direction: row; }
    }
    .setup-step-btn { flex: 1; display: flex; align-items: center; justify-content: center; gap: 0.5rem; padding: 0.75rem 1.25rem; border-radius: 0.5rem; font-size: 0.875rem; font-weight: 700; border: none; cursor: pointer; transition: all 0.2s; }
    .setup-step-btn-tour { background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%); color: #fff; box-shadow: 0 0 20px rgba(59, 130, 246, 0.4); }
    .setup-step-btn-tour:hover { transform: scale(1.05); }
    .setup-step-btn-start { background: transparent; color: rgba(255,255,255,0.8); border: 1px solid rgba(255,255,255,0.1); font-weight: 500; }
    .setup-step-btn-start:hover { color: #fff; background: rgba(255,255,255,0.05); }
    
    .setup-step-complete-tip { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; text-align: center; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06); color: rgba(255,255,255,0.6); }
    .setup-step-complete-tip .highlight { color: #fff; font-weight: 500; }
  `],
})
export class SetupStepCompleteComponent {
  @Output() complete = new EventEmitter<boolean>();

  onComplete(showGuide: boolean): void {
    this.complete.emit(showGuide);
  }
}
