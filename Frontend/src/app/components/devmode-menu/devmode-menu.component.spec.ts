import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { of } from 'rxjs';
import { Router } from '@angular/router';
import { DevmodeMenuComponent } from './devmode-menu.component';
import { JobService } from '../../services/job.service';
import { MetadataService } from '../../services/metadata.service';
import { SystemService } from '../../services/system.service';
import { ToastService } from '../../services/toast.service';
import { WorkflowService } from '../../services/workflow.service';

describe('DevmodeMenuComponent', () => {
  let component: DevmodeMenuComponent;
  let fixture: ComponentFixture<DevmodeMenuComponent>;
  let toastSpy: jasmine.SpyObj<Pick<ToastService, 'show'>>;
  let systemServiceMock: {
    getQuickPostProcessTestsEnabled: jasmine.Spy;
    setQuickPostProcessTestsEnabled: jasmine.Spy;
    getFfmpegDetectionEnabled: jasmine.Spy;
    setFfmpegDetectionEnabled: jasmine.Spy;
    getDiscdbDisabled: jasmine.Spy;
    setDiscdbDisabled: jasmine.Spy;
    relookupDiscdb: jasmine.Spy;
  };
  let workflowServiceMock: { getJobStatus$: jasmine.Spy; setWorkflowModeOverride: jasmine.Spy; updateContext: jasmine.Spy };

  beforeEach(async () => {
    toastSpy = jasmine.createSpyObj('ToastService', ['show']);
    systemServiceMock = {
      getQuickPostProcessTestsEnabled: jasmine.createSpy().and.returnValue(of({ enabled: true })),
      setQuickPostProcessTestsEnabled: jasmine.createSpy().and.returnValue(of({ enabled: true })),
      getFfmpegDetectionEnabled: jasmine.createSpy().and.returnValue(of({ enabled: true })),
      setFfmpegDetectionEnabled: jasmine.createSpy().and.returnValue(of({ enabled: true })),
      getDiscdbDisabled: jasmine.createSpy().and.returnValue(of({ disabled: false })),
      setDiscdbDisabled: jasmine.createSpy().and.returnValue(of({ disabled: true })),
      relookupDiscdb: jasmine.createSpy().and.returnValue(of({ result: 'hit', disc_id: 'disc-1' })),
    };
    workflowServiceMock = {
      getJobStatus$: jasmine.createSpy().and.returnValue(of(null)),
      setWorkflowModeOverride: jasmine.createSpy(),
      updateContext: jasmine.createSpy(),
    };
    await TestBed.configureTestingModule({
      imports: [DevmodeMenuComponent],
      providers: [
        { provide: JobService, useValue: {} },
        { provide: MetadataService, useValue: {} },
        { provide: SystemService, useValue: systemServiceMock },
        { provide: ToastService, useValue: toastSpy },
        { provide: WorkflowService, useValue: workflowServiceMock },
        { provide: Router, useValue: { url: '/', navigate: () => {} } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(DevmodeMenuComponent);
    component = fixture.componentInstance;
    component.jobStatus = { post_state: 'completed', jobId: 'j1' } as any;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('shows revert options when jobStatus has post_state completed', () => {
    expect(component.revertOptions.length).toBeGreaterThanOrEqual(1);
    const post = component.revertOptions.find((o) => o.stage === 'postprocess');
    expect(post?.label).toBe('Revert Post-Process');
  });

  it('loads Quick Post-Process Tests, FFmpeg Detection, and Disable DiscDB state on init', () => {
    expect(systemServiceMock.getQuickPostProcessTestsEnabled).toHaveBeenCalled();
    expect(systemServiceMock.getFfmpegDetectionEnabled).toHaveBeenCalled();
    expect(systemServiceMock.getDiscdbDisabled).toHaveBeenCalled();
  });

  it('shows Disable DiscDB toggle row', () => {
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Disable DiscDB');
  });

  it('toggling Disable DiscDB ON calls set API + forces miss override', () => {
    systemServiceMock.setDiscdbDisabled.and.returnValue(of({ disabled: true }));
    component.toggleDiscdbDisabled();
    expect(systemServiceMock.setDiscdbDisabled).toHaveBeenCalledWith(true);
    // Force miss in the active context until the next backend refresh.
    expect(workflowServiceMock.setWorkflowModeOverride).toHaveBeenCalledWith(false);
    expect(workflowServiceMock.updateContext).toHaveBeenCalledWith({ discdbHit: false });
    expect(component.discdbDisabled).toBe(true);
  });

  it('toggling Disable DiscDB OFF clears the workflow override', () => {
    // Pretend it was on already.
    component.discdbDisabled = true;
    systemServiceMock.setDiscdbDisabled.and.returnValue(of({ disabled: false }));
    component.toggleDiscdbDisabled();
    expect(systemServiceMock.setDiscdbDisabled).toHaveBeenCalledWith(false);
    expect(workflowServiceMock.setWorkflowModeOverride).toHaveBeenCalledWith(null);
    // Don't write to updateContext when releasing the override —
    // the backend hit/miss settles on the next context fetch.
    expect(component.discdbDisabled).toBe(false);
  });

  it('toggles Quick Post-Process Tests and updates state', () => {
    systemServiceMock.setQuickPostProcessTestsEnabled.and.returnValue(of({ enabled: false }));
    component.toggleQuickPostProcessTests();
    expect(systemServiceMock.setQuickPostProcessTestsEnabled).toHaveBeenCalledWith(false);
    expect(component.quickPostProcessTestsEnabled).toBe(false);
  });

  it('toggles FFmpeg Detection and updates state', () => {
    systemServiceMock.setFfmpegDetectionEnabled.and.returnValue(of({ enabled: false }));
    component.toggleFfmpegDetection();
    expect(systemServiceMock.setFfmpegDetectionEnabled).toHaveBeenCalledWith(false);
    expect(component.ffmpegDetectionEnabled).toBe(false);
  });

  describe('Re-lookup DiscDB button', () => {
    it('calls service with the disc id and toasts "hit" on success', () => {
      component.discId = 'disc-1';
      systemServiceMock.relookupDiscdb.and.returnValue(of({ result: 'hit', disc_id: 'disc-1' }));
      component.relookupDiscdb();
      expect(systemServiceMock.relookupDiscdb).toHaveBeenCalledWith('disc-1');
      expect(toastSpy.show).toHaveBeenCalledWith(jasmine.stringMatching(/hit/i), 'success', jasmine.any(Number));
      expect(component.relookingUpDiscdb).toBeFalse();
    });

    it('toasts "miss" when the lookup returns miss', () => {
      component.discId = 'disc-1';
      systemServiceMock.relookupDiscdb.and.returnValue(of({ result: 'miss', disc_id: 'disc-1' }));
      component.relookupDiscdb();
      expect(toastSpy.show).toHaveBeenCalledWith(jasmine.stringMatching(/miss/i), 'info', jasmine.any(Number));
    });

    it('is a no-op when there is no active disc', () => {
      component.discId = null;
      component.relookupDiscdb();
      expect(systemServiceMock.relookupDiscdb).not.toHaveBeenCalled();
    });
  });

  it('testNotifications calls toast.show multiple times', fakeAsync(() => {
    component.testNotifications();
    expect(toastSpy.show).toHaveBeenCalledWith('Info notification test', 'info', 2000);
    tick(500);
    expect(toastSpy.show).toHaveBeenCalledWith('Success notification test', 'success', 2000);
    tick(500);
    expect(toastSpy.show).toHaveBeenCalledWith('Warning notification test', 'warning', 2000);
    tick(500);
    expect(toastSpy.show).toHaveBeenCalledWith('Error notification test', 'error', 2000);
    expect(toastSpy.show).toHaveBeenCalledTimes(4);
  }));
});
