import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { EmptyStateComponent } from './empty-state.component';

@Component({
  standalone: true,
  imports: [EmptyStateComponent],
  template: `
    <ui-empty-state [title]="title" [body]="body">
      <span uiEmptyIcon class="icon-slot">★</span>
      <button class="action">Refresh</button>
    </ui-empty-state>
  `,
})
class HostComponent {
  title = 'No discs detected';
  body?: string;
}

describe('EmptyStateComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders the title and projected icon + action', () => {
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-empty__title')?.textContent?.trim()).toBe('No discs detected');
    expect((fixture.nativeElement as HTMLElement).querySelector('.icon-slot')).toBeTruthy();
    expect((fixture.nativeElement as HTMLElement).querySelector('.action')).toBeTruthy();
  });

  it('omits the body line when not provided', () => {
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-empty__body')).toBeNull();
  });

  it('shows the body line when provided', () => {
    host.body = 'Insert a disc to begin.';
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-empty__body')?.textContent?.trim()).toBe('Insert a disc to begin.');
  });
});
