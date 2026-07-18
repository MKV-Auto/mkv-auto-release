// src/app/state/meta-reducers/local-storage.reducer.ts
import { ActionReducer, MetaReducer } from '@ngrx/store';
import { environment } from '../../../environments/environment';
import { AppState } from '../../root-state';   // interface with all top‑level slices

const KEY = 'app-settings';

function loadSettings(): AppState['settings'] | undefined {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? 'null') ?? undefined;
  } catch {
    return undefined;
  }
}

export function localStorageMetaReducer(
  reducer: ActionReducer<AppState>
): ActionReducer<AppState> {
  return (state, action) => {
    const next = reducer(state, action);

    // write every time the settings slice changes
    localStorage.setItem(KEY, JSON.stringify(next.settings));

    return next;
  };
}

export const metaReducers: MetaReducer<AppState>[] = [
  ...(environment.production ? [] : []),
  localStorageMetaReducer,
];

// expose the initial state so we can feed it into provideStore()
export const preloadedSettings = loadSettings() ?? {
  selectedDrive: null,
};
