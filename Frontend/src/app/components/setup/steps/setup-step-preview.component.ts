import { Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { IconComponent } from '../../../ui/icon/icon.component';

export interface PreviewStepData {
  duration: number;
  maxParallel: number;
}

@Component({
  selector: 'app-setup-step-preview',
  standalone: true,
  imports: [CommonModule, FormsModule, IconComponent],
  template: `
    <div class="setup-step">
      <!-- Header -->
      <div class="setup-step-header">
        <div class="setup-step-icon setup-step-icon-pink">
          <ui-icon name="columns" [size]="24"></ui-icon>
        </div>
        <div class="setup-step-header-text">
          <h3 class="setup-step-title">Preview Settings</h3>
          <p class="setup-step-desc">Configure how preview images are generated for your titles. These previews help you identify content before ripping. You can keep the defaults or customize to your preferences.</p>
        </div>
      </div>

      <!-- Settings -->
      <div class="setup-step-body">
        <!-- Preview Duration -->
        <div class="setup-step-setting">
          <label class="setup-step-label">Preview Duration</label>
          <div class="setup-step-slider-container">
            <input 
              type="range" 
              min="30" 
              max="300" 
              step="30" 
              [ngModel]="data.duration" 
              (ngModelChange)="onDurationChange($event)" 
              class="setup-step-range"
              [style.background]="getRangeGradient()"
            />
            <div class="setup-step-range-labels">
              <span class="setup-step-range-label-start">30 seconds</span>
              <div class="setup-step-range-value">{{ data.duration }} seconds</div>
              <span class="setup-step-range-label-end">5 minutes</span>
            </div>
          </div>
          <p class="setup-step-help-text">Longer previews take more time to generate but may be more representative</p>
        </div>

        <!-- Max Parallel -->
        <div class="setup-step-setting">
          <label class="setup-step-label">
            <span style="color: #facc15; display: inline-flex;">
              <ui-icon name="alert" [size]="16"></ui-icon>
            </span>
            Maximum Parallel Previews
          </label>
          <div class="setup-step-buttons">
            <button 
              *ngFor="let n of [1,2,3,4,5]" 
              type="button" 
              class="setup-step-num-btn" 
              [class.active]="data.maxParallel === n" 
              (click)="dataChange.emit({ maxParallel: n })"
            >{{ n }}</button>
          </div>
          <p class="setup-step-help-text">Higher values generate previews faster but use more system resources</p>
        </div>
      </div>

      <!-- Recommendations -->
      <div class="setup-step-info setup-step-info-pink">
        <p class="setup-step-info-title">💡 Recommended settings</p>
        <ul>
          <li><strong>90 seconds</strong> is a good balance for most content</li>
          <li><strong>2 parallel</strong> works well on most modern systems</li>
          <li>Increase parallelism if you have a powerful CPU</li>
          <li>You can adjust these anytime in Settings</li>
        </ul>
      </div>
    </div>
  `,
  styles: [`
    .setup-step { display: flex; flex-direction: column; gap: 1.5rem; }
    .setup-step-header { display: flex; gap: 1rem; align-items: flex-start; }
    .setup-step-icon { width: 3rem; height: 3rem; border-radius: 0.75rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .setup-step-icon-pink { background: linear-gradient(135deg, #ec4899 0%, #db2777 100%); box-shadow: 0 0 20px rgba(236, 72, 153, 0.3); color: #fff; }
    .setup-step-header-text { flex: 1; }
    .setup-step-title { font-size: 1.125rem; font-weight: 700; color: #fff; margin: 0 0 0.5rem 0; }
    .setup-step-desc { font-size: 0.875rem; color: rgba(255,255,255,0.7); margin: 0; line-height: 1.5; }
    
    .setup-step-body { display: flex; flex-direction: column; gap: 1.25rem; }
    .setup-step-setting { display: flex; flex-direction: column; gap: 0.75rem; }
    .setup-step-label { font-size: 0.875rem; font-weight: 500; color: rgba(255,255,255,0.8); display: flex; align-items: center; gap: 0.5rem; }
    
    .setup-step-slider-container { display: flex; flex-direction: column; gap: 0.75rem; }
    .setup-step-range { width: 100%; height: 0.5rem; border-radius: 0.5rem; appearance: none; cursor: pointer; }
    .setup-step-range::-webkit-slider-thumb { appearance: none; width: 1rem; height: 1rem; border-radius: 50%; background: #fff; cursor: pointer; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }
    .setup-step-range::-moz-range-thumb { width: 1rem; height: 1rem; border-radius: 50%; background: #fff; cursor: pointer; border: none; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }
    .setup-step-range-labels { display: flex; align-items: center; justify-content: space-between; }
    .setup-step-range-label-start, .setup-step-range-label-end { font-size: 0.75rem; color: rgba(255,255,255,0.5); }
    .setup-step-range-value { font-size: 0.875rem; color: #fff; font-weight: 500; padding: 0.375rem 0.75rem; background: rgba(236, 72, 153, 0.2); border: 1px solid rgba(236, 72, 153, 0.4); border-radius: 0.5rem; }
    .setup-step-help-text { font-size: 0.75rem; color: rgba(255,255,255,0.5); margin: 0; }
    
    .setup-step-buttons { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0.5rem; }
    .setup-step-num-btn { padding: 0.75rem 1rem; border-radius: 0.5rem; font-size: 0.875rem; font-weight: 700; border: 1px solid rgba(255,255,255,0.1); background: rgba(255,255,255,0.06); color: rgba(255,255,255,0.6); cursor: pointer; transition: all 0.2s; }
    .setup-step-num-btn:hover { color: #fff; background: rgba(255,255,255,0.1); }
    .setup-step-num-btn.active { background: linear-gradient(135deg, #ec4899 0%, #db2777 100%); color: #fff; border-color: rgba(236, 72, 153, 0.5); box-shadow: 0 0 15px rgba(236, 72, 153, 0.3); }
    
    .setup-step-info { padding: 1rem; border-radius: 0.5rem; font-size: 0.875rem; }
    .setup-step-info-pink { background: rgba(236, 72, 153, 0.08); border: 1px solid rgba(236, 72, 153, 0.2); }
    .setup-step-info-title { color: #f9a8d4; font-weight: 500; margin: 0 0 0.5rem 0; }
    .setup-step-info ul { margin: 0; padding-left: 1rem; color: rgba(249, 168, 212, 0.8); list-style-type: disc; }
    .setup-step-info ul li { margin-bottom: 0.25rem; }
  `],
})
export class SetupStepPreviewComponent {
  @Input() data!: PreviewStepData;
  @Output() dataChange = new EventEmitter<Partial<PreviewStepData>>();

  onDurationChange(duration: number): void {
    this.dataChange.emit({ duration });
  }

  getRangeGradient(): string {
    const progress = ((this.data.duration - 30) / 270) * 100;
    return `linear-gradient(to right, #ec4899 0%, #ec4899 ${progress}%, rgba(255, 255, 255, 0.1) ${progress}%, rgba(255, 255, 255, 0.1) 100%)`;
  }
}
