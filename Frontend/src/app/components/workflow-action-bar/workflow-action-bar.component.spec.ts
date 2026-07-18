import { ComponentFixture, TestBed } from '@angular/core/testing';
import { WorkflowActionBarComponent } from './workflow-action-bar.component';

describe('WorkflowActionBarComponent', () => {
  let component: WorkflowActionBarComponent;
  let fixture: ComponentFixture<WorkflowActionBarComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [WorkflowActionBarComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(WorkflowActionBarComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('disables continue button when canContinue is false', () => {
    component.canContinue = false;
    component.buttonText = 'Continue';
    fixture.detectChanges();
    const btn = fixture.nativeElement.querySelector('button.ui-btn[data-variant="primary"]');
    expect(btn).toBeTruthy();
    expect(btn.disabled).toBe(true);
  });

  it('enables continue button when canContinue is true', () => {
    component.canContinue = true;
    component.buttonText = 'Continue';
    fixture.detectChanges();
    const btn = fixture.nativeElement.querySelector('button.ui-btn[data-variant="primary"]');
    expect(btn).toBeTruthy();
    expect(btn.disabled).toBe(false);
  });

  it('switches the continue button to the danger variant when buttonText is Failed', () => {
    component.canContinue = true;
    component.buttonText = 'Failed';
    fixture.detectChanges();
    const btn = fixture.nativeElement.querySelector('button.ui-btn[data-variant="danger"]');
    expect(btn).toBeTruthy();
    expect(btn?.textContent?.trim()).toBe('Failed');
  });

  it('emits continue when onContinue is called', () => {
    spyOn(component.continue, 'emit');
    component.onContinue();
    expect(component.continue.emit).toHaveBeenCalled();
  });

  it('emits back when onBack is called', () => {
    spyOn(component.back, 'emit');
    component.onBack();
    expect(component.back.emit).toHaveBeenCalled();
  });

  it('disables Back button when canGoBack is false', () => {
    component.canGoBack = false;
    fixture.detectChanges();
    const backBtn = fixture.nativeElement.querySelector('button.ui-btn[data-variant="ghost"]');
    expect(backBtn).toBeTruthy();
    expect(backBtn?.disabled).toBe(true);
  });

  it('shows Back button when canGoBack is true', () => {
    component.canGoBack = true;
    fixture.detectChanges();
    const backBtn = fixture.nativeElement.querySelector('button.ui-btn[data-variant="ghost"]');
    expect(backBtn).toBeTruthy();
    expect(backBtn?.textContent?.trim()).toBe('Back');
  });
});
