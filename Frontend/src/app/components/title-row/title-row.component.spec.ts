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
      [userType]="userType"
      [autoType]="autoType"
      [discdbHit]="discdbHit"
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
  userType: string | null = null;
  autoType: string | null = null;
  discdbHit = false;
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

  it('leads with the type chip: amber Type? when unlabeled, amber row outline', () => {
    // The chip replaced the decorative gradient thumb (cleanup mock).
    const chip = () => (fixture.nativeElement as HTMLElement).querySelector('.title-row__chip');
    expect(chip()?.getAttribute('data-chip')).toBe('todo');
    expect(chip()?.textContent?.trim()).toBe('Type?');
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row.is-unlabeled')).toBeTruthy();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row__thumb')).toBeNull();
  });

  it('chip states: green for user-labeled, indigo for auto (DiscDB in sub-line), grey for ignored', () => {
    const chip = () => (fixture.nativeElement as HTMLElement).querySelector('.title-row__chip');

    host.userType = 'Episode';
    fixture.detectChanges();
    expect(chip()?.getAttribute('data-chip')).toBe('done');
    expect(chip()?.textContent?.trim()).toBe('Episode');

    host.userType = null;
    host.autoType = 'MainMovie';
    host.discdbHit = true;
    fixture.detectChanges();
    expect(chip()?.getAttribute('data-chip')).toBe('auto');
    expect(chip()?.textContent?.trim()).toBe('Main Movie');
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row__discdb')?.textContent?.trim()).toBe('DiscDB');

    host.autoType = 'ignore';
    fixture.detectChanges();
    expect(chip()?.getAttribute('data-chip')).toBe('off');
    expect(chip()?.textContent?.trim()).toBe('Ignored');
    // Unlabeled outline never fires on decided rows.
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row.is-unlabeled')).toBeNull();
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

  it('running shows a plain % (the number IS the information); only failed gets a pill', () => {
    host.status = 'running';
    host.progress = 42;
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.title-row__progress')?.textContent?.trim()).toBe('42%');
    expect((fixture.nativeElement as HTMLElement).querySelector('ui-pill')).toBeNull();

    host.status = 'failed';
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('ui-pill')?.textContent?.trim()).toBe('Failed');

    host.status = 'complete';
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('ui-pill')).toBeNull();
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
