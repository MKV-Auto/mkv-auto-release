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

  // #915: automatic naming replaces "clear the field to regenerate".
  describe('automatic naming toggle', () => {
    const q = (id: string) => fixture.nativeElement.querySelector(`[data-testid="${id}"]`) as HTMLInputElement;
    const autoSwitch = () => fixture.nativeElement.querySelector('[data-testid="auto-naming"] button') as HTMLButtonElement;

    it('is on by default and the name and slug are read-only', () => {
      component.labelForm = { disc_name: 'Thor - DVD', disc_slug: 'thor_-_dvd' };
      fixture.detectChanges();
      expect(autoSwitch().getAttribute('aria-checked')).toBe('true');
      expect(q('disc-name').readOnly).toBeTrue();
      expect(q('disc-slug').readOnly).toBeTrue();
    });

    it('unlocks both fields when the user has chosen their own name', () => {
      component.labelForm = { auto_naming: false, disc_name: 'Thor SteelBook' };
      fixture.detectChanges();
      expect(autoSwitch().getAttribute('aria-checked')).toBe('false');
      expect(q('disc-name').readOnly).toBeFalse();
      expect(q('disc-slug').readOnly).toBeFalse();
    });

    it('switching it off reports the choice', () => {
      component.labelForm = { disc_name: 'Thor - DVD' };
      fixture.detectChanges();
      const events: any[] = [];
      component.labelChanged.subscribe(e => events.push(e));
      autoSwitch().click();
      expect(events).toEqual([{ field: 'auto_naming', value: false }]);
    });
  });

  // #873: disc number set by hand.
  describe('disc number', () => {
    const q = (id: string) => fixture.nativeElement.querySelector(`[data-testid="${id}"]`) as HTMLInputElement | null;

    it('is editable once the disc exists', () => {
      fixture.componentRef.setInput('labelForm', { disc_id: 'd1', disc_number: 3 });
      fixture.detectChanges();
      expect(q('disc-number')?.value).toBe('3');
      fixture.componentRef.setInput('labelForm', {});
      fixture.detectChanges();
      expect(q('disc-number')).toBeNull();
    });

    it('emits a valid new number and ignores the rest', () => {
      component.labelForm = { disc_id: 'd1', disc_number: 3 };
      const seen: number[] = [];
      component.discNumberChanged.subscribe(n => seen.push(n));
      component.commitDiscNumber('1');
      component.commitDiscNumber('3');    // unchanged
      component.commitDiscNumber('0');
      component.commitDiscNumber('2.5');
      component.commitDiscNumber('');
      expect(seen).toEqual([1]);
    });

    it('offers a reset to scan order', () => {
      component.labelForm = { disc_id: 'd1', disc_number: 3 };
      fixture.detectChanges();
      let reset = 0;
      component.resetDiscNumbers.subscribe(() => reset++);
      (fixture.nativeElement.querySelector('[data-testid="reset-disc-numbers"]') as HTMLButtonElement).click();
      expect(reset).toBe(1);
    });
  });
});
