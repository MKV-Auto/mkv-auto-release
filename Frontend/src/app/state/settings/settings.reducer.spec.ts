import { settingsReducer, initialState } from './settings.reducer';
import { setSelectedDrive } from './settings.actions';

describe('settings.reducer', () => {
  it('has initialState with selectedDrive null', () => {
    expect(initialState).toEqual({ selectedDrive: null });
  });

  it('on setSelectedDrive sets selectedDrive', () => {
    const drive = { disc_num: '2', mount_point: '/mnt/cdrom' };
    const result = settingsReducer(initialState, setSelectedDrive({ drive }));
    expect(result.selectedDrive).toEqual(drive);
    expect(result).toEqual({ ...initialState, selectedDrive: drive });
  });
});
