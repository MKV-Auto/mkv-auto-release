import { ComponentFixture, fakeAsync, TestBed, tick } from '@angular/core/testing';
import { PathChipComponent } from './path-chip.component';

describe('PathChipComponent', () => {
  let fixture: ComponentFixture<PathChipComponent>;
  let component: PathChipComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [PathChipComponent] }).compileComponents();
    fixture = TestBed.createComponent(PathChipComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('path', '/var/lib/mkv/movies/Inception (2010)/Inception (2010).mkv');
    fixture.detectChanges();
  });

  it('renders the path text and copy button', () => {
    const text = (fixture.nativeElement as HTMLElement).querySelector('.ui-pathchip__text');
    const btn = (fixture.nativeElement as HTMLElement).querySelector('.ui-pathchip__btn');
    expect(text?.textContent).toContain('Inception');
    expect(btn).toBeTruthy();
  });

  it('flashes the copied state and emits copied$ on copy', fakeAsync(() => {
    spyOn((navigator as any).clipboard, 'writeText').and.resolveTo(undefined);
    let emitted: string | undefined;
    component.copied$.subscribe((p) => (emitted = p));

    component.copy();
    tick();
    fixture.detectChanges();

    expect(component.copied).toBeTrue();
    expect(emitted).toContain('Inception');

    tick(1400);
    fixture.detectChanges();
    expect(component.copied).toBeFalse();
  }));

  it('toggles ui-pathchip--full when full input is true', () => {
    fixture.componentRef.setInput('full', true);
    fixture.detectChanges();
    const root = (fixture.nativeElement as HTMLElement).querySelector('.ui-pathchip');
    expect(root?.classList.contains('ui-pathchip--full')).toBeTrue();
  });

  it('survives clipboard rejection without throwing', fakeAsync(() => {
    spyOn((navigator as any).clipboard, 'writeText').and.rejectWith(new Error('blocked'));
    expect(() => {
      component.copy();
      tick();
    }).not.toThrow();
    fixture.detectChanges();
    expect(component.copied).toBeTrue();
    tick(1400);
  }));
});
