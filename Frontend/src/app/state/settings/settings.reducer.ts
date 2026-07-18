import { createReducer, on } from '@ngrx/store';
import { setSelectedDrive } from './settings.actions';
import { SettingsState } from './settings.models';

export const initialState: SettingsState = {
  selectedDrive: null,
};

// we’ll overwrite `initialState` below with the copy in localStorage
export const settingsReducer = createReducer(
  initialState,
  on(setSelectedDrive, (state, { drive }) => ({ ...state, selectedDrive: drive }))
);