import { ComponentFixture, TestBed } from '@angular/core/testing';
import { StageChipComponent } from './stage-chip.component';

describe('StageChipComponent', () => {
  let fixture: ComponentFixture<StageChipComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [StageChipComponent] }).compileComponents();
    fixture = TestBed.createComponent(StageChipComponent);
  });

  it('renders the label', () => {
    fixture.componentRef.setInput('status', 'pending');
    fixture.componentRef.setInput('label', 'Ripping');
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-stage__label')?.textContent?.trim()).toBe('Ripping');
  });

  it('reflects the status as data attribute', () => {
    fixture.componentRef.setInput('status', 'active');
    fixture.componentRef.setInput('label', 'Working');
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-stage')?.getAttribute('data-status')).toBe('active');
  });

  it('shows a dot for pending status', () => {
    fixture.componentRef.setInput('status', 'pending');
    fixture.componentRef.setInput('label', 'Queued');
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-stage__dot')).toBeTruthy();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-stage__icon')).toBeNull();
  });

  it('shows the spinner icon for active status', () => {
    fixture.componentRef.setInput('status', 'active');
    fixture.componentRef.setInput('label', 'Working');
    fixture.detectChanges();
    const icon = (fixture.nativeElement as HTMLElement).querySelector('.ui-stage__icon');
    expect(icon).toBeTruthy();
    expect(icon?.classList.contains('ui-stage__icon--spin')).toBeTrue();
  });

  it('omits the sub line when not provided', () => {
    fixture.componentRef.setInput('status', 'done');
    fixture.componentRef.setInput('label', 'Complete');
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-stage__sub')).toBeNull();
  });

  it('shows the sub line when provided', () => {
    fixture.componentRef.setInput('status', 'done');
    fixture.componentRef.setInput('label', 'Complete');
    fixture.componentRef.setInput('sub', '12 titles');
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-stage__sub')?.textContent?.trim()).toBe('12 titles');
  });
});
