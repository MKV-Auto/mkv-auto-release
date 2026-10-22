import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';

import { DismissFailedJobModalComponent } from './dismiss-failed-job-modal.component';
import { DismissFailedPreview, WorkflowService } from '../../services/workflow.service';

describe('DismissFailedJobModalComponent (#877)', () => {
  let fixture: ComponentFixture<DismissFailedJobModalComponent>;
  let component: DismissFailedJobModalComponent;
  let workflow: { getDismissFailedPreview: jasmine.Spy; dismissFailedJob: jasmine.Spy };
  let closed: boolean[];

  const partial: DismissFailedPreview = {
    job_ids: ['j1', 'j0'], bytes: 31.5 * 1024 ** 3, has_finished_rip: false, retention_days: 7,
  };

  function render(preview: DismissFailedPreview): HTMLElement {
    workflow.getDismissFailedPreview.and.returnValue(of(preview));
    fixture = TestBed.createComponent(DismissFailedJobModalComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('jobId', 'j1');
    fixture.componentRef.setInput('title', 'Revenge of the Sith');
    closed = [];
    component.closed.subscribe(v => closed.push(v));
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function click(el: HTMLElement, testId: string): void {
    (el.querySelector(`[data-testid="${testId}"] button`) as HTMLButtonElement).click();
  }

  beforeEach(async () => {
    workflow = {
      getDismissFailedPreview: jasmine.createSpy('getDismissFailedPreview'),
      dismissFailedJob: jasmine.createSpy('dismissFailedJob').and.returnValue(of(['j1', 'j0'])),
    };
    await TestBed.configureTestingModule({
      imports: [DismissFailedJobModalComponent],
      providers: [{ provide: WorkflowService, useValue: workflow }],
    }).compileComponents();
  });

  it('shows the size, the number of attempts and when the files expire', () => {
    const el = render(partial);
    expect(el.querySelector('[data-testid="dismiss-size"]')?.textContent).toContain('32 GB');
    expect(el.textContent).toContain('failed 2 times');
    expect(el.textContent).toContain('deleted automatically 7 days after the copy started');
    expect(el.querySelector('[data-testid="dismiss-only-copy"]')).toBeNull();
  });

  it('warns when the files are the only copy of a finished rip', () => {
    const el = render({ ...partial, has_finished_rip: true });
    expect(el.querySelector('[data-testid="dismiss-only-copy"]')).not.toBeNull();
    expect(el.textContent).not.toContain('deleted automatically');
  });

  it('Delete files dismisses with delete and closes', () => {
    const el = render(partial);
    click(el, 'dismiss-delete');
    expect(workflow.dismissFailedJob).toHaveBeenCalledWith('j1', true);
    expect(closed).toEqual([true]);
  });

  it('Keep files dismisses without deleting', () => {
    const el = render(partial);
    click(el, 'dismiss-keep');
    expect(workflow.dismissFailedJob).toHaveBeenCalledWith('j1', false);
    expect(closed).toEqual([true]);
  });

  it('offers only Dismiss when there is nothing to delete', () => {
    const el = render({ ...partial, bytes: 0 });
    expect(el.querySelector('[data-testid="dismiss-delete"]')).toBeNull();
    click(el, 'dismiss-keep');
    expect(workflow.dismissFailedJob).toHaveBeenCalledWith('j1', false);
  });

  it('stays open and shows the error when dismissing fails', () => {
    const el = render(partial);
    workflow.dismissFailedJob.and.returnValue(throwError(() => ({ error: { detail: 'Job is running' } })));
    click(el, 'dismiss-delete');
    fixture.detectChanges();
    expect(closed).toEqual([]);
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Job is running');
  });

  it('cancel closes without dismissing', () => {
    render(partial);
    component.onEscape();
    expect(workflow.dismissFailedJob).not.toHaveBeenCalled();
    expect(closed).toEqual([false]);
  });
});
