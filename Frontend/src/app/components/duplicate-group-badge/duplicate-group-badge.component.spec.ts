import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DuplicateGroupBadgeComponent } from './duplicate-group-badge.component';

describe('DuplicateGroupBadgeComponent', () => {
  let fixture: ComponentFixture<DuplicateGroupBadgeComponent>;
  let component: DuplicateGroupBadgeComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [DuplicateGroupBadgeComponent] }).compileComponents();
    fixture = TestBed.createComponent(DuplicateGroupBadgeComponent);
    component = fixture.componentInstance;
  });

  it('legacy: visible when effectiveGroupSize > 1 (no duplicateSiblingCount provided)', () => {
    component.duplicateInfo = {
      groupId: 'g',
      groupSize: 2,
      effectiveGroupSize: 2,
      sameAs: ['x'],
      confidence: 'high',
    } as any;
    expect(component.visible).toBeTrue();
    expect(component.countSuffix).toBe(' (2)');
  });

  it('hidden when duplicateSiblingCount === 0 (component-clip only wrapper)', () => {
    component.duplicateInfo = {
      groupId: 'g',
      groupSize: 3,
      effectiveGroupSize: 3,
      sameAs: ['x', 'y'],
      confidence: 'high',
    } as any;
    component.duplicateSiblingCount = 0;
    component.componentClipCount = 5;
    expect(component.visible).toBeFalse();
  });

  it('mixed: shows "N dupes · M clips" when both counts > 0', () => {
    component.duplicateInfo = {
      groupId: 'g',
      groupSize: 4,
      effectiveGroupSize: 4,
      sameAs: ['x', 'y', 'z'],
      confidence: 'high',
    } as any;
    component.duplicateSiblingCount = 2;
    component.componentClipCount = 3;
    expect(component.visible).toBeTrue();
    // duplicateSiblingCount=2 siblings + 1 primary = 3 duplicates total
    expect(component.countSuffix).toBe(' (3 dupes · 3 clips)');
    expect(component.badgeTitle).toContain('3 duplicates');
    expect(component.badgeTitle).toContain('3 component clips');
  });

  it('1 clip uses singular wording', () => {
    component.duplicateInfo = { groupId: 'g', groupSize: 3, effectiveGroupSize: 3, sameAs: [], confidence: 'high' } as any;
    component.duplicateSiblingCount = 2;
    component.componentClipCount = 1;
    expect(component.countSuffix).toBe(' (3 dupes · 1 clip)');
    expect(component.badgeTitle).toContain('1 component clip');
  });
});
