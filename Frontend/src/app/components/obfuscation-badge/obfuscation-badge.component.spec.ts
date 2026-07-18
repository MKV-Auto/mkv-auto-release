import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ObfuscationBadgeComponent } from './obfuscation-badge.component';

describe('ObfuscationBadgeComponent', () => {
  let component: ObfuscationBadgeComponent;
  let fixture: ComponentFixture<ObfuscationBadgeComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ObfuscationBadgeComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(ObfuscationBadgeComponent);
    component = fixture.componentInstance;
  });

  function html(): string {
    fixture.detectChanges();
    return (fixture.nativeElement as HTMLElement).textContent || '';
  }

  function pillTone(): string | null {
    fixture.detectChanges();
    const pill = (fixture.nativeElement as HTMLElement).querySelector('.ui-pill');
    return pill ? pill.getAttribute('data-tone') : null;
  }

  it('renders nothing when there is no signal at all', () => {
    component.flagged = false;
    component.reason = null;
    expect(html().trim()).toBe('');
    expect(pillTone()).toBeNull();
  });

  it('HIGH tier: reason="segment_set_sibling" renders a red "Decoy" pill', () => {
    component.reason = 'segment_set_sibling';
    component.flagged = true;
    expect(html()).toContain('Decoy');
    expect(html()).not.toContain('Likely decoy');
    expect(pillTone()).toBe('red');
  });

  it('HIGH tier: reason="path_a_decoy" renders a red "Decoy" pill', () => {
    component.reason = 'path_a_decoy';
    component.flagged = true;
    expect(html()).toContain('Decoy');
    expect(pillTone()).toBe('red');
  });

  it('MEDIUM tier: reason="makemkv_msg3307" renders a slate "Likely decoy" pill', () => {
    component.reason = 'makemkv_msg3307';
    component.flagged = true;
    expect(html()).toContain('Likely decoy');
    expect(pillTone()).toBe('slate');
  });

  it('legacy: flagged=true with no reason still renders the MEDIUM badge', () => {
    component.reason = null;
    component.flagged = true;
    expect(html()).toContain('Likely decoy');
    expect(pillTone()).toBe('slate');
  });

  it('reason wins over the legacy flag when both are set', () => {
    component.reason = 'segment_set_sibling';
    component.flagged = true;
    expect(html()).toContain('Decoy');
    expect(html()).not.toContain('Likely decoy');
    expect(pillTone()).toBe('red');
  });

  it('exposes a human-readable tooltip per reason', () => {
    component.reason = 'segment_set_sibling';
    component.flagged = true;
    fixture.detectChanges();
    // The `title` lives on the `<ui-pill>` host, not on the inner `.ui-pill` span.
    const host = (fixture.nativeElement as HTMLElement).querySelector('ui-pill');
    expect(host?.getAttribute('title')).toMatch(/permutation/i);
  });

  it('HIGH tier: reason="duration_short" renders a red "Decoy" pill with an arithmetic-explanation tooltip', () => {
    component.reason = 'duration_short';
    component.flagged = true;
    expect(html()).toContain('Decoy');
    expect(html()).not.toContain('Likely decoy');
    expect(pillTone()).toBe('red');
    const host = (fixture.nativeElement as HTMLElement).querySelector('ui-pill');
    expect(host?.getAttribute('title')).toMatch(/(longer than|declared)/i);
  });

  it('HIGH tier: reason="low_bitrate_decoy" renders a red "Decoy" pill with a bitrate-explanation tooltip', () => {
    component.reason = 'low_bitrate_decoy';
    component.flagged = true;
    expect(html()).toContain('Decoy');
    expect(html()).not.toContain('Likely decoy');
    expect(pillTone()).toBe('red');
    const host = (fixture.nativeElement as HTMLElement).querySelector('ui-pill');
    expect(host?.getAttribute('title')).toMatch(/(bitrate|Mbps)/i);
  });
});
