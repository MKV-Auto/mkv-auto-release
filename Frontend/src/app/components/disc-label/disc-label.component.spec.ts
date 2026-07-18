import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DiscLabelComponent } from './disc-label.component';

describe('DiscLabelComponent', () => {
  let component: DiscLabelComponent;
  let fixture: ComponentFixture<DiscLabelComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [DiscLabelComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(DiscLabelComponent);
    component = fixture.componentInstance;
    component.labelForm = {};
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  describe('missingDiscFormat', () => {
    it('is true when labelForm.disc_format is empty', () => {
      component.labelForm = {};
      expect(component.missingDiscFormat).toBeTrue();
    });

    it('is true when labelForm.disc_format is null', () => {
      component.labelForm = { disc_format: null };
      expect(component.missingDiscFormat).toBeTrue();
    });

    it('is false when labelForm.disc_format is set', () => {
      component.labelForm = { disc_format: 'Blu-Ray' };
      expect(component.missingDiscFormat).toBeFalse();
    });
  });

  describe('onDiscFormatChange', () => {
    it('emits labelChanged', () => {
      let emitted = false;
      component.labelChanged.subscribe(() => (emitted = true));
      component.onDiscFormatChange();
      expect(emitted).toBeTrue();
    });
  });

  describe('showHeading', () => {
    it('shows full header (icon, title, subtitle) when showHeading is true', () => {
      component.labelForm = { disc_number: 1 };
      component.showHeading = true;
      fixture.detectChanges();
      const header = fixture.nativeElement.querySelector('.disc-step-header');
      const inline = fixture.nativeElement.querySelector('.disc-step-status-inline');
      expect(header).toBeTruthy();
      expect(inline).toBeFalsy();
    });

    it('hides full header and shows compact status when showHeading is false', () => {
      component.labelForm = { disc_number: 1 };
      component.showHeading = false;
      fixture.detectChanges();
      const header = fixture.nativeElement.querySelector('.disc-step-header');
      const inline = fixture.nativeElement.querySelector('.disc-step-status-inline');
      expect(header).toBeFalsy();
      expect(inline).toBeTruthy();
    });
  });
});
