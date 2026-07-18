import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';

import { LibraryReattachModalComponent } from './library-reattach-modal.component';
import { WorkflowService } from '../../services/workflow.service';
import { ToastService } from '../../services/toast.service';
import type { LibraryReattachReport } from '../../services/library-reattach.types';

function makeReport(overrides: Partial<LibraryReattachReport> = {}): LibraryReattachReport {
  return {
    deterministic_matches: [],
    heuristic_matches: [],
    orphan_files: [],
    orphan_titles: [],
    conflicts: [],
    transfer_dir: '/data/mkvauto/library',
    dry_run: true,
    applied: false,
    ...overrides,
  };
}

describe('LibraryReattachModalComponent', () => {
  let workflowSvc: jasmine.SpyObj<WorkflowService>;
  let toast: jasmine.SpyObj<ToastService>;
  let fixture: ComponentFixture<LibraryReattachModalComponent>;
  let component: LibraryReattachModalComponent;

  beforeEach(async () => {
    workflowSvc = jasmine.createSpyObj<WorkflowService>('WorkflowService', ['verifyLibraryLinks']);
    toast = jasmine.createSpyObj<ToastService>('ToastService', ['show']);

    await TestBed.configureTestingModule({
      imports: [LibraryReattachModalComponent],
      providers: [
        { provide: WorkflowService, useValue: workflowSvc },
        { provide: ToastService, useValue: toast },
      ],
    }).compileComponents();
  });

  function create(initialReport: LibraryReattachReport | Error): void {
    if (initialReport instanceof Error) {
      workflowSvc.verifyLibraryLinks.and.returnValue(throwError(() => initialReport));
    } else {
      workflowSvc.verifyLibraryLinks.and.returnValue(of(initialReport));
    }
    fixture = TestBed.createComponent(LibraryReattachModalComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  it('calls verifyLibraryLinks(true) on init and stores the report', () => {
    const report = makeReport({
      deterministic_matches: [{ title_id: 't1', old_path: null, new_path: '/x.mkv', tier: 'segment_uid' }],
    });
    create(report);

    expect(workflowSvc.verifyLibraryLinks).toHaveBeenCalledOnceWith(true);
    expect(component.loading).toBeFalse();
    expect(component.report).toEqual(report);
    expect(component.loadError).toBeNull();
  });

  it('captures the formatted error when the dry-run rejects', () => {
    create(new Error('No active TransferConfig'));

    expect(component.loading).toBeFalse();
    expect(component.report).toBeNull();
    expect(component.loadError).toContain('No active TransferConfig');
  });

  it('disables Apply when both match buckets are empty', () => {
    create(makeReport());
    expect(component.applyCount).toBe(0);
    expect(component.canApply).toBeFalse();
  });

  it('disables Apply while the dry-run is still loading', () => {
    // Return an observable that never emits — simulates in-flight.
    workflowSvc.verifyLibraryLinks.and.returnValue(of());
    fixture = TestBed.createComponent(LibraryReattachModalComponent);
    component = fixture.componentInstance;
    // Don't call detectChanges → component.loading stays true.
    expect(component.canApply).toBeFalse();
  });

  it('enables Apply when at least one match exists', () => {
    create(makeReport({
      deterministic_matches: [{ title_id: 't1', old_path: null, new_path: '/x.mkv', tier: 'segment_uid' }],
      heuristic_matches: [{ title_id: 't2', old_path: null, new_path: '/y.mkv', tier: 'filename' }],
    }));
    expect(component.applyCount).toBe(2);
    expect(component.canApply).toBeTrue();
  });

  it('calls verifyLibraryLinks(false), shows success toast, and emits applied on Apply click', (done) => {
    const dryRunReport = makeReport({
      deterministic_matches: [{ title_id: 't1', old_path: null, new_path: '/x.mkv', tier: 'segment_uid' }],
    });
    const wetRunReport = { ...dryRunReport, dry_run: false, applied: true };
    create(dryRunReport);

    workflowSvc.verifyLibraryLinks.and.returnValue(of(wetRunReport));
    component.applied.subscribe((emitted) => {
      expect(emitted).toEqual(wetRunReport);
      expect(toast.show).toHaveBeenCalled();
      expect(toast.show.calls.mostRecent().args[0]).toContain('Reattached');
      done();
    });

    component.onApply();
    expect(workflowSvc.verifyLibraryLinks).toHaveBeenCalledWith(false);
    expect(component.applying).toBeFalse(); // synchronous of() completes immediately
  });

  it('keeps the modal open and shows an error toast when Apply fails', () => {
    create(makeReport({
      deterministic_matches: [{ title_id: 't1', old_path: null, new_path: '/x.mkv', tier: 'segment_uid' }],
    }));

    let appliedFired = false;
    component.applied.subscribe(() => { appliedFired = true; });

    workflowSvc.verifyLibraryLinks.and.returnValue(throwError(() => new Error('boom')));
    component.onApply();

    expect(appliedFired).toBeFalse();
    expect(toast.show).toHaveBeenCalled();
    expect(toast.show.calls.mostRecent().args[1]).toBe('error');
    expect(component.applying).toBeFalse();
  });

  it('emits dismiss on Cancel click', (done) => {
    create(makeReport());
    component.dismiss.subscribe(() => done());
    component.onCancel();
  });

  it('toggles section expansion state', () => {
    create(makeReport());
    expect(component.expanded.deterministic).toBeTrue();
    component.toggleSection('deterministic');
    expect(component.expanded.deterministic).toBeFalse();
    component.toggleSection('deterministic');
    expect(component.expanded.deterministic).toBeTrue();
  });

  it('dismisses on backdrop click (event.target === currentTarget)', (done) => {
    create(makeReport());
    component.dismiss.subscribe(() => done());
    const fakeBackdrop = {} as HTMLElement;
    component.onBackdropClick({ target: fakeBackdrop, currentTarget: fakeBackdrop } as unknown as MouseEvent);
  });

  it('does NOT dismiss on inner-click (event.target !== currentTarget)', () => {
    create(makeReport());
    let dismissed = false;
    component.dismiss.subscribe(() => { dismissed = true; });
    const backdrop = {} as HTMLElement;
    const inner = {} as HTMLElement;
    component.onBackdropClick({ target: inner, currentTarget: backdrop } as unknown as MouseEvent);
    expect(dismissed).toBeFalse();
  });
});
