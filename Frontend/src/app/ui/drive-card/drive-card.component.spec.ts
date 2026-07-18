import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DriveCardComponent, DriveCardData } from './drive-card.component';

function makeDrive(over: Partial<DriveCardData> = {}): DriveCardData {
  return {
    id: 'sr0',
    title: 'Inception',
    meta: 'Blu-ray · Disc 1',
    mount: '/dev/sr0',
    state: 'idle',
    ...over,
  };
}

describe('DriveCardComponent', () => {
  let fixture: ComponentFixture<DriveCardComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [DriveCardComponent] }).compileComponents();
    fixture = TestBed.createComponent(DriveCardComponent);
  });

  it('renders the title, meta, and mount', () => {
    fixture.componentRef.setInput('drive', makeDrive());
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.ui-drivecard__title')?.textContent?.trim()).toBe('Inception');
    expect(root.querySelector('.ui-drivecard__meta')?.textContent?.trim()).toBe('Blu-ray · Disc 1');
    expect(root.querySelector('.ui-drivecard__mount')?.textContent?.trim()).toBe('/dev/sr0');
  });

  it('shows the progress ring when ripping', () => {
    fixture.componentRef.setInput('drive', makeDrive({ state: 'canonical_ripping', progress: 42 }));
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('ui-progress-ring')).toBeTruthy();
    expect(root.querySelector('.ui-drivecard__sub')?.textContent?.trim()).toBe('Ripping main feature... 42%');
  });

  it('shows the spinner indicator while matching playlists', () => {
    fixture.componentRef.setInput('drive', makeDrive({ state: 'matching_playlists' }));
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.ui-drivecard__indicator--spin')).toBeTruthy();
    expect(root.querySelector('ui-progress-ring')).toBeNull();
  });

  it('shows an amber pill for awaiting_user_choice instead of the sub label', () => {
    fixture.componentRef.setInput('drive', makeDrive({ state: 'awaiting_user_choice' }));
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const pill = root.querySelector('ui-pill');
    expect(pill).toBeTruthy();
    expect(pill?.textContent?.trim()).toBe('Action needed');
    expect(root.querySelector('.ui-drivecard__sub')).toBeNull();
  });

  it('renders the attention dot when attention flag is set', () => {
    fixture.componentRef.setInput('drive', makeDrive({ attention: true }));
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-drivecard__dot')).toBeTruthy();
  });

  it('emits the drive on click', () => {
    const drive = makeDrive();
    fixture.componentRef.setInput('drive', drive);
    fixture.detectChanges();
    let received: DriveCardData | undefined;
    fixture.componentInstance.clicked.subscribe((d) => (received = d));
    (fixture.nativeElement as HTMLElement).querySelector('button')?.click();
    expect(received).toBe(drive);
  });

  it('reflects active as a modifier class', () => {
    fixture.componentRef.setInput('drive', makeDrive());
    fixture.componentRef.setInput('active', true);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-drivecard--active')).toBeTruthy();
  });
});
