import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ReleaseLabelComponent } from './release-label.component';

describe('ReleaseLabelComponent', () => {
  let component: ReleaseLabelComponent;
  let fixture: ComponentFixture<ReleaseLabelComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ReleaseLabelComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(ReleaseLabelComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('emits groupSelected when onApplyGroup is called', () => {
    component.labelForm = {};
    const group = { release_id: 'r1', release_name: 'UHD Release', release_year: 2024 };
    spyOn(component.groupSelected, 'emit');
    fixture.detectChanges();
    component.onApplyGroup(group);
    expect(component.groupSelected.emit).toHaveBeenCalledWith(group);
  });

  it('getSelectedReleaseDisplayName returns release name when set in labelForm', () => {
    component.labelForm = { release_name: 'Collectors Edition' };
    fixture.detectChanges();
    expect(component.getSelectedReleaseDisplayName()).toBe('Collectors Edition');
  });

  it('getSelectedReleaseDisplayName returns "Select release" when nothing set', () => {
    component.labelForm = {};
    component.lastReleaseDetails = null;
    fixture.detectChanges();
    expect(component.getSelectedReleaseDisplayName()).toBe('Select release');
  });
});
