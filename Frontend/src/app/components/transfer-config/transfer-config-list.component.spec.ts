import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TransferConfigListComponent } from './transfer-config-list.component';
import type { TransferCapabilities, TransferConfigSummary } from '../../services/system.service';

function makeConfig(over: Partial<TransferConfigSummary> = {}): TransferConfigSummary {
  return {
    id: 'cfg-1',
    mode: 'smb',
    name: 'Plex',
    is_active: true,
    transfer_dir: '/',
    path_template: null,
    conflict_resolution: 'overwrite',
    health_status: 'healthy',
    capabilities: null,
    created_at: '2026-07-09T00:00:00Z',
    updated_at: '2026-07-09T00:00:00Z',
    ...over,
  };
}

function caps(over: Partial<TransferCapabilities> = {}): TransferCapabilities {
  return {
    can_write_new: true,
    can_overwrite_in_place: true,
    can_delete: true,
    can_rename: true,
    probed_at: '2026-07-09T12:00:00Z',
    probe_error: null,
    notes: null,
    ...over,
  };
}

describe('TransferConfigListComponent', () => {
  let component: TransferConfigListComponent;
  let fixture: ComponentFixture<TransferConfigListComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TransferConfigListComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(TransferConfigListComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('getHealthTone / getHealthLabel map status to ui-pill tones + labels', () => {
    expect(component.getHealthTone('healthy')).toBe('emerald');
    expect(component.getHealthLabel('healthy')).toBe('Healthy');
    expect(component.getHealthTone('unhealthy')).toBe('red');
    expect(component.getHealthLabel('unhealthy')).toBe('Unhealthy');
    expect(component.getHealthTone('degraded')).toBe('amber');
    expect(component.getHealthLabel('degraded')).toBe('Degraded');
    expect(component.getHealthTone(null)).toBe('slate');
    expect(component.getHealthLabel(null)).toBe('Unknown');
  });

  it('onEdit emits config id', () => {
    let emitted: string | undefined;
    component.onEdit.subscribe((id) => (emitted = id));
    component.onEdit.emit('cfg-1');
    expect(emitted).toBe('cfg-1');
  });

  describe('#635 commit C: getCapabilityPill', () => {
    it('returns not_probed when capabilities is null', () => {
      const pill = component.getCapabilityPill(makeConfig({ capabilities: null }));
      expect(pill.kind).toBe('not_probed');
    });

    it('returns probe_error with the message when probe failed', () => {
      const pill = component.getCapabilityPill(
        makeConfig({ capabilities: caps({ probe_error: 'connection refused' }) }),
      );
      expect(pill.kind).toBe('probe_error');
      if (pill.kind === 'probe_error') expect(pill.error).toBe('connection refused');
    });

    it('overwrite + can_overwrite_in_place → direct', () => {
      const pill = component.getCapabilityPill(
        makeConfig({ conflict_resolution: 'overwrite', capabilities: caps({ can_overwrite_in_place: true }) }),
      );
      expect(pill.kind).toBe('direct');
      if (pill.kind === 'direct') {
        expect(pill.label).toBe('Overwrite: direct');
        expect(pill.tone).toBe('emerald');
      }
    });

    it('overwrite + !overwrite_in_place + can_delete → delete_then_copy', () => {
      const pill = component.getCapabilityPill(
        makeConfig({
          conflict_resolution: 'overwrite',
          capabilities: caps({ can_overwrite_in_place: false, can_delete: true }),
        }),
      );
      expect(pill.kind).toBe('delete_then_copy');
      if (pill.kind === 'delete_then_copy') {
        expect(pill.label).toBe('Overwrite: delete+copy');
        expect(pill.tone).toBe('cyan');
      }
    });

    it('overwrite + !overwrite + !delete + can_rename → rename', () => {
      const pill = component.getCapabilityPill(
        makeConfig({
          conflict_resolution: 'overwrite',
          capabilities: caps({ can_overwrite_in_place: false, can_delete: false, can_rename: true }),
        }),
      );
      expect(pill.kind).toBe('rename');
      if (pill.kind === 'rename') {
        expect(pill.label).toBe('Overwrite: via rename');
        expect(pill.tone).toBe('amber');
      }
    });

    it('overwrite + no capability → unavailable', () => {
      const pill = component.getCapabilityPill(
        makeConfig({
          conflict_resolution: 'overwrite',
          capabilities: caps({ can_overwrite_in_place: false, can_delete: false, can_rename: false }),
        }),
      );
      expect(pill.kind).toBe('unavailable');
      if (pill.kind === 'unavailable') expect(pill.tone).toBe('red');
    });

    it('rename intent + can_rename → supported; !can_rename → unavailable', () => {
      const supported = component.getCapabilityPill(
        makeConfig({ conflict_resolution: 'rename', capabilities: caps({ can_rename: true }) }),
      );
      expect(supported.kind).toBe('rename');
      const unavailable = component.getCapabilityPill(
        makeConfig({ conflict_resolution: 'rename', capabilities: caps({ can_rename: false }) }),
      );
      expect(unavailable.kind).toBe('unavailable');
    });

    it('skip / fail intents are capability-independent (ready)', () => {
      for (const intent of ['skip', 'fail']) {
        const pill = component.getCapabilityPill(
          makeConfig({ conflict_resolution: intent, capabilities: caps() }),
        );
        expect(pill.kind).toBe('direct');
        if (pill.kind === 'direct') expect(pill.label).toContain('ready');
      }
    });
  });
});
