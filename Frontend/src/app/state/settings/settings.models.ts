export interface SelectedDrive {
    disc_num: string;
    mount_point: string;
  }
  
  export interface SettingsState {
    selectedDrive: SelectedDrive | null;
  }
  