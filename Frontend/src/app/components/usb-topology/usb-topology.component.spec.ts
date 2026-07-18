/**
 * Tests for UsbTopologyComponent — Settings page USB bus topology section (#578).
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { BehaviorSubject, of } from 'rxjs';

import { UsbTopologyComponent } from './usb-topology.component';
import {
  UsbTopology,
  UsbTopologyService,
} from '../../services/usb-topology.service';


class MockUsbTopologyService {
  private readonly _topology$ = new BehaviorSubject<UsbTopology>({ drives: [], warnings: [] });
  private readonly _loading$ = new BehaviorSubject<boolean>(false);
  private readonly _error$ = new BehaviorSubject<string | null>(null);

  topology$ = this._topology$.asObservable();
  loading$ = this._loading$.asObservable();
  error$ = this._error$.asObservable();

  refreshCalls = 0;
  refresh = jasmine.createSpy('refresh').and.callFake(() => {
    this.refreshCalls++;
    return of(this._topology$.value);
  });

  /** Test helper: seed the observable. */
  setTopology(t: UsbTopology): void { this._topology$.next(t); }
  setError(err: string | null): void { this._error$.next(err); }
}


describe('UsbTopologyComponent', () => {
  let fixture: ComponentFixture<UsbTopologyComponent>;
  let component: UsbTopologyComponent;
  let svc: MockUsbTopologyService;

  beforeEach(async () => {
    svc = new MockUsbTopologyService();
    await TestBed.configureTestingModule({
      imports: [UsbTopologyComponent],
      providers: [{ provide: UsbTopologyService, useValue: svc }],
    }).compileComponents();
    fixture = TestBed.createComponent(UsbTopologyComponent);
    component = fixture.componentInstance;
  });

  it('triggers a refresh on init', () => {
    fixture.detectChanges();
    expect(svc.refresh).toHaveBeenCalled();
  });

  describe('groupByBus', () => {
    it('groups drives by bus number and labels each with bus speed', () => {
      const topology: UsbTopology = {
        drives: [
          { bus: 2, speed_mbps: 480, product: 'Pioneer', manufacturer: 'P', serial: 'A', sysfs_path: '' },
          { bus: 2, speed_mbps: 480, product: 'ASUS', manufacturer: 'A', serial: 'B', sysfs_path: '' },
          { bus: 3, speed_mbps: 5000, product: 'LG', manufacturer: 'L', serial: 'C', sysfs_path: '' },
        ],
        warnings: [],
      };

      const groups = component.groupByBus(topology);

      expect(groups.length).toBe(2);
      const bus2 = groups.find(g => g.bus === 2)!;
      const bus3 = groups.find(g => g.bus === 3)!;
      expect(bus2.drives.length).toBe(2);
      expect(bus3.drives.length).toBe(1);
      expect(bus2.speed_mbps).toBe(480);
      expect(bus3.speed_mbps).toBe(5000);
      expect(bus2.warning).toBeNull();
      expect(bus3.warning).toBeNull();
    });

    it('attaches the matching warning to its bus group', () => {
      const topology: UsbTopology = {
        drives: [
          { bus: 2, speed_mbps: 480, product: 'Pioneer', manufacturer: 'P', serial: 'A', sysfs_path: '' },
          { bus: 2, speed_mbps: 480, product: 'ASUS', manufacturer: 'A', serial: 'B', sysfs_path: '' },
        ],
        warnings: [{
          bus: 2,
          speed_mbps: 480,
          drive_count: 2,
          drives: ['Pioneer', 'ASUS'],
          message: 'Bus 2 ... #578',
        }],
      };

      const groups = component.groupByBus(topology);

      expect(groups[0].warning?.bus).toBe(2);
      expect(groups[0].warning?.message).toContain('Bus 2');
    });

    it('sorts contended buses first, then by bus number', () => {
      const topology: UsbTopology = {
        drives: [
          { bus: 4, speed_mbps: 5000, product: 'Clean', manufacturer: '', serial: '1', sysfs_path: '' },
          { bus: 2, speed_mbps: 480, product: 'A', manufacturer: '', serial: '2', sysfs_path: '' },
          { bus: 2, speed_mbps: 480, product: 'B', manufacturer: '', serial: '3', sysfs_path: '' },
          { bus: 3, speed_mbps: 480, product: 'Single', manufacturer: '', serial: '4', sysfs_path: '' },
        ],
        warnings: [
          { bus: 2, speed_mbps: 480, drive_count: 2, drives: ['A', 'B'], message: 'contended' },
        ],
      };

      const groups = component.groupByBus(topology);

      // Bus 2 (contended) comes first; then bus 3, then bus 4 (both clean).
      expect(groups.map(g => g.bus)).toEqual([2, 3, 4]);
    });

    it('handles empty topology', () => {
      const groups = component.groupByBus({ drives: [], warnings: [] });
      expect(groups).toEqual([]);
    });

    it('uses max speed when sysfs reports different speeds for siblings', () => {
      const topology: UsbTopology = {
        drives: [
          { bus: 2, speed_mbps: 480, product: 'A', manufacturer: '', serial: '1', sysfs_path: '' },
          { bus: 2, speed_mbps: 12, product: 'B', manufacturer: '', serial: '2', sysfs_path: '' },
        ],
        warnings: [],
      };
      expect(component.groupByBus(topology)[0].speed_mbps).toBe(480);
    });
  });

  describe('formatSpeed', () => {
    it('labels SuperSpeed Plus (≥10000 Mbps)', () => {
      expect(component.formatSpeed(10000)).toContain('USB 3.1+');
      expect(component.formatSpeed(20000)).toContain('USB 3.1+');
    });

    it('labels SuperSpeed (5000 Mbps)', () => {
      expect(component.formatSpeed(5000)).toContain('USB 3.0');
      expect(component.formatSpeed(5000)).toContain('SuperSpeed');
    });

    it('labels USB 2.0 (480 Mbps)', () => {
      expect(component.formatSpeed(480)).toContain('USB 2.0');
    });

    it('labels USB 1.1 (12 Mbps)', () => {
      expect(component.formatSpeed(12)).toContain('USB 1.1');
    });

    it('handles unknown speeds (< 12 Mbps)', () => {
      expect(component.formatSpeed(1)).toBe('1 Mbps');
    });
  });

  describe('refresh button', () => {
    it('calls svc.refresh when refresh() is invoked', () => {
      const initialCalls = svc.refreshCalls;
      component.refresh();
      expect(svc.refreshCalls).toBe(initialCalls + 1);
    });
  });

  describe('rendering', () => {
    it('renders an empty state when no drives detected', () => {
      svc.setTopology({ drives: [], warnings: [] });
      fixture.detectChanges();
      const text = (fixture.nativeElement as HTMLElement).textContent || '';
      expect(text).toContain('No optical drives detected');
    });

    it('renders the warning banner above a contended bus', () => {
      svc.setTopology({
        drives: [
          { bus: 2, speed_mbps: 480, product: 'Pioneer Blu-ray', manufacturer: 'P', serial: 'A', sysfs_path: '' },
          { bus: 2, speed_mbps: 480, product: 'ASUS External', manufacturer: 'A', serial: 'B', sysfs_path: '' },
        ],
        warnings: [{
          bus: 2,
          speed_mbps: 480,
          drive_count: 2,
          drives: ['Pioneer Blu-ray', 'ASUS External'],
          message: 'USB Bus 2 (480 Mbps) hosts 2 optical drives. ... #578',
        }],
      });
      fixture.detectChanges();
      const text = (fixture.nativeElement as HTMLElement).textContent || '';
      expect(text).toContain('USB Bus 2');
      expect(text).toContain('hosts 2 optical drives');
      expect(text).toContain('Pioneer Blu-ray');
      expect(text).toContain('ASUS External');
    });

    it('renders error banner when service reports an error', () => {
      svc.setError('Failed to fetch');
      fixture.detectChanges();
      const text = (fixture.nativeElement as HTMLElement).textContent || '';
      expect(text).toContain('Failed to fetch');
    });
  });
});
