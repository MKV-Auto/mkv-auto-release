import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { SetupModalComponent } from './setup-modal.component';

describe('SetupModalComponent', () => {
  let component: SetupModalComponent;
  let fixture: ComponentFixture<SetupModalComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SetupModalComponent],
      providers: [provideHttpClient()],
    }).compileComponents();

    fixture = TestBed.createComponent(SetupModalComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should start at step 1', () => {
    expect(component.currentStep).toBe(1);
  });

  it('canProceed is false when MakeMKV not installed', () => {
    component.stepData = {
      ...component.stepData,
      makemkv: { key: 'T-xxx', valid: true, installed: false },
    };
    expect(component.canProceed).toBe(false);
  });

  it('canProceed is false when MakeMKV not valid and disc workflow is blocked', () => {
    // canProceed now permits installed+!blocked even when valid=false.
    // Only when the backend marks the disc workflow as blocked must we
    // also require valid=true to proceed.
    component.stepData = {
      ...component.stepData,
      makemkv: { key: 'x', valid: false, installed: true, disc_workflow_blocked: true } as any,
    };
    expect(component.canProceed).toBe(false);
  });

  it('canProceed is true when MakeMKV installed and valid', () => {
    component.stepData = {
      ...component.stepData,
      makemkv: { key: 'T-xxx', valid: true, installed: true },
    };
    expect(component.canProceed).toBe(true);
  });

  it('onBack decrements step when step > 1', () => {
    component.currentStep = 3;
    component.onBack();
    expect(component.currentStep).toBe(2);
  });

  it('onBack does nothing when step is 1', () => {
    component.currentStep = 1;
    component.onBack();
    expect(component.currentStep).toBe(1);
  });

  it('goToStep sets currentStep when step is allowed', () => {
    component.completedSteps = [1, 2];
    component.goToStep(2);
    expect(component.currentStep).toBe(2);
  });

  it('onStepDataChange updates stepData', () => {
    component.onStepDataChange('makemkv', { key: 'T-key', valid: true, installed: true });
    expect(component.stepData.makemkv.key).toBe('T-key');
    expect(component.stepData.makemkv.valid).toBe(true);
    expect(component.stepData.makemkv.installed).toBe(true);
  });

  it('#689: selecting a library type persists media_server to the backend', () => {
    // The wizard used to keep the Plex/Jellyfin choice in component memory only —
    // the backend stayed on its default and the main UI showed Plex regardless.
    const svc = (component as any).systemSvc;
    const save = spyOn(svc, 'saveMediaServerConfig').and.returnValue({ subscribe: () => {} } as any);
    component.onStepDataChange('library', { type: 'jellyfin' });
    expect(save).toHaveBeenCalledWith({ media_server: 'jellyfin' });
    expect(component.stepData.library.type).toBe('jellyfin');
  });

  it('#689: non-library step changes do not touch media-server config', () => {
    const svc = (component as any).systemSvc;
    const save = spyOn(svc, 'saveMediaServerConfig');
    component.onStepDataChange('makemkv', { key: 'T-key' });
    expect(save).not.toHaveBeenCalled();
  });
});
