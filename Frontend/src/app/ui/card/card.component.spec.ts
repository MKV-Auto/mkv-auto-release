import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { CardComponent } from './card.component';

@Component({
  standalone: true,
  imports: [CardComponent],
  template: `
    <ui-card [active]="active" [interactive]="interactive">
      <p class="content">Hello</p>
    </ui-card>
  `,
})
class HostComponent {
  active = false;
  interactive = false;
}

describe('CardComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('projects content', () => {
    const card = (fixture.nativeElement as HTMLElement).querySelector('.ui-card');
    expect(card?.querySelector('.content')?.textContent).toBe('Hello');
  });

  it('toggles the active modifier when active is true', () => {
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-card--active')).toBeNull();
    host.active = true;
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-card--active')).toBeTruthy();
  });

  it('adds button role and tabindex when interactive', () => {
    host.interactive = true;
    fixture.detectChanges();
    const card = (fixture.nativeElement as HTMLElement).querySelector('.ui-card');
    expect(card?.getAttribute('role')).toBe('button');
    expect(card?.getAttribute('tabindex')).toBe('0');
    expect(card?.classList.contains('ui-card--interactive')).toBeTrue();
  });

  it('omits role and tabindex when not interactive', () => {
    const card = (fixture.nativeElement as HTMLElement).querySelector('.ui-card');
    expect(card?.getAttribute('role')).toBeNull();
    expect(card?.getAttribute('tabindex')).toBeNull();
  });
});
