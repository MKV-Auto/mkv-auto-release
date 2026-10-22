import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { SettingsPageComponent } from './settings-page.component';
import {
  SystemService,
  defaultNotificationPreferences,
  Dv7Config,
  Dv7Destination,
  Dv7LastScan,
  Dv7LibraryFile,
  Dv7LibrarySummary,
} from '../../services/system.service';
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
    'getDv7Config', 'saveDv7Config', 'startDv7Scan',
    'getDv7LibrarySummary', 'getDv7LibraryFiles', 'retryDv7File',
  ]);
  spy.getDv7Config.and.returnValue(of({
    enabled: false, convert_new_rips: false, last_scan: null,
    destination: { available: false, mode: null, name: null, display: null, problem: null },
  }));
  spy.getDv7LibrarySummary.and.returnValue(of({ total: 0, dv7: 0, by_status: {} }));
  spy.getDv7LibraryFiles.and.returnValue(of([]));
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

describe('SettingsPageComponent (#885 storage figures say what they measured)', () => {
  async function render(summary: any): Promise<HTMLElement> {
    const spy = makeSystemServiceSpy();
    spy.getStorageSummary.and.returnValue(of(summary));
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [SettingsPageComponent],
      providers: [
        { provide: SystemService, useValue: spy },
        { provide: ToastService, useValue: jasmine.createSpyObj('ToastService', ['show']) },
        { provide: WorkflowService, useValue: { syncCoordinator: () => {} } },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(SettingsPageComponent);
    fixture.componentInstance.activeTab = 'tmdb';
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows the reason instead of a 0 B figure when the destination does not exist', async () => {
    const el = await render({
      data_root: { free: 1000, total: 2000, path: '/data' },
      transfer_root: {
        free: 0, total: 0, used: 0, path: '/mnt/user/media',
        error: "/mnt/user/media doesn't exist inside the container.",
      },
    });
    const blocks = el.querySelectorAll('.settings-storage-block');
    expect(blocks[1].textContent).toContain("doesn't exist inside the container");
    expect(blocks[1].querySelector('.settings-storage-values')).toBeNull();
    expect(blocks[0].querySelector('.settings-storage-values')).not.toBeNull();
  });

  it('warns when the figure is the container\'s own disk', async () => {
    const el = await render({
      data_root: { free: 1000, total: 2000, path: '/data' },
      transfer_root: {
        free: 500, total: 1000, used: 500, path: '/library',
        warning: "/library isn't on a mounted drive",
      },
    });
    const block = el.querySelectorAll('.settings-storage-block')[1];
    expect(block.querySelector('.settings-storage-warning')?.textContent).toContain("isn't on a mounted drive");
    expect(block.querySelector('.settings-storage-values')).not.toBeNull();
  });
});

describe('SettingsPageComponent (#894 Dolby Vision conversion)', () => {
  let fixture: ComponentFixture<SettingsPageComponent>;
  let component: SettingsPageComponent | undefined;
  let spy: jasmine.SpyObj<SystemService>;
  let toast: jasmine.SpyObj<ToastService>;

  const SHARE: Dv7Destination = {
    available: true, mode: 'smb', name: 'Plex', display: 'smb://nas/PLEX Media', problem: null,
  };
  const OFF: Dv7Config = { enabled: false, convert_new_rips: false, destination: SHARE, last_scan: null };
  const ON: Dv7Config = { ...OFF, enabled: true };
  const scan = (over: Partial<Dv7LastScan> = {}): Dv7LastScan => ({
    at: '2026-10-12T10:00:00Z', ok: true, destination: SHARE.display,
    files: 4210, dv7: 4, queued: 4, unreadable: 0, problem: null, ...over,
  });
  const file = (over: Partial<Dv7LibraryFile> = {}): Dv7LibraryFile => ({
    id: 'f1', path: '/media/Movies/Dune (2021)/Dune.mkv', library_root: '/media',
    size: 60 * 1024 ** 3, video_codec: 'hevc', width: 3840, height: 2160, dv_profile: 7,
    duration: 9300, status: 'catalogued', progress: null, error: null,
    scanned_at: null, converted_at: null,
    ...over,
  });
  const summary = (by_status: Dv7LibrarySummary['by_status'] = {}): Dv7LibrarySummary => ({
    total: 40, dv7: 3, by_status,
  });

  beforeEach(async () => {
    spy = makeSystemServiceSpy();
    toast = jasmine.createSpyObj('ToastService', ['show']);
    component = undefined;
    await TestBed.configureTestingModule({
      imports: [SettingsPageComponent],
      providers: [
        { provide: SystemService, useValue: spy },
        { provide: ToastService, useValue: toast },
        { provide: WorkflowService, useValue: { syncCoordinator: () => {} } },
      ],
    }).compileComponents();
  });

  afterEach(() => component?.ngOnDestroy());

  /** Opens the Dolby Vision tab. Run inside fakeAsync whenever polling may start. */
  function open(): SettingsPageComponent {
    fixture = TestBed.createComponent(SettingsPageComponent);
    component = fixture.componentInstance;
    component.activeTab = 'dv7';
    fixture.detectChanges();
    return component;
  }

  const q = (sel: string): HTMLElement | null => fixture.nativeElement.querySelector(sel);
  const text = (): string => fixture.nativeElement.textContent;
  const scanButton = (): HTMLButtonElement => q('[data-testid="dv7-scan"] button') as HTMLButtonElement;

  it('has its own sidebar entry', () => {
    const c = open();
    expect(c.nav.some(n => n.id === 'dv7' && n.label === 'Dolby Vision')).toBe(true);
  });

  it('renders with everything off by default', () => {
    open();
    expect(spy.getDv7Config).toHaveBeenCalled();
    expect(q('[data-testid="dv7-enabled"]')!.getAttribute('aria-pressed')).toBe('false');
    expect(q('[data-testid="dv7-new-rips"]')!.getAttribute('aria-pressed')).toBe('false');
    expect(text()).toContain('Convert Dolby Vision profile 7 to 8.1');
    expect(text()).toContain('lossless');
    expect(text()).toContain('twice its size');
    expect(q('[data-testid="dv7-folder-input"]')).toBeNull();
    expect(q('[data-testid="dv7-destination"]')!.textContent).toContain('No transfer destination');
    expect(scanButton().disabled).toBe(true);
  });

  it('does not fetch Dolby Vision data until the tab is opened', () => {
    fixture = TestBed.createComponent(SettingsPageComponent);
    component = fixture.componentInstance;
    component.activeTab = 'tmdb';
    fixture.detectChanges();
    expect(spy.getDv7Config).not.toHaveBeenCalled();

    component.selectTab('dv7');
    expect(spy.getDv7Config).toHaveBeenCalledTimes(1);
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(1);
  });

  it('the master toggle saves via PUT', () => {
    spy.saveDv7Config.and.returnValue(of({ ...OFF, enabled: true }));
    open();

    q('[data-testid="dv7-enabled"]')!.click();
    fixture.detectChanges();

    expect(spy.saveDv7Config).toHaveBeenCalledWith({ enabled: true });
    expect(q('[data-testid="dv7-enabled"]')!.getAttribute('aria-pressed')).toBe('true');
  });

  it('the new-rips toggle is disabled while the master is off', () => {
    const c = open();
    const sub = q('[data-testid="dv7-new-rips"]') as HTMLButtonElement;
    expect(sub.disabled).toBe(true);
    c.setDv7ConvertNewRips(true);
    expect(spy.saveDv7Config).not.toHaveBeenCalled();
  });

  it('the new-rips toggle reads as off while the master is off, whatever is stored', () => {
    // The server stores convert_new_rips=true by default; with the master off
    // nothing converts, so the toggle must not look on.
    spy.getDv7Config.and.returnValue(of({ ...OFF, convert_new_rips: true }));
    open();
    expect(q('[data-testid="dv7-new-rips"]')!.getAttribute('aria-pressed')).toBe('false');
  });

  it('the new-rips toggle saves once the master is on', () => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.saveDv7Config.and.returnValue(of({ ...ON, convert_new_rips: true }));
    open();
    const sub = q('[data-testid="dv7-new-rips"]') as HTMLButtonElement;
    expect(sub.disabled).toBe(false);

    sub.click();
    expect(spy.saveDv7Config).toHaveBeenCalledWith({ convert_new_rips: true });
  });

  it('shows the transfer destination as the library, with its name (#902)', () => {
    spy.getDv7Config.and.returnValue(of(ON));
    open();
    const dest = q('[data-testid="dv7-destination"]')!;
    expect(dest.textContent).toContain('smb://nas/PLEX Media');
    expect(dest.textContent).toContain('Plex');
    expect(dest.classList).not.toContain('is-bad');
  });

  it('shows why the destination can\'t be scanned and disables Scan', () => {
    const problem = "Your transfer destination uses rsync, which the library scan can't read from.";
    spy.getDv7Config.and.returnValue(of({
      ...ON, destination: { available: false, mode: 'rsync', name: 'Box', display: null, problem },
    }));
    const c = open();
    const dest = q('[data-testid="dv7-destination"]')!;
    expect(dest.classList).toContain('is-bad');
    expect(dest.textContent).toContain(problem);
    expect(scanButton().disabled).toBe(true);
    expect(c.dv7ScanHint).toBe(problem);
  });

  it('Manage destinations opens the Destinations tab', () => {
    const c = open();
    (q('[data-testid="dv7-manage-destinations"] button') as HTMLElement).click();
    expect(c.activeTab).toBe('configs');
  });

  it('the counts are filters: select one to list it, select it again for queued + converting', () => {
    spy.getDv7Config.and.returnValue(of(ON));
    const c = open();
    const cell = (key: string) => q(`[data-filter="${key}"]`) as HTMLButtonElement;
    expect(c.dv7Filter).toBeNull();
    expect(q('[data-testid="dv7-filter-note"]')!.textContent).toContain('queued and converting');

    cell('unreadable').click();
    fixture.detectChanges();
    expect(c.dv7Filter).toBe('unreadable');
    expect(cell('unreadable').getAttribute('aria-pressed')).toBe('true');
    expect(spy.getDv7LibraryFiles).toHaveBeenCalledWith({ dv7_only: false, limit: 200, status: 'unreadable' });
    expect(q('[data-testid="dv7-filter-note"]')!.textContent).toContain('Unreadable');

    cell('unreadable').click();
    fixture.detectChanges();
    expect(c.dv7Filter).toBeNull();
    expect(spy.getDv7LibraryFiles.calls.mostRecent().args[0])
      .toEqual({ dv7_only: false, limit: 200, status: ['queued', 'converting'] });
  });

  it('Catalogued lists every video and DV7 found lists profile 7 files', () => {
    spy.getDv7Config.and.returnValue(of(ON));
    const c = open();
    c.toggleDv7Filter('all');
    expect(spy.getDv7LibraryFiles.calls.mostRecent().args[0]).toEqual({ dv7_only: false, limit: 200 });
    c.toggleDv7Filter('dv7');
    expect(spy.getDv7LibraryFiles.calls.mostRecent().args[0]).toEqual({ dv7_only: false, limit: 200, dv_profile: 7 });
    expect(c.dv7EmptyText).toBe('No Dolby Vision profile 7 files found.');
  });

  it('lists an unreadable file with its reason and no Retry (#902)', () => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.getDv7LibraryFiles.and.returnValue(of([file({
      id: 'u1', path: '/media/Movies/Due Date/Due Date.mp4', dv_profile: null, status: 'unreadable',
      error: "Couldn't read this file. It may be damaged or incomplete; check whether it plays.",
    })]));
    open();
    const row = q('[data-status="unreadable"]')!;
    expect(row.textContent).toContain('Due Date.mp4');
    expect(row.textContent).toContain("Couldn't read this file");
    expect(row.querySelector('[data-testid="dv7-retry"]')).toBeNull();
  });

  it('offers Retry only on Dolby Vision 7 files', () => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.getDv7LibraryFiles.and.returnValue(of([
      file({ id: 'a', status: 'failed', error: 'boom' }),
      file({ id: 'b', path: '/media/x.mp4', dv_profile: null, status: 'failed', error: 'old 1.7.0 row' }),
    ]));
    open();
    expect(fixture.nativeElement.querySelectorAll('[data-testid="dv7-retry"]').length).toBe(1);
  });

  it('a finished scan with file problems shows counts plus a short note, not "failed"', () => {
    spy.getDv7Config.and.returnValue(of({ ...ON, last_scan: scan({
      ok: true, problem: "3 files couldn't be read from the share; details are in the app log.",
    }) }));
    open();
    const line = q('[data-testid="dv7-last-scan"]')!;
    expect(line.textContent).toContain('4210 videos');
    expect(line.textContent).toContain("3 files couldn't be read from the share");
    expect(line.textContent).not.toContain('failed');
    expect(line.classList).not.toContain('is-bad');
  });

  it('the last scan mentions unreadable files', () => {
    spy.getDv7Config.and.returnValue(of({ ...ON, last_scan: scan({ unreadable: 3 }) }));
    open();
    expect(q('[data-testid="dv7-last-scan"]')!.textContent).toContain('3 unreadable (listed below)');
  });

  it('shows the last scan\'s counts, or why it failed', () => {
    spy.getDv7Config.and.returnValue(of({ ...ON, last_scan: scan() }));
    open();
    const line = q('[data-testid="dv7-last-scan"]')!;
    expect(line.textContent).toContain('4210 videos, 4 Dolby Vision 7, 4 queued for conversion.');
    expect(line.classList).not.toContain('is-bad');
    component!.ngOnDestroy();

    spy.getDv7Config.and.returnValue(of({
      ...ON, last_scan: scan({ ok: false, problem: "Couldn't mount //nas/PLEX Media: Host is down" }),
    }));
    open();
    const bad = q('[data-testid="dv7-last-scan"]')!;
    expect(bad.textContent).toContain("failed: Couldn't mount //nas/PLEX Media: Host is down");
    expect(bad.classList).toContain('is-bad');
  });

  it('while a scan runs, Scan is disabled and the page keeps polling for its result', fakeAsync(() => {
    spy.getDv7Config.and.returnValue(of({ ...ON, last_scan: scan({ ok: null, files: 1200 }) }));
    const c = open();
    expect(q('[data-testid="dv7-last-scan"]')!.textContent).toContain('Scanning since');
    expect(q('[data-testid="dv7-last-scan"]')!.textContent).toContain('1200 files checked so far.');
    expect(scanButton().disabled).toBe(true);

    const calls = spy.getDv7Config.calls.count();
    spy.getDv7Config.and.returnValue(of({ ...ON, last_scan: scan() }));
    tick(c.dv7PollMs);
    fixture.detectChanges();
    expect(spy.getDv7Config.calls.count()).toBeGreaterThan(calls);
    expect(q('[data-testid="dv7-last-scan"]')!.textContent).toContain('4210 videos');
    expect(scanButton().disabled).toBe(false);
    c.ngOnDestroy();
  }));

  it('Scan now is disabled when off and calls the endpoint when on', fakeAsync(() => {
    open().ngOnDestroy();
    expect(scanButton().disabled).toBe(true);

    spy.getDv7Config.and.returnValue(of(ON));
    spy.startDv7Scan.and.returnValue(of({ queued: true }));
    const c = open();
    expect(scanButton().disabled).toBe(false);

    scanButton().click();
    expect(spy.startDv7Scan).toHaveBeenCalledTimes(1);
    expect(toast.show).toHaveBeenCalledWith(jasmine.any(String), 'success');
    c.ngOnDestroy();
  }));

  it('Scan now is disabled with no transfer destination even when on', () => {
    spy.getDv7Config.and.returnValue(of({
      ...ON, destination: { available: false, mode: null, name: null, display: null, problem: 'No transfer destination is set up.' },
    }));
    open();
    expect(scanButton().disabled).toBe(true);
  });

  it('shows the server\'s reason when a scan is refused', () => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.startDv7Scan.and.returnValue(
      throwError(() => ({ status: 409, error: { detail: 'No transfer destination is set up.' } })),
    );
    const c = open();
    c.scanDv7Library();
    fixture.detectChanges();
    expect(q('[data-testid="dv7-error"]')!.textContent).toContain('No transfer destination is set up.');
  });

  it('shows the summary counts, treating missing statuses as 0', () => {
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ converted: 2, failed: 1 })));
    open();
    const cells = Array.from(fixture.nativeElement.querySelectorAll('.settings-dv7-summary > button'))
      .map((cell: any) => [
        cell.querySelector('.settings-import-summary__value').textContent.trim(),
        cell.querySelector('.settings-import-summary__label').textContent.trim(),
      ].join(' '));
    expect(cells).toEqual([
      '40 Catalogued', '3 DV7 found', '0 Queued', '0 Converting',
      '2 Converted', '1 Failed', '0 Skipped', '0 Unreadable',
    ]);
    expect(spy.getDv7LibraryFiles).toHaveBeenCalledWith({ dv7_only: false, limit: 200, status: ['queued', 'converting'] });
  });

  it('lists files with status, progress, errors and Retry', fakeAsync(() => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ converting: 1, failed: 1, skipped: 1 })));
    spy.getDv7LibraryFiles.and.returnValue(of([
      file({ id: 'a', status: 'converting', progress: 0.42 }),
      file({ id: 'b', path: '/media/B.mkv', status: 'failed', error: 'Verification failed: frame count differs.' }),
      file({ id: 'c', path: '/media/C.mkv', status: 'skipped', error: 'Not enough free space.' }),
      file({ id: 'd', path: '/media/D.mkv', status: 'converted', dv_profile: 8 }),
    ]));
    spy.retryDv7File.and.returnValue(of({ queued: true }));
    const c = open();

    const rows = fixture.nativeElement.querySelectorAll('.settings-dv7-file');
    expect(rows.length).toBe(4);
    // Folder-relative path, size and resolution.
    expect(rows[0].textContent).toContain('Movies/Dune (2021)/Dune.mkv');
    expect(rows[0].textContent).not.toContain('/media/Movies');
    expect(rows[0].textContent).toContain('60 GB');
    expect(rows[0].textContent).toContain('3840×2160');
    // Progress bar only while converting.
    const fill = rows[0].querySelector('.settings-export-progress__fill') as HTMLElement;
    expect(fill.style.width).toBe('42%');
    expect(rows[3].querySelector('.settings-export-progress')).toBeNull();
    // Pills carry the status and its tone.
    expect(rows[0].querySelector('.ui-pill').getAttribute('data-tone')).toBe('indigo');
    expect(rows[1].querySelector('.ui-pill').getAttribute('data-tone')).toBe('red');
    expect(rows[2].querySelector('.ui-pill').getAttribute('data-tone')).toBe('amber');
    expect(rows[3].querySelector('.ui-pill').textContent).toContain('Converted');
    // Errors and Retry on failed / skipped only.
    expect(rows[1].textContent).toContain('frame count differs');
    expect(rows[2].textContent).toContain('Not enough free space');
    expect(rows[0].querySelector('[data-testid="dv7-retry"]')).toBeNull();
    expect(rows[2].querySelector('[data-testid="dv7-retry"]')).not.toBeNull();
    expect(rows[3].querySelector('[data-testid="dv7-retry"]')).toBeNull();

    (rows[1].querySelector('[data-testid="dv7-retry"] button') as HTMLButtonElement).click();
    expect(spy.retryDv7File).toHaveBeenCalledWith('b');
    expect(toast.show).toHaveBeenCalledWith(jasmine.any(String), 'success');
    c.ngOnDestroy();
  }));

  it('shows why a queued file is waiting for disk space, without a Retry (#895)', fakeAsync(() => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ queued: 1 })));
    spy.getDv7LibraryFiles.and.returnValue(of([
      file({ id: 'w', status: 'queued', error: 'Waiting for disk space: this needs 110.0 GB.' }),
      file({ id: 'q', path: '/media/Q.mkv', status: 'queued' }),
    ]));
    const c = open();
    const rows = fixture.nativeElement.querySelectorAll('.settings-dv7-file');
    expect(rows[0].textContent).toContain('Waiting for disk space');
    expect(rows[0].querySelector('[data-testid="dv7-retry"]')).toBeNull();
    expect(rows[1].querySelector('.settings-dv7-error')).toBeNull();
    c.ngOnDestroy();
  }));

  it('toasts the server\'s reason when a retry is refused', () => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.getDv7LibraryFiles.and.returnValue(of([file({ status: 'failed', error: 'x' })]));
    spy.retryDv7File.and.returnValue(
      throwError(() => ({ status: 409, error: { detail: 'Only a failed or skipped file can be retried.' } })),
    );
    const c = open();
    c.retryDv7File(c.dv7Files[0]);
    expect(toast.show).toHaveBeenCalledWith('Only a failed or skipped file can be retried.', 'error');
    expect(c.dv7RetryingId).toBeNull();
  });

  it('polls every 5 s while something is queued or converting, then stops', fakeAsync(() => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ queued: 1 })));
    spy.getDv7LibraryFiles.and.returnValue(of([file({ status: 'queued' })]));
    open();
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(1);

    tick(5000);
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(2);
    expect(spy.getDv7LibraryFiles).toHaveBeenCalledTimes(2);

    // The conversion finishes: the next poll sees it, then polling stops.
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ converted: 1 })));
    spy.getDv7LibraryFiles.and.returnValue(of([file({ status: 'converted', dv_profile: 8 })]));
    tick(5000);
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(3);
    tick(30000);
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(3);
  }));

  it('does not poll when nothing is in flight', fakeAsync(() => {
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ converted: 3 })));
    open();
    tick(30000);
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(1);
  }));

  it('stops polling when leaving the tab or the page', fakeAsync(() => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ converting: 1 })));
    const c = open();

    c.selectTab('tmdb');
    tick(30000);
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(1);

    c.selectTab('dv7');
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(2);
    c.ngOnDestroy();
    tick(30000);
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(2);
  }));

  it('keeps watching briefly after a scan so newly queued files appear', fakeAsync(() => {
    spy.getDv7Config.and.returnValue(of(ON));
    spy.startDv7Scan.and.returnValue(of({ queued: true }));
    const c = open();
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(1);

    c.scanDv7Library();
    expect(spy.getDv7LibrarySummary).toHaveBeenCalledTimes(2);

    // The scan queues a file a few seconds later; polling picks it up.
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ queued: 1 })));
    tick(5000);
    expect(c.dv7InFlight).toBe(true);

    // Once it is done and the grace window is spent, polling stops.
    spy.getDv7LibrarySummary.and.returnValue(of(summary({ converted: 1 })));
    tick(60000);
    const calls = spy.getDv7LibrarySummary.calls.count();
    tick(30000);
    expect(spy.getDv7LibrarySummary.calls.count()).toBe(calls);
    c.ngOnDestroy();
  }));
});

describe('SettingsPageComponent (#911 eject when the copy finishes)', () => {
  let fixture: ComponentFixture<SettingsPageComponent>;
  let component: SettingsPageComponent;
  let spy: jasmine.SpyObj<SystemService>;

  beforeEach(async () => {
    spy = makeSystemServiceSpy();
    spy.getDiscdbLookupConfig.and.returnValue(of({
      discdb_miss_workflow_with_prefill: true, eject_after_copy: false, failed_rip_retention_days: 7,
    }));
    spy.saveDiscdbLookupConfig.and.callFake((cfg: any) => of(cfg));
    spy.saveAutoRipConfig.and.callFake((cfg: any) => of(cfg));
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
    component.activeTab = 'copy';
    fixture.detectChanges();
  });

  afterEach(() => component.ngOnDestroy());

  function toggle(label: string): HTMLButtonElement {
    const fields = Array.from(fixture.nativeElement.querySelectorAll('ui-field')) as HTMLElement[];
    const field = fields.find(f => (f.getAttribute('label') || f.textContent || '').includes(label));
    expect(field).withContext(label).toBeTruthy();
    return field!.querySelector('button.settings-toggle') as HTMLButtonElement;
  }

  it('offers eject after copy and no longer offers eject on finish', () => {
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Eject disc when the copy finishes');
    expect(text).not.toContain('Eject disc on finish');
  });

  it('saves eject_after_copy', () => {
    toggle('when the copy finishes').click();
    fixture.detectChanges();
    component.saveDiscDbLookupConfig();
    const saved = spy.saveDiscdbLookupConfig.calls.mostRecent().args[0] as any;
    expect(saved.eject_after_copy).toBeTrue();
    expect('eject_on_finish' in saved).toBeFalse();
  });

  it('saves how long failed copies are kept, clamping bad input (#877)', () => {
    const input = fixture.nativeElement.querySelector('[data-testid="failed-rip-retention-days"]') as HTMLInputElement;
    expect(input).toBeTruthy();
    component.discDbLookup.failed_rip_retention_days = 14;
    component.saveDiscDbLookupConfig();
    expect((spy.saveDiscdbLookupConfig.calls.mostRecent().args[0] as any).failed_rip_retention_days).toBe(14);

    component.discDbLookup.failed_rip_retention_days = -3;
    component.saveDiscDbLookupConfig();
    expect((spy.saveDiscdbLookupConfig.calls.mostRecent().args[0] as any).failed_rip_retention_days).toBe(0);

    component.discDbLookup.failed_rip_retention_days = null as unknown as number;   // emptied field
    component.saveDiscDbLookupConfig();
    expect((spy.saveDiscdbLookupConfig.calls.mostRecent().args[0] as any).failed_rip_retention_days).toBe(7);
  });
});
