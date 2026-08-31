import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { BehaviorSubject } from 'rxjs';

import { PreviewLabelModalComponent } from './preview-label-modal.component';
import { WorkflowService } from '../../services/workflow.service';
import { LoggerService } from '../../services/logger.service';
import { JobService } from '../../services/job.service';
import { DriveService } from '../../services/drive.service';
import { MetadataService } from '../../services/metadata.service';

/** #848/#849 — the shared preview + label modal. */
describe('PreviewLabelModalComponent', () => {
  let fixture: ComponentFixture<PreviewLabelModalComponent>;
  let component: PreviewLabelModalComponent;

  beforeEach(async () => {
    // The embedded TitleEditor injects the workflow collaborator graph.
    const jobSpy = jasmine.createSpyObj('JobService', ['getJobStatus', 'titleJobProgress']);
    const driveSpy = jasmine.createSpyObj('DriveService',
      ['currentSelected', 'getDrives'],
      { drives$: new BehaviorSubject<any[]>([]) });
    driveSpy.currentSelected.and.returnValue(null);
    driveSpy.getDrives.and.returnValue([]);
    const metadataSpy = jasmine.createSpyObj('MetadataService', [
      'getCachedOptions', 'loadWorkflowOptions', 'refreshWorkflowOptions',
    ]);
    metadataSpy.getCachedOptions.and.returnValue({
      movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
    });
    metadataSpy.loadWorkflowOptions.and.returnValue(new BehaviorSubject({
      movieOptions: [], boxsetOptions: [], releaseOptions: [], groupOptions: [],
    }).asObservable());
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);

    await TestBed.configureTestingModule({
      imports: [PreviewLabelModalComponent, HttpClientTestingModule],
      providers: [
        WorkflowService,
        { provide: JobService, useValue: jobSpy },
        { provide: DriveService, useValue: driveSpy },
        { provide: MetadataService, useValue: metadataSpy },
        { provide: LoggerService, useValue: loggerSpy },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(PreviewLabelModalComponent);
    component = fixture.componentInstance;
    component.title = { title_id: 't1', title: 'A Title', type: null };
  });

  afterEach(() => {
    fixture.destroy();
  });

  it('portals its host element to document.body (#849) and removes it on destroy', () => {
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    // Fixed positioning must resolve against the viewport, so the host has
    // to sit outside the (filtered/transformed) component subtree.
    expect(host.parentElement).toBe(document.body);
    fixture.destroy();
    expect(host.parentElement).toBeNull();
  });

  it('embeds the full TitleEditor with the preview row hidden (#848)', () => {
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('app-title-editor')).not.toBeNull();
    // The editor's own Play-preview row is redundant inside the modal.
    expect(host.textContent).not.toContain('Play preview');
  });

  it('Escape closes; arrows advance the loop', () => {
    fixture.detectChanges();
    const closed = spyOn(component.closed, 'emit');
    const next = spyOn(component.next, 'emit');
    const prev = spyOn(component.prev, 'emit');

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    expect(next).toHaveBeenCalledTimes(1);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
    expect(prev).toHaveBeenCalledTimes(1);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it('arrow keys never navigate while the user is typing in a field', () => {
    fixture.detectChanges();
    const next = spyOn(component.next, 'emit');
    const input = document.createElement('input');
    document.body.appendChild(input);
    try {
      const ev = new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true });
      input.dispatchEvent(ev);
      expect(next).not.toHaveBeenCalled();
    } finally {
      input.remove();
    }
  });

  it('Save & next flushes the embedded editor before advancing', () => {
    fixture.detectChanges();
    const next = spyOn(component.next, 'emit');
    const flush = spyOn(component.editor as any, 'flushPendingFieldEdits');
    component.saveAndNext();
    expect(flush).toHaveBeenCalled();
    expect(next).toHaveBeenCalledTimes(1);
  });

  it('Space toggles video playback; not while typing', () => {
    component.previewUrl = 'blob:test';
    fixture.detectChanges();
    const video: HTMLVideoElement = fixture.nativeElement.querySelector('video');
    expect(video).not.toBeNull();
    const play = spyOn(video, 'play').and.returnValue(Promise.resolve());
    const pause = spyOn(video, 'pause');

    document.dispatchEvent(new KeyboardEvent('keydown', { key: ' ' }));
    expect(play).toHaveBeenCalledTimes(1);
    Object.defineProperty(video, 'paused', { value: false });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: ' ' }));
    expect(pause).toHaveBeenCalledTimes(1);

    // A space typed into a field is a space, not a playback toggle.
    const input = document.createElement('input');
    document.body.appendChild(input);
    try {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
      expect(play).toHaveBeenCalledTimes(1);
    } finally {
      input.remove();
    }
  });

  it('keeps Tab cycling inside the modal (portaled host would leak focus)', () => {
    component.previewUrl = null;
    fixture.detectChanges();
    const els = (component as any).focusables() as HTMLElement[];
    expect(els.length).toBeGreaterThan(2);
    const first = els[0];
    const last = els[els.length - 1];

    last.focus();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
    expect(document.activeElement).toBe(first);

    first.focus();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true }));
    expect(document.activeElement).toBe(last);
  });

  it('header mirrors the rail row: name (or Untitled) plus source · duration · size', () => {
    component.title = { title_id: 't1', title: '', source_file: '00362.mpls', duration: 240, size: 654 * 1024 * 1024 };
    fixture.detectChanges();
    expect(component.headMeta()).toBe('00362.mpls · 4m · 654 MB');
    const head = (fixture.nativeElement as HTMLElement);
    expect(head.querySelector('.plm-heading')?.textContent?.trim()).toBe('Untitled');
    expect(head.querySelector('.plm-headmeta')?.textContent?.trim()).toBe('00362.mpls · 4m · 654 MB');

    component.title = { title_id: 't2', title: 'The Lost Commanders', source_file: '00351.m2ts', duration: 45 };
    expect(component.headMeta()).toBe('00351.m2ts · 45s');
    component.title = { title_id: 't3', duration: 5400 };
    expect(component.headMeta()).toBe('1h 30m');
  });

  it('shows the no-preview placeholder when the title has no clip', () => {
    component.previewUrl = null;
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.plm-no-preview')).not.toBeNull();
    expect(host.querySelector('app-preview-viewer')).toBeNull();
  });
});
