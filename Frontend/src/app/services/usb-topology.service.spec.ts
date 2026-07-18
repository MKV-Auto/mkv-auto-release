/**
 * Tests for UsbTopologyService — wraps GET /drives/usb-topology (#578).
 */
import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';

import {
  UsbTopologyService,
  UsbTopology,
  UsbOpticalDrive,
  UsbBusContentionWarning,
} from './usb-topology.service';
import { environment } from '../environments/environment';

const URL = `${environment.apiBase ?? ''}/drives/usb-topology`;

const SAMPLE: UsbTopology = {
  drives: [
    {
      bus: 2,
      speed_mbps: 480,
      product: 'Pioneer Blu-ray Drive',
      manufacturer: 'Pioneer Corporation',
      serial: '1958040110900395',
      sysfs_path: '/sys/bus/usb/devices/2-1',
    },
    {
      bus: 2,
      speed_mbps: 480,
      product: 'External Drive',
      manufacturer: 'ASUSTek',
      serial: 'AAAABBBB000E',
      sysfs_path: '/sys/bus/usb/devices/2-3',
    },
  ],
  warnings: [
    {
      bus: 2,
      speed_mbps: 480,
      drive_count: 2,
      drives: ['Pioneer Blu-ray Drive', 'External Drive'],
      message: 'USB Bus 2 (480 Mbps) hosts 2 optical drives. ... #578',
    },
  ],
};


describe('UsbTopologyService', () => {
  let service: UsbTopologyService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [UsbTopologyService],
    });
    service = TestBed.inject(UsbTopologyService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('exposes empty topology before refresh', () => {
    expect(service.current().drives).toEqual([]);
    expect(service.current().warnings).toEqual([]);
  });

  it('refresh fetches the endpoint and updates observable + current()', done => {
    service.topology$.subscribe(topology => {
      if (topology.drives.length > 0) {
        expect(topology.drives.length).toBe(2);
        expect(topology.warnings.length).toBe(1);
        expect(service.current().warnings[0].bus).toBe(2);
        done();
      }
    });

    service.refresh().subscribe();
    const req = httpMock.expectOne(URL);
    expect(req.request.method).toBe('GET');
    req.flush(SAMPLE);
  });

  it('toggles loading$ during fetch', done => {
    const states: boolean[] = [];
    service.loading$.subscribe(v => {
      states.push(v);
      if (states.length === 3) {
        expect(states).toEqual([false, true, false]);
        done();
      }
    });
    service.refresh().subscribe();
    httpMock.expectOne(URL).flush(SAMPLE);
  });

  it('fails soft on HTTP error: observable carries empty topology + error string', done => {
    service.refresh().subscribe(value => {
      expect(value.drives).toEqual([]);
      expect(value.warnings).toEqual([]);
      service.error$.subscribe(err => {
        // HttpClient's err.message is the canonical fallback; the response
        // body ('Boom' here) lands in err.error. We use err.message when
        // err.error.detail isn't present — assert on the HttpClient format.
        expect(err).toContain('500');
        done();
      });
    });
    httpMock
      .expectOne(URL)
      .flush('Boom', { status: 500, statusText: 'Internal Server Error' });
  });

  it('clears error on a successful refresh after a previous failure', done => {
    // First call: fails.
    service.refresh().subscribe();
    httpMock
      .expectOne(URL)
      .flush('Boom', { status: 500, statusText: 'Internal Server Error' });

    let sawErrorThenCleared = false;
    let lastError: string | null = '';
    service.error$.subscribe(err => {
      if (err && !sawErrorThenCleared) lastError = err;
      if (lastError && err === null) {
        sawErrorThenCleared = true;
        done();
      }
    });

    // Second call: succeeds — error should clear.
    service.refresh().subscribe();
    httpMock.expectOne(URL).flush(SAMPLE);
  });
});
