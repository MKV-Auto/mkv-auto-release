import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TransferConfigFormComponent } from './transfer-config-form.component';
import { SystemService } from '../../services/system.service';
import { LoggerService } from '../../services/logger.service';

describe('TransferConfigFormComponent', () => {
  let component: TransferConfigFormComponent;
  let fixture: ComponentFixture<TransferConfigFormComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TransferConfigFormComponent],
      providers: [
        { provide: SystemService, useValue: { uploadRsyncKey: () => ({ subscribe: () => {} }) } },
        { provide: LoggerService, useValue: { error: () => {} } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(TransferConfigFormComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('has default formData with mode local and conflict_resolution overwrite', () => {
    expect(component.formData.mode).toBe('local');
    expect(component.formData.conflict_resolution).toBe('overwrite');
  });

  it('ngOnInit patches formData when config is provided', () => {
    component.config = {
      id: 'c1',
      name: 'Test',
      mode: 'local',
      transfer_dir: '/path',
      path_template: '{movie_name}',
      conflict_resolution: 'skip',
      health_check_interval_minutes: 30,
      is_active: false,
      health_status: null,
      output_dir: null,
      config_data: null,
    } as any;
    component.ngOnInit();
    expect(component.formData.name).toBe('Test');
    expect(component.formData.transfer_dir).toBe('/path');
    expect(component.formData.conflict_resolution).toBe('skip');
  });

  it('onSubmit emits payload with formData for mode local', () => {
    component.formData.mode = 'local';
    component.formData.name = 'L';
    component.formData.transfer_dir = '/d';
    let emitted: any;
    component.onSave.subscribe((v) => (emitted = v));
    component.onSubmit();
    expect(emitted).toBeDefined();
    expect(emitted.name).toBe('L');
    expect(emitted.transfer_dir).toBe('/d');
    expect(emitted.mode).toBe('local');
  });

  it('onCancel emits onCancel', () => {
    let emitted = false;
    component.onCancel.subscribe(() => (emitted = true));
    component.onCancel.emit();
    expect(emitted).toBe(true);
  });
});
