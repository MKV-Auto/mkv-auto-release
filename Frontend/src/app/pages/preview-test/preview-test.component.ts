import { CommonModule } from '@angular/common';
import { Component, ViewChild, OnDestroy } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { environment } from '../../environments/environment';
import { PreviewViewerComponent } from '../../components/preview-viewer/preview-viewer.component';

@Component({
  selector: 'app-preview-test-page',
  standalone: true,
  imports: [CommonModule, FormsModule, PreviewViewerComponent],
  templateUrl: './preview-test.component.html',
})
export class PreviewTestComponent implements OnDestroy {
  @ViewChild('viewer') viewer?: PreviewViewerComponent;

  readonly defaultUrl = `http://${environment.ffmpegHost}:8090/preview.m3u8`;
  streamUrl = this.defaultUrl;
  status = '';
  error = '';

  useDefault(): void {
    this.streamUrl = this.defaultUrl;
    this.reload();
  }

  reload(): void {
    this.status = 'Loading stream…';
    this.error = '';
    this.viewer?.reload();
  }

  ngOnDestroy(): void {
  }
}
