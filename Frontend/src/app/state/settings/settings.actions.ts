import { createAction, props } from '@ngrx/store';
import { SelectedDrive } from './settings.models';

export const setSelectedDrive = createAction(
  '[Drives] Set Selected Drive',
  props<{ drive: SelectedDrive }>()
);
