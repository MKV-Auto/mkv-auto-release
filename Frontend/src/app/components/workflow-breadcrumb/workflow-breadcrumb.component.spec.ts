import { ComponentFixture, TestBed } from '@angular/core/testing';
import { WorkflowBreadcrumbComponent } from './workflow-breadcrumb.component';
import { WorkflowStep } from '../../services/workflow.service';

describe('WorkflowBreadcrumbComponent', () => {
  let component: WorkflowBreadcrumbComponent;
  let fixture: ComponentFixture<WorkflowBreadcrumbComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [WorkflowBreadcrumbComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(WorkflowBreadcrumbComponent);
    component = fixture.componentInstance;
    // #365 Phase 2 § 6.4 — 'postprocess' removed (collapsed into transfer).
    component.steps = ['film', 'boxset', 'disc', 'titles', 'transfer'];
    component.currentStep = 'film';
    component.canNavigateToStep = () => true;
    component.getStepLabel = (s) => s;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  describe('getDisplayName', () => {
    it('returns "Name (Year)" when both set', () => {
      component.movieName = 'Test Movie';
      component.productionYear = 2020;
      expect(component.getDisplayName()).toBe('Test Movie (2020)');
    });

    it('returns "Name" when year is null', () => {
      component.movieName = 'Test Movie';
      component.productionYear = null;
      expect(component.getDisplayName()).toBe('Test Movie');
    });

    it('returns "" when no name', () => {
      component.movieName = null;
      component.productionYear = 2020;
      expect(component.getDisplayName()).toBe('');
    });
  });

  describe('getStepIndex', () => {
    it('returns index in steps', () => {
      expect(component.getStepIndex('film')).toBe(0);
      expect(component.getStepIndex('titles')).toBe(3);
      expect(component.getStepIndex('unknown' as WorkflowStep)).toBe(-1);
    });
  });

  describe('isMuted', () => {
    it('returns true when step is ahead of current and not navigable', () => {
      component.currentStep = 'film';
      component.canNavigateToStep = (s) => s === 'film';
      expect(component.isMuted('boxset')).toBeTrue();
    });

    it('returns false when step is navigable', () => {
      component.currentStep = 'film';
      component.canNavigateToStep = () => true;
      expect(component.isMuted('boxset')).toBeFalse();
    });
  });

  describe('onStepClick', () => {
    it('emits stepNavigate when canNavigateToStep is true', () => {
      component.canNavigateToStep = () => true;
      let emitted: WorkflowStep | undefined;
      component.stepNavigate.subscribe((s) => (emitted = s));
      component.onStepClick('boxset', { stopPropagation: () => {}, preventDefault: () => {}, type: 'click' } as any);
      expect(emitted).toBe('boxset');
    });

    it('does not emit when canNavigateToStep is false', () => {
      component.canNavigateToStep = () => false;
      let emitted = false;
      component.stepNavigate.subscribe(() => (emitted = true));
      component.onStepClick('boxset', { stopPropagation: () => {}, preventDefault: () => {}, type: 'click' } as any);
      expect(emitted).toBeFalse();
    });
  });

  describe('primary-season select (#536)', () => {
    it('renders Season N as selected when primarySeason input is N', () => {
      component.tvSeasonCount = 4;
      component.primarySeason = 3;
      fixture.detectChanges();
      const sel = fixture.nativeElement.querySelector('select.breadcrumb-primary-season-select') as HTMLSelectElement;
      expect(sel).toBeTruthy();
      expect(sel.value).toBe('3');
      const selectedOption = sel.options[sel.selectedIndex];
      expect(selectedOption.text).toContain('Season 3');
    });

    it('renders Season 1 as the fallback when primarySeason is null', () => {
      component.tvSeasonCount = 4;
      component.primarySeason = null;
      fixture.detectChanges();
      const sel = fixture.nativeElement.querySelector('select.breadcrumb-primary-season-select') as HTMLSelectElement;
      expect(sel.value).toBe('1');
    });

    it('hides the select entirely when tvSeasonCount is null or 0', () => {
      for (const n of [null, 0]) {
        component.tvSeasonCount = n as any;
        component.primarySeason = 2;
        fixture.detectChanges();
        const sel = fixture.nativeElement.querySelector('select.breadcrumb-primary-season-select') as HTMLSelectElement | null;
        expect(sel).toBeNull();
      }
    });

    it('emits primarySeasonChange with the picked integer on change', () => {
      component.tvSeasonCount = 4;
      component.primarySeason = 1;
      fixture.detectChanges();
      let emitted: number | undefined;
      component.primarySeasonChange.subscribe((v) => (emitted = v));
      const sel = fixture.nativeElement.querySelector('select.breadcrumb-primary-season-select') as HTMLSelectElement;
      sel.value = '3';
      sel.dispatchEvent(new Event('change'));
      expect(emitted).toBe(3);
    });
  });

  describe('toggleDropdown and closeDropdown', () => {
    it('toggleDropdown flips dropdownOpen', () => {
      expect(component.dropdownOpen).toBeFalse();
      component.toggleDropdown({ stopPropagation: () => {} } as any);
      expect(component.dropdownOpen).toBeTrue();
      component.toggleDropdown({ stopPropagation: () => {} } as any);
      expect(component.dropdownOpen).toBeFalse();
    });

    it('closeDropdown sets dropdownOpen to false', () => {
      component.dropdownOpen = true;
      component.closeDropdown();
      expect(component.dropdownOpen).toBeFalse();
    });
  });
});
