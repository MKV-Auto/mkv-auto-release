import { ComponentFixture, TestBed } from '@angular/core/testing';
import { IconComponent } from './icon.component';

describe('IconComponent', () => {
  let component: IconComponent;
  let fixture: ComponentFixture<IconComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [IconComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(IconComponent);
    component = fixture.componentInstance;
  });

  it('renders an SVG for the requested icon', () => {
    fixture.componentRef.setInput('name', 'check');
    fixture.detectChanges();
    const html = (fixture.nativeElement as HTMLElement).innerHTML;
    expect(html).toContain('<svg');
    expect(html).toContain('viewBox="0 0 24 24"');
    expect(html).toContain('M20 6 9 17l-5-5');
  });

  it('applies the size input to width and height', () => {
    fixture.componentRef.setInput('name', 'disc');
    fixture.componentRef.setInput('size', 32);
    fixture.detectChanges();
    const html = (fixture.nativeElement as HTMLElement).innerHTML;
    expect(html).toContain('width="32"');
    expect(html).toContain('height="32"');
  });

  it('uses currentColor for stroke so it inherits text color', () => {
    fixture.componentRef.setInput('name', 'plus');
    fixture.detectChanges();
    const html = (fixture.nativeElement as HTMLElement).innerHTML;
    expect(html).toContain('stroke="currentColor"');
  });

  it('marks itself aria-hidden when no aria-label is provided', () => {
    fixture.componentRef.setInput('name', 'film');
    fixture.detectChanges();
    const span = (fixture.nativeElement as HTMLElement).querySelector('span.ui-icon');
    expect(span?.getAttribute('aria-hidden')).toBe('true');
    expect(span?.getAttribute('aria-label')).toBeNull();
  });

  it('exposes the aria-label when set, and drops aria-hidden', () => {
    fixture.componentRef.setInput('name', 'film');
    fixture.componentRef.setInput('ariaLabel', 'Movie');
    fixture.detectChanges();
    const span = (fixture.nativeElement as HTMLElement).querySelector('span.ui-icon');
    expect(span?.getAttribute('aria-label')).toBe('Movie');
    expect(span?.getAttribute('aria-hidden')).toBeNull();
  });

  it('exposes a default truthy component instance', () => {
    expect(component).toBeTruthy();
  });
});
