/**
 * Frontend types for the self-healing library reattach endpoint (#449).
 *
 * Mirrors the backend's ``LibraryReattachReport`` / ``LibraryReattachMatch``
 * / ``LibraryReattachConflict`` Pydantic models in
 * ``Backend/api/schemas.py``. Both halves move together — if the backend
 * shape changes, update here in the same PR.
 *
 * Produced by ``POST /releases/library/reattach``. See the endpoint
 * docstring in ``Backend/api/routers/releases.py`` for the full
 * semantics; the short version is:
 *
 *   * deterministic_matches → segment_uid hits (the trust path)
 *   * heuristic_matches → filename / basename fallback for legacy rows
 *     whose segment_uid is NULL (pre-PR #451)
 *   * conflicts → one file matched multiple titles; skipped, operator
 *     disambiguates
 *   * orphan_files / orphan_titles → things that didn't match
 *
 * dry_run=true returns the report without writing; dry_run=false applies
 * the matches and re-returns the same shape with ``applied=true``.
 */

export type LibraryReattachTier = 'segment_uid' | 'filename' | 'uri' | 'hash';

export interface LibraryReattachMatch {
  title_id: string;
  /** Existing file_path on the DiscTitle row (null when the row never had
   * a file_path — e.g. fresh-import recovery scenario). */
  old_path: string | null;
  /** Absolute path of the on-disk MKV that matched. */
  new_path: string;
  tier: LibraryReattachTier;
}

export interface LibraryReattachConflict {
  file_path: string;
  candidate_title_ids: string[];
  tier: string;
}

export interface LibraryReattachReport {
  deterministic_matches: LibraryReattachMatch[];
  heuristic_matches: LibraryReattachMatch[];
  /** Files at the destination with no matching DiscTitle row. */
  orphan_files: string[];
  /** DiscTitle.id values with no on-disk match. */
  orphan_titles: string[];
  conflicts: LibraryReattachConflict[];
  /** The directory the endpoint walked (``config.transfer_dir`` resolved). */
  transfer_dir: string;
  dry_run: boolean;
  /** True when writes occurred (dry_run=false + at least one match). */
  applied: boolean;
}
