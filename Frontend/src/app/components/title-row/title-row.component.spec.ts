import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TitleRowComponent, TitleRowStatus } from './title-row.component';

@Component({
  standalone: true,
  imports: [TitleRowComponent],
  template: `
    <app-title-row
      [title]="title"
      [sourceFile]="sourceFile"
      [duration]="duration"
      [previewUrl]="previewUrl"
      [status]="status"
      [progress]="progress"
      [selected]="selected"
      (selected$)="onSelected()">
    </app-title-row>
  `,
})
class HostComponent {
  title: string | null = 'The Movie';
  sourceFile: string | null = '00539.mpls';
  duration: string | null = '2h 18m';
  previewUrl: string | null = null;
  status: TitleRowStatus = 'pending';
  progress: number | null = null;
  selected = false;
  selectedCount = 0;
  onSelected() { this.selectedCount++; }
}

describe('TitleRowComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders the title, source, and duration', () => {
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.title-row__name')?.textContent?.trim()).toBe('The Movie');
    expect(root.querySelector('.title-row__source')?.textContent?.trim()).toBe('00539.mpls');
    expect(root.querySelector('.title-row__duration')?.textContent?.trim()).toBe('2h 18m');
  });

  it('falls back to "Untitled" placeholder when title is empty', () => {
    host.title = null;
    fixture.detectChanges();
    const name = (fixture.nativeElement as HTMLElement).querySelector('.title-row__name');
    expect(name?.textContent?.trim()).toBe('Untitled');
    expect(name?.classList.contains('title-row__name--placeholder')).toBeTrue();
  });

  it('shows the play-icon thumb fallback when previewUrl is null', () => {
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row__thumb-icon')).toBeTruthy();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row__thumb-img')).toBeNull();
  });

  it('shows the preview image when previewUrl is set', () => {
    host.previewUrl = 'https://example.com/preview.jpg';
    fixture.detectChanges();
    const img = (fixture.nativeElement as HTMLElement).querySelector('img.title-row__thumb-img');
    expect(img).toBeTruthy();
    expect(img?.getAttribute('src')).toBe('https://example.com/preview.jpg');
  });

  it('shows the running progress when status is running and progress > 0', () => {
    host.status = 'running';
    host.progress = 42;
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row__progress')?.textContent?.trim()).toBe('42%');
  });

  it('hides the running progress when status is not running', () => {
    host.status = 'complete';
    host.progress = 100;
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row__progress')).toBeNull();
  });

  it('renders a runtime status pill for running/failed (source chips win for complete)', () => {
    host.status = 'running';
    fixture.detectChanges();
    let pill = (fixture.nativeElement as HTMLElement).querySelector('ui-pill');
    expect(pill?.textContent?.trim()).toBe('Running');
    expect(pill?.querySelector('.ui-pill')?.getAttribute('data-tone')).toBe('blue');

    // 'complete' yields the source chip set (silent here — no user/auto type
    // configured), not a "Complete" pill. The labeling step's job is to
    // surface label *source*, and the overall rip status already lives in
    // the breadcrumb / progress bar above.
    host.status = 'complete';
    fixture.detectChanges();
    pill = (fixture.nativeElement as HTMLElement).querySelector('ui-pill');
    expect(pill).toBeNull();
  });

  it('reflects selected as aria-pressed and a modifier class', () => {
    host.selected = true;
    fixture.detectChanges();
    const btn = (fixture.nativeElement as HTMLElement).querySelector('.title-row');
    expect(btn?.getAttribute('aria-pressed')).toBe('true');
    expect(btn?.classList.contains('is-selected')).toBeTrue();
  });

  it('emits selected$ on click', () => {
    (fixture.nativeElement as HTMLElement).querySelector('.title-row')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(host.selectedCount).toBe(1);
  });

  it('dims the row when status is ignored', () => {
    host.status = 'ignored';
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row.is-ignored')).toBeTruthy();
  });
});
