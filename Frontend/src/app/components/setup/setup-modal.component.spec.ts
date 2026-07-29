import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
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

  describe('environment-managed settings', () => {
    // An unattended deployment pins keys via Docker env vars. Opening the
    // wizard on a form asking for a key the operator already supplied is the
    // failure this guards against.
    const skipTo = (managed: string[]) => {
      component.envManaged = managed;
      component.stepData = {
        ...component.stepData,
        makemkv: { key: 'T-x', valid: true, installed: true },
      };
      (component as any).skipEnvSatisfiedSteps();
    };

    it('lands on the first step the environment has not answered', () => {
      skipTo(['makemkv_registration_key']);
      // Step 2 (Transfer) is database-backed — the environment cannot answer it.
      expect(component.currentStep).toBe(2);
      expect(component.completedSteps).toContain(1);
    });

    it('marks skipped steps complete rather than hiding them', () => {
      skipTo(['makemkv_registration_key']);
      expect(component.canGoToStep(1)).toBe(true);
    });

    it('does not skip a step the environment only partly answered', () => {
      // Webhook pinned but `enabled` left to the user: still a question.
      component.envManaged = ['discord.webhook_url'];
      expect(component.isStepEnvSatisfied(6)).toBe(false);
    });

    it('does not skip past a MakeMKV that failed to install', () => {
      // The pinned key is irrelevant if the binary is broken — step 1 exists
      // precisely to surface that.
      component.envManaged = ['makemkv_registration_key'];
      component.stepData = {
        ...component.stepData,
        makemkv: { key: 'T-x', valid: true, installed: false },
      };
      (component as any).skipEnvSatisfiedSteps();
      expect(component.currentStep).toBe(1);
    });

    it('does not skip in targeted mode', () => {
      component.config = { targetStep: 1 } as any;
      component.currentStep = 1;
      skipTo(['makemkv_registration_key']);
      expect(component.currentStep).toBe(1);
    });

    it('Discord counts as answered when the environment pins both keys', () => {
      // Otherwise a deployment that pins a webhook but leaves it disabled
      // dead-ends on a step whose fields are all disabled.
      component.envManaged = ['discord.webhook_url', 'discord.enabled'];
      component.stepData = {
        ...component.stepData,
        discord: { enabled: false, webhookUrl: 'https://x', dismissed: false },
      };
      expect(component.isStepComplete(6)).toBe(true);
    });

    it('passes envManaged down so a revisited step is read-only', fakeAsync(() => {
      // The wizard skips steps the environment answered, but nothing stops a
      // user clicking back into one — a field that looks editable there saves
      // and is then reverted by the next restart.
      component.envManaged = ['tmdb_api_key'];
      component.currentStep = 5;
      component.loading = false;
      fixture.detectChanges();

      // NgModel applies a [disabled] binding through its FormControl on a
      // microtask, so the DOM property lands after the queue drains, not on the
      // same change-detection pass.
      tick();
      fixture.detectChanges();

      const step = fixture.nativeElement.querySelector('app-setup-step-tmdb');
      expect(step).toBeTruthy();
      expect(step.querySelector('input[type="text"]').disabled).toBe(true);
      expect(step.querySelector('app-env-managed-note')).toBeTruthy();
    }));

    it('leaves a step editable when the environment does not pin it', () => {
      component.envManaged = [];
      component.currentStep = 5;
      component.loading = false;
      fixture.detectChanges();

      const step = fixture.nativeElement.querySelector('app-setup-step-tmdb');
      expect(step.querySelector('app-env-managed-note')).toBeNull();
    });

    it('isEnvManaged reports only pinned settings', () => {
      component.envManaged = ['tmdb_api_key'];
      expect(component.isEnvManaged('tmdb_api_key')).toBe(true);
      expect(component.isEnvManaged('media_server')).toBe(false);
    });
  });
});
