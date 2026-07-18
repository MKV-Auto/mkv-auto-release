import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { LabelShellComponent } from './label-shell.component';

describe('LabelShellComponent', () => {
  let component: LabelShellComponent;
  let fixture: ComponentFixture<LabelShellComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [LabelShellComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(LabelShellComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('shows release-label when labelForm has movie_id and not discdbHit', () => {
    component.labelForm = { movie_id: 'm1' };
    component.discdbHit = false;
    fixture.detectChanges();
    const releaseLabel = fixture.nativeElement.querySelector('app-release-label');
    expect(releaseLabel).toBeTruthy();
  });

  it('hides release-label when discdbHit is true', () => {
    component.labelForm = { movie_id: 'm1' };
    component.discdbHit = true;
    fixture.detectChanges();
    const releaseLabel = fixture.nativeElement.querySelector('app-release-label');
    expect(releaseLabel).toBeFalsy();
  });

  it('shows disc-label when isReleaseComplete (e.g. boxset_id set) and disc fields filled', () => {
    component.labelForm = {
      movie_id: 'm1',
      boxset_id: 'b1',
      disc_format: 'Blu-ray',
      disc_name: 'Disc 1',
      disc_slug: 'disc-1',
      disc_number: 1,
    };
    component.discdbHit = false;
    fixture.detectChanges();
    const discLabel = fixture.nativeElement.querySelector('app-disc-label');
    expect(discLabel).toBeTruthy();
  });

  it('shows disc-label when disc_slug is empty (slug generated on save)', () => {
    component.labelForm = {
      movie_id: 'm1',
      boxset_id: 'b1',
      disc_format: 'Blu-ray',
      disc_name: 'Disc 1',
      disc_slug: '',
      disc_number: 1,
    };
    component.discdbHit = false;
    fixture.detectChanges();
    const discLabel = fixture.nativeElement.querySelector('app-disc-label');
    expect(discLabel).toBeTruthy();
  });
});
