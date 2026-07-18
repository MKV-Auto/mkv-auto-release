import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TitleChipsComponent, computeTitleChips } from './title-chips.component';

describe('computeTitleChips (quiet-by-default matrix)', () => {
  it('NULL + NULL → silent (no chip; labeling-complete check covers attention)', () => {
    expect(computeTitleChips(null, null)).toEqual([]);
  });

  it('user=NULL + auto=MainMovie + discdbHit=true → DiscDB chip', () => {
    const chips = computeTitleChips(null, 'MainMovie', true);
    expect(chips.length).toBe(1);
    expect(chips[0].label).toBe('DiscDB');
    expect(chips[0].tone).toBe('cyan');
  });

  it('user=NULL + auto=MainMovie + discdbHit=false → silent (auto came from scan/dedupe, not DiscDB)', () => {
    expect(computeTitleChips(null, 'MainMovie', false)).toEqual([]);
  });

  it('user=MainMovie + auto=NULL → silent (user labeled, title text signals it)', () => {
    expect(computeTitleChips('MainMovie', null)).toEqual([]);
  });

  it('user=MainMovie + auto=MainMovie + discdbHit → silent (user labeled wins; no double-pill)', () => {
    expect(computeTitleChips('MainMovie', 'MainMovie', true)).toEqual([]);
  });

  it('user=Extra + auto=MainMovie + discdbHit (disagreement) → silent (user override is quiet)', () => {
    expect(computeTitleChips('Extra', 'MainMovie', true)).toEqual([]);
  });

  it('user=ignore + auto=anything → Ignored chip (slate)', () => {
    expect(computeTitleChips('ignore', null)).toEqual([
      jasmine.objectContaining({ label: 'Ignored', tone: 'slate' }),
    ]);
    expect(computeTitleChips('ignore', 'MainMovie')).toEqual([
      jasmine.objectContaining({ label: 'Ignored', tone: 'slate' }),
    ]);
    expect(computeTitleChips('ignore', 'ignore')).toEqual([
      jasmine.objectContaining({ label: 'Ignored', tone: 'slate' }),
    ]);
  });

  it('user=NULL + auto=ignore → silent (visible for review, no chip)', () => {
    expect(computeTitleChips(null, 'ignore')).toEqual([]);
  });

  it('user=MainMovie + auto=ignore (user overrode auto-ignore) → silent', () => {
    expect(computeTitleChips('MainMovie', 'ignore')).toEqual([]);
  });

  it('empty-string types are treated as null', () => {
    expect(computeTitleChips('', '')).toEqual([]);
    expect(computeTitleChips('  ', null)).toEqual([]);
  });

  describe('chip tooltips (hover-to-learn)', () => {
    it('DiscDB chip carries a tooltip explaining the source + review CTA', () => {
      const [chip] = computeTitleChips(null, 'MainMovie', true);
      expect(chip.tooltip).toBeTruthy();
      expect(chip.tooltip).toMatch(/DiscDB/i);
      expect(chip.tooltip).toMatch(/(review|confirm|override)/i);
    });

    it('Ignored chip carries a tooltip explaining the hide-by-default behavior', () => {
      const [chip] = computeTitleChips('ignore', null);
      expect(chip.tooltip).toBeTruthy();
      expect(chip.tooltip).toMatch(/Show ignored/i);
    });
  });
});


describe('TitleChipsComponent rendering', () => {
  let component: TitleChipsComponent;
  let fixture: ComponentFixture<TitleChipsComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TitleChipsComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(TitleChipsComponent);
    component = fixture.componentInstance;
  });

  function chipsText(): string {
    fixture.detectChanges();
    return (fixture.nativeElement as HTMLElement).textContent || '';
  }

  it('renders nothing for the auto-ignore/no-user case', () => {
    component.userType = null;
    component.autoType = 'ignore';
    expect(chipsText().trim()).toBe('');
  });

  it('renders nothing for user-labeled rows (silent)', () => {
    component.userType = 'MainMovie';
    component.autoType = 'MainMovie';
    component.discdbHit = true;
    expect(chipsText().trim()).toBe('');
  });

  it('renders DiscDB chip when auto is set and discdbHit=true and user is empty', () => {
    component.userType = null;
    component.autoType = 'MainMovie';
    component.discdbHit = true;
    expect(chipsText()).toContain('DiscDB');
  });

  it('renders Ignored chip for user-ignored rows', () => {
    component.userType = 'ignore';
    component.autoType = null;
    expect(chipsText()).toContain('Ignored');
  });
});
