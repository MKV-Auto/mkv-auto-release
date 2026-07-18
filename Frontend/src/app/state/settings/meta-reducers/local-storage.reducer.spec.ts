import { localStorageMetaReducer } from './local-storage.reducer';

describe('localStorageMetaReducer', () => {
  let setItemSpy: jasmine.Spy;

  beforeEach(() => {
    setItemSpy = jasmine.createSpy('setItem');
    Object.defineProperty(window, 'localStorage', {
      value: { getItem: () => null, setItem: setItemSpy, removeItem: () => {} },
      configurable: true,
    });
  });

  it('wraps reducer and returns next state', () => {
    const inner = jasmine.createSpy('reducer').and.returnValue({
      settings: { selectedDrive: { disc_num: '1', mount_point: '/mnt/x' } },
    });
    const wrapped = localStorageMetaReducer(inner as any);
    const state = { settings: { selectedDrive: null } };
    const action = { type: '[Drives] Set Selected Drive', drive: { disc_num: '1', mount_point: '/mnt/x' } };
    const result = wrapped(state, action);
    expect(inner).toHaveBeenCalledWith(state, action);
    expect(result.settings.selectedDrive).toEqual({ disc_num: '1', mount_point: '/mnt/x' });
  });

  it('calls localStorage.setItem with settings when reducer returns next state', () => {
    const nextSettings = { selectedDrive: { disc_num: '2', mount_point: '/mnt/y' } };
    const inner = () => ({ settings: nextSettings });
    const wrapped = localStorageMetaReducer(inner as any);
    wrapped(undefined as any, { type: 'ANY' });
    expect(setItemSpy).toHaveBeenCalledWith('app-settings', JSON.stringify(nextSettings));
  });
});
