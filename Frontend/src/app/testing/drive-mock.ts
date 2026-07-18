/**
 * Shared drive/disc mocks for unit and component specs.
 * Use for DriveService, WorkflowService.getDrives$(), and CardCarousel discs$.
 */
import { of, Observable } from 'rxjs';
import { Drive } from '../services/drive.service';
import { DiscMetadata } from '../services/workflow.service';

/** Drive[] for getDrives$ / DriveService-style tests */
export const MOCK_DRIVES: Drive[] = [
  { disc_num: '1', mount_point: '/dev/sr0', name: 'Drive 1' },
  { disc_num: '2', mount_point: '/dev/sr1', name: 'Drive 2' },
];

/** DiscMetadata[] with in_drive entries for CardCarousel discs$ (drive cards) */
export const MOCK_DISC_METADATA: DiscMetadata[] = [
  {
    disc_id: 'd1',
    disc_num: '1',
    mount_point: '/dev/sr0',
    disc_state: 'in_drive',
    scan_state: 'ready',
    movie_name: 'Test Movie',
    resolution: '1080p',
  } as DiscMetadata,
  {
    disc_id: 'd2',
    disc_num: '2',
    mount_point: '/dev/sr1',
    disc_state: 'in_drive',
    scan_state: 'ready',
    info_title: 'Second Drive',
  } as DiscMetadata,
];

/** Observable of MOCK_DRIVES for getDrives$() stubs */
export function createMockDrives$(): Observable<Drive[]> {
  return of(MOCK_DRIVES);
}

/** Observable of MOCK_DISC_METADATA for discs$ stubs (drive cards in carousel) */
export function createMockDiscs$(): Observable<DiscMetadata[]> {
  return of(MOCK_DISC_METADATA);
}
