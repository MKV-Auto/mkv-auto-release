import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SwitchComponent } from './switch.component';

describe('SwitchComponent', () => {
  let fixture: ComponentFixture<SwitchComponent>;
  const button = () => fixture.nativeElement.querySelector('button') as HTMLButtonElement;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [SwitchComponent] }).compileComponents();
    fixture = TestBed.createComponent(SwitchComponent);
    fixture.detectChanges();
  });

  it('is a switch announced with its state', () => {
    fixture.componentRef.setInput('checked', true);
    fixture.detectChanges();
    expect(button().getAttribute('role')).toBe('switch');
    expect(button().getAttribute('aria-checked')).toBe('true');
  });

  it('a click flips it and reports the new value', () => {
    const seen: boolean[] = [];
    fixture.componentInstance.checkedChange.subscribe(v => seen.push(v));
    button().click();
    expect(seen).toEqual([true]);
  });

  it('does nothing while disabled', () => {
    fixture.componentRef.setInput('disabled', true);
    fixture.detectChanges();
    const seen: boolean[] = [];
    fixture.componentInstance.checkedChange.subscribe(v => seen.push(v));
    fixture.componentInstance.toggle();
    expect(seen).toEqual([]);
  });
});
