import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ProgressRingComponent } from './progress-ring.component';

describe('ProgressRingComponent', () => {
  let fixture: ComponentFixture<ProgressRingComponent>;
  let component: ProgressRingComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [ProgressRingComponent] }).compileComponents();
    fixture = TestBed.createComponent(ProgressRingComponent);
    component = fixture.componentInstance;
  });

  it('renders 0% by default', () => {
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).querySelector('.ui-ring__value')?.textContent?.trim();
    expect(text).toBe('0');
  });

  it('clamps values above 100 to 100', () => {
    fixture.componentRef.setInput('value', 150);
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).querySelector('.ui-ring__value')?.textContent?.trim();
    expect(text).toBe('100');
  });

  it('clamps negative values to 0', () => {
    fixture.componentRef.setInput('value', -10);
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).querySelector('.ui-ring__value')?.textContent?.trim();
    expect(text).toBe('0');
  });

  it('rounds the displayed percentage', () => {
    fixture.componentRef.setInput('value', 42.7);
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).querySelector('.ui-ring__value')?.textContent?.trim();
    expect(text).toBe('43');
  });

  it('applies the requested size to the svg width and height', () => {
    fixture.componentRef.setInput('size', 64);
    fixture.detectChanges();
    const svg = (fixture.nativeElement as HTMLElement).querySelector('svg');
    expect(svg?.getAttribute('width')).toBe('64');
    expect(svg?.getAttribute('height')).toBe('64');
  });

  it('selects the right stroke color for the tone', () => {
    fixture.componentRef.setInput('tone', 'emerald');
    fixture.detectChanges();
    const filledCircle = (fixture.nativeElement as HTMLElement).querySelectorAll('circle')[1];
    expect(filledCircle?.getAttribute('stroke')).toBe('#10b981');
  });

  it('has dashOffset = 0 when value is 100', () => {
    fixture.componentRef.setInput('value', 100);
    fixture.detectChanges();
    const filledCircle = (fixture.nativeElement as HTMLElement).querySelectorAll('circle')[1];
    expect(parseFloat(filledCircle?.getAttribute('stroke-dashoffset') ?? 'NaN')).toBeCloseTo(0, 3);
  });
});
