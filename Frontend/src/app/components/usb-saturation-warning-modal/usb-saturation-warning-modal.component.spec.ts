/**
 * Tests for UsbSaturationWarningModalComponent (#578).
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { UsbSaturationWarningModalComponent } from './usb-saturation-warning-modal.component';
import { UsbSaturationWarningPayload } from '../../services/usb-saturation-warning.service';


const SAMPLE: UsbSaturationWarningPayload = {
  bus: 2,
  speed_mbps: 480,
  competing_mount_points: ['/dev/sr1'],
  message: 'USB Bus 2 (480 Mbps) already hosts an active rip on /dev/sr1.',
  override_field: 'force_concurrent_on_saturated_bus',
};


describe('UsbSaturationWarningModalComponent', () => {
  let fixture: ComponentFixture<UsbSaturationWarningModalComponent>;
  let component: UsbSaturationWarningModalComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UsbSaturationWarningModalComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(UsbSaturationWarningModalComponent);
    component = fixture.componentInstance;
  });

  it('renders nothing when payload is null', () => {
    component.payload = null;
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent || '';
    expect(text).not.toContain('Proceed anyway');
  });

  it('renders the backend message + bus + competing mount_points', () => {
    component.payload = SAMPLE;
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent || '';
    expect(text).toContain('USB Bus 2');
    expect(text).toContain('480 Mbps');
    expect(text).toContain('/dev/sr1');
    expect(text).toContain('Proceed anyway');
    expect(text).toContain('Cancel');
  });

  it('emits confirm() when "Proceed anyway" is clicked', () => {
    component.payload = SAMPLE;
    fixture.detectChanges();
    spyOn(component.confirm, 'emit');

    const buttons = fixture.nativeElement.querySelectorAll('button');
    const proceedBtn = [...buttons].find((b: HTMLButtonElement) =>
      b.textContent?.includes('Proceed anyway'),
    ) as HTMLButtonElement;
    proceedBtn.click();

    expect(component.confirm.emit).toHaveBeenCalled();
  });

  it('emits dismiss() when Cancel is clicked', () => {
    component.payload = SAMPLE;
    fixture.detectChanges();
    spyOn(component.dismiss, 'emit');

    const buttons = fixture.nativeElement.querySelectorAll('button');
    const cancelBtn = [...buttons].find((b: HTMLButtonElement) =>
      b.textContent?.includes('Cancel'),
    ) as HTMLButtonElement;
    cancelBtn.click();

    expect(component.dismiss.emit).toHaveBeenCalled();
  });

  it('emits dismiss() when backdrop is clicked', () => {
    component.payload = SAMPLE;
    fixture.detectChanges();
    spyOn(component.dismiss, 'emit');

    const backdrop = fixture.nativeElement.querySelector('.usw-modal__backdrop') as HTMLElement;
    backdrop.click();

    expect(component.dismiss.emit).toHaveBeenCalled();
  });

  it('does not dismiss when the modal body is clicked (stopPropagation)', () => {
    component.payload = SAMPLE;
    fixture.detectChanges();
    spyOn(component.dismiss, 'emit');

    const modal = fixture.nativeElement.querySelector('.usw-modal') as HTMLElement;
    modal.click();

    expect(component.dismiss.emit).not.toHaveBeenCalled();
  });

  it('hides "Active rip(s) on this bus" row when no competing mount_points', () => {
    component.payload = { ...SAMPLE, competing_mount_points: [] };
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent || '';
    expect(text).not.toContain('Active rip(s)');
  });

  it('lists multiple competing mount_points comma-separated', () => {
    component.payload = {
      ...SAMPLE,
      competing_mount_points: ['/dev/sr1', '/dev/sr2', '/dev/sr3'],
    };
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent || '';
    expect(text).toContain('/dev/sr1');
    expect(text).toContain('/dev/sr2');
    expect(text).toContain('/dev/sr3');
  });
});
