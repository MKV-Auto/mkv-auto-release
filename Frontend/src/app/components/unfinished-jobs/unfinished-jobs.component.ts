import { Component, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { JobStatus } from '../../services/job.service';

@Component({
  selector: 'app-unfinished-jobs',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './unfinished-jobs.component.html',
  styleUrls: ['./unfinished-jobs.component.scss'],
})
export class UnfinishedJobsComponent {
  @Input() jobs: JobStatus[] = [];
  @Input() currentJobId: string | null = null;
  @Input() movieOptions: any[] = [];

  @Output() jobSelected = new EventEmitter<string>();

  onJobClick(jobId: string): void {
    this.jobSelected.emit(jobId);
  }

  getJobCardTitle(job: JobStatus): string {
    // Priority 1: Use movie_name from job status (from linked release/movie)
    if (job.movie_name) {
      return job.movie_name;
    }
    
    // Priority 2: Try to get from disc_payload or label_draft (fallback)
    const payload = job.disc_payload?.label_payload || job.label_draft || null;
    if (payload) {
      const movieName = payload.movie_name || 
                       payload.release_name ||
                       payload.show_title ||
                       null;
      if (movieName) {
        return movieName;
      }
    }

    return 'Unknown Disc';
  }

  getJobProductionYear(job: JobStatus): string | null {
    // Priority 1: Use production_year from job status (from linked movie)
    if (job.production_year) {
      const result = String(job.production_year);
      return result;
    }

    // Priority 2: Fallback to payload data
    const payload = job.disc_payload?.label_payload || job.label_draft || null;
    if (!payload) {
      return null;
    }

    // Priority 3: Look up from movie options if we have a movie_id
    if (payload.movie_id && this.movieOptions.length > 0) {
      const movie = this.movieOptions.find(m => m.id === payload.movie_id);
      if (movie?.production_year) {
        return String(movie.production_year);
      }
    }

    // Priority 4: Check payload for production_year
    if (payload.production_year) {
      return String(payload.production_year);
    }

    // Priority 5: Check for release_year as fallback (but this is boxset year, not production year)
    if (payload.release_year) {
      const result = String(payload.release_year);
      return result;
    }

    return null;
  }

  getJobResolutionFormat(job: JobStatus): string | null {
    const parts: string[] = [];
    
    // Priority 1: Use resolution from job status (from disc record)
    if (job.resolution) {
      parts.push(job.resolution);
    }
    
    // Priority 2: Fallback to payload data
    const payload = job.disc_payload?.label_payload || job.disc_payload || null;
    if (payload) {
      const resolution = payload.resolution || null;
      const discFormat = payload.disc_format || payload.format || null;
      
      if (resolution && !job.resolution) {
        parts.push(resolution);
      }
      if (discFormat) {
        parts.push(discFormat);
      }
    }

    return parts.length > 0 ? parts.join(' ') : null;
  }

  getJobStage(job: JobStatus): string {
    // Check pipeline states to determine current stage
    const pipeline = job.pipeline || {};
    const transferState = (job.transfer_state || pipeline['transfer'] || '').toLowerCase();
    const postState = (job.post_state || pipeline['postprocess'] || '').toLowerCase();
    const labelState = (job.label_state || pipeline['label'] || '').toLowerCase();
    const ripState = (job.rip_state || pipeline['rip'] || job.job_status || '').toLowerCase();

    // Check transfer state first
    if (transferState === 'running' || transferState === 'pending') {
      return 'Transferring';
    }

    // Check post-processing state
    const ripDone = ripState === 'completed' || ripState === 'skipped';
    if (ripDone && (postState === 'running' || postState === 'pending' || (job.post_progress && job.post_progress > 0))) {
      return 'Post-processing';
    }

    // Check labeling state (only if rip is done)
    if (ripDone && (labelState === 'running' || labelState === 'pending')) {
      return 'Labeling';
    }

    // Default: If rip is done but no other stage is active, likely waiting or completed
    if (ripDone) {
      return 'Waiting';
    }

    // Rip stage
    return 'Ripping';
  }

  getJobInfoLine(job: JobStatus): string {
    const parts: string[] = [];
    
    const year = this.getJobProductionYear(job);
    if (year) {
      parts.push(`(${year})`);
    }

    const resolutionFormat = this.getJobResolutionFormat(job);
    if (resolutionFormat) {
      parts.push(resolutionFormat);
    }

    const stage = this.getJobStage(job);
    parts.push(stage);

    return parts.join(' · ');
  }

  getJobMeta(job: JobStatus): string {
    const parts: string[] = [];
    
    const year = this.getJobProductionYear(job);
    if (year) {
      parts.push(`(${year})`);
    }

    const resolutionFormat = this.getJobResolutionFormat(job);
    if (resolutionFormat) {
      parts.push(resolutionFormat);
    }

    return parts.length > 0 ? parts.join(' · ') : '—';
  }
}

