/**
 * Unit tests for ``computeDrivePresenceState`` (#571).
 *
 * Pure-function tests, no TestBed scaffolding required.
 */
import {
  computeDrivePresenceState,
  DrivePresenceInputs,
} from './drive-presence-state.util';
import { DriveSnapshotRow } from '../services/drive-snapshot.service';

const driveRow = (mount: string, loaded: boolean): DriveSnapshotRow => ({
  mount_point: mount,
  loaded,
  volume_label: null,
  media_kind: loaded ? 'BD' : null,
  by_id_serial: 'X',
  identity_source: 'by-id',
  multi_drive_safe: true,
  vendor: 'V',
  model: 'M',
  bus: 'usb',
});

const unfinishedDisc = (hash: string | null) =>
  ({
    disc_id: 'disc-failed-1',
    disc_state: 'unfinished' as const,
    disc_hash: hash,
    job_id: 'job-1',
    job_status: 'failed',
    mount_point: '/dev/sr0',
  } as any);

const inDriveDisc = (hash: string) =>
  ({
    disc_id: 'disc-loaded',
    disc_state: 'in_drive' as const,
    disc_hash: hash,
    mount_point: '/dev/sr0',
  } as any);

const baseInputs = (
  overrides: Partial<DrivePresenceInputs>,
): DrivePresenceInputs => ({
  selectedCard: { type: 'job', id: 'disc-failed-1' },
  discs: [unfinishedDisc('HASH-FAILED')],
  driveSnapshot: [],
  ...overrides,
});

describe('computeDrivePresenceState', () => {
  it('returns "available" when no card is selected', () => {
    expect(
      computeDrivePresenceState(baseInputs({ selectedCard: null })),
    ).toBe('available');
  });

  it('returns "available" when both inputs are empty (first paint)', () => {
    expect(
      computeDrivePresenceState(
        baseInputs({ discs: [], driveSnapshot: [], selectedCard: { type: 'job', id: 'x' } }),
      ),
    ).toBe('available');
  });

  it('returns "available" when matching disc is currently loaded (post-dedupe carries job_id)', () => {
    // Realistic post-dedupe shape: the unfinished record is dropped and the
    // in_drive disc inherits the failed job_id. selectedCard is still the
    // type='job' card persisted from before the dedupe.
    const inputs = baseInputs({
      selectedCard: { type: 'job', id: 'job-1' },
      discs: [
        {
          disc_id: 'disc-fallout',
          disc_state: 'in_drive' as const,
          disc_hash: 'HASH-FAILED',
          job_id: 'job-1', // carried over by dedupe
          mount_point: '/dev/sr0',
        } as any,
      ],
      driveSnapshot: [driveRow('/dev/sr0', true)],
    });
    expect(computeDrivePresenceState(inputs)).toBe('available');
  });

  it('returns "available" when the selected card itself is in_drive', () => {
    const inputs = baseInputs({
      selectedCard: { type: 'drive', id: 'disc-loaded' },
      discs: [inDriveDisc('HASH-LIVE')],
      driveSnapshot: [driveRow('/dev/sr0', true)],
    });
    expect(computeDrivePresenceState(inputs)).toBe('available');
  });

  it('returns "available" when type="drive" card.id is a mount_point', () => {
    // The card-carousel keys drive cards on mount_point so they survive
    // disc_id changes across rescans. selectedCard.id == '/dev/sr1' must
    // resolve to the disc currently at that mount.
    const inputs = baseInputs({
      selectedCard: { type: 'drive', id: '/dev/sr1' },
      discs: [
        {
          disc_id: 'disc-fallout',
          disc_state: 'in_drive' as const,
          disc_hash: 'HASH-FALLOUT',
          mount_point: '/dev/sr1',
        } as any,
      ],
      driveSnapshot: [driveRow('/dev/sr1', true)],
    });
    expect(computeDrivePresenceState(inputs)).toBe('available');
  });

  it('returns "drive-empty" when drive connected but disc not loaded', () => {
    expect(
      computeDrivePresenceState(
        baseInputs({ driveSnapshot: [driveRow('/dev/sr0', false)] }),
      ),
    ).toBe('drive-empty');
  });

  it('returns "drive-empty" when drive has a DIFFERENT disc loaded', () => {
    const inputs = baseInputs({
      discs: [unfinishedDisc('HASH-FAILED'), inDriveDisc('HASH-OTHER')],
      driveSnapshot: [driveRow('/dev/sr0', true)],
    });
    expect(computeDrivePresenceState(inputs)).toBe('drive-empty');
  });

  it('returns "drive-missing" when no drives connected at all', () => {
    expect(
      computeDrivePresenceState(
        baseInputs({ driveSnapshot: [] }),
      ),
    ).toBe('drive-missing');
  });

  it('handles unknown selectedCard.id (no matching disc) — still gates correctly', () => {
    // selectedCard.id doesn't match any disc → targetHash is null →
    // anyLoadedMatchesHash is false → fall through to drive-presence check.
    const inputs = baseInputs({
      selectedCard: { type: 'job', id: 'unknown-id' },
      discs: [],
      driveSnapshot: [driveRow('/dev/sr0', false)],
    });
    expect(computeDrivePresenceState(inputs)).toBe('drive-empty');
  });

  it('handles unfinished card with null disc_hash — defers to drive presence', () => {
    const inputs = baseInputs({
      discs: [unfinishedDisc(null)],
      driveSnapshot: [],
    });
    expect(computeDrivePresenceState(inputs)).toBe('drive-missing');
  });

  it('resolves type="job" cards by job_id (post-dedupe in_drive carries job_id)', () => {
    // After dedupe, the unfinished entry is removed and the in_drive disc
    // inherits the failed job_id. The saved selectedCard still has
    // type='job', id=<job_id> — we must find the disc by job_id, not
    // disc_id, and recognize it as in_drive.
    const inDriveWithFailedJob = {
      disc_id: 'disc-fallout',
      disc_state: 'in_drive' as const,
      disc_hash: 'HASH-FALLOUT',
      job_id: 'failed-job-uuid',
      job_status: 'failed',
      mount_point: '/dev/sr1',
    } as any;
    const inputs = baseInputs({
      selectedCard: { type: 'job', id: 'failed-job-uuid' },
      discs: [inDriveWithFailedJob],
      driveSnapshot: [driveRow('/dev/sr1', true)],
    });
    expect(computeDrivePresenceState(inputs)).toBe('available');
  });

  it('type="job" card with no matching in_drive disc still gates correctly', () => {
    // The failed job's disc is NOT loaded anywhere; one other drive
    // currently carries some unrelated disc. Should report drive-empty.
    const inputs = baseInputs({
      selectedCard: { type: 'job', id: 'failed-job-uuid' },
      discs: [{
        disc_id: 'disc-other',
        disc_state: 'in_drive' as const,
        disc_hash: 'HASH-OTHER',
        job_id: null,
        mount_point: '/dev/sr0',
      } as any],
      driveSnapshot: [driveRow('/dev/sr0', true)],
    });
    expect(computeDrivePresenceState(inputs)).toBe('drive-empty');
  });
});
