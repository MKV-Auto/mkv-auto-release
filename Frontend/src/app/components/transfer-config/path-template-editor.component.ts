import { Component, Input, Output, EventEmitter, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-path-template-editor',
  standalone: true,
  imports: [CommonModule, FormsModule],
  host: { class: 'path-template-editor-host' },
  template: `
    <div class="path-template-editor">
      <div class="path-template-editor-header">
        <div class="path-template-editor-title-row">
          <div class="path-template-editor-icon">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-2.5a1 1 0 0 0-.5.5L14 8"/><path d="M14 8V4a1 1 0 0 0-1-1h-2a1 1 0 0 0-1 1v4"/><path d="M20 21v-3a1 1 0 0 0-1-1h-2a1 1 0 0 0-1 1v3"/><path d="M14 15v4a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-4"/><path d="M4 10a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h2.5a1 1 0 0 1 .5.5L10 8"/><path d="M10 8V4a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v4"/><path d="M4 21v-3a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v3"/><path d="M10 15v4a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-4"/></svg>
          </div>
          <h5 class="path-template-editor-title">Path Template</h5>
        </div>
        <button
          type="button"
          class="path-template-editor-toggle"
          (click)="showVariables = !showVariables"
          [attr.aria-expanded]="showVariables">
          {{ showVariables ? 'Hide Variables' : 'Show Variables' }}
          <svg *ngIf="showVariables" xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
          <svg *ngIf="!showVariables" xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>

      <div class="path-template-editor-field">
        <label class="path-template-editor-label">Template Pattern</label>
        <input
          type="text"
          class="path-template-editor-input"
          [(ngModel)]="template"
          (ngModelChange)="onTemplateChange()"
          placeholder="{movie_name} ({year})/Disc {disc_number}">
      </div>

      @if (showVariables) {
        <div class="path-template-editor-variables">
          <div class="path-template-editor-variables-title">Available Variables (click to insert)</div>
          <div class="path-template-editor-variables-grid">
            @for (variable of availableVariables; track variable) {
              <button
                type="button"
                class="path-template-editor-variable-btn"
                (click)="insertVariable(variable)">
                <span class="path-template-editor-variable-key">{{ '{' + variable + '}' }}</span>
                <span class="path-template-editor-variable-desc">{{ getVariableDescription(variable) }}</span>
              </button>
            }
          </div>
        </div>
      }

      <div class="path-template-editor-preview-block">
        <div class="path-template-editor-preview-label">
          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
          <span>Preview</span>
        </div>
        <div class="path-template-editor-preview-value">
          @if (preview) {
            {{ preview }}
          } @else {
            <span class="path-template-editor-preview-empty">No preview available</span>
          }
        </div>
      </div>
    </div>
  `,
})
export class PathTemplateEditorComponent implements OnInit {
  @Input() template = '';
  @Input() sampleData: Record<string, any> = {};
  @Output() templateChange = new EventEmitter<string>();

  showVariables = false;
  preview = '';

  availableVariables = [
    'movie_name',
    'movie_year',
    'year',
    'release_name',
    'release_year',
    'release_slug',
    'disc_number',
    'disc_name',
    'type',
    'format',
  ];

  ngOnInit() {
    this.updatePreview();
  }

  onTemplateChange() {
    this.templateChange.emit(this.template);
    this.updatePreview();
  }

  insertVariable(variable: string) {
    const cursorPos = (document.activeElement as HTMLInputElement)?.selectionStart || this.template.length;
    const before = this.template.substring(0, cursorPos);
    const after = this.template.substring(cursorPos);
    this.template = before + `{${variable}}` + after;
    this.onTemplateChange();
  }

  updatePreview() {
    if (!this.template) {
      this.preview = '';
      return;
    }

    // Simple preview resolution
    let preview = this.template;
    for (const [key, value] of Object.entries(this.sampleData)) {
      const placeholder = `{${key}}`;
      if (preview.includes(placeholder)) {
        preview = preview.replace(new RegExp(placeholder.replace(/[{}]/g, '\\$&'), 'g'), String(value || ''));
      }
    }
    
    // Handle year alias
    if (preview.includes('{year}') && this.sampleData['movie_year']) {
      preview = preview.replace(/{year}/g, String(this.sampleData['movie_year']));
    }
    
    // Format disc_number
    if (preview.includes('{disc_number}') && this.sampleData['disc_number']) {
      const discNum = String(this.sampleData['disc_number']).padStart(2, '0');
      preview = preview.replace(/{disc_number}/g, discNum);
    }
    
    // Remove any remaining placeholders
    preview = preview.replace(/{[^}]+}/g, '');
    
    this.preview = preview;
  }

  getVariableDescription(variable: string): string {
    const descriptions: Record<string, string> = {
      movie_name: 'Movie name',
      movie_year: 'Production year',
      year: 'Production year (alias)',
      release_name: 'Release/edition name',
      release_year: 'Release year',
      release_slug: 'Release slug',
      disc_number: 'Disc number (formatted as 01, 02, etc.)',
      disc_name: 'Disc name',
      type: 'Content type (movie, series, boxset)',
      format: 'Disc format (UHD, Blu-Ray, DVD)',
    };
    return descriptions[variable] || variable;
  }
}

