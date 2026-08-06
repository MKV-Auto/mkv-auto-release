import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SimpleChange } from '@angular/core';
import { BehaviorSubject, of } from 'rxjs';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TitleLabelComponent } from './title-label.component';
import { MobileService } from '../../services/mobile.service';

describe('TitleLabelComponent', () => {
  let component: TitleLabelComponent;
  let fixture: ComponentFixture<TitleLabelComponent>;
  /** Drives the component's isMobile state through the real subscription
   *  (OnPush: a direct field poke won't re-render). Defaults to desktop. */
  let isMobile$: BehaviorSubject<boolean>;

  /** Helper: set titles input and trigger ngOnChanges (Angular doesn't call ngOnChanges for direct property writes in tests). */
  function setTitles(titles: any[]): void {
    const prev = component.titles;
    component.titles = titles;
    component.ngOnChanges({
      titles: new SimpleChange(prev, titles, prev === undefined || prev.length === 0),
    });
  }

  beforeEach(async () => {
    isMobile$ = new BehaviorSubject<boolean>(false);
    const mobileStub = { isMobile$ };
    await TestBed.configureTestingModule({
      imports: [TitleLabelComponent],
      providers: [
        { provide: MobileService, useValue: mobileStub },
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(TitleLabelComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('typing updates the model immediately but does NOT write per keystroke', () => {
    // Every keystroke used to PATCH. The responses echoed the server value
    // back into the ngModel-bound input, so late echoes visibly re-typed the
    // field and could drop the last characters typed.
    const title = { title_id: 'tid1', title: 'Old' };
    setTitles([title]);
    spyOn(component.labelChanged, 'emit');
    spyOn(component.titlePatched, 'emit');
    fixture.detectChanges();

    for (const v of ['N', 'Ne', 'New', 'New Name']) {
      component.onTitleChange(title, v);
    }

    expect(title.title).toBe('New Name');           // model is live
    expect(component.labelChanged.emit).toHaveBeenCalled();
    expect(component.titlePatched.emit).not.toHaveBeenCalled();  // no writes yet
  });

  it('blur flushes the typed value exactly once', () => {
    const title = { title_id: 'tid1', title: 'Old' };
    setTitles([title]);
    fixture.detectChanges();
    for (const v of ['N', 'Ne', 'New Name']) component.onTitleChange(title, v);

    spyOn(component.titlePatched, 'emit');
    component.onBlur();

    expect(component.titlePatched.emit).toHaveBeenCalledTimes(1);
    expect(component.titlePatched.emit).toHaveBeenCalledWith(
      jasmine.objectContaining({ title_id: 'tid1', title: 'New Name' })
    );
  });

  it('a second blur with nothing new sends nothing', () => {
    const title = { title_id: 'tid1', title: 'Old' };
    setTitles([title]);
    fixture.detectChanges();
    component.onTitleChange(title, 'Typed');
    component.onBlur();

    spyOn(component.titlePatched, 'emit');
    spyOn(component.titleBatchPatched, 'emit');
    component.onBlur();

    expect(component.titlePatched.emit).not.toHaveBeenCalled();
    expect(component.titleBatchPatched.emit).not.toHaveBeenCalled();
  });

  it('changing the type carries the pending name in the SAME write', () => {
    // The reported sequence: type a name, then change the type. Flushing the
    // buffered name as a separate write raced the type write — both left
    // with the same base_seq, so one always lost (measured on rc.3: 7/7
    // same-tick pairs collided). One request can't race itself.
    const title = { title_id: 'tid1', title: 'Old', type: null };
    setTitles([title]);
    fixture.detectChanges();
    component.onTitleChange(title, 'Renamed');

    const seen: any[] = [];
    spyOn(component.titlePatched, 'emit').and.callFake((p: any) => {
      seen.push(p);
      return true as any;
    });
    spyOn(component.titleBatchPatched, 'emit');
    component.onTypeChange(title, 'Featurette');

    expect(seen.length).toBe(1);
    expect(seen[0]).toEqual(jasmine.objectContaining({
      title_id: 'tid1', title: 'Renamed', type: 'Featurette',
    }));
    expect(component.titleBatchPatched.emit).not.toHaveBeenCalled();

    // Nothing buffered survives to flush late and re-race the type write.
    component.onBlur();
    expect(seen.length).toBe(1);
  });

  it('teardown never strands an unsaved edit', () => {
    const title = { title_id: 'tid1', title: 'Old' };
    setTitles([title]);
    fixture.detectChanges();
    component.onTitleChange(title, 'Unsaved when leaving');

    spyOn(component.titlePatched, 'emit');
    component.ngOnDestroy();

    expect(component.titlePatched.emit).toHaveBeenCalledWith(
      jasmine.objectContaining({ title_id: 'tid1', title: 'Unsaved when leaving' })
    );
  });

  it('isIgnored returns true when title type is ignore', () => {
    expect(component.isIgnored({ type: 'ignore' })).toBe(true);
    expect(component.isIgnored({ type: 'Ignore' })).toBe(true);
    expect(component.isIgnored({ type: 'MainMovie' })).toBe(false);
    expect(component.isIgnored({})).toBe(false);
  });

  describe('getTitleDurationLabel', () => {
    it('renders seconds for sub-60s clips', () => {
      expect(component.getTitleDurationLabel({ duration: 45 })).toBe('45s');
      expect(component.getTitleDurationLabel({ duration: 10 })).toBe('10s');
      expect(component.getTitleDurationLabel({ duration: 59 })).toBe('59s');
    });

    it('renders minutes for clips < 1h', () => {
      expect(component.getTitleDurationLabel({ duration: 60 })).toBe('1m');
      expect(component.getTitleDurationLabel({ duration: 3540 })).toBe('59m');
    });

    it('renders hours+minutes for clips >= 1h', () => {
      expect(component.getTitleDurationLabel({ duration: 3600 })).toBe('1h 0m');
      expect(component.getTitleDurationLabel({ duration: 8304 })).toBe('2h 18m');
    });

    it('returns null for zero/falsy', () => {
      expect(component.getTitleDurationLabel({ duration: 0 })).toBeNull();
      expect(component.getTitleDurationLabel({})).toBeNull();
      expect(component.getTitleDurationLabel(null)).toBeNull();
    });
  });

  describe('Show ignored toggle', () => {
    it('ignoredCount counts user_type=ignore rows that are not dedupe siblings', () => {
      const titles = [
        { title_id: 'a', type: 'MainMovie', user_type: 'MainMovie', index: 0 },
        { title_id: 'b', type: 'ignore', user_type: 'ignore', index: 1 },
        { title_id: 'c', type: 'ignore', user_type: 'ignore', index: 2 },
        { title_id: 'd', type: 'Extra', user_type: 'Extra', index: 3 },
      ];
      setTitles(titles);
      expect(component.ignoredCount).toBe(2);
    });

    it('showIgnored defaults to false and toggleShowIgnored flips it', () => {
      expect(component.showIgnored).toBeFalse();
      component.toggleShowIgnored();
      expect(component.showIgnored).toBeTrue();
      component.toggleShowIgnored();
      expect(component.showIgnored).toBeFalse();
    });

    it('ignoredCount returns 0 when no titles are ignored', () => {
      setTitles([
        { title_id: 'a', type: 'MainMovie', index: 0 },
        { title_id: 'b', type: 'Extra', index: 1 },
      ]);
      expect(component.ignoredCount).toBe(0);
    });

    it('ignoredCount handles missing/empty user_type strings', () => {
      setTitles([
        { title_id: 'a', user_type: null, index: 0 },
        { title_id: 'b', index: 1 },
        { title_id: 'c', user_type: 'IGNORE', index: 2 },
      ]);
      expect(component.ignoredCount).toBe(1);
    });
  });

  describe('getComponentClips', () => {
    it('returns titles whose subsumed_by_title_id matches the active title', () => {
      const mpls = { title_id: 'mpls-1', source_file: '00451.mpls(2)' };
      const m2tsA = { title_id: 'm2ts-a', source_file: '02807.m2ts', subsumed_by_title_id: 'mpls-1' };
      const m2tsB = { title_id: 'm2ts-b', source_file: '02808.m2ts', subsumed_by_title_id: 'mpls-1' };
      const free  = { title_id: 'free', source_file: '00006.m2ts' };
      setTitles([mpls, m2tsA, m2tsB, free]);
      expect(component.getComponentClips(mpls).map(t => t.title_id))
        .toEqual(['m2ts-a', 'm2ts-b']);
    });

    it('returns empty when no clip points at this title', () => {
      const mpls = { title_id: 'mpls-1', source_file: '00539.mpls' };
      const stranger = { title_id: 'm2ts-x', subsumed_by_title_id: 'someone-else' };
      setTitles([mpls, stranger]);
      expect(component.getComponentClips(mpls)).toEqual([]);
    });

    it('falls back to selectedTitle when called with no arg', () => {
      const mpls = { title_id: 'mpls-1', source_file: '00451.mpls(2)' };
      const m2ts = { title_id: 'm2ts-a', subsumed_by_title_id: 'mpls-1' };
      setTitles([mpls, m2ts]);
      component.selectedTitleId = 'mpls-1';
      expect(component.getComponentClips().map(t => t.title_id)).toEqual(['m2ts-a']);
    });

    it('returns empty when no selection and no arg', () => {
      setTitles([{ title_id: 'a' }]);
      component.selectedTitleId = null;
      expect(component.getComponentClips()).toEqual([]);
    });
  });

  describe('subsumed m2ts folded into the wrapper dedupe group (#534)', () => {
    const mpls = { title_id: 'mpls-1', source_file: '00451.mpls', index: 5 };
    const mplsSibling = { title_id: 'mpls-2', source_file: '00452.mpls', index: 6 };
    const m2tsA = { title_id: 'm2ts-a', source_file: '02807.m2ts', subsumed_by_title_id: 'mpls-1' };
    const m2tsB = { title_id: 'm2ts-b', source_file: '02808.m2ts', subsumed_by_title_id: 'mpls-1' };
    const foldedGroup = {
      group_id: 'subsumed:abc123def456',
      sorted_segment_key: '2807,2808',
      duration_bucket_s: 8000,
      representative_title_id: 'mpls-1',
      representative_source: 'subsumption',
      sibling_title_ids: ['m2ts-a', 'm2ts-b', 'mpls-2'],
      discdb_pick_id: null,
      makemkv_flag_pick_id: null,
    } as any;

    it('hides folded m2ts from the rail via isDedupeSibling', () => {
      setTitles([mpls, mplsSibling, m2tsA, m2tsB]);
      component.dedupeGroups = [foldedGroup];
      expect(component.isDedupeSibling('m2ts-a')).toBeTrue();
      expect(component.isDedupeSibling('m2ts-b')).toBeTrue();
      expect(component.isDedupeSibling('mpls-1')).toBeFalse();
    });

    it('getEditorSiblings excludes the wrapper\'s component clips (already in Component clips section)', () => {
      setTitles([mpls, mplsSibling, m2tsA, m2tsB]);
      component.dedupeGroups = [foldedGroup];
      component.selectedTitleId = 'mpls-1';
      expect(component.getEditorSiblings().map(t => t.title_id)).toEqual(['mpls-2']);
      expect(component.getComponentClips().map(t => t.title_id)).toEqual(['m2ts-a', 'm2ts-b']);
    });
  });

  describe('component-clip vs duplicate-sibling display (#534 Phase 2)', () => {
    const wrapper = {
      title_id: 'mpls-1',
      source_file: '00451.mpls',
      duplicate_info: { group_id: 'g', same_as: ['m2ts-a', 'm2ts-b'] },
    };
    const m2tsA = {
      title_id: 'm2ts-a', source_file: '02807.m2ts', subsumed_by_title_id: 'mpls-1',
      duplicate_info: { group_id: 'g', same_as: ['mpls-1', 'm2ts-b'] },
    };
    const m2tsB = {
      title_id: 'm2ts-b', source_file: '02808.m2ts', subsumed_by_title_id: 'mpls-1',
      duplicate_info: { group_id: 'g', same_as: ['mpls-1', 'm2ts-a'] },
    };

    it('wrapper with only component-clip same_as does NOT get "duplicate" row status', () => {
      setTitles([wrapper, m2tsA, m2tsB]);
      expect(component.getTitleRowStatus(wrapper)).not.toBe('duplicate');
    });

    it('wrapper with a real-sibling permutation DOES get "duplicate" row status', () => {
      const sibling = {
        title_id: 'mpls-2', source_file: '00452.mpls',
        duplicate_info: { group_id: 'g', same_as: ['mpls-1', 'm2ts-a', 'm2ts-b'] },
      };
      const wrapperMixed = {
        ...wrapper,
        duplicate_info: { group_id: 'g', same_as: ['mpls-2', 'm2ts-a', 'm2ts-b'] },
      };
      setTitles([wrapperMixed, sibling, m2tsA, m2tsB]);
      expect(component.getTitleRowStatus(wrapperMixed)).toBe('duplicate');
    });

    it('getDuplicateSiblingCount + getComponentClipCount split same_as correctly', () => {
      setTitles([wrapper, m2tsA, m2tsB]);
      expect(component.getDuplicateSiblingCount(wrapper)).toBe(0);
      expect(component.getComponentClipCount(wrapper)).toBe(2);
    });

    it('mobile titleListRows hides subsumed m2ts and child-only "groups"', () => {
      setTitles([wrapper, m2tsA, m2tsB]);
      // Force the mobile branch to compute titleListRows.
      (component as any).isMobile = true;
      (component as any).recomputeDerivedState();
      const rows = (component as any).titleListRows as Array<{ kind: string; title?: any; group?: any }>;
      const kinds = rows.map((r) => r.kind);
      const ids = rows.map((r) =>
        r.kind === 'single' ? r.title.title_id : r.group.titles.map((t: any) => t.title_id).join(','),
      );
      // Wrapper renders as a single card; no "group" card emitted; component clips hidden.
      expect(kinds).toEqual(['single']);
      expect(ids).toEqual(['mpls-1']);
    });
  });

  describe('isMatchedCanonical', () => {
    it('returns true when title.index equals matchedCanonicalIndex', () => {
      component.matchedCanonicalIndex = 109;
      expect(component.isMatchedCanonical({ index: 109 })).toBe(true);
    });

    it('returns false when title.index differs', () => {
      component.matchedCanonicalIndex = 109;
      expect(component.isMatchedCanonical({ index: 42 })).toBe(false);
    });

    it('returns false when matchedCanonicalIndex is null', () => {
      component.matchedCanonicalIndex = null;
      expect(component.isMatchedCanonical({ index: 109 })).toBe(false);
    });

    it('returns false when title.index is missing', () => {
      component.matchedCanonicalIndex = 109;
      expect(component.isMatchedCanonical({})).toBe(false);
    });

    it('returns false when title is null/undefined', () => {
      component.matchedCanonicalIndex = 109;
      expect(component.isMatchedCanonical(null)).toBe(false);
      expect(component.isMatchedCanonical(undefined)).toBe(false);
    });
  });

  it('getDisplayOrderedTitles returns titles in stable order; order does not change when title text changes', () => {
    const t1 = { title_id: 'id-a', title: 'Alpha', type: 'MainMovie', order_index: 0 };
    const t2 = { title_id: 'id-b', title: 'Beta', type: 'Extra', order_index: 1 };
    setTitles([t1, t2]);
    fixture.detectChanges();
    const first = component.getDisplayOrderedTitles();
    expect(first.map(t => component.getTitleId(t))).toEqual(['id-a', 'id-b']);
    t1.title = 'Alpha edited';
    fixture.detectChanges();
    const second = component.getDisplayOrderedTitles();
    expect(second.map(t => component.getTitleId(t))).toEqual(['id-a', 'id-b']);
  });

  it('onBlur emits current visual order (no full re-sort) and labelBlur', () => {
    const t1 = { title_id: 'id-ignore', title: 'Z', type: 'ignore', order_index: 0 };
    const t2 = { title_id: 'id-main', title: 'A', type: 'MainMovie', order_index: 1 };
    setTitles([t1, t2]);
    fixture.detectChanges();
    spyOn(component.labelChanged, 'emit');
    spyOn(component.labelBlur, 'emit');
    component.onBlur();
    const emitted = (component.labelChanged.emit as jasmine.Spy).calls.mostRecent().args[0];
    expect(emitted.map((t: any) => component.getTitleId(t))).toEqual(['id-main', 'id-ignore']);
    expect(component.labelBlur.emit).toHaveBeenCalled();
  });

  it('preserves display order when titles is a new array reference with the same title ids', () => {
    const t1 = { title_id: 'x', title: 'A', type: 'MainMovie', order_index: 0 };
    const t2 = { title_id: 'y', title: 'B', type: 'Extra', order_index: 1 };
    const first = [t1, t2];
    setTitles(first);
    fixture.detectChanges();
    const before = component.getDisplayOrderedTitles().map((t) => t.title_id);
    const prev = component.titles;
    component.titles = [t1, t2];
    component.ngOnChanges({
      titles: new SimpleChange(prev, component.titles, false),
    });
    const after = component.getDisplayOrderedTitles().map((t) => t.title_id);
    expect(after).toEqual(before);
  });

  it('changing type between non-ignore values does not reorder rows', () => {
    const t1 = { title_id: 'x', title: 'A', type: 'MainMovie', order_index: 0 };
    const t2 = { title_id: 'y', title: 'B', type: 'Extra', order_index: 1 };
    setTitles([t1, t2]);
    fixture.detectChanges();
    const before = component.getDisplayOrderedTitles().map((t) => t.title_id);
    spyOn(component.labelChanged, 'emit');
    spyOn(component.titlePatched, 'emit');
    component.onTypeChange(t1, 'Episode');
    const after = component.getDisplayOrderedTitles().map((t) => t.title_id);
    expect(after).toEqual(before);
  });

  it('markAsIgnore moves the title to the bottom of display order', () => {
    const t1 = { title_id: 'x', title: 'A', type: 'MainMovie', order_index: 0 };
    const t2 = { title_id: 'y', title: 'B', type: 'Extra', order_index: 1 };
    setTitles([t1, t2]);
    fixture.detectChanges();
    spyOn(component.labelChanged, 'emit');
    spyOn(component.titlePatched, 'emit');
    component.markAsIgnore(t1);
    const order = component.getDisplayOrderedTitles().map((t) => t.title_id);
    expect(order).toEqual(['y', 'x']);
  });

  it('shows detection badge on desktop when title has detection_warning', () => {
    // Phase 3 layout: detection badge is projected into the title-row's
    // [uiRowSuffix] slot so it sits alongside the status pill on the right.
    setTitles([{ title_id: '1', title: 'Flagged', detection_warning: true, order_index: 0 }]);
    fixture.detectChanges();
    const badge = fixture.nativeElement.querySelector('app-title-row app-detection-badge');
    expect(badge).toBeTruthy();
  });

  it('shows detection badge on mobile when title has detection_warning', () => {
    TestBed.resetTestingModule();
    const mobileStub = { isMobile$: of(true) };
    TestBed.configureTestingModule({
      imports: [TitleLabelComponent],
      providers: [
        { provide: MobileService, useValue: mobileStub },
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
    const mobileFixture = TestBed.createComponent(TitleLabelComponent);
    const mobileComponent = mobileFixture.componentInstance;
    const prev = mobileComponent.titles;
    mobileComponent.titles = [{ title_id: '1', title: 'Flagged', detection_warning: true, order_index: 0 }];
    mobileComponent.ngOnChanges({
      titles: new SimpleChange(prev, mobileComponent.titles, true),
    });
    mobileFixture.detectChanges();
    const badge = mobileFixture.nativeElement.querySelector('app-detection-badge');
    expect(badge).toBeTruthy();
  });

  it('getDuplicateInfo sets effectiveGroupSize to 1 when only one group member is in titles (no duplicate UI)', () => {
    const title88 = {
      title_id: '88',
      title: 'Title 88',
      order_index: 0,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['125'] },
    };
    setTitles([title88]);
    fixture.detectChanges();
    const info = component.getDuplicateInfo(title88);
    expect(info).toBeTruthy();
    expect(info.groupSize).toBe(2);
    expect(info.effectiveGroupSize).toBe(1);
    // No duplicate group cards rendered when effectiveGroupSize <= 1
    expect(component.duplicateGroups.length).toBe(0);
  });

  it('getDuplicateInfo sets effectiveGroupSize to 2 when both group members are in titles (duplicate UI shown)', () => {
    const title88 = {
      title_id: '88',
      title: 'Title 88',
      order_index: 0,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['125'] },
    };
    const title125 = {
      title_id: '125',
      title: 'Title 125',
      order_index: 1,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['88'] },
    };
    setTitles([title88, title125]);
    fixture.detectChanges();
    const info = component.getDuplicateInfo(title88);
    expect(info.effectiveGroupSize).toBe(2);
    // Phase 3 layout: duplicate group is surfaced via the title-row status
    // ('duplicate' → purple pill on the row). The component's internal
    // duplicate-group bookkeeping still tracks groups as before.
    expect(component.duplicateGroups.length).toBe(1);
    expect(component.duplicateGroups[0].groupId).toBe('g1');
    expect(component.getTitleRowStatus(title88)).toBe('duplicate');
  });

  it('duplicateGroupHasNoComparativeDiff is true when every member has empty diff_tags', () => {
    const t1 = {
      title_id: 'a',
      duplicate_info: { group_id: 'g', group_size: 2, same_as: ['b'], diff_tags: [] },
    };
    const t2 = {
      title_id: 'b',
      duplicate_info: { group_id: 'g', group_size: 2, same_as: ['a'], diff_tags: [] },
    };
    setTitles([t1, t2]);
    fixture.detectChanges();
    expect(component.duplicateGroupHasNoComparativeDiff([t1, t2])).toBe(true);
  });

  it('duplicateGroupHasNoComparativeDiff is false when any member has diff_tags', () => {
    const t1 = {
      title_id: 'a',
      duplicate_info: { group_id: 'g', group_size: 2, same_as: ['b'], diff_tags: ['chapters:more'] },
    };
    const t2 = {
      title_id: 'b',
      duplicate_info: { group_id: 'g', group_size: 2, same_as: ['a'], diff_tags: [] },
    };
    setTitles([t1, t2]);
    fixture.detectChanges();
    expect(component.duplicateGroupHasNoComparativeDiff([t1, t2])).toBe(false);
  });

  it('duplicateGroupHasNoComparativeDiff is true when every member only has full-group tie diff_tags', () => {
    const dup = { group_id: 'g', group_size: 2, same_as: ['b'], diff_tags: ['chapters:more', 'audio:best'] };
    const dupB = { group_id: 'g', group_size: 2, same_as: ['a'], diff_tags: ['chapters:more', 'audio:best'] };
    const t1 = { title_id: 'a', duplicate_info: dup };
    const t2 = { title_id: 'b', duplicate_info: { ...dupB } };
    setTitles([t1, t2]);
    fixture.detectChanges();
    expect(component.duplicateGroupHasNoComparativeDiff([t1, t2])).toBe(true);
  });

  it('getVisibleDuplicateDiffTags hides comparative tags every member shares', () => {
    const t1 = {
      title_id: 'a',
      duplicate_info: { group_id: 'g', group_size: 2, same_as: ['b'], diff_tags: ['chapters:more'] },
    };
    const t2 = {
      title_id: 'b',
      duplicate_info: { group_id: 'g', group_size: 2, same_as: ['a'], diff_tags: ['chapters:more'] },
    };
    setTitles([t1, t2]);
    fixture.detectChanges();
    expect(component.getVisibleDuplicateDiffTags(t1, [t1, t2])).toEqual([]);
  });

  it('getVariantMetadataLines appends scan warning after metadata_summary lines', () => {
    const title = {
      metadata_summary: {
        quality_tier: 'high',
        quality_hints: ['4K'],
        subtitle_tier: 'full',
        subtitle_hints: [],
        audio_tier: 'surround',
        audio_hints: [],
      },
      metadata_scan: { warning: 'ffprobe failed or timed out' },
    };
    setTitles([title]);
    fixture.detectChanges();
    const lines = component.getVariantMetadataLines(title);
    expect(lines.some((l) => l.startsWith('Quality:'))).toBe(true);
    expect(lines.some((l) => l.includes('ffprobe failed'))).toBe(true);
  });

  // ── Pre-computed property tests (rendering loop fix) ──

  it('duplicateGroups is populated via ngOnChanges when titles input changes', () => {
    const t1 = {
      title_id: 'a', title: 'A', order_index: 0,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['b'] },
    };
    const t2 = {
      title_id: 'b', title: 'B', order_index: 1,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['a'] },
    };
    const t3 = { title_id: 'c', title: 'C', order_index: 2 };
    setTitles([t1, t2, t3]);
    fixture.detectChanges();
    expect(component.duplicateGroups.length).toBe(1);
    expect(component.duplicateGroups[0].titles.length).toBe(2);
  });

  it('singleTitles excludes duplicate group members', () => {
    const t1 = {
      title_id: 'a', title: 'A', order_index: 0,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['b'] },
    };
    const t2 = {
      title_id: 'b', title: 'B', order_index: 1,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['a'] },
    };
    const t3 = { title_id: 'c', title: 'C', type: 'MainMovie', order_index: 2 };
    setTitles([t1, t2, t3]);
    fixture.detectChanges();
    expect(component.singleTitles.length).toBe(1);
    expect(component.singleTitles[0].title_id).toBe('c');
  });

  it('duplicateGroups and singleTitles update when titles input changes', () => {
    // Start with no duplicates
    const t1 = { title_id: 'x', title: 'X', type: 'MainMovie', order_index: 0 };
    setTitles([t1]);
    fixture.detectChanges();
    expect(component.duplicateGroups.length).toBe(0);
    expect(component.singleTitles.length).toBe(1);

    // Add duplicate pair
    const d1 = {
      title_id: 'a', title: 'A', order_index: 1,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['b'] },
    };
    const d2 = {
      title_id: 'b', title: 'B', order_index: 2,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['a'] },
    };
    setTitles([t1, d1, d2]);
    fixture.detectChanges();
    expect(component.duplicateGroups.length).toBe(1);
    expect(component.singleTitles.length).toBe(1);
    expect(component.singleTitles[0].title_id).toBe('x');
  });

  it('expandedGroups is initialized for new groups in recomputeDerivedState', () => {
    const t1 = {
      title_id: 'a', title: 'A', order_index: 0,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['b'] },
    };
    const t2 = {
      title_id: 'b', title: 'B', order_index: 1,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['a'] },
    };
    setTitles([t1, t2]);
    fixture.detectChanges();
    expect(component.expandedGroups.has('g1')).toBe(true);
    expect(component.isGroupExpanded('g1')).toBe(true);
  });

  it('getEffectivePreviewState does not mutate retryingPreviews set', () => {
    const title = { title_id: 'p1', title: 'Preview Title' };
    component.previewStateFn = () => ({ status: 'completed' });
    setTitles([title]);
    fixture.detectChanges();

    // Simulate retry click (adds to retryingPreviews)
    component.retryPreview(title);

    // Now getEffectivePreviewState should NOT delete from retryingPreviews
    // even though real state is no longer 'failed'
    const state = component.getEffectivePreviewState(title);
    // It should return the real state (completed) since backend confirmed
    expect(state?.status).toBe('completed');
    // Calling it again should return the same result (method is pure)
    const state2 = component.getEffectivePreviewState(title);
    expect(state2?.status).toBe('completed');
  });

  it('getDisplayOrderedTitles does not mutate displayOrderIds when called during template eval', () => {
    const t1 = { title_id: 'id-a', title: 'Alpha', type: 'MainMovie', order_index: 0 };
    const t2 = { title_id: 'id-b', title: 'Beta', type: 'Extra', order_index: 1 };
    setTitles([t1, t2]);
    fixture.detectChanges();
    const result1 = component.getDisplayOrderedTitles();
    const result2 = component.getDisplayOrderedTitles();
    // Both calls should return the same order
    expect(result1.map(t => t.title_id)).toEqual(result2.map(t => t.title_id));
  });

  it('trackByGroupId returns groupId', () => {
    expect(component.trackByGroupId(0, { groupId: 'g1' })).toBe('g1');
    expect(component.trackByGroupId(3, { groupId: '' })).toBe('');
    expect(component.trackByGroupId(5, null as any)).toBe('group-5');
  });

  it('onBlur recomputes singleTitles and duplicateGroups', () => {
    const t1 = { title_id: 'x', title: 'X', type: 'MainMovie', order_index: 0 };
    setTitles([t1]);
    fixture.detectChanges();
    expect(component.singleTitles.length).toBe(1);

    // Spy on labelChanged to verify onBlur emits
    spyOn(component.labelChanged, 'emit');
    spyOn(component.labelBlur, 'emit');
    component.onBlur();
    expect(component.singleTitles.length).toBe(1);
    expect(component.labelChanged.emit).toHaveBeenCalled();
    expect(component.labelBlur.emit).toHaveBeenCalled();
  });

  it('duplicate group onTypeChange to ignore emits titleBatchPatched for all members', () => {
    const t1 = {
      title_id: 'a',
      title: 'Shared',
      type: 'MainMovie',
      active: true,
      order_index: 0,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['b'] },
    };
    const t2 = {
      title_id: 'b',
      title: 'Shared',
      type: 'MainMovie',
      active: false,
      order_index: 1,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['a'] },
    };
    setTitles([t1, t2]);
    fixture.detectChanges();
    spyOn(component.titleBatchPatched, 'emit');
    spyOn(component.titlePatched, 'emit');
    component.onTypeChange(t1, 'ignore');
    expect(component.titleBatchPatched.emit).toHaveBeenCalled();
    expect(component.titlePatched.emit).not.toHaveBeenCalled();
    const patches = (component.titleBatchPatched.emit as jasmine.Spy).calls.mostRecent().args[0];
    expect(patches.length).toBe(2);
    expect(new Set(patches.map((p: { title_id: string }) => p.title_id))).toEqual(new Set(['a', 'b']));
    expect(t1.type).toBe('ignore');
    expect(t2.type).toBe('ignore');
  });

  it('duplicate group markAsIgnore collapses expanded state; un-ignore restores expand', () => {
    const t1 = {
      title_id: 'a',
      title: 'X',
      type: 'MainMovie',
      active: true,
      order_index: 0,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['b'] },
    };
    const t2 = {
      title_id: 'b',
      title: 'X',
      type: 'MainMovie',
      active: false,
      order_index: 1,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['a'] },
    };
    setTitles([t1, t2]);
    fixture.detectChanges();
    expect(component.isGroupExpanded('g1')).toBe(true);
    component.markAsIgnore(t1);
    expect(component.isGroupExpanded('g1')).toBe(false);
    component.markAsIgnore(t1);
    expect(component.isGroupExpanded('g1')).toBe(true);
  });

  it('duplicate group with active primary and ignored secondary shows duplicate-group-card (not ignored shell)', () => {
    const t1 = {
      title_id: 'a',
      title: 'Feature',
      type: 'MainMovie',
      active: true,
      order_index: 0,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['b'] },
    };
    const t2 = {
      title_id: 'b',
      title: '',
      type: 'ignore',
      active: false,
      order_index: 1,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['a'] },
    };
    setTitles([t1, t2]);
    fixture.detectChanges();
    // Phase 3: the legacy `.duplicate-group-card` / `.title-card-desktop`
    // shells were removed when desktop swapped to TitleRow + TitleEditor.
    // Behavior preserved at the component level: the group is still tracked
    // as non-ignored, the active primary still surfaces a duplicate status,
    // and the ignored secondary surfaces an ignored status.
    expect(component.isDuplicateGroupIgnored({ groupId: 'g1', titles: [t1, t2] })).toBe(false);
    expect(component.getTitleRowStatus(t1)).toBe('duplicate');
    expect(component.getTitleRowStatus(t2)).toBe('ignored');
  });

  it('ignored duplicate group renders title-card-desktop shell not duplicate-group-card', () => {
    const t1 = {
      title_id: 'a',
      title: '',
      type: 'ignore',
      active: true,
      order_index: 0,
      source_file: 'a.mkv',
      duration: 60,
      size: 1024,
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['b'] },
    };
    const t2 = {
      title_id: 'b',
      title: '',
      type: 'ignore',
      active: false,
      order_index: 1,
      source_file: 'b.mkv',
      duplicate_info: { group_id: 'g1', group_size: 2, same_as: ['a'] },
    };
    setTitles([t1, t2]);
    // Ignored rows are hidden behind the Show-ignored toggle by default
    // (prototype pattern). Flip the toggle on so the DOM-level assertions
    // still see them rendered.
    component.showIgnored = true;
    fixture.detectChanges();
    // Phase 3: an ignored duplicate group renders both members as
    // `is-ignored` title rows. The legacy `.title-card-desktop.ignored` shell
    // is gone; the rows expose the ignored state via a class + ignored pill.
    expect(component.isDuplicateGroupIgnored({ groupId: 'g1', titles: [t1, t2] })).toBe(true);
    expect(component.getTitleRowStatus(t1)).toBe('ignored');
    expect(component.getTitleRowStatus(t2)).toBe('ignored');
    const ignoredRows = fixture.nativeElement.querySelectorAll('app-title-row .title-row.is-ignored');
    expect(ignoredRows.length).toBe(2);
  });

  describe('#701: quick-ignore + mobile quick-preview', () => {
    it('desktop rows expose a quick-ignore control that toggles ignore', () => {
      const t = { title_id: 'a', title: 'Feature', type: 'MainMovie', index: 0 };
      setTitles([t]);
      const spy = spyOn(component, 'markAsIgnore').and.callThrough();
      fixture.detectChanges();
      const btn: HTMLElement | null = fixture.nativeElement.querySelector('.title-label-row-ignore');
      expect(btn).toBeTruthy();
      btn!.click();
      expect(spy).toHaveBeenCalledWith(t);
    });

    it('getQuickPreviewUrl reflects previewUrlFn and is null-safe', () => {
      component.previewUrlFn = (t: any) => (t?.title_id === 'has' ? 'http://p/1.m3u8' : null);
      expect(component.getQuickPreviewUrl({ title_id: 'has' })).toBe('http://p/1.m3u8');
      expect(component.getQuickPreviewUrl({ title_id: 'none' })).toBeNull();
      expect(component.getQuickPreviewUrl(null)).toBeNull();
    });

    it('getQuickPreviewUrl swallows a throwing previewUrlFn', () => {
      component.previewUrlFn = () => { throw new Error('boom'); };
      expect(component.getQuickPreviewUrl({ title_id: 'x' })).toBeNull();
    });

    it('openQuickPreview opens only when a clip exists; close clears it', () => {
      component.previewUrlFn = (t: any) => (t?.title_id === 'has' ? 'http://p/1.m3u8' : null);
      component.openQuickPreview({ title_id: 'none' });
      expect(component.quickPreviewTitle).toBeNull(); // disabled path is a no-op
      const withClip = { title_id: 'has' };
      component.openQuickPreview(withClip);
      expect(component.quickPreviewTitle).toBe(withClip);
      component.closeQuickPreview();
      expect(component.quickPreviewTitle).toBeNull();
    });

    it('the mobile preview button is disabled when no clip is available', () => {
      fixture.detectChanges();      // ngOnInit wires the isMobile$ subscription
      isMobile$.next(true);         // drive mobile through the real stream (→ markForCheck)
      component.previewUrlFn = (t: any) => (t?.title_id === 'has' ? 'http://p/1.m3u8' : null);
      setTitles([
        { title_id: 'has', title: 'A', type: 'MainMovie', index: 0 },
        { title_id: 'none', title: 'B', type: 'MainMovie', index: 1 },
      ]);
      fixture.detectChanges();
      const btns = fixture.nativeElement.querySelectorAll('.title-card-preview-btn') as NodeListOf<HTMLButtonElement>;
      expect(btns.length).toBe(2);
      const disabled = Array.from(btns).map((b) => b.disabled);
      expect(disabled).toContain(true);   // the 'none' title
      expect(disabled).toContain(false);  // the 'has' title
    });
  });
});
