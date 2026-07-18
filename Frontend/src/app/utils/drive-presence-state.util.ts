/**
 * #571 — derive the three-state drive-presence label for the CTA.
 *
 * Used by ``WorkflowActionsComponent`` (the real CTA renderer) to surface
 * "Insert Disc" / "Drive Not Connected" instead of an enabled "Start Copy"
 * when the selected card refers to a disc that isn't currently loaded in
 * any attached drive.
 *
 * Pure function so unit tests don't need TestBed scaffolding. Callers
 * supply the three signals; the function returns the state.
 */

import { DriveSnapshotRow } from '../services/drive-snapshot.service';
import { DiscMetadata } from '../services/workflow.service';

export type DrivePresenceState = 'available' | 'drive-empty' | 'drive-missing';

export interface DrivePresenceInputs {
  /** Card the user has currently selected on the Ripper page. */
  selectedCard: { type: 'drive' | 'job'; id: string } | null;
  /** Unified disc metadata from the coordinator. */
  discs: ReadonlyArray<DiscMetadata>;
  /** Drive snapshot from ``GET /drives/snapshot`` (loaded + unloaded). */
  driveSnapshot: ReadonlyArray<DriveSnapshotRow>;
}

/**
 * Returns:
 *   'available'     — drive present AND the disc the selected card refers to
 *                     is currently loaded (or no card is selected at all).
 *   'drive-empty'   — at least one drive connected but the disc isn't loaded.
 *   'drive-missing' — no optical drives connected.
 *
 * Conservative defaults:
 *   - No selected card → 'available' (keep existing CTA behaviour).
 *   - Both inputs empty at first paint → 'available' (avoid flashing
 *     "Drive Not Connected" before the first snapshot poll lands).
 */
export function computeDrivePresenceState(
  inputs: DrivePresenceInputs,
): DrivePresenceState {
  const { selectedCard, discs, driveSnapshot } = inputs;

  if (!selectedCard) return 'available';

  // First paint — snapshot poll and coordinator have not landed yet. Stay
  // optimistic; the next emission will re-evaluate.
  if (driveSnapshot.length === 0 && discs.length === 0) return 'available';

  // ``selectedCard.id`` carries different identifiers depending on type:
  //   - type='drive': mount_point (the card-carousel keys drive cards on
  //     mount_point so they survive disc_id reassignment across rescans)
  //     or sometimes disc_id when the carousel was seeded from a server
  //     payload — accept both.
  //   - type='job': job_id. After the workflow.service.ts dedupe pass
  //     merges the failed-job entry into the in_drive disc, the in_drive
  //     disc inherits the failed job_id — so a job-card selection still
  //     resolves to the loaded disc via that key.
  const selectedDisc =
    selectedCard.type === 'drive'
      ? discs.find(
          d =>
            d.disc_id === selectedCard.id ||
            (d.mount_point && d.mount_point === selectedCard.id),
        )
      : discs.find(d => d.job_id === selectedCard.id);

  // 'in_drive' card: the disc IS already loaded by construction.
  if (selectedDisc?.disc_state === 'in_drive') return 'available';

  // 'unfinished' / 'job' card: cross-check the snapshot for a loaded disc
  // with the matching ``disc_hash``. ``disc_hash`` is the stable identity
  // across drive renumbering — same disc moved to a different /dev/sr*
  // still resolves.
  const targetHash = selectedDisc?.disc_hash || null;
  const anyLoadedMatchesHash = targetHash
    ? discs.some(d => d.disc_state === 'in_drive' && d.disc_hash === targetHash)
    : false;
  if (anyLoadedMatchesHash) return 'available';

  return driveSnapshot.length > 0 ? 'drive-empty' : 'drive-missing';
}
