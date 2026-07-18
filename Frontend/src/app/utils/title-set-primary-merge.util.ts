/**
 * Merge rows returned from POST /discs/{id}/titles/{title_id}/set-primary into existing
 * workflow title objects. Server payload is _serialize_disc_title — no duplicate_info.
 */

const SET_PRIMARY_SERVER_FIELDS: (keyof Record<string, unknown>)[] = [
  'src',
  'source_file',
  'title_seq',
  'title',
  'edition',
  'description',
  'type',
  'season',
  'episode',
  'duration',
  'duration_raw',
  'size',
  'display_size',
  'comment',
  'order_index',
  'streams',
  'file_path',
  'file_path_stage',
  'active',
];

/**
 * Apply server fields from set-primary response onto an existing title; keep duplicateInfo,
 * metadata_scan, segment_map, and other client/enrichment fields not in the API row.
 */
export function mergeTitleFromSetPrimaryResponse(existing: any, server: any): any {
  if (!existing) return server;
  if (!server || typeof server !== 'object') return existing;
  const merged = { ...existing };
  for (const k of SET_PRIMARY_SERVER_FIELDS) {
    if (Object.prototype.hasOwnProperty.call(server, k)) {
      (merged as Record<string, unknown>)[k as string] = (server as Record<string, unknown>)[k as string];
    }
  }
  return merged;
}
