import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ChipComponent } from './chip.component';

@Component({
  standalone: true,
  imports: [ChipComponent],
  template: `
    <ui-chip [active]="active" (toggled)="onToggled($event)">
      <span uiChipIcon class="icon-slot">★</span>
      <span class="label">{{ label }}</span>
    </ui-chip>
  `,
})
class HostComponent {
  active = false;
  label = 'Movies';
  toggled?: boolean;
  onToggled(v: boolean) { this.toggled = v; }
}

describe('ChipComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders the projected label and icon', () => {
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Movies');
    expect((fixture.nativeElement as HTMLElement).querySelector('.icon-slot')).toBeTruthy();
  });

  it('reflects active as aria-pressed and a modifier class', () => {
    host.active = true;
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button.ui-chip');
    expect(btn?.getAttribute('aria-pressed')).toBe('true');
    expect(btn?.classList.contains('ui-chip--active')).toBeTrue();
  });

  it('emits the inverted state on click', () => {
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button.ui-chip') as HTMLButtonElement;
    btn.click();
    expect(host.toggled).toBe(true);
    host.active = true;
    fixture.detectChanges();
    btn.click();
    expect(host.toggled).toBe(false);
  });
});
