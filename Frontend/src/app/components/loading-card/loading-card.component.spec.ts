import { ComponentFixture, TestBed } from '@angular/core/testing';
import { LoadingCardComponent } from './loading-card.component';

describe('LoadingCardComponent', () => {
  let component: LoadingCardComponent;
  let fixture: ComponentFixture<LoadingCardComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [LoadingCardComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(LoadingCardComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('shows default message', () => {
    fixture.detectChanges();
    expect(component.message).toBe('Loading...');
  });

  it('shows custom message when set', () => {
    component.message = 'Fetching...';
    fixture.detectChanges();
    expect(component.message).toBe('Fetching...');
  });
});
