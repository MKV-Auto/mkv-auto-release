import { ComponentFixture, TestBed } from '@angular/core/testing';
import { LibraryCardComponent } from './library-card.component';

describe('LibraryCardComponent', () => {
  let fixture: ComponentFixture<LibraryCardComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [LibraryCardComponent] }).compileComponents();
    fixture = TestBed.createComponent(LibraryCardComponent);
  });

  it('renders the title', () => {
    fixture.componentRef.setInput('title', 'Inception');
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-libcard__title')?.textContent?.trim())
      .toBe('Inception');
  });

  it('shows the cover image when coverUrl is provided', () => {
    fixture.componentRef.setInput('title', 'Inception');
    fixture.componentRef.setInput('coverUrl', 'https://example.com/cover.jpg');
    fixture.detectChanges();
    const img = (fixture.nativeElement as HTMLElement).querySelector('img.ui-libcard__cover') as HTMLImageElement | null;
    expect(img).toBeTruthy();
    expect(img?.alt).toBe('Inception');
  });

  it('falls back to initials placeholder when coverUrl is missing', () => {
    fixture.componentRef.setInput('title', 'Star Wars Episode IV');
    fixture.detectChanges();
    const placeholder = (fixture.nativeElement as HTMLElement).querySelector('.ui-libcard__cover--placeholder');
    expect(placeholder).toBeTruthy();
    expect(placeholder?.textContent?.trim()).toBe('SW');
  });

  it('renders year and resolution chips when provided', () => {
    fixture.componentRef.setInput('title', 'Inception');
    fixture.componentRef.setInput('year', 2010);
    fixture.componentRef.setInput('resolution', '4K');
    fixture.detectChanges();
    const pills = (fixture.nativeElement as HTMLElement).querySelectorAll('ui-pill');
    expect(pills.length).toBe(2);
    expect(pills[0].textContent?.trim()).toBe('2010');
    expect(pills[1].textContent?.trim()).toBe('4K');
  });

  it('omits the completion ring when completion is null', () => {
    fixture.componentRef.setInput('title', 'Inception');
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-libcard__ring')).toBeNull();
  });

  it('shows the completion ring when completion is set', () => {
    fixture.componentRef.setInput('title', 'Inception');
    fixture.componentRef.setInput('completion', 75);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-libcard__ring')).toBeTruthy();
  });

  it('emits activated on click', () => {
    fixture.componentRef.setInput('title', 'Inception');
    fixture.detectChanges();
    let fired = false;
    fixture.componentInstance.activated.subscribe(() => (fired = true));
    (fixture.nativeElement as HTMLElement).querySelector('button')?.click();
    expect(fired).toBeTrue();
  });
});
