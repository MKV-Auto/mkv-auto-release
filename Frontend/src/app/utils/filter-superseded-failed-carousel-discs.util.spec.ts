import type { DiscMetadata } from '../services/workflow.service';
import { filterSupersededFailedCarouselDiscs } from './filter-superseded-failed-carousel-discs.util';

function job(partial: Partial<DiscMetadata> & Pick<DiscMetadata, 'disc_id' | 'job_id'>): DiscMetadata {
  return {
    disc_state: 'unfinished',
    ...partial,
  } as DiscMetadata;
}

describe('filterSupersededFailedCarouselDiscs', () => {
  it('removes failed when a newer non-failed unfinished job shares disc_id', () => {
    const discs: DiscMetadata[] = [
      job({
        disc_id: 'disc-1',
        job_id: 'job-failed',
        job_status: 'failed',
        created_at: '2026-04-10T12:00:00.000Z',
      }),
      job({
        disc_id: 'disc-1',
        job_id: 'job-active',
        job_status: 'running',
        created_at: '2026-04-11T12:00:00.000Z',
      }),
    ];
    const out = filterSupersededFailedCarouselDiscs(discs);
    expect(out.map((d) => d.job_id)).toEqual(['job-active']);
  });

  it('removes failed when a newer non-failed job matches on disc_hash', () => {
    const discs: DiscMetadata[] = [
      job({
        disc_id: 'disc-a',
        disc_hash: 'abc123',
        job_id: 'job-failed',
        job_status: 'failed',
        created_at: '2026-04-10T12:00:00.000Z',
      }),
      job({
        disc_id: 'disc-b',
        disc_hash: 'abc123',
        job_id: 'job-active',
        job_status: 'running',
        created_at: '2026-04-11T12:00:00.000Z',
      }),
    ];
    const out = filterSupersededFailedCarouselDiscs(discs);
    expect(out.map((d) => d.job_id)).toEqual(['job-active']);
  });

  it('keeps failed when it is the only unfinished job', () => {
    const discs: DiscMetadata[] = [
      job({
        disc_id: 'disc-1',
        job_id: 'job-failed',
        job_status: 'failed',
        created_at: '2026-04-10T12:00:00.000Z',
      }),
    ];
    expect(filterSupersededFailedCarouselDiscs(discs)).toEqual(discs);
  });

  it('keeps failed when non-failed is older (edge case)', () => {
    const discs: DiscMetadata[] = [
      job({
        disc_id: 'disc-1',
        job_id: 'job-active',
        job_status: 'running',
        created_at: '2026-04-09T12:00:00.000Z',
      }),
      job({
        disc_id: 'disc-1',
        job_id: 'job-failed',
        job_status: 'failed',
        created_at: '2026-04-12T12:00:00.000Z',
      }),
    ];
    const out = filterSupersededFailedCarouselDiscs(discs);
    expect(out.length).toBe(2);
  });

  it('preserves in_drive entries', () => {
    const discs: DiscMetadata[] = [
      {
        disc_id: 'empty-1',
        disc_state: 'in_drive',
        disc_num: '1',
        mount_point: '/dev/sr0',
      } as DiscMetadata,
      job({
        disc_id: 'disc-1',
        job_id: 'job-failed',
        job_status: 'failed',
        created_at: '2026-04-10T12:00:00.000Z',
      }),
      job({
        disc_id: 'disc-1',
        job_id: 'job-active',
        job_status: 'running',
        created_at: '2026-04-11T12:00:00.000Z',
      }),
    ];
    const out = filterSupersededFailedCarouselDiscs(discs);
    expect(out.some((d) => d.disc_state === 'in_drive')).toBe(true);
    expect(out.some((d) => d.job_id === 'job-failed')).toBe(false);
  });
});
