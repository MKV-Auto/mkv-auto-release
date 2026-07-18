import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SectionHeaderComponent } from './section-header.component';

@Component({
  standalone: true,
  imports: [SectionHeaderComponent],
  template: `
    <ui-section-header [title]="title" [subtitle]="subtitle">
      <span uiSecIcon class="icon-slot">★</span>
      <button class="action">Refresh</button>
    </ui-section-header>
  `,
})
class HostComponent {
  title = 'Drives';
  subtitle?: string;
}

describe('SectionHeaderComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders the title with projected icon and actions', () => {
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-sec__title')?.textContent?.trim()).toContain('Drives');
    expect((fixture.nativeElement as HTMLElement).querySelector('.icon-slot')).toBeTruthy();
    expect((fixture.nativeElement as HTMLElement).querySelector('.action')).toBeTruthy();
  });

  it('omits the subtitle when not provided', () => {
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-sec__sub')).toBeNull();
  });

  it('shows the subtitle when provided', () => {
    host.subtitle = 'Connected MakeMKV devices';
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-sec__sub')?.textContent?.trim()).toBe('Connected MakeMKV devices');
  });
});
