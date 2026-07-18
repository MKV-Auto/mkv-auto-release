import { APP_INITIALIZER, type ApplicationConfig } from "@angular/core";
import { provideHttpClient } from "@angular/common/http";
import { provideRouter } from "@angular/router";
import { provideStore } from "@ngrx/store";
import { provideAnimationsAsync } from "@angular/platform-browser/animations/async";
import { routes } from "./app.routes";
import { settingsReducer } from "./state/settings/settings.reducer";
import { metaReducers, preloadedSettings } from "./state/settings/meta-reducers/local-storage.reducer";
import { ReadinessService } from "./services/readiness.service";
import { FrontendVersionService } from "./services/frontend-version.service";

/**
 * Bootstrap-time readiness wait. Blocks Angular until /readyz is 200 (or the
 * 90-second cap elapses) so the user never lands on a dead page during Postgres
 * WAL recovery. Updates the static #app-loading-overlay caption in index.html
 * with progress messages while polling, then removes the overlay on success.
 */
function readinessInitializerFactory(readiness: ReadinessService): () => Promise<void> {
  return async () => {
    const overlay = typeof document !== "undefined" ? document.getElementById("app-loading-overlay") : null;
    const caption = (overlay?.querySelector("[data-loading-caption]") as HTMLElement | null) ?? null;
    const updateMessage = (msg: string) => {
      if (caption) caption.textContent = msg;
    };
    try {
      await readiness.waitUntilReady(updateMessage);
    } catch (_err) {
      // Swallow — let the app boot and surface real errors via existing handlers.
    }
    if (overlay && overlay.parentNode) {
      overlay.parentNode.removeChild(overlay);
    }
  };
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideHttpClient(),
    provideRouter(routes),
    provideAnimationsAsync(),
    // Overlay is provided automatically by CDK when injected
    provideStore(
      {
        settings: settingsReducer,
      },
      {
        metaReducers,
        initialState: {
          settings: preloadedSettings,
        },
      }
    ),
    {
      provide: APP_INITIALIZER,
      useFactory: readinessInitializerFactory,
      deps: [ReadinessService],
      multi: true,
    },
    {
      // Kick off the version-polling poll once the app boots. The poll
      // fires every 30s; on a hash change the dev tab auto-reloads, prod
      // tabs see a toast prompt. Eliminates the manual hard-refresh cycle
      // when iterating in the docker container.
      provide: APP_INITIALIZER,
      useFactory: (svc: FrontendVersionService) => () => { svc.start(); },
      deps: [FrontendVersionService],
      multi: true,
    },
  ],
};
