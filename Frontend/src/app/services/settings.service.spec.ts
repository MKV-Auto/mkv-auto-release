import { SettingsService } from './settings.service';

describe('SettingsService', () => {
  let svc: SettingsService;
  const store: Record<string, string> = {};

  beforeEach(() => {
    Object.defineProperty(window, 'localStorage', {
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => { store[k] = v; },
        removeItem: (k: string) => { delete store[k]; },
      },
      configurable: true,
    });
    delete store['bluray-ripper-settings'];
    svc = new SettingsService();
  });

  it('persists and retrieves settings from localStorage', () => {
    const payload = { output: '/tmp/out', mode: 'copy' };
    svc.saveSettings(payload);
    const loaded = svc.getSettings();
    expect(loaded).toEqual(payload);
  });

  it('returns null when nothing saved', () => {
    expect(svc.getSettings()).toBeNull();
  });
});
