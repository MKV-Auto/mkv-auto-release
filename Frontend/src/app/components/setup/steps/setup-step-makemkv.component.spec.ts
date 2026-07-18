import { ComponentFixture, TestBed, fakeAsync, tick, discardPeriodicTasks } from '@angular/core/testing';
import { of, Subject } from 'rxjs';

import { SetupStepMakemkvComponent, MakemkvStepData } from './setup-step-makemkv.component';
import { MakeMKVDownloadState, MakeMKVHealth, SystemService } from '../../../services/system.service';
import { WorkflowService } from '../../../services/workflow.service';

/**
 * Focus of this spec: the #625 pre-download + EULA link behaviour. The install
 * flow itself is covered by end-to-end tests; here we care about
 * ``downloadState`` gating and the "review the End User License Agreement"
 * copy branches.
 */
describe('SetupStepMakemkvComponent (#625 pre-download + EULA)', () => {
  let component: SetupStepMakemkvComponent;
  let fixture: ComponentFixture<SetupStepMakemkvComponent>;
  let system: jasmine.SpyObj<SystemService>;
  let workflow: jasmine.SpyObj<WorkflowService>;

  function healthWith(dlState: MakeMKVDownloadState | undefined, installed = false): MakeMKVHealth {
    return {
      installed,
      valid: installed,
      can_rip: installed,
      version: installed ? '1.17.5' : null,
      missing_components: [],
      error: null,
      binary_path: '/usr/bin/makemkvcon',
      disc_workflow_blocked: false,
      download: dlState ? { state: dlState, version: '1.17.5', downloaded_at: null, error: null } : undefined,
    };
  }

  function freshData(): MakemkvStepData {
    return { key: '', valid: false, installed: false };
  }

  async function setup(initialHealth: MakeMKVHealth) {
    system = jasmine.createSpyObj<SystemService>('SystemService', [
      'getMakeMKVHealth',
      'getMakeMKVEulaUrl',
      'startMakeMKVUpdate',
      'getMakeMKVUpdateJob',
      'getMakeMKVUpdateActive',
    ]);
    system.getMakeMKVHealth.and.returnValue(of(initialHealth));
    system.getMakeMKVEulaUrl.and.returnValue('/api/system/makemkv/eula');
    system.getMakeMKVUpdateActive.and.returnValue(of({ active: false } as any));

    workflow = jasmine.createSpyObj<WorkflowService>('WorkflowService', [], {
      makemkvUpdateMessages$: new Subject<any>().asObservable(),
    });

    await TestBed.configureTestingModule({
      imports: [SetupStepMakemkvComponent],
      providers: [
        { provide: SystemService, useValue: system },
        { provide: WorkflowService, useValue: workflow },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(SetupStepMakemkvComponent);
    component = fixture.componentInstance;
    component.data = freshData();
    fixture.detectChanges();
  }

  it('exposes the EULA URL from SystemService.getMakeMKVEulaUrl', async () => {
    await setup(healthWith('ready'));
    expect(component.eulaUrl).toBe('/api/system/makemkv/eula');
    expect(system.getMakeMKVEulaUrl).toHaveBeenCalled();
  });

  it('downloadState="ready" renders the review-EULA copy with a hyperlink', async () => {
    await setup(healthWith('ready'));
    const html: string = fixture.nativeElement.innerHTML;
    expect(component.downloadState).toBe('ready');
    expect(html).toContain('The latest version of MakeMKV has been downloaded');
    const link: HTMLAnchorElement | null = fixture.nativeElement.querySelector(
      'a[href="/api/system/makemkv/eula"]'
    );
    expect(link).withContext('EULA anchor should render when ready').not.toBeNull();
    expect(link!.textContent).toContain('End User License Agreement');
    expect(link!.target).toBe('_blank');
  });

  it('downloadState="downloading" hides the EULA link and shows the in-flight message', async () => {
    await setup(healthWith('downloading'));
    const html: string = fixture.nativeElement.innerHTML;
    expect(component.downloadState).toBe('downloading');
    expect(html).toContain('Downloading MakeMKV sources');
    // No EULA anchor while downloading
    expect(fixture.nativeElement.querySelector('a[href="/api/system/makemkv/eula"]')).toBeNull();
  });

  it('downloadState="missing" falls back to the pre-download copy', async () => {
    await setup(healthWith('missing'));
    const html: string = fixture.nativeElement.innerHTML;
    expect(html).toContain('End User License Agreement will be downloaded with MakeMKV');
    expect(html).toContain('Source pre-download unavailable');
  });

  it('downloadState="failed" surfaces the inline-download fallback hint', async () => {
    await setup(healthWith('failed'));
    const html: string = fixture.nativeElement.innerHTML;
    expect(html).toContain('Source pre-download unavailable');
  });

  it('Install button is disabled while downloading and reads "Downloading MakeMKV..."', async () => {
    await setup(healthWith('downloading'));
    const btn = fixture.nativeElement.querySelector('button.setup-step-btn-amber') as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toContain('Downloading MakeMKV...');
  });

  it('Install button is enabled when downloadState is ready', async () => {
    await setup(healthWith('ready'));
    const btn = fixture.nativeElement.querySelector('button.setup-step-btn-amber') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toContain('Install MakeMKV');
  });

  it('polls health while downloading and stops once state becomes ready', fakeAsync(async () => {
    await setup(healthWith('downloading'));
    expect(system.getMakeMKVHealth).toHaveBeenCalledTimes(1);

    // Second health check flips to ready
    system.getMakeMKVHealth.and.returnValue(of(healthWith('ready')));
    tick(3000);
    expect(system.getMakeMKVHealth).toHaveBeenCalledTimes(2);
    expect(component.downloadState).toBe('ready');

    // Poll should have stopped; advancing time further does not re-invoke health.
    tick(6000);
    expect(system.getMakeMKVHealth).toHaveBeenCalledTimes(2);
    discardPeriodicTasks();
  }));

  it('destroy stops the pre-download poll', fakeAsync(async () => {
    await setup(healthWith('downloading'));
    expect(system.getMakeMKVHealth).toHaveBeenCalledTimes(1);
    component.ngOnDestroy();
    tick(6000);
    expect(system.getMakeMKVHealth).toHaveBeenCalledTimes(1);
  }));
});
