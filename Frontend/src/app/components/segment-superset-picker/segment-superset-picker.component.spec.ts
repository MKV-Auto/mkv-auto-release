import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SegmentSupersetPickerComponent } from './segment-superset-picker.component';
import { SupersetCandidate } from '../../services/job.service';

describe('SegmentSupersetPickerComponent', () => {
  let component: SegmentSupersetPickerComponent;
  let fixture: ComponentFixture<SegmentSupersetPickerComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SegmentSupersetPickerComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(SegmentSupersetPickerComponent);
    component = fixture.componentInstance;
  });

  function cand(
    title_index: number,
    extras: string[],
    sorted_set_key: string,
    source_file: string | null = null,
    mpls_total_size_b: number | null = null,
  ): SupersetCandidate {
    return {
      title_index,
      source_file: source_file ?? `0${title_index}.mpls`,
      extras_clips: extras,
      extras_positions: extras.map((_, i) => i + 1),
      mpls_total_size_b,
      sorted_set_key,
    };
  }

  it('renders one cluster header per cluster', () => {
    component.clusters = [
      [cand(10, ['X'], 'a,b,c,X'), cand(11, ['X'], 'a,b,c,X')],
      [cand(20, ['Y'], 'a,b,c,Y')],
    ];
    fixture.detectChanges();
    const heads = fixture.nativeElement.querySelectorAll('.ssp-cluster-head');
    expect(heads.length).toBe(2);
  });

  it('expands the first cluster by default and renders its candidates', () => {
    component.clusters = [
      [cand(10, ['X'], 'k1', '00010.mpls'), cand(11, ['X'], 'k1', '00011.mpls')],
      [cand(20, ['Y'], 'k2')],
    ];
    fixture.detectChanges();
    const candidates = fixture.nativeElement.querySelectorAll('.ssp-candidate');
    // Two candidates from the first cluster only (second cluster collapsed).
    expect(candidates.length).toBe(2);
    expect(fixture.nativeElement.textContent).toContain('00010.mpls');
    expect(fixture.nativeElement.textContent).toContain('00011.mpls');
    expect(fixture.nativeElement.textContent).not.toContain('020.mpls');
  });

  it('toggleCluster expands a collapsed cluster and collapses the prior one', () => {
    component.clusters = [
      [cand(10, ['X'], 'k1')],
      [cand(20, ['Y'], 'k2')],
    ];
    fixture.detectChanges();
    // Click the second cluster's header — dispatches through Angular's zone
    // so OnPush change detection fires (calling the method directly from
    // the spec wouldn't, since internal state changes don't trigger OnPush).
    const heads = fixture.nativeElement.querySelectorAll('.ssp-cluster-head');
    (heads[1] as HTMLButtonElement).click();
    fixture.detectChanges();
    const candidates = fixture.nativeElement.querySelectorAll('.ssp-candidate');
    expect(candidates.length).toBe(1);
    expect(fixture.nativeElement.textContent).toContain('020.mpls');
    expect(fixture.nativeElement.textContent).not.toContain('010.mpls');
  });

  it('shows "Most likely" pill on the first (top) cluster only', () => {
    component.clusters = [
      [cand(10, ['X'], 'k1')],
      [cand(20, ['Y'], 'k2')],
    ];
    fixture.detectChanges();
    const pills = fixture.nativeElement.querySelectorAll('ui-pill[tone="amber"]');
    // Only one "Most likely" pill should render.
    expect(pills.length).toBe(1);
  });

  it('emits select(candidate) when a candidate row is clicked', () => {
    let selected: SupersetCandidate | null = null;
    component.select.subscribe((c) => (selected = c));
    const target = cand(99, ['X'], 'k1');
    component.clusters = [[target]];
    fixture.detectChanges();
    const pick = fixture.nativeElement.querySelector('.ssp-pick') as HTMLButtonElement;
    pick.click();
    expect(selected!.title_index).toBe(99);
  });

  it('disables all picks when pendingTitleIndex is set (in-flight rip)', () => {
    let selected: SupersetCandidate | null = null;
    component.select.subscribe((c) => (selected = c));
    component.clusters = [[cand(10, ['X'], 'k1'), cand(11, ['X'], 'k1')]];
    component.pendingTitleIndex = 10;
    fixture.detectChanges();
    const picks = fixture.nativeElement.querySelectorAll('.ssp-pick');
    picks.forEach((p: HTMLButtonElement) => expect(p.disabled).toBe(true));
    (picks[1] as HTMLButtonElement).click();
    expect(selected).toBeNull();
  });

  it('formatSize formats GB / MB / unknown correctly', () => {
    expect(component.formatSize(null)).toBe('unknown size');
    expect(component.formatSize(500 * 1024 * 1024)).toBe('500 MB');
    expect(component.formatSize(2.5 * 1024 ** 3)).toBe('2.5 GB');
  });

  it('totalExtrasForCluster sums extras across cluster members', () => {
    const cluster = [cand(1, ['X', 'Y'], 'k'), cand(2, ['Z'], 'k')];
    expect(component.totalExtrasForCluster(cluster)).toBe(3);
  });

  it('emits dismiss when backdrop is clicked', () => {
    let dismissed = false;
    component.dismiss.subscribe(() => (dismissed = true));
    component.clusters = [[cand(10, ['X'], 'k1')]];
    fixture.detectChanges();
    const backdrop = fixture.nativeElement.querySelector('.ssp-backdrop') as HTMLElement;
    backdrop.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    // Simulate the target being the backdrop itself via the click handler arg.
    component.onBackdropClick({
      target: backdrop,
    } as unknown as MouseEvent);
    expect(dismissed).toBe(true);
  });
});
