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

  it('carries no season control — the selector moved to the Disc step + titles eyebrow', () => {
    // The breadcrumb is wayfinding; the primary-season select that lived
    // here (#371/#536) now renders in WorkflowLabeling (authoritative field
    // on the Disc step, SEASON stat in the titles eyebrow).
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('select')).toBeNull();
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
