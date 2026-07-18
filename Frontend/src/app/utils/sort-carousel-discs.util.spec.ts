import type { DiscMetadata } from '../services/workflow.service';
import { sortCarouselDiscsForDisplay } from './sort-carousel-discs.util';

function unfinished(partial: Partial<DiscMetadata> & Pick<DiscMetadata, 'disc_id' | 'job_id'>): DiscMetadata {
  return {
    disc_state: 'unfinished',
    ...partial,
  } as DiscMetadata;
}

describe('sortCarouselDiscsForDisplay', () => {
  it('places all in_drive rows before unfinished rows', () => {
    const drive: DiscMetadata = {
      disc_id: 'd-drive',
      disc_state: 'in_drive',
      disc_num: '1',
      mount_point: '/dev/sr0',
    } as DiscMetadata;
    const job = unfinished({
      disc_id: 'd-job',
      job_id: 'job-1',
      job_status: 'running',
      created_at: '2026-04-01T00:00:00.000Z',
    });
    const out = sortCarouselDiscsForDisplay([job, drive]);
    expect(out.map((d) => d.disc_state)).toEqual(['in_drive', 'unfinished']);
  });

  it('sorts newer non-failed unfinished before older failed unlinked', () => {
    const olderFailed = unfinished({
      disc_id: 'disc-old-fail',
      disc_hash: 'ORPHAN1',
      job_id: 'job-failed',
      job_status: 'failed',
      created_at: '2026-04-01T00:00:00.000Z',
    });
    const newerRunning = unfinished({
      disc_id: 'disc-new-run',
      job_id: 'job-running',
      job_status: 'running',
      created_at: '2026-04-10T00:00:00.000Z',
    });
    const out = sortCarouselDiscsForDisplay([olderFailed, newerRunning]);
    expect(out.map((d) => d.job_id)).toEqual(['job-running', 'job-failed']);
  });

  it('does not deprioritize failed unfinished that matches an in_drive disc by disc_hash', () => {
    const drive: DiscMetadata = {
      disc_id: 'in-1',
      disc_state: 'in_drive',
      disc_num: '1',
      mount_point: '/dev/sr0',
      disc_hash: 'SHARED',
    } as DiscMetadata;
    const failedLinked = unfinished({
      disc_id: 'job-disc',
      disc_hash: 'SHARED',
      job_id: 'job-failed-linked',
      job_status: 'failed',
      created_at: '2026-04-10T00:00:00.000Z',
    });
    const runningOther = unfinished({
      disc_id: 'other',
      disc_hash: 'OTHER',
      job_id: 'job-running',
      job_status: 'running',
      created_at: '2026-04-01T00:00:00.000Z',
    });
    const out = sortCarouselDiscsForDisplay([drive, runningOther, failedLinked]);
    expect(out[0].disc_state).toBe('in_drive');
    const tail = out.slice(1).map((d) => d.job_id);
    expect(tail).toEqual(['job-failed-linked', 'job-running']);
  });

  it('sorts failed unlinked after non-failed unfinished even when failed is newer', () => {
    const olderRunning = unfinished({
      disc_id: 'disc-a',
      job_id: 'job-running',
      job_status: 'running',
      created_at: '2026-01-01T00:00:00.000Z',
    });
    const newerFailed = unfinished({
      disc_id: 'disc-b',
      disc_hash: 'NO_DRIVE',
      job_id: 'job-failed',
      job_status: 'failed',
      created_at: '2026-06-01T00:00:00.000Z',
    });
    const out = sortCarouselDiscsForDisplay([newerFailed, olderRunning]);
    expect(out.map((d) => d.job_id)).toEqual(['job-running', 'job-failed']);
  });

  it('matches in_drive by disc_id for failed unfinished linkage', () => {
    const drive: DiscMetadata = {
      disc_id: 'shared-id',
      disc_state: 'in_drive',
      mount_point: '/dev/sr0',
    } as DiscMetadata;
    const failedSameId = unfinished({
      disc_id: 'shared-id',
      job_id: 'job-f',
      job_status: 'failed',
      created_at: '2026-04-20T00:00:00.000Z',
    });
    const runningOther = unfinished({
      disc_id: 'other-id',
      job_id: 'job-r',
      job_status: 'running',
      created_at: '2026-01-01T00:00:00.000Z',
    });
    const out = sortCarouselDiscsForDisplay([drive, runningOther, failedSameId]);
    expect(out.map((d) => d.disc_state)).toEqual(['in_drive', 'unfinished', 'unfinished']);
    // Linked failed is group 1: newest first vs running
    expect(out[1].job_id).toBe('job-f');
    expect(out[2].job_id).toBe('job-r');
  });
});
