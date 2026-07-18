/**
 * library-disc-drawer — Phase 4 spec (#500).
 * Covers: open/close lifecycle, disc-load, breadcrumb, disc-level edit
 * (delta payload + emit), per-title edit debounced PATCH dispatch,
 * file_path stage labelling, and the v2 Rename slot being disabled.
 */
import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { of, throwError } from 'rxjs';

import { LibraryDiscDrawerComponent } from './library-disc-drawer.component';
import {
  MetadataService,
  DiscSummary,
  DiscRecord,
  ReleaseSummary,
} from '../../services/metadata.service';
import { WorkflowService } from '../../services/workflow.service';
import { ToastService } from '../../services/toast.service';
import { LoggerService } from '../../services/logger.service';
import { DiscDbService } from '../../services/discdb.service';

function makeDisc(over: Partial<DiscSummary> = {}): DiscSummary {
  return {
    id: over.id ?? 'd-1',
    content_hash: over.content_hash ?? 'h-1',
    label_present: over.label_present ?? true,
    finalized: over.finalized ?? false,
    disc_number: over.disc_number ?? 1,
    disc_name: over.disc_name ?? 'Theatrical',
    ...over,
  } as DiscSummary;
}

function makeRelease(over: Partial<ReleaseSummary> = {}): ReleaseSummary {
  return {
    id: over.id ?? 'r-1',
    slug: over.slug ?? 'wednesday',
    type: over.type ?? 'series',
    name: over.name ?? 'Wednesday',
    production_year: over.production_year ?? 2022,
    ...over,
  } as ReleaseSummary;
}

function makeRecord(over: Partial<DiscRecord> = {}): DiscRecord {
  return {
    id: over.id ?? 'd-1',
    content_hash: over.content_hash ?? 'h-1',
    disc_number: 1,
    disc_name: 'Theatrical',
    format: 'Blu-Ray',
    finalized: false,
    titles: [
      { title_id: 't-1', title: 'Pilot', type: 'Episode', season: 1, episode: 1,
        description: '', edition: '', duration: 3540,
        file_path: '/library/Series/Wednesday/Season 01/Wednesday - s01e01 - Pilot.mkv',
        file_path_stage: 'transfer', title_seq: 4 },
      { title_id: 't-2', title: '', type: '', season: null, episode: null,
        description: '', edition: '', duration: 60,
        file_path: '/data/mkvauto/data/jobs/abc/transient/Movies/X.mkv',
        file_path_stage: 'postprocess', title_seq: 0 },
    ] as any,
    ...over,
  } as DiscRecord;
}

describe('LibraryDiscDrawerComponent (Phase 4)', () => {
  let fixture: ComponentFixture<LibraryDiscDrawerComponent>;
  let component: LibraryDiscDrawerComponent;
  let metadataSpy: jasmine.SpyObj<MetadataService>;
  let workflowSpy: jasmine.SpyObj<WorkflowService>;
  let discdbSpy: jasmine.SpyObj<DiscDbService>;
  let toastSpy: jasmine.SpyObj<ToastService>;

  beforeEach(async () => {
    metadataSpy = jasmine.createSpyObj('MetadataService', ['getDiscRecord', 'patchDiscRecord']);
    workflowSpy = jasmine.createSpyObj('WorkflowService', ['patchDiscTitle']);
    discdbSpy = jasmine.createSpyObj('DiscDbService', ['getContributionBundle']);
    toastSpy = jasmine.createSpyObj('ToastService', ['show']);
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);

    metadataSpy.getDiscRecord.and.returnValue(of(makeRecord()));
    workflowSpy.patchDiscTitle.and.returnValue(of({
      result: { title_id: 't-1', success: true, updated_title: {} },
      titles_version: 1,
    } as any));

    await TestBed.configureTestingModule({
      imports: [LibraryDiscDrawerComponent, HttpClientTestingModule],
      providers: [
        { provide: MetadataService, useValue: metadataSpy },
        { provide: WorkflowService, useValue: workflowSpy },
        { provide: ToastService, useValue: toastSpy },
        { provide: LoggerService, useValue: loggerSpy },
        { provide: DiscDbService, useValue: discdbSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(LibraryDiscDrawerComponent);
    component = fixture.componentInstance;
  });

  it('does not load anything when disc is null', () => {
    fixture.componentRef.setInput('disc', null);
    fixture.detectChanges();
    expect(metadataSpy.getDiscRecord).not.toHaveBeenCalled();
  });

  // #531: the virtual-scroll spacer is computed from itemSize. If the real
  // row height drifts from it, the scroll range is wrong and bottom titles
  // become unreachable (shipped bug: itemSize 160 vs ~418px real rows on a
  // 26-title disc → only the top half scrollable). Pin the contract:
  // declared itemSize == enforced CSS row height, and the content actually
  // fits inside that box (overflow:hidden would otherwise clip silently).
  it('virtual-scroll itemSize matches the rendered fixed row height (#531)', fakeAsync(() => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    tick(500);
    fixture.detectChanges();

    const viewport: HTMLElement | null = fixture.nativeElement.querySelector('cdk-virtual-scroll-viewport');
    expect(viewport).withContext('titles viewport should render').not.toBeNull();
    const declared = Number(viewport!.getAttribute('itemsize'));
    // #601 redesign dropped this from 420 (the old stacked-form layout)
    // to 200 (compact card). The next bullet still asserts the rendered
    // CSS height equals whatever is declared here.
    expect(declared).toBe(200);

    const row: HTMLElement | null = fixture.nativeElement.querySelector('.library-disc-drawer__title-row');
    expect(row).withContext('at least one title row should render').not.toBeNull();
    expect(Math.round(row!.getBoundingClientRect().height))
      .withContext('CSS row height must equal the declared itemSize')
      .toBe(declared);
    expect(row!.scrollHeight)
      .withContext('row content must fit the fixed box — a new field made it overflow; grow itemSize + height together')
      .toBeLessThanOrEqual(row!.clientHeight + 1);
  }));

  // --- #601 compact-card redesign --------------------------------------------

  describe('#601 compact-card row', () => {
    it('typeChipTone maps named types to the right palette tone', () => {
      expect(component.typeChipTone('MainMovie')).toBe('cyan');
      expect(component.typeChipTone('Main Movie')).toBe('cyan');
      expect(component.typeChipTone('Episode')).toBe('indigo');
      expect(component.typeChipTone('Featurette')).toBe('amber');
      expect(component.typeChipTone('Interview')).toBe('amber');
      expect(component.typeChipTone('BehindTheScenes')).toBe('amber');
      expect(component.typeChipTone('Trailer')).toBe('blue');
      expect(component.typeChipTone('Other')).toBe('slate');
      // Unset and Ignore return null so the row has no accent / no chip.
      expect(component.typeChipTone('')).toBeNull();
      expect(component.typeChipTone('Ignore')).toBeNull();
    });

    it('typeChipLabel returns the dropdown label or null for unset', () => {
      expect(component.typeChipLabel('MainMovie')).toBe('Main Movie');
      expect(component.typeChipLabel('Main Movie')).toBe('Main Movie');
      expect(component.typeChipLabel('Episode')).toBe('Episode');
      expect(component.typeChipLabel('')).toBeNull();
    });

    it('row exposes data-tone attribute driving the left-edge accent', fakeAsync(() => {
      const record = makeRecord({
        titles: [
          { title_id: 't-1', title: 'Main', type: 'MainMovie', duration: 7200,
            file_path: null, file_path_stage: null, season: null, episode: null,
            edition: '', description: '' },
          { title_id: 't-2', title: '(unset)', type: '', duration: 60,
            file_path: null, file_path_stage: null, season: null, episode: null,
            edition: '', description: '' },
        ] as any,
      });
      metadataSpy.getDiscRecord.and.returnValue(of(record));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      tick(500);
      fixture.detectChanges();

      const rows: NodeListOf<HTMLElement> = fixture.nativeElement.querySelectorAll('.library-disc-drawer__title-row');
      expect(rows.length).toBeGreaterThanOrEqual(2);
      expect(rows[0].getAttribute('data-tone')).toBe('cyan');
      expect(rows[1].getAttribute('data-tone')).toBeNull();
    }));

    it('drops the disabled v2 Rename button from the per-row footer', () => {
      fixture.componentRef.setInput('disc', makeDisc());
      fixture.detectChanges();
      const renameBtns = fixture.nativeElement.querySelectorAll('.library-disc-drawer__rename');
      expect(renameBtns.length).toBe(0);
    });
  });

  // --- #601 follow-up: view/edit toggle per row ------------------------------

  describe('#601 view/edit toggle', () => {
    function findRow(): HTMLElement {
      return fixture.nativeElement.querySelector('.library-disc-drawer__title-row') as HTMLElement;
    }

    beforeEach(() => {
      const record = makeRecord({
        titles: [
          { title_id: 't-1', title: 'Pilot', type: 'Episode', season: 1, episode: 1,
            edition: '', description: '', duration: 3540,
            file_path: null, file_path_stage: null },
          { title_id: 't-2', title: '', type: '', season: null, episode: null,
            edition: '', description: '', duration: 60,
            file_path: null, file_path_stage: null },
        ] as any,
      });
      metadataSpy.getDiscRecord.and.returnValue(of(record));
    });

    it('defaults to display mode — title rendered as text, inputs hidden, Edit button visible', fakeAsync(() => {
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      tick(500);
      fixture.detectChanges();

      const row = findRow();
      const titleText = row.querySelector('.library-disc-drawer__title-title-text');
      expect(titleText).withContext('display-mode title text should render').not.toBeNull();
      expect(titleText!.textContent?.trim()).toBe('Pilot');
      expect(row.querySelector('.library-disc-drawer__title-title'))
        .withContext('input field should NOT render in display mode')
        .toBeNull();
      expect(row.querySelector('.library-disc-drawer__title-edit-btn'))
        .withContext('Edit button must be visible in display mode')
        .not.toBeNull();
    }));

    it('clicking Edit switches the row to edit mode (inputs render, button reads "Done")', fakeAsync(() => {
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      tick(500);
      fixture.detectChanges();

      const row = findRow();
      (row.querySelector('.library-disc-drawer__title-edit-btn') as HTMLButtonElement).click();
      fixture.detectChanges();

      expect(component.editingTitleId).toBe('t-1');
      expect(row.querySelector('.library-disc-drawer__title-title'))
        .withContext('input field must render in edit mode')
        .not.toBeNull();
      expect(row.querySelector('.library-disc-drawer__title-title-text'))
        .withContext('display-mode text must hide in edit mode')
        .toBeNull();
      const doneBtn = row.querySelector('.library-disc-drawer__title-edit-btn--done');
      expect(doneBtn).withContext('button should flip to Done variant').not.toBeNull();
      expect(doneBtn!.textContent?.trim()).toBe('Done');
    }));

    it('clicking Done collapses back to display mode', fakeAsync(() => {
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      tick(500);
      fixture.detectChanges();

      const row = findRow();
      (row.querySelector('.library-disc-drawer__title-edit-btn') as HTMLButtonElement).click();
      fixture.detectChanges();
      expect(component.editingTitleId).toBe('t-1');

      (row.querySelector('.library-disc-drawer__title-edit-btn--done') as HTMLButtonElement).click();
      fixture.detectChanges();

      expect(component.editingTitleId).toBeNull();
      expect(row.querySelector('.library-disc-drawer__title-title-text'))
        .withContext('display-mode text should return after Done')
        .not.toBeNull();
    }));

    it('rowSubline formats edition · description with both, single piece, or empty', () => {
      expect(component.rowSubline({ edition: 'Director\'s Cut', description: 'Extended notes' } as any))
        .toBe("Director's Cut · Extended notes");
      expect(component.rowSubline({ edition: '', description: 'Just description' } as any))
        .toBe('Just description');
      expect(component.rowSubline({ edition: 'Edition only', description: '' } as any))
        .toBe('Edition only');
      expect(component.rowSubline({ edition: '', description: '' } as any)).toBeNull();
    });

    it('unset title renders as "Untitled" with a muted class', fakeAsync(() => {
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      tick(500);
      fixture.detectChanges();

      const rows = fixture.nativeElement.querySelectorAll('.library-disc-drawer__title-row');
      const secondTitle = rows[1].querySelector('.library-disc-drawer__title-title-text');
      expect(secondTitle?.textContent?.trim()).toBe('Untitled');
      expect(secondTitle?.classList.contains('is-muted')).toBeTrue();
    }));

    it('startEditing is a no-op when the disc is finalized', () => {
      fixture.componentRef.setInput('disc', makeDisc({ finalized: true }));
      fixture.detectChanges();
      component.startEditing({ title_id: 't-1' } as any);
      expect(component.editingTitleId).toBeNull();
    });
  });

  it('loads the disc record on open and populates the title list', () => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    expect(metadataSpy.getDiscRecord).toHaveBeenCalledWith('d-1');
    expect(component.discRecord?.disc_name).toBe('Theatrical');
    expect(component.titles.length).toBe(2);
    expect(component.titles[0].title).toBe('Pilot');
    expect(component.titles[0].file_path_stage).toBe('transfer');
  });

  it('breadcrumb shows release name and disc label', () => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.componentRef.setInput('release', makeRelease());
    fixture.componentRef.setInput('releaseDisplayName', 'Wednesday (2022)');
    fixture.detectChanges();
    expect(component.breadcrumb).toBe('Wednesday (2022) / Disc 1: Theatrical');
  });

  it('saveDiscForm PATCHes only changed fields and emits discUpdated', () => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();

    metadataSpy.patchDiscRecord.and.returnValue(of(makeRecord({ disc_name: 'Director\'s Cut' })));
    let emitted: DiscRecord | null = null;
    component.discUpdated.subscribe((r) => (emitted = r));

    component.discForm.disc_name = "Director's Cut";
    component.saveDiscForm();

    expect(metadataSpy.patchDiscRecord).toHaveBeenCalledWith('d-1', {
      disc_name: "Director's Cut",
    });
    expect(emitted!.disc_name).toBe("Director's Cut");
  });

  it('saveDiscForm no-op when nothing changed', () => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    component.saveDiscForm();
    expect(metadataSpy.patchDiscRecord).not.toHaveBeenCalled();
  });

  it('selectFormat triggers a PATCH with the new format', () => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();

    metadataSpy.patchDiscRecord.and.returnValue(of(makeRecord({ format: 'UHD' })));
    component.selectFormat('UHD');
    expect(metadataSpy.patchDiscRecord).toHaveBeenCalledWith('d-1', { disc_format: 'UHD' } as any);
  });

  it('per-title field change debounces and PATCHes via WorkflowService', fakeAsync(() => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();

    const row = component.titles[0];
    row.title = 'Edited title';
    component.onTitleFieldChange(row);
    // 300ms debounce — should NOT have fired yet at t=0.
    expect(workflowSpy.patchDiscTitle).not.toHaveBeenCalled();
    tick(300);
    expect(workflowSpy.patchDiscTitle).toHaveBeenCalledTimes(1);
    const [, patch] = workflowSpy.patchDiscTitle.calls.mostRecent().args;
    expect(patch.title_id).toBe('t-1');
    expect(patch.title).toBe('Edited title');
  }));

  it('rapid per-title changes coalesce into one PATCH', fakeAsync(() => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();

    const row = component.titles[0];
    row.title = 'a';
    component.onTitleFieldChange(row);
    tick(100);
    row.title = 'ab';
    component.onTitleFieldChange(row);
    tick(100);
    row.title = 'abc';
    component.onTitleFieldChange(row);
    tick(300);
    expect(workflowSpy.patchDiscTitle).toHaveBeenCalledTimes(1);
    const [, patch] = workflowSpy.patchDiscTitle.calls.mostRecent().args;
    expect(patch.title).toBe('abc');
  }));

  it('formatFilePath collapses the long transient prefix', () => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    const transient = component.titles[1];
    const display = component.formatFilePath(transient);
    expect(display).toContain('Movies/X.mkv');
    expect(display.startsWith('…')).toBe(true);
    expect(display).not.toContain('/data/mkvauto/data/jobs/abc/');
  });

  it('filePathStageLabel maps each stage to the right user-facing string', () => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    expect(component.filePathStageLabel({ file_path_stage: 'transfer' } as any)).toBe('At destination');
    expect(component.filePathStageLabel({ file_path_stage: 'postprocess' } as any)).toBe('In transient');
    expect(component.filePathStageLabel({ file_path_stage: 'rip' } as any)).toBe('Rip output');
    expect(component.filePathStageLabel({ file_path_stage: null } as any)).toBe('Path unknown');
  });

  it('close() emits closed', (done) => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    component.closed.subscribe(() => done());
    component.close();
  });

  it('does not emit title PATCHes on a finalized disc', fakeAsync(() => {
    fixture.componentRef.setInput('disc', makeDisc({ finalized: true }));
    metadataSpy.getDiscRecord.and.returnValue(of(makeRecord({ finalized: true })));
    fixture.detectChanges();

    const row = component.titles[0];
    row.title = 'x';
    component.onTitleFieldChange(row);
    tick(500);
    expect(workflowSpy.patchDiscTitle).not.toHaveBeenCalled();
  }));

  it('surfaces load errors instead of mounting the form', () => {
    metadataSpy.getDiscRecord.and.returnValue(throwError(() => ({ error: { detail: 'forbidden' } })));
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    expect(component.discRecord).toBeNull();
    expect(component.loadError).toContain('forbidden');
  });

  // #500 Phase 5: passive "Contributed to DiscDB" chip slot.
  it('contributed_to_discdb defaults to false when backend omits the field (v1 reality)', () => {
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    // Neither row in the default fixture sets `contributed_to_discdb`,
    // mirroring the v1 backend that doesn't yet project the column.
    expect(component.titles.every((r) => r.contributed_to_discdb === false)).toBe(true);
  });

  it('contributed_to_discdb=true reaches the row state when backend ships the flag (v2 path)', () => {
    metadataSpy.getDiscRecord.and.returnValue(of(makeRecord({
      titles: [
        { title_id: 't-1', title: 'Pilot', type: 'Episode', season: 1, episode: 1,
          description: '', edition: '', duration: 3540,
          file_path: null, file_path_stage: null, title_seq: 0,
          contributed_to_discdb: true },
      ] as any,
    })));
    fixture.componentRef.setInput('disc', makeDisc());
    fixture.detectChanges();
    expect(component.titles[0].contributed_to_discdb).toBe(true);
  });

  // #86 — manual DiscDB bundle export (misses only)

  it('canExportDiscDbBundle is true for misses and false for hits', () => {
    fixture.componentRef.setInput('disc', makeDisc({ discdb_hit: false }));
    fixture.detectChanges();
    expect(component.canExportDiscDbBundle).toBe(true);

    fixture.componentRef.setInput('disc', makeDisc({ discdb_hit: true }));
    fixture.detectChanges();
    expect(component.canExportDiscDbBundle).toBe(false);
  });

  it('exportDiscDbBundle downloads the bundle and toasts success', async () => {
    fixture.componentRef.setInput('disc', makeDisc({ discdb_hit: false }));
    fixture.detectChanges();
    discdbSpy.getContributionBundle.and.resolveTo({
      schema: 'thediscdb-bundle/v1',
      generated_at: 'now',
      disc_id: 'd-1',
      content_hash: 'h-1',
      disc_number: 1,
      release_slug: 'midway-4k',
      release: {},
      disc: {},
      summary: 'Name: Midway',
      info_log_included: true,
    });
    spyOn(URL, 'createObjectURL').and.returnValue('blob:test');
    spyOn(URL, 'revokeObjectURL');
    const clickSpy = spyOn(HTMLAnchorElement.prototype, 'click');

    await component.exportDiscDbBundle();

    expect(discdbSpy.getContributionBundle).toHaveBeenCalledWith('d-1');
    expect(clickSpy).toHaveBeenCalled();
    expect(toastSpy.show).toHaveBeenCalledWith(jasmine.stringMatching(/exported/i), 'success', jasmine.any(Number));
    expect(component.exporting).toBe(false);
  });

  it('exportDiscDbBundle surfaces backend errors as a toast', async () => {
    fixture.componentRef.setInput('disc', makeDisc({ discdb_hit: false }));
    fixture.detectChanges();
    discdbSpy.getContributionBundle.and.rejectWith(new Error('No completed rip job found for this disc'));

    await component.exportDiscDbBundle();

    expect(toastSpy.show).toHaveBeenCalledWith('No completed rip job found for this disc', 'error', jasmine.any(Number));
    expect(component.exporting).toBe(false);
  });

  // --- #599: hide-ignored + sort by type priority ---------------------------

  describe('#599 ignored-titles filter + type-priority sort', () => {
    function makeRecordWithMixedTitles(): DiscRecord {
      return makeRecord({
        titles: [
          // Ignored (auto-detected junk). 57:97 ratio observed in
          // production on the Harry Potter Deathly Hallows Pt 2 disc.
          { title_id: 'i-1', title: '', type: 'Ignore', duration: 30, season: null, episode: null, edition: '', description: '' },
          { title_id: 'i-2', title: '', type: 'ignore', duration: 12, season: null, episode: null, edition: '', description: '' },
          { title_id: 'i-3', title: '', type: 'Ignore', duration: 8, season: null, episode: null, edition: '', description: '' },
          // Named non-main extras.
          { title_id: 'fx-1', title: 'Cast Interviews', type: 'Interview', duration: 600, season: null, episode: null, edition: '', description: '' },
          { title_id: 'bts-1', title: 'Behind The Scenes', type: 'BehindTheScenes', duration: 1200, season: null, episode: null, edition: '', description: '' },
          // Main features — should sort to the very top.
          { title_id: 'm-1', title: 'Main Feature', type: 'MainMovie', duration: 8400, season: null, episode: null, edition: '', description: '' },
          { title_id: 'm-2', title: 'Director Cut', type: 'Main Movie', duration: 9000, season: null, episode: null, edition: '', description: '' },
          // Unset (no type chosen).
          { title_id: 'u-1', title: '', type: '', duration: 300, season: null, episode: null, edition: '', description: '' },
        ] as any,
      });
    }

    it('hides ignored titles by default; toggle reveals them', () => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeRecordWithMixedTitles()));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();

      expect(component.allTitles.length).toBe(8);
      expect(component.ignoredCount).toBe(3);
      expect(component.visibleCount).toBe(5);
      expect(component.showIgnored).toBeFalse();
      expect(component.visibleTitles.length).toBe(5);
      expect(component.visibleTitles.every((r) => !component.isRowIgnored(r))).toBeTrue();

      component.toggleShowIgnored();
      expect(component.showIgnored).toBeTrue();
      expect(component.visibleTitles.length).toBe(8);
    });

    it('sorts visible titles by type priority — MainMovie first, then named extras, then unset; duration descending within ties', () => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeRecordWithMixedTitles()));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();

      const ids = component.visibleTitles.map((r) => r.title_id);
      // Both Main Movies first (Director Cut 9000s > Main Feature 8400s),
      // then Interview vs BehindTheScenes (Interview priority 21 <
      // BehindTheScenes 22 — Interview first), then Unset last.
      expect(ids).toEqual(['m-2', 'm-1', 'fx-1', 'bts-1', 'u-1']);
    });

    it('places ignored titles at the very end of the sort when shown', () => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeRecordWithMixedTitles()));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      component.toggleShowIgnored();

      const tail = component.visibleTitles.slice(-3).map((r) => r.title_id);
      // All three ignored rows are at the tail, sorted by duration desc
      // (30s > 12s > 8s).
      expect(tail).toEqual(['i-1', 'i-2', 'i-3']);
    });

    it('clears showIgnored state when the disc input is reset', () => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeRecordWithMixedTitles()));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      component.toggleShowIgnored();
      expect(component.showIgnored).toBeTrue();

      fixture.componentRef.setInput('disc', null);
      fixture.detectChanges();

      expect(component.showIgnored).toBeFalse();
      expect(component.allTitles).toEqual([]);
    });

    it('isRowIgnored normalizes both "Ignore" and "ignore" casings', () => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeRecordWithMixedTitles()));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();

      const i1 = component.allTitles.find((r) => r.title_id === 'i-1')!;
      const i2 = component.allTitles.find((r) => r.title_id === 'i-2')!;
      const m1 = component.allTitles.find((r) => r.title_id === 'm-1')!;
      expect(component.isRowIgnored(i1)).toBeTrue();
      expect(component.isRowIgnored(i2)).toBeTrue();
      expect(component.isRowIgnored(m1)).toBeFalse();
    });

    it('still finds and patches an ignored row (so the user can un-ignore false positives)', fakeAsync(() => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeRecordWithMixedTitles()));
      workflowSpy.patchDiscTitle.and.returnValue(of({ title_seq: 1 } as any));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();

      // The drawer's PATCH dispatcher reads from `allTitles`, not the
      // filtered `visibleTitles`, so a debounced edit on an ignored row
      // still flushes. (We don't reveal the row first because a future
      // bulk-edit feature might trigger off-screen patches.)
      const ignoredRow = component.allTitles.find((r) => r.title_id === 'i-1')!;
      ignoredRow.title = 'Not actually junk';
      component.onTitleFieldChange(ignoredRow);
      tick(400);
      expect(workflowSpy.patchDiscTitle).toHaveBeenCalledTimes(1);
    }));
  });

  // --- #602: Backdrop rows hide title / season / episode / edition ---------

  describe('#602 Backdrop type — hide redundant fields', () => {
    function makeBackdropRecord(): DiscRecord {
      return makeRecord({
        titles: [
          { title_id: 'bd-1', title: 'old-title', type: 'Backdrop', duration: 30,
            season: null, episode: null, edition: '', description: '',
            file_path: null, file_path_stage: null },
          { title_id: 'main', title: 'The Movie', type: 'MainMovie', duration: 7200,
            season: null, episode: null, edition: '', description: '',
            file_path: null, file_path_stage: null },
        ] as any,
      });
    }

    it('isBackdrop normalises casing — Backdrop / backdrop / BACKDROP all match', () => {
      expect(component.isBackdrop({ type: 'Backdrop' } as any)).toBeTrue();
      expect(component.isBackdrop({ type: 'backdrop' } as any)).toBeTrue();
      expect(component.isBackdrop({ type: 'BACKDROP' } as any)).toBeTrue();
      expect(component.isBackdrop({ type: 'MainMovie' } as any)).toBeFalse();
      expect(component.isBackdrop({ type: 'Episode' } as any)).toBeFalse();
      expect(component.isBackdrop({ type: '' } as any)).toBeFalse();
    });

    it('display mode on a Backdrop row hides the title text', fakeAsync(() => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeBackdropRecord()));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      tick(500);
      fixture.detectChanges();

      const rows: NodeListOf<HTMLElement> = fixture.nativeElement.querySelectorAll('.library-disc-drawer__title-row');
      // Sort puts MainMovie first; Backdrop sorts under "Unknown named" priority.
      const backdropRow = Array.from(rows).find((r) => r.getAttribute('data-title-id') === 'bd-1');
      expect(backdropRow).withContext('Backdrop row should render').toBeDefined();
      expect(backdropRow!.querySelector('.library-disc-drawer__title-title-text'))
        .withContext('Backdrop row should not render the title text in display mode')
        .toBeNull();
      // Chip + duration + Edit button still render.
      expect(backdropRow!.querySelector('.library-disc-drawer__title-chip')).not.toBeNull();
      expect(backdropRow!.querySelector('.library-disc-drawer__title-duration')).not.toBeNull();
      expect(backdropRow!.querySelector('.library-disc-drawer__title-edit-btn')).not.toBeNull();
    }));

    it('edit mode on a Backdrop row hides title / edition / season / episode inputs but keeps type select', fakeAsync(() => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeBackdropRecord()));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      tick(500);
      fixture.detectChanges();

      // Programmatically enter edit mode for the Backdrop row.
      const backdropRow = component.allTitles.find((r) => r.title_id === 'bd-1')!;
      component.startEditing(backdropRow);
      fixture.detectChanges();

      const rows: NodeListOf<HTMLElement> = fixture.nativeElement.querySelectorAll('.library-disc-drawer__title-row');
      const rowEl = Array.from(rows).find((r) => r.getAttribute('data-title-id') === 'bd-1')!;

      expect(rowEl.querySelector('.library-disc-drawer__title-title'))
        .withContext('title input must be absent on Backdrop rows in edit mode')
        .toBeNull();
      expect(rowEl.querySelector('.library-disc-drawer__title-edition'))
        .withContext('edition input must be absent on Backdrop rows')
        .toBeNull();
      expect(rowEl.querySelector('.library-disc-drawer__title-num'))
        .withContext('season/episode inputs must be absent on Backdrop rows')
        .toBeNull();
      // Type select still renders — user must be able to change away.
      expect(rowEl.querySelector('.library-disc-drawer__title-type'))
        .withContext('type select must remain available on Backdrop rows')
        .not.toBeNull();
    }));

    it('switching type away from Backdrop restores the input fields', fakeAsync(() => {
      metadataSpy.getDiscRecord.and.returnValue(of(makeBackdropRecord()));
      fixture.componentRef.setInput('disc', makeDisc({ id: 'd-1' }));
      fixture.detectChanges();
      tick(500);
      fixture.detectChanges();

      const backdropRow = component.allTitles.find((r) => r.title_id === 'bd-1')!;
      component.startEditing(backdropRow);
      fixture.detectChanges();

      // Flip the row's type to Other and re-render.
      backdropRow.type = 'Other';
      fixture.detectChanges();

      const rows: NodeListOf<HTMLElement> = fixture.nativeElement.querySelectorAll('.library-disc-drawer__title-row');
      const rowEl = Array.from(rows).find((r) => r.getAttribute('data-title-id') === 'bd-1')!;
      expect(rowEl.querySelector('.library-disc-drawer__title-title'))
        .withContext('title input must return when type is no longer Backdrop')
        .not.toBeNull();
      expect(rowEl.querySelector('.library-disc-drawer__title-edition'))
        .withContext('edition input must return when type is no longer Backdrop')
        .not.toBeNull();
    }));
  });
});
