import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DuplicateCompareModalComponent } from './duplicate-compare-modal.component';

describe('DuplicateCompareModalComponent', () => {
  let component: DuplicateCompareModalComponent;
  let fixture: ComponentFixture<DuplicateCompareModalComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [DuplicateCompareModalComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(DuplicateCompareModalComponent);
    component = fixture.componentInstance;
  });

  function member(over: Partial<any> = {}): any {
    return {
      title_id: 't' + Math.random().toString(36).slice(2, 6),
      source_file: '00504.mpls',
      duration: 8304,
      size: 76 * 1024 ** 3,
      chapters: [{}, {}, {}, {}],
      active: false,
      ...over,
    };
  }

  it('renders one column per group member', () => {
    component.members = [member(), member(), member()];
    fixture.detectChanges();
    const cols = fixture.nativeElement.querySelectorAll('.dcm-member');
    expect(cols.length).toBe(3);
  });

  it('marks the primary member with the Primary pill', () => {
    component.members = [member({ active: true }), member()];
    fixture.detectChanges();
    const primaries = fixture.nativeElement.querySelectorAll('.dcm-member-primary');
    expect(primaries.length).toBe(1);
  });

  it('highlights the currently-edited member', () => {
    const current = member({ title_id: 'current' });
    component.members = [current, member()];
    component.currentTitleId = 'current';
    fixture.detectChanges();
    const editing = fixture.nativeElement.querySelectorAll('.dcm-member-current');
    expect(editing.length).toBe(1);
  });

  it('formatSize uses display_size when present', () => {
    expect(component.formatSize({ display_size: '12.3 GB' })).toBe('12.3 GB');
  });

  it('formatSize converts bytes to GB/MB when display_size absent', () => {
    expect(component.formatSize({ size: 2.5 * 1024 ** 3 })).toContain('GB');
    expect(component.formatSize({ size: 500 * 1024 ** 2 })).toContain('MB');
    expect(component.formatSize({})).toBe('—');
  });

  it('chaptersCount counts chapters array length', () => {
    expect(component.chaptersCount({ chapters: [{}, {}] })).toBe('2');
    expect(component.chaptersCount({})).toBe('—');
  });

  it('audioSummary formats codec + channels + language', () => {
    const m = {
      metadata_summary: {
        audio_summary: [
          { codec_name: 'eac3', channels: 5, language: 'eng' },
          { codec_name: 'dts', channels: 2 },
        ],
      },
    };
    const out = component.audioSummary(m);
    expect(out).toContain('EAC3');
    expect(out).toContain('eng');
    expect(out).toContain('DTS');
  });

  it('Make primary button hidden for the current primary, shown otherwise', () => {
    component.members = [member({ active: true }), member()];
    fixture.detectChanges();
    const btns = fixture.nativeElement.querySelectorAll('ui-btn[variant="primary"]');
    // Only the non-primary member should expose a "Make primary" button.
    expect(btns.length).toBe(1);
  });

  it('emits select with the picked member', () => {
    let picked: any = null;
    component.select.subscribe((m: any) => (picked = m));
    const target = member({ title_id: 'pick-me' });
    component.members = [member({ active: true }), target];
    fixture.detectChanges();
    component.onSelect(target);
    expect(picked.title_id).toBe('pick-me');
  });

  it('emits dismiss on backdrop click', () => {
    let dismissed = false;
    component.dismiss.subscribe(() => (dismissed = true));
    component.members = [member()];
    fixture.detectChanges();
    const backdrop = fixture.nativeElement.querySelector('.dcm-backdrop') as HTMLElement;
    component.onBackdropClick({ target: backdrop } as unknown as MouseEvent);
    expect(dismissed).toBe(true);
  });
});
