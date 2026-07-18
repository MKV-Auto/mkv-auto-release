import type { DiscMetadata } from '../services/workflow.service';
import { carouselDiscSameIdentity } from './carousel-disc-identity.util';

/**
 * Drop unfinished job cards with job_status === 'failed' when another unfinished
 * card exists for the same disc (disc_id or normalized disc_hash) with a newer
 * created_at and a non-failed job_status. Mirrors GET /jobs/unfinished/summaries
 * so the carousel stays consistent if stale rows were merged into discs$.
 */
export function filterSupersededFailedCarouselDiscs(discs: DiscMetadata[]): DiscMetadata[] {
  const createdMs = (c: string | null | undefined): number => (c ? new Date(c).getTime() : 0);

  const unfinished = discs.filter((d) => d.disc_state === 'unfinished');
  const dropJobIds = new Set<string>();

  for (const failed of unfinished) {
    if (failed.job_status !== 'failed' || !failed.job_id) continue;
    const ft = createdMs(failed.created_at);
    for (const other of unfinished) {
      if (other.job_status === 'failed') continue;
      if (!carouselDiscSameIdentity(failed, other)) continue;
      if (createdMs(other.created_at) > ft) {
        dropJobIds.add(failed.job_id);
        break;
      }
    }
  }

  return discs.filter((d) => {
    if (d.disc_state !== 'unfinished' || d.job_status !== 'failed' || !d.job_id) return true;
    return !dropJobIds.has(d.job_id);
  });
}
