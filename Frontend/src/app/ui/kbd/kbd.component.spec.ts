import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { KbdComponent } from './kbd.component';

@Component({
  standalone: true,
  imports: [KbdComponent],
  template: `<ui-kbd>Esc</ui-kbd>`,
})
class HostComponent {}

describe('KbdComponent', () => {
  let fixture: ComponentFixture<HostComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    fixture.detectChanges();
  });

  it('renders a kbd element with projected content', () => {
    const kbd = (fixture.nativeElement as HTMLElement).querySelector('kbd.ui-kbd');
    expect(kbd).toBeTruthy();
    expect(kbd?.textContent?.trim()).toBe('Esc');
  });
});
