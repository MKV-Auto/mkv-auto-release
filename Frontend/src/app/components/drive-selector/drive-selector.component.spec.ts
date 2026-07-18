import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DriveSelector } from './drive-selector.component';

describe('DriveSelector', () => {
  let fixture: ComponentFixture<DriveSelector>;
  let component: DriveSelector;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [DriveSelector],
    }).compileComponents();

    fixture = TestBed.createComponent(DriveSelector);
    component = fixture.componentInstance;
    localStorage.removeItem('preferred-drive');
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('with drives=[] onChange does not throw and does not emit', () => {
    component.drives = [];
    const emitted: any[] = [];
    component.driveSelected.subscribe((d) => emitted.push(d));
    expect(() => component.onChange('1')).not.toThrow();
    expect(emitted.length).toBe(0);
    expect(component.selectedDrive).toBeNull();
  });

  it('selects drive and emits event on change', () => {
    const drives = [
      { disc_num: '1', mount_point: '/mnt/a' },
      { disc_num: '2', mount_point: '/mnt/b' },
    ];
    component.drives = drives;
    const emitted: any[] = [];
    component.driveSelected.subscribe(d => emitted.push(d));
    // Earlier specs in the full run may have already replaced
    // localStorage.setItem with a Jasmine spy — re-spying throws
    // "already been spied upon". Reuse the existing spy when present.
    const existing = (localStorage as any).setItem as any;
    const setItemSpy = existing && existing.and ? existing : spyOn(localStorage, 'setItem');
    if (existing && existing.and) existing.calls.reset();

    component.onChange('2');

    expect(component.selectedDrive?.disc_num).toBe('2');
    expect(emitted[0]?.mount_point).toBe('/mnt/b');
    expect(setItemSpy).toHaveBeenCalledWith('preferred-drive', jasmine.any(String));
    expect(setItemSpy.calls.mostRecent().args[1]).toContain('"disc_num":"2"');
  });
});
