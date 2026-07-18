import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { BtnComponent, BtnVariant } from './btn.component';

@Component({
  standalone: true,
  imports: [BtnComponent],
  template: `
    <ui-btn
      [variant]="variant"
      [disabled]="disabled"
      [loading]="loading"
      [fullWidth]="fullWidth"
      (click)="onClick()"
    >
      <span uiBtnIcon class="icon-slot">★</span>
      <span class="label">{{ label }}</span>
    </ui-btn>
  `,
})
class HostComponent {
  variant: BtnVariant = 'primary';
  disabled = false;
  loading = false;
  fullWidth = false;
  label = 'Save';
  clicks = 0;
  onClick() { this.clicks++; }
}

describe('BtnComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('reflects the variant as data attribute', () => {
    host.variant = 'danger';
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button.ui-btn');
    expect(btn?.getAttribute('data-variant')).toBe('danger');
  });

  it('forwards click events when enabled', () => {
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button.ui-btn') as HTMLButtonElement;
    btn.click();
    expect(host.clicks).toBe(1);
  });

  it('disables the button when disabled is set', () => {
    host.disabled = true;
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button.ui-btn') as HTMLButtonElement;
    expect(btn.disabled).toBeTrue();
    btn.click();
    expect(host.clicks).toBe(0);
  });

  it('shows a spinner and hides the icon slot when loading', () => {
    host.loading = true;
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button.ui-btn');
    expect(btn?.querySelector('.ui-btn__spin')).toBeTruthy();
    expect(btn?.querySelector('.icon-slot')).toBeNull();
    expect(btn?.getAttribute('aria-busy')).toBe('true');
  });

  it('disables the button while loading', () => {
    host.loading = true;
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button.ui-btn') as HTMLButtonElement;
    expect(btn.disabled).toBeTrue();
  });

  it('toggles the full-width modifier class', () => {
    host.fullWidth = true;
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button.ui-btn');
    expect(btn?.classList.contains('ui-btn--full')).toBeTrue();
  });
});
