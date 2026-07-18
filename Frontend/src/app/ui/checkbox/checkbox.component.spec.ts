import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FormsModule } from '@angular/forms';
import { CheckboxComponent } from './checkbox.component';

@Component({
  standalone: true,
  imports: [CheckboxComponent, FormsModule],
  template: `
    <ui-checkbox
      [(ngModel)]="value"
      [disabled]="disabled"
      [ariaLabel]="ariaLabel"
    >Test label</ui-checkbox>
  `,
})
class HostComponent {
  value = false;
  disabled = false;
  ariaLabel = 'Test';
}

describe('CheckboxComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  function getInput(): HTMLInputElement {
    return fixture.nativeElement.querySelector('input.ui-checkbox__input') as HTMLInputElement;
  }

  it('reflects ngModel into the native input on init', async () => {
    expect(getInput().checked).toBeFalse();
    host.value = true;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(getInput().checked).toBeTrue();
  });

  it('updates ngModel when the user toggles', async () => {
    const input = getInput();
    input.click();
    fixture.detectChanges();
    await fixture.whenStable();
    expect(host.value).toBeTrue();
  });

  it('respects the disabled input', () => {
    host.disabled = true;
    fixture.detectChanges();
    const input = getInput();
    expect(input.disabled).toBeTrue();
    input.click();
    fixture.detectChanges();
    expect(host.value).toBeFalse();
  });

  it('exposes the aria-label on the native input', () => {
    host.ariaLabel = 'Enable notifications';
    fixture.detectChanges();
    expect(getInput().getAttribute('aria-label')).toBe('Enable notifications');
  });

  it('toggles the disabled visual modifier class', () => {
    host.disabled = true;
    fixture.detectChanges();
    const label = fixture.nativeElement.querySelector('.ui-checkbox') as HTMLElement;
    expect(label.classList.contains('ui-checkbox--disabled')).toBeTrue();
  });
});
