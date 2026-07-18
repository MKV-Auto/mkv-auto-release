import { ComponentFixture, TestBed } from '@angular/core/testing';
import { BehaviorSubject } from 'rxjs';
import { provideRouter } from '@angular/router';
import { AlreadyInLibraryCardComponent } from './already-in-library-card.component';
import { DiscMetadata, WorkflowService } from '../../../../services/workflow.service';

describe('AlreadyInLibraryCardComponent (#603)', () => {
  let component: AlreadyInLibraryCardComponent;
  let fixture: ComponentFixture<AlreadyInLibraryCardComponent>;
  let discs$: BehaviorSubject<DiscMetadata[]>;
  let selectedCard$: BehaviorSubject<{ type: 'drive' | 'job'; id: string } | null>;

  const finalizedDisc: DiscMetadata = {
    disc_id: 'd-finalized',
    disc_state: 'in_drive',
    mount_point: '/dev/sr0',
    finalized: true,
    finalized_release_id: 'rel-1',
    finalized_release_name: 'The Goonies',
    finalized_release_slug: 'the-goonies',
    movie_name: 'The Goonies',
  } as DiscMetadata;

  const plainDrive: DiscMetadata = {
    disc_id: 'd-plain',
    disc_state: 'in_drive',
    mount_point: '/dev/sr1',
    movie_name: 'Fresh Disc',
  } as DiscMetadata;

  beforeEach(async () => {
    discs$ = new BehaviorSubject<DiscMetadata[]>([]);
    selectedCard$ = new BehaviorSubject<{ type: 'drive' | 'job'; id: string } | null>(null);
    const workflow = {
      discs$: discs$.asObservable(),
      getSelectedCard$: () => selectedCard$.asObservable(),
    };
    await TestBed.configureTestingModule({
      imports: [AlreadyInLibraryCardComponent],
      providers: [
        provideRouter([]),
        { provide: WorkflowService, useValue: workflow },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(AlreadyInLibraryCardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders nothing when no card is selected', () => {
    discs$.next([finalizedDisc]);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.already-in-library')).toBeNull();
  });

  it('renders nothing when the selected card points at a plain (non-finalized) drive', () => {
    discs$.next([plainDrive]);
    selectedCard$.next({ type: 'drive', id: '/dev/sr1' });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.already-in-library')).toBeNull();
  });

  it('renders nothing when the selected card is a job (not a drive)', () => {
    discs$.next([finalizedDisc]);
    selectedCard$.next({ type: 'job', id: 'd-finalized' });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.already-in-library')).toBeNull();
  });

  it('renders the card with the release name and Open-in-Library link when the selected drive is finalized', () => {
    discs$.next([finalizedDisc]);
    selectedCard$.next({ type: 'drive', id: '/dev/sr0' });
    fixture.detectChanges();
    const root = fixture.nativeElement.querySelector('.already-in-library');
    expect(root).not.toBeNull();
    expect(fixture.nativeElement.querySelector('.already-in-library__title').textContent.trim()).toBe('The Goonies');
    const link = fixture.nativeElement.querySelector('.already-in-library__library-link');
    expect(link).not.toBeNull();
    expect(link.textContent.trim().startsWith('Open in Library')).toBeTrue();
  });

  it('"Open in Library" link points at /library with the release slug query param', () => {
    discs$.next([finalizedDisc]);
    selectedCard$.next({ type: 'drive', id: '/dev/sr0' });
    fixture.detectChanges();
    const link = fixture.nativeElement.querySelector('.already-in-library__library-link');
    expect(link).not.toBeNull();
    expect(link.getAttribute('href')).toContain('/library');
    expect(link.getAttribute('href')).toContain('release=the-goonies');
  });

  it('updates when the selected card changes between finalized discs', () => {
    const second: DiscMetadata = {
      ...finalizedDisc,
      disc_id: 'd-second',
      mount_point: '/dev/sr1',
      finalized_release_name: 'Tron',
      finalized_release_slug: 'tron',
    } as DiscMetadata;
    discs$.next([finalizedDisc, second]);
    selectedCard$.next({ type: 'drive', id: '/dev/sr0' });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.already-in-library__title').textContent.trim()).toBe('The Goonies');
    selectedCard$.next({ type: 'drive', id: '/dev/sr1' });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.already-in-library__title').textContent.trim()).toBe('Tron');
  });

  describe('cardTitle fallback chain', () => {
    it('prefers finalized_release_name', () => {
      expect(component.cardTitle({
        finalized_release_name: 'Release Name',
        movie_name: 'Movie Name',
        info_title: 'Info Title',
      } as DiscMetadata)).toBe('Release Name');
    });

    it('falls back to movie_name when release_name is missing', () => {
      expect(component.cardTitle({
        movie_name: 'Movie Name',
        info_title: 'Info Title',
      } as DiscMetadata)).toBe('Movie Name');
    });

    it('falls back to info_title when movie_name is missing', () => {
      expect(component.cardTitle({
        info_title: 'Info Title',
      } as DiscMetadata)).toBe('Info Title');
    });

    it('falls back to a generic label when nothing else is set', () => {
      expect(component.cardTitle({} as DiscMetadata)).toBe('Already in Library');
    });
  });
});
