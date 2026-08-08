import { of, throwError } from 'rxjs';
import { ShellComponent } from './shell.component';

/**
 * Gating logic for the bell-panel support prompt. The component is built
 * directly rather than through TestBed — the shell pulls in a dozen services
 * and a full mount would test the wiring rather than the decision being made.
 */
describe('ShellComponent support prompt', () => {
  let systemSvc: {
    getSupportPromptStatus: jasmine.Spy;
    dismissSupportPrompt: jasmine.Spy;
  };
  let component: ShellComponent;

  const build = () => {
    const stub = () => ({}) as any;
    return new ShellComponent(
      stub(), // toast
      stub(), // driveSvc
      stub(), // jobSvc
      systemSvc as any,
      stub(), // workflowService
      stub(), // setupModalSvc
      stub(), // ripSizeWarningSvc
      stub(), // usbSaturationSvc
      stub(), // logger
      stub(), // router
      stub(), // notifHistory
      stub(), // browserNotif
    );
  };

  beforeEach(() => {
    systemSvc = {
      getSupportPromptStatus: jasmine
        .createSpy('getSupportPromptStatus')
        .and.returnValue(of({ should_show: true, completed_rips: 9, dismissed_forever: false })),
      dismissSupportPrompt: jasmine
        .createSpy('dismissSupportPrompt')
        .and.returnValue(of({ should_show: false, completed_rips: 9, dismissed_forever: true })),
    };
    component = build();
  });

  it('stays hidden until the backend says the install has earned it', () => {
    expect(component.supportPromptVisible).toBe(false);
  });

  it('shows once eligible and nothing is ripping', () => {
    (component as any).refreshSupportPrompt();
    expect(component.supportPromptVisible).toBe(true);
  });

  it('stays hidden while a job is in flight', () => {
    (component as any).refreshSupportPrompt();
    (component as any).jobInFlight = true;
    expect(component.supportPromptVisible).toBe(false);
  });

  it('stays hidden when the backend says not to show it', () => {
    systemSvc.getSupportPromptStatus.and.returnValue(
      of({ should_show: false, completed_rips: 2, dismissed_forever: false }),
    );
    (component as any).refreshSupportPrompt();
    expect(component.supportPromptVisible).toBe(false);
  });

  it('stays hidden when the eligibility request fails', () => {
    systemSvc.getSupportPromptStatus.and.returnValue(throwError(() => new Error('offline')));
    (component as any).refreshSupportPrompt();
    expect(component.supportPromptVisible).toBe(false);
  });

  it('hides immediately and silences permanently on "Don\'t show again"', () => {
    (component as any).refreshSupportPrompt();
    component.onDismissSupportPrompt(new Event('click'), true);
    expect(component.supportPromptVisible).toBe(false);
    expect(systemSvc.dismissSupportPrompt).toHaveBeenCalledWith(true);
  });

  it('hides immediately and snoozes on "Maybe later"', () => {
    (component as any).refreshSupportPrompt();
    component.onDismissSupportPrompt(new Event('click'), false);
    expect(component.supportPromptVisible).toBe(false);
    expect(systemSvc.dismissSupportPrompt).toHaveBeenCalledWith(false);
  });

  it('still hides when the dismiss request fails — the user has answered', () => {
    systemSvc.dismissSupportPrompt.and.returnValue(throwError(() => new Error('offline')));
    (component as any).refreshSupportPrompt();
    component.onDismissSupportPrompt(new Event('click'), true);
    expect(component.supportPromptVisible).toBe(false);
  });
});
