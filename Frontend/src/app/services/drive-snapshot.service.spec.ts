/**
 * Tests for DriveSnapshotService (#571).
 *
 * The service polls ``GET /drives/snapshot`` to surface drives the
 * registry knows about regardless of media-presence. Coverage:
 *   - first poll fires immediately, returns rows on success
 *   - subsequent polls update the observable
 *   - HTTP errors fail soft (empty array, no observable error)
 *   - startPolling is idempotent
 *   - stopPolling halts further calls
 */
import {
  discardPeriodicTasks,
  fakeAsync,
  TestBed,
  tick,
} from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';

import {
  DriveSnapshotRow,
  DriveSnapshotService,
} from './drive-snapshot.service';
import { environment } from '../environments/environment';

const URL = `${environment.apiBase ?? ''}/drives/snapshot`;

const SAMPLE: DriveSnapshotRow[] = [
  {
    mount_point: '/dev/sr0',
    loaded: true,
    volume_label: 'VENOM_2018',
    media_kind: 'BD',
    by_id_serial: '1958040110900395',
    identity_source: 'by-id',
    multi_drive_safe: true,
    vendor: 'PIONEER',
    model: 'BD-RW BDR-XD06U',
    bus: 'usb',
  },
  {
    mount_point: '/dev/sr1',
    loaded: false,
    volume_label: null,
    media_kind: null,
    by_id_serial: 'AAAABBBB000E',
    identity_source: 'by-id',
    multi_drive_safe: true,
    vendor: 'ASUS',
    model: 'BW-16D1HT',
    bus: 'usb',
  },
];

describe('DriveSnapshotService', () => {
  let service: DriveSnapshotService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [DriveSnapshotService],
    });
    service = TestBed.inject(DriveSnapshotService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    service.stopPolling();
    httpMock.verify();
  });

  it('first poll fires immediately and exposes rows on the observable', fakeAsync(() => {
    let latest: DriveSnapshotRow[] = [];
    service.drives$.subscribe(rows => {
      latest = rows;
    });

    service.startPolling(10_000);
    tick(0);

    const req = httpMock.expectOne(URL);
    expect(req.request.method).toBe('GET');
    req.flush(SAMPLE);

    expect(latest.length).toBe(2);
    expect(service.current()[0].by_id_serial).toBe('1958040110900395');
    expect(service.current()[1].loaded).toBe(false);

    service.stopPolling();
    discardPeriodicTasks();
  }));

  it('polls again on the configured interval', fakeAsync(() => {
    service.startPolling(1_000);
    tick(0);
    httpMock.expectOne(URL).flush(SAMPLE);

    tick(1_000);
    httpMock.expectOne(URL).flush([SAMPLE[0]]);
    expect(service.current().length).toBe(1);

    service.stopPolling();
    discardPeriodicTasks();
  }));

  it('HTTP errors fail soft: observable stays subscribed, value becomes []', fakeAsync(() => {
    let emitCount = 0;
    let latest: DriveSnapshotRow[] = [];
    service.drives$.subscribe(rows => {
      emitCount++;
      latest = rows;
    });

    service.startPolling(1_000);
    tick(0);
    httpMock.expectOne(URL).flush('boom', { status: 500, statusText: 'Internal Server Error' });

    expect(latest).toEqual([] as DriveSnapshotRow[]);
    // Next tick — still polling
    tick(1_000);
    httpMock.expectOne(URL).flush(SAMPLE);
    expect(latest.length).toBe(2);

    // Initial seed (1, []) + error ([], same value not re-emitted by BehaviorSubject
    // when value is identical) + success (3 rows). At minimum we got >=2 emissions.
    expect(emitCount).toBeGreaterThanOrEqual(2);

    service.stopPolling();
    discardPeriodicTasks();
  }));

  it('startPolling is idempotent', fakeAsync(() => {
    service.startPolling(10_000);
    tick(0);
    httpMock.expectOne(URL).flush(SAMPLE);

    service.startPolling(10_000); // second call must NOT spawn another GET
    tick(0);
    const noopRequests = httpMock.match(URL);
    expect(noopRequests.length).toBe(0);

    service.stopPolling();
    discardPeriodicTasks();
  }));

  it('stopPolling halts further calls', fakeAsync(() => {
    service.startPolling(500);
    tick(0);
    httpMock.expectOne(URL).flush(SAMPLE);

    service.stopPolling();
    tick(2_000);
    const noopRequests = httpMock.match(URL);
    expect(noopRequests.length).toBe(0);
  }));
});
