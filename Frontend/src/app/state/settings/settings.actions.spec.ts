import { setSelectedDrive } from './settings.actions';

describe('settings.actions', () => {
  describe('setSelectedDrive', () => {
    it('returns action with type and drive', () => {
      const drive = { disc_num: '1', mount_point: '/mnt/dvd' };
      const action = setSelectedDrive({ drive });
      expect(action.type).toBe('[Drives] Set Selected Drive');
      expect(action.drive).toEqual(drive);
    });
  });
});
