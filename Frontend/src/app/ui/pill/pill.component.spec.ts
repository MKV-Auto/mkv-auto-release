import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { PillComponent, PillTone } from './pill.component';

@Component({
  standalone: true,
  imports: [PillComponent],
  template: `
    <ui-pill [tone]="tone">
      <span uiPillIcon class="icon-slot">★</span>
      <span class="label">{{ label }}</span>
    </ui-pill>
  `,
})
class HostComponent {
  tone: PillTone = 'slate';
  label = 'Hello';
}

describe('PillComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders the projected label', () => {
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Hello');
  });

  it('projects the icon slot ahead of the label', () => {
    const pill = (fixture.nativeElement as HTMLElement).querySelector('.ui-pill');
    const icon = pill?.querySelector('.icon-slot');
    const label = pill?.querySelector('.label');
    expect(icon).toBeTruthy();
    expect(label).toBeTruthy();
    expect(icon!.compareDocumentPosition(label!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('reflects the tone input as a data attribute for styling', () => {
    host.tone = 'emerald';
    fixture.detectChanges();
    const pill = (fixture.nativeElement as HTMLElement).querySelector('.ui-pill');
    expect(pill?.getAttribute('data-tone')).toBe('emerald');
  });

  it('defaults to slate tone when not set', () => {
    const standalone = TestBed.createComponent(PillComponent);
    standalone.detectChanges();
    const pill = (standalone.nativeElement as HTMLElement).querySelector('.ui-pill');
    expect(pill?.getAttribute('data-tone')).toBe('slate');
  });
});
