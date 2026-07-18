import type { DiscMetadata } from '../services/workflow.service';
import { carouselDiscSameIdentity } from './carousel-disc-identity.util';

const createdMs = (c: string | null | undefined): number => (c ? new Date(c).getTime() : 0);

/**
 * Order discs for the ripper card carousel: in_drive first (stable relative order),
 * then unfinished with newest first, except unfinished failed jobs with no matching
 * inserted disc are sorted after all other unfinished rows.
 */
export function sortCarouselDiscsForDisplay(discs: DiscMetadata[]): DiscMetadata[] {
  const copy = [...discs];
  const inDrive = copy.filter((d) => d.disc_state === 'in_drive');

  const isFailedUnlinkedUnfinished = (d: DiscMetadata): boolean =>
    d.disc_state === 'unfinished' &&
    d.job_status === 'failed' &&
    !inDrive.some((drv) => carouselDiscSameIdentity(d, drv));

  return copy.sort((a, b) => {
    if (a.disc_state === 'in_drive' && b.disc_state === 'unfinished') {
      return -1;
    }
    if (a.disc_state === 'unfinished' && b.disc_state === 'in_drive') {
      return 1;
    }

    if (a.disc_state === 'unfinished' && b.disc_state === 'unfinished') {
      const aTail = isFailedUnlinkedUnfinished(a);
      const bTail = isFailedUnlinkedUnfinished(b);
      if (aTail !== bTail) {
        return aTail ? 1 : -1;
      }
      return createdMs(b.created_at) - createdMs(a.created_at);
    }

    return 0;
  });
}
