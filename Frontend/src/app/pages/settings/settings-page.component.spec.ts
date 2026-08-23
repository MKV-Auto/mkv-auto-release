import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { SettingsPageComponent } from './settings-page.component';
import { SystemService, defaultNotificationPreferences } from '../../services/system.service';
import { ToastService } from '../../services/toast.service';
import { WorkflowService } from '../../services/workflow.service';

/** Lean SystemService double — returns empty/default observables for every
 *  load call settings-page fires in ngOnInit. We don't care about most of
 *  these; we only need ngOnInit to complete without throwing so the
 *  component is in a renderable state. */
function makeSystemServiceSpy(): jasmine.SpyObj<SystemService> {
  const spy = jasmine.createSpyObj<SystemService>('SystemService', [
    'getPreviewConfig', 'savePreviewConfig',
    'getDiscordConfig', 'saveDiscordConfig',
    'getMediaServerConfig', 'saveMediaServerConfig',
    'getDiscdbLookupConfig', 'saveDiscdbLookupConfig',
    'getAutoRipConfig', 'saveAutoRipConfig',
    'getTmdbConfig', 'saveTmdbConfig',
    'getTransferConfigs', 'getStorageSummary',
    'exportHistory', 'importHistory',
    'getEnvManagedSettings',
    'startDiscDbExport', 'getDiscDbExportStatus', 'getActiveDiscDbExport',
    'cancelDiscDbExport', 'downloadDiscDbExport',
  ]);
  spy.getActiveDiscDbExport.and.returnValue(of({ status: 'idle' } as any));
  spy.getEnvManagedSettings.and.returnValue(of({ managed: [], supported: [] } as any));
  spy.getPreviewConfig.and.returnValue(of({
    duration_seconds: 60, max_parallel: 2, max_parallel_ceiling: 8,
  } as any));
  spy.getDiscordConfig.and.returnValue(of({
    enabled: true, webhook_url: '',
    notification_preferences: defaultNotificationPreferences(),
  } as any));
  spy.getMediaServerConfig.and.returnValue(of({
    media_server: 'plex', merge_movie_releases: true,
  } as any));
  spy.getDiscdbLookupConfig.and.returnValue(of({ enabled: true } as any));
  spy.getAutoRipConfig.and.returnValue(of({ enabled: false } as any));
  spy.getTmdbConfig.and.returnValue(of({ api_key_set: false } as any));
  spy.getTransferConfigs.and.returnValue(of([] as any));
  spy.getStorageSummary.and.returnValue(of({
    data_root: { free: 1000, total: 2000, path: '/x' },
    transfer_root: { free: 500, total: 1000, path: '/transfer' },
  } as any));
  return spy;
}

describe('SettingsPageComponent (#741 DiscDB submission export — own section)', () => {
  let fixture: ComponentFixture<SettingsPageComponent>;
  let component: SettingsPageComponent;
  let spy: jasmine.SpyObj<SystemService>;

  const job = (over: Partial<any> = {}) => ({
    job_id: 'j-1', status: 'running', done: 0, total: 4, current: '',
    error: null, included: 0, skipped: 0, cancelled: false, download_ready: false,
    ...over,
  });

  beforeEach(async () => {
    spy = makeSystemServiceSpy();
    await TestBed.configureTestingModule({
      imports: [SettingsPageComponent],
      providers: [
        { provide: SystemService, useValue: spy },
        { provide: ToastService, useValue: jasmine.createSpyObj('ToastService', ['show']) },
        { provide: WorkflowService, useValue: { syncCoordinator: () => {} } },
        provideHttpClient(),
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(SettingsPageComponent);
    component = fixture.componentInstance;
    component.activeTab = 'discdb';
    fixture.detectChanges();
    spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
    spyOn(URL, 'revokeObjectURL');
    spyOn(HTMLAnchorElement.prototype, 'click');
  });

  afterEach(() => component.ngOnDestroy());

  it('lives in its own section, not inside Export / Import', () => {
    // Sandwiched between backup-export and import, a contribution tool read as
    // part of user-data backup. It is not: it produces a public submission.
    expect(fixture.nativeElement.textContent).toContain('TheDiscDB submissions');

    component.activeTab = 'export';
    fixture.detectChanges();
    const exportTab = fixture.nativeElement.textContent;
    expect(exportTab).not.toContain('Export DiscDB submissions');
  });

  it('has its own sidebar entry', () => {
    expect(component.nav.some(n => n.id === 'discdb' && n.label === 'TheDiscDB')).toBe(true);
  });


  it('shows progress against a total while the export runs', fakeAsync(() => {
    spy.startDiscDbExport.and.returnValue(of(job() as any));
    spy.getDiscDbExportStatus.and.returnValue(of(job({ done: 3 }) as any));

    component.exportDiscDbSubmissions();
    tick(1000);

    // The count is what tells you whether to wait; a bare bar does not.
    expect(component.discdbExportDone).toBe(3);
    expect(component.discdbExportTotal).toBe(4);
    expect(component.discdbExportPercent).toBe(75);
    component.ngOnDestroy();
  }));

  it('downloads and reports skipped discs once complete', fakeAsync(() => {
    spy.startDiscDbExport.and.returnValue(of(job() as any));
    spy.getDiscDbExportStatus.and.returnValue(
      of(job({ status: 'completed', done: 4, included: 3, skipped: 1 }) as any),
    );
    spy.downloadDiscDbExport.and.returnValue(
      of({ blob: new Blob(['zip']), filename: 'thediscdb-submissions.zip' } as any),
    );

    component.exportDiscDbSubmissions();
    tick(1000);

    // A silent partial export reads as "everything" and gets submitted as such.
    expect(component.discdbExportResult).toContain('3 discs exported');
    expect(component.discdbExportResult).toContain('1 skipped');
    expect(component.discdbExporting).toBe(false);
  }));

  it('omits the skipped clause when nothing was skipped', fakeAsync(() => {
    spy.startDiscDbExport.and.returnValue(of(job() as any));
    spy.getDiscDbExportStatus.and.returnValue(
      of(job({ status: 'completed', included: 1, skipped: 0 }) as any),
    );
    spy.downloadDiscDbExport.and.returnValue(
      of({ blob: new Blob(['zip']), filename: 'f.zip' } as any),
    );

    component.exportDiscDbSubmissions();
    tick(1000);

    expect(component.discdbExportResult).toContain('1 disc exported');
    expect(component.discdbExportResult).not.toContain('skipped');
  }));

  it('surfaces the reason when the job fails', fakeAsync(() => {
    spy.startDiscDbExport.and.returnValue(of(job() as any));
    spy.getDiscDbExportStatus.and.returnValue(
      of(job({ status: 'failed', error: 'No discs are ready to export' }) as any),
    );

    component.exportDiscDbSubmissions();
    tick(1000);

    expect(component.discdbExportError).toBe('No discs are ready to export');
    expect(component.discdbExporting).toBe(false);
    expect(spy.downloadDiscDbExport).not.toHaveBeenCalled();
  }));

  it('stops polling once the job reaches a terminal state', fakeAsync(() => {
    spy.startDiscDbExport.and.returnValue(of(job() as any));
    spy.getDiscDbExportStatus.and.returnValue(
      of(job({ status: 'failed', error: 'boom' }) as any),
    );

    component.exportDiscDbSubmissions();
    tick(5000);

    // One poll, not five: a finished job must not be asked about forever.
    expect(spy.getDiscDbExportStatus).toHaveBeenCalledTimes(1);
  }));

  it('rejoins an export already running when the page loads', () => {
    // A reload must not orphan a job the user started before it.
    spy.getActiveDiscDbExport.and.returnValue(of(job({ done: 2 }) as any));
    spy.getDiscDbExportStatus.and.returnValue(of(job({ done: 2 }) as any));

    const f2 = TestBed.createComponent(SettingsPageComponent);
    f2.componentInstance.activeTab = 'export';
    f2.detectChanges();

    expect(f2.componentInstance.discdbExporting).toBe(true);
    expect(f2.componentInstance.discdbExportJobId).toBe('j-1');
    f2.componentInstance.ngOnDestroy();
  });

  it('offers a finished archive instead of rebuilding it', () => {
    // The whole point of the background job is that you can walk away — so the
    // common case is the export finishing while nobody is on the page.
    spy.getActiveDiscDbExport.and.returnValue(
      of(job({ status: 'completed', done: 4, included: 4, skipped: 0,
               download_ready: true }) as any),
    );

    const f2 = TestBed.createComponent(SettingsPageComponent);
    f2.componentInstance.activeTab = 'export';
    f2.detectChanges();

    expect(f2.componentInstance.discdbExportReady?.job_id).toBe('j-1');
    // Not treated as still running, and not polled.
    expect(f2.componentInstance.discdbExporting).toBe(false);
    expect(spy.getDiscDbExportStatus).not.toHaveBeenCalled();
    f2.componentInstance.ngOnDestroy();
  });

  it('does not auto-download the waiting archive', () => {
    // A file landing unprompted every time you open Settings would be obnoxious.
    spy.getActiveDiscDbExport.and.returnValue(
      of(job({ status: 'completed', download_ready: true }) as any),
    );

    const f2 = TestBed.createComponent(SettingsPageComponent);
    f2.componentInstance.activeTab = 'export';
    f2.detectChanges();

    expect(spy.downloadDiscDbExport).not.toHaveBeenCalled();
    f2.componentInstance.ngOnDestroy();
  });

  it('downloads the waiting archive on request without re-running', () => {
    spy.getActiveDiscDbExport.and.returnValue(
      of(job({ status: 'completed', included: 4, skipped: 1, download_ready: true }) as any),
    );
    spy.downloadDiscDbExport.and.returnValue(
      of({ blob: new Blob(['zip']), filename: 'thediscdb-submissions.zip' } as any),
    );

    const f2 = TestBed.createComponent(SettingsPageComponent);
    const c2 = f2.componentInstance;
    c2.activeTab = 'export';
    f2.detectChanges();
    c2.downloadReadyDiscDbExport();

    expect(spy.downloadDiscDbExport).toHaveBeenCalledWith('j-1');
    expect(spy.startDiscDbExport).not.toHaveBeenCalled();
    expect(c2.discdbExportResult).toContain('4 discs exported');
    expect(c2.discdbExportReady).toBeNull();
    c2.ngOnDestroy();
  });

  it('stops offering an archive the server no longer has', () => {
    // Retention swept it, or the tmp volume was cleared — a button that 410s
    // is worse than no button.
    spy.getActiveDiscDbExport.and.returnValue(
      of(job({ status: 'completed', download_ready: true }) as any),
    );
    spy.downloadDiscDbExport.and.returnValue(
      throwError(() => ({ error: { detail: 'Export archive is no longer available' } })),
    );

    const f2 = TestBed.createComponent(SettingsPageComponent);
    const c2 = f2.componentInstance;
    c2.activeTab = 'export';
    f2.detectChanges();
    c2.downloadReadyDiscDbExport();

    expect(c2.discdbExportReady).toBeNull();
    expect(c2.discdbExportError).toContain('no longer available');
    c2.ngOnDestroy();
  });

  it('an unfinished archive is not offered for download', () => {
    spy.getActiveDiscDbExport.and.returnValue(
      of(job({ status: 'completed', download_ready: false }) as any),
    );

    const f2 = TestBed.createComponent(SettingsPageComponent);
    f2.detectChanges();

    expect(f2.componentInstance.discdbExportReady).toBeNull();
    f2.componentInstance.ngOnDestroy();
  });

  it('leaving the page stops the poll', fakeAsync(() => {
    spy.startDiscDbExport.and.returnValue(of(job() as any));
    spy.getDiscDbExportStatus.and.returnValue(of(job() as any));

    component.exportDiscDbSubmissions();
    tick(1000);
    const before = spy.getDiscDbExportStatus.calls.count();

    component.ngOnDestroy();
    tick(5000);

    expect(spy.getDiscDbExportStatus.calls.count()).toBe(before);
  }));
});

describe('SettingsPageComponent (#609 Notifications matrix)', () => {
  let component: SettingsPageComponent;
  let fixture: ComponentFixture<SettingsPageComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SettingsPageComponent],
      providers: [
        { provide: SystemService, useValue: makeSystemServiceSpy() },
        { provide: ToastService, useValue: jasmine.createSpyObj('ToastService', ['show']) },
        { provide: WorkflowService, useValue: { syncCoordinator: () => {} } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(SettingsPageComponent);
    component = fixture.componentInstance;
    component.activeTab = 'notifications';
    fixture.detectChanges();
  });

  function matrixTable(): HTMLTableElement | null {
    return fixture.nativeElement.querySelector('.settings-notif-table table');
  }

  it('hides the per-category matrix when the master Informative toggle is off', () => {
    component.discord.notification_preferences!.informative.enabled = false;
    fixture.detectChanges();
    expect(matrixTable()).toBeNull();
  });

  it('shows the per-category matrix when the master Informative toggle is on', () => {
    component.discord.notification_preferences!.informative.enabled = true;
    fixture.detectChanges();
    expect(matrixTable()).not.toBeNull();
    // One row per known informative category (rip_start, rip_complete,
    // job_completed, per_title, previews_ready, transfer_started,
    // label_complete — the last moved here from action-required).
    const rows = fixture.nativeElement.querySelectorAll('.settings-notif-table tbody tr');
    expect(rows.length).toBe(7);
  });

  it('restores prior per-channel values when re-enabling the master toggle', () => {
    // Tweak a few categories under "enabled" so we can verify they survive.
    component.discord.notification_preferences!.informative.enabled = true;
    component.discord.notification_preferences!.informative.categories['rip_start'].in_app = false;
    component.discord.notification_preferences!.informative.categories['rip_start'].discord = false;
    component.discord.notification_preferences!.informative.categories['previews_ready'].discord = false;
    fixture.detectChanges();

    component.discord.notification_preferences!.informative.enabled = false;
    fixture.detectChanges();
    expect(matrixTable()).toBeNull();

    component.discord.notification_preferences!.informative.enabled = true;
    fixture.detectChanges();
    expect(component.discord.notification_preferences!.informative.categories['rip_start'].in_app).toBe(false);
    expect(component.discord.notification_preferences!.informative.categories['rip_start'].discord).toBe(false);
    expect(component.discord.notification_preferences!.informative.categories['previews_ready'].discord).toBe(false);
    // The other categories untouched.
    expect(component.discord.notification_preferences!.informative.categories['rip_complete'].in_app).toBe(true);
  });

  it('keeps Action Required + Errors checkboxes visible regardless of Informative master state', () => {
    component.discord.notification_preferences!.informative.enabled = false;
    fixture.detectChanges();
    const blocks = fixture.nativeElement.querySelectorAll('.settings-notif-block');
    expect(blocks.length).toBe(3);
    // Action required + Errors each render a checkbox-row-group.
    const groups = fixture.nativeElement.querySelectorAll('.settings-checkbox-row-group');
    expect(groups.length).toBe(2);
  });

  it('renders Notifications blocks in order Errors → Action required → Informative', () => {
    component.discord.notification_preferences!.informative.enabled = true;
    fixture.detectChanges();
    const titles = Array.from(
      fixture.nativeElement.querySelectorAll('.settings-notif-block__title')
    ).map((el: any) => el.textContent.trim());
    expect(titles).toEqual([
      'Errors',
      'Action required',
      'Informative notifications',
    ]);
  });
});


describe('SettingsPageComponent (#610 TMDB key echo)', () => {
  let component: SettingsPageComponent;
  let fixture: ComponentFixture<SettingsPageComponent>;
  let systemSvc: jasmine.SpyObj<SystemService>;

  function buildHarness(tmdbResponse: { api_key_set: boolean; api_key?: string | null }) {
    const spy = makeSystemServiceSpy();
    spy.getTmdbConfig.and.returnValue(of(tmdbResponse as any));
    TestBed.resetTestingModule();
    return TestBed.configureTestingModule({
      imports: [SettingsPageComponent],
      providers: [
        { provide: SystemService, useValue: spy },
        { provide: ToastService, useValue: jasmine.createSpyObj('ToastService', ['show']) },
        { provide: WorkflowService, useValue: { syncCoordinator: () => {} } },
      ],
    }).compileComponents().then(() => {
      fixture = TestBed.createComponent(SettingsPageComponent);
      component = fixture.componentInstance;
      systemSvc = TestBed.inject(SystemService) as jasmine.SpyObj<SystemService>;
      component.activeTab = 'tmdb';
      fixture.detectChanges();
    });
  }

  it('pre-populates the tmdbApiKey field from the persisted api_key', async () => {
    await buildHarness({ api_key_set: true, api_key: 'tmdb-test-key-abc' });
    expect(component.tmdbApiKey).toBe('tmdb-test-key-abc');
    expect(component.tmdbApiKeySet).toBe(true);
  });

  it('leaves the field empty when no key is configured', async () => {
    await buildHarness({ api_key_set: false, api_key: null });
    expect(component.tmdbApiKey).toBe('');
    expect(component.tmdbApiKeySet).toBe(false);
  });

  it('updates the field from the save response (so a re-fetch is not needed)', async () => {
    await buildHarness({ api_key_set: false, api_key: null });
    systemSvc.saveTmdbConfig.and.returnValue(of({
      api_key_set: true,
      api_key: 'tmdb-fresh-save-xyz',
    } as any));

    component.tmdbApiKey = 'tmdb-fresh-save-xyz';
    component.saveTmdbConfig();

    expect(component.tmdbApiKey).toBe('tmdb-fresh-save-xyz');
    expect(component.tmdbApiKeySet).toBe(true);
  });

  it('clears the field when the save response returns null api_key (cleared on disk)', async () => {
    await buildHarness({ api_key_set: true, api_key: 'about-to-clear' });
    systemSvc.saveTmdbConfig.and.returnValue(of({
      api_key_set: false,
      api_key: null,
    } as any));

    component.tmdbApiKey = '';
    component.saveTmdbConfig();

    expect(component.tmdbApiKey).toBe('');
    expect(component.tmdbApiKeySet).toBe(false);
  });
});
