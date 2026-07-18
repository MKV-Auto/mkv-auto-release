import { ComponentFixture, TestBed } from '@angular/core/testing';
import { StepperComponent } from './stepper.component';

describe('StepperComponent', () => {
  let fixture: ComponentFixture<StepperComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [StepperComponent] }).compileComponents();
    fixture = TestBed.createComponent(StepperComponent);
    fixture.componentRef.setInput('steps', ['Film', 'Release', 'Disc', 'Titles']);
    fixture.componentRef.setInput('activeIndex', 1);
    fixture.detectChanges();
  });

  it('renders one item per step plus N-1 connectors', () => {
    const items = (fixture.nativeElement as HTMLElement).querySelectorAll('.ui-stepper__item');
    const connectors = (fixture.nativeElement as HTMLElement).querySelectorAll('.ui-stepper__connector');
    expect(items.length).toBe(4);
    expect(connectors.length).toBe(3);
  });

  it('marks the active step with the active modifier', () => {
    const items = (fixture.nativeElement as HTMLElement).querySelectorAll('.ui-stepper__item');
    expect(items[0].classList.contains('ui-stepper__item--active')).toBeFalse();
    expect(items[1].classList.contains('ui-stepper__item--active')).toBeTrue();
    expect(items[2].classList.contains('ui-stepper__item--active')).toBeFalse();
  });

  it('marks earlier steps as done with a check icon instead of a number', () => {
    const items = (fixture.nativeElement as HTMLElement).querySelectorAll('.ui-stepper__item');
    expect(items[0].classList.contains('ui-stepper__item--done')).toBeTrue();
    const idx = items[0].querySelector('.ui-stepper__index');
    expect(idx?.querySelector('svg')).toBeTruthy();
  });

  it('shows the 1-based number for pending and active steps', () => {
    const items = (fixture.nativeElement as HTMLElement).querySelectorAll('.ui-stepper__item');
    expect(items[1].querySelector('.ui-stepper__index')?.textContent?.trim()).toBe('2');
    expect(items[2].querySelector('.ui-stepper__index')?.textContent?.trim()).toBe('3');
    expect(items[3].querySelector('.ui-stepper__index')?.textContent?.trim()).toBe('4');
  });

  it('renders the labels in order', () => {
    const labels = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('.ui-stepper__label'),
    ).map((el) => el.textContent?.trim());
    expect(labels).toEqual(['Film', 'Release', 'Disc', 'Titles']);
  });
});
