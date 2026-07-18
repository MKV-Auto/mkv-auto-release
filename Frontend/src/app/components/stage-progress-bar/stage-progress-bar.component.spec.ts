import { ComponentFixture, TestBed } from '@angular/core/testing';
import { StageProgressBarComponent, StageTimelineItem } from './stage-progress-bar.component';

describe('StageProgressBarComponent', () => {
  let component: StageProgressBarComponent;
  let fixture: ComponentFixture<StageProgressBarComponent>;

  const timeline: StageTimelineItem[] = [
    { key: 'rip', label: 'Rip' },
    { key: 'label', label: 'Label' },
    { key: 'postprocess', label: 'Post-Process' },
    { key: 'transfer', label: 'Transfer' },
  ];

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [StageProgressBarComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(StageProgressBarComponent);
    component = fixture.componentInstance;
    component.stageTimeline = timeline;
    component.stageProgress = { rip: 100, label: 100, postprocess: 50, transfer: 0 };
    component.activeStage = 'postprocess';
    component.isStageCompleted = { rip: true, label: true, postprocess: false, transfer: false };
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('getProgressValue returns value for stage key', () => {
    expect(component.getProgressValue('rip')).toBe(100);
    expect(component.getProgressValue('postprocess')).toBe(50);
    expect(component.getProgressValue('transfer')).toBe(0);
  });

  it('getProgressValue returns null for done when transfer not completed', () => {
    expect(component.getProgressValue('done')).toBeNull();
  });

  it('getProgressValue returns 100 for done when transfer completed', () => {
    component.isStageCompleted = { rip: true, label: true, postprocess: true, transfer: true };
    expect(component.getProgressValue('done')).toBe(100);
  });

  it('isStageCompletedCheck returns correct value', () => {
    expect(component.isStageCompletedCheck('rip')).toBe(true);
    expect(component.isStageCompletedCheck('postprocess')).toBe(false);
    expect(component.isStageCompletedCheck('done')).toBe(false);
    component.isStageCompleted = { ...component.isStageCompleted!, transfer: true };
    expect(component.isStageCompletedCheck('done')).toBe(true);
  });

  it('isStageFuture returns true for stages after active', () => {
    // activeStage is postprocess (index 2). transfer is index 3.
    expect(component.isStageFuture('transfer', 3)).toBe(true);
    expect(component.isStageFuture('rip', 0)).toBe(false);
  });

  it('stageGridTemplate returns desktop grid template', () => {
    const template = component.stageGridTemplate;
    expect(template).toContain('minmax(0, 90px)');
    expect(template).toContain('minmax(120px, 2fr)');
  });
});
