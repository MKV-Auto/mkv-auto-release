import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
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
  ]);
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
    // job_completed, per_title, previews_ready, transfer_started).
    const rows = fixture.nativeElement.querySelectorAll('.settings-notif-table tbody tr');
    expect(rows.length).toBe(6);
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
