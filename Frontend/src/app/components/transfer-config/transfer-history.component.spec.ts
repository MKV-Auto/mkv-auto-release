import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { TransferHistoryComponent } from './transfer-history.component';
import { SystemService, TransferHistorySummary } from '../../services/system.service';

function row(over: Partial<TransferHistorySummary>): TransferHistorySummary {
  return {
    id: 'tx-' + Math.random().toString(36).slice(2),
    job_id: null,
    transfer_config_id: 'cfg-1',
    mode: 'smb',
    source_path: '/data/mkvauto/data/jobs/foo',
    destination_path: '/share/Movies/foo',
    status: 'completed',
    bytes_transferred: 1024 * 1024 * 1024,
    transfer_duration_seconds: 30,
    average_speed_mbps: 100,
    verification_status: null,
    was_deduplicated: false,
    created_at: '2026-04-12T15:43:22Z',
    ...over,
  };
}

describe('TransferHistoryComponent (KPI derivation — #592)', () => {
  let component: TransferHistoryComponent;
  let fixture: ComponentFixture<TransferHistoryComponent>;
  let mockSystem: { getTransferHistory: jasmine.Spy };

  beforeEach(async () => {
    mockSystem = {
      getTransferHistory: jasmine.createSpy('getTransferHistory').and.returnValue(of([])),
    };
    await TestBed.configureTestingModule({
      imports: [TransferHistoryComponent],
      providers: [{ provide: SystemService, useValue: mockSystem }],
    }).compileComponents();
    fixture = TestBed.createComponent(TransferHistoryComponent);
    component = fixture.componentInstance;
  });

  it('returns zeroed KPIs when there is no history', () => {
    expect(component.kpis).toEqual({ total: 0, successful: 0, failed: 0, avgSpeed: 0 });
  });

  it('counts total / successful / failed from the filtered rows', () => {
    component.history = [
      row({ status: 'completed', average_speed_mbps: 30 }),
      row({ status: 'completed', average_speed_mbps: 50 }),
      row({ status: 'failed', average_speed_mbps: undefined }),
      row({ status: 'in_progress', average_speed_mbps: null }),
    ];
    component.applyStatusFilter();

    expect(component.kpis.total).toBe(4);
    expect(component.kpis.successful).toBe(2);
    expect(component.kpis.failed).toBe(1);
    // 30 + 50 over 2 completed entries with a speed.
    expect(component.kpis.avgSpeed).toBe(40);
  });

  it('excludes failed and zero-byte / null-speed rows from the avg speed', () => {
    component.history = [
      row({ status: 'completed', average_speed_mbps: 0 }),       // zero-byte completed
      row({ status: 'completed', average_speed_mbps: undefined }), // missing speed
      row({ status: 'completed', average_speed_mbps: 60 }),
      row({ status: 'failed', average_speed_mbps: 1000 }),       // failed row inflates avg if not excluded
    ];
    component.applyStatusFilter();
    expect(component.kpis.avgSpeed).toBe(60);
  });

  it('honors the status filter — only filtered rows feed the KPIs', () => {
    component.history = [
      row({ status: 'completed', average_speed_mbps: 30 }),
      row({ status: 'completed', average_speed_mbps: 50 }),
      row({ status: 'failed', average_speed_mbps: null }),
    ];
    component.selectedStatus = 'failed';
    component.applyStatusFilter();

    expect(component.kpis.total).toBe(1);
    expect(component.kpis.successful).toBe(0);
    expect(component.kpis.failed).toBe(1);
    expect(component.kpis.avgSpeed).toBe(0);
  });

  it('does not call the removed /transfer/statistics endpoint on init', () => {
    fixture.detectChanges(); // triggers ngOnInit
    expect(mockSystem.getTransferHistory).toHaveBeenCalled();
    // statistics endpoint is no longer called from this view (#592)
    expect((mockSystem as any).getTransferStatistics).toBeUndefined();
  });

  // --- #593: title resolution from server-joined identity --------------------

  it('prefers the full Movie (Year) — Disc Name title when all identity fields are populated', () => {
    const entry = row({
      job_id: 'j1',
      movie_name: 'V for Vendetta',
      release_year: 2006,
      release_name: 'V for Vendetta — Blu-Ray Edition',
      disc_name: 'V for Vendetta - Blu-Ray',
      source_path: '/data/mkvauto/data/jobs/old-uuid',
    });
    expect(component.getEntryTitle(entry)).toBe('V for Vendetta (2006) — V for Vendetta - Blu-Ray');
  });

  it('falls back to release_name when movie_name is missing', () => {
    const entry = row({
      job_id: 'j1',
      movie_name: null,
      release_name: 'Special Director Cut',
      release_year: 2011,
      disc_name: 'Disc 1',
    });
    expect(component.getEntryTitle(entry)).toBe('Special Director Cut (2011) — Disc 1');
  });

  it('falls back to disc_name when neither movie_name nor release_name is set', () => {
    const entry = row({
      job_id: 'j1',
      movie_name: null,
      release_name: null,
      disc_name: 'Mystery Disc',
    });
    expect(component.getEntryTitle(entry)).toBe('Mystery Disc');
  });

  it('falls back to the source-path parser for orphaned rows', () => {
    const entry = row({
      job_id: null,
      movie_name: null,
      release_name: null,
      disc_name: null,
      source_path: '/data/mkvauto/data/jobs/my-old-job/raw/file.mkv',
    });
    // Source-path parser pulls the parent dir name as identity.
    expect(component.getEntryTitle(entry)).toBe('raw');
  });

  it('flags rows as orphan when job_id is null', () => {
    expect(
      component.isOrphan(row({ job_id: null, movie_name: null, release_name: null, disc_name: null }))
    ).toBeTrue();
    expect(
      component.isOrphan(row({ job_id: 'j1', movie_name: 'Movie' }))
    ).toBeFalse();
  });
});
