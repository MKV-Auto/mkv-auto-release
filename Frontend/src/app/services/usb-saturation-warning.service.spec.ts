/**
 * Tests for UsbSaturationWarningService — modal state + confirm-callback
 * plumbing for the #578 USB-bus-saturation gate.
 */
import { TestBed } from '@angular/core/testing';

import {
  UsbSaturationModalState,
  UsbSaturationWarningPayload,
  UsbSaturationWarningService,
} from './usb-saturation-warning.service';


const SAMPLE_PAYLOAD: UsbSaturationWarningPayload = {
  bus: 2,
  speed_mbps: 480,
  competing_mount_points: ['/dev/sr1'],
  message: 'USB Bus 2 (480 Mbps) already hosts an active rip on /dev/sr1...',
  override_field: 'force_concurrent_on_saturated_bus',
};


describe('UsbSaturationWarningService', () => {
  let service: UsbSaturationWarningService;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    service = TestBed.inject(UsbSaturationWarningService);
  });

  it('starts with null state', () => {
    expect(service.current()).toBeNull();
  });

  it('open() emits the payload and stores the confirm callback', done => {
    const cb = jasmine.createSpy('onConfirm');
    let states: (UsbSaturationModalState | null)[] = [];
    service.state$.subscribe(s => states.push(s));

    service.open(SAMPLE_PAYLOAD, cb);

    expect(states[states.length - 1]?.payload.bus).toBe(2);
    expect(cb).not.toHaveBeenCalled();  // not invoked yet
    done();
  });

  it('confirm() invokes the callback and clears state', () => {
    const cb = jasmine.createSpy('onConfirm');
    service.open(SAMPLE_PAYLOAD, cb);

    service.confirm();

    expect(cb).toHaveBeenCalledTimes(1);
    expect(service.current()).toBeNull();
  });

  it('dismiss() clears state WITHOUT invoking callback', () => {
    const cb = jasmine.createSpy('onConfirm');
    service.open(SAMPLE_PAYLOAD, cb);

    service.dismiss();

    expect(cb).not.toHaveBeenCalled();
    expect(service.current()).toBeNull();
  });

  it('confirm() twice after one open() fires callback only once', () => {
    const cb = jasmine.createSpy('onConfirm');
    service.open(SAMPLE_PAYLOAD, cb);

    service.confirm();
    service.confirm();

    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('a stale callback from a previous open is not invoked on the next confirm', () => {
    const firstCb = jasmine.createSpy('firstCb');
    const secondCb = jasmine.createSpy('secondCb');

    service.open(SAMPLE_PAYLOAD, firstCb);
    service.dismiss();  // user cancels first dialog
    service.open(SAMPLE_PAYLOAD, secondCb);
    service.confirm();

    expect(firstCb).not.toHaveBeenCalled();
    expect(secondCb).toHaveBeenCalledTimes(1);
  });
});
