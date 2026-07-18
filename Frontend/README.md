# Frontend (Angular)

Angular 19 UI for MakeMKV-Auto. Talks to the backend at `http://localhost:8000` by default (see `src/app/environments`). State comes from the backend; the UI can reconnect to active jobs at any time.

## Install
```bash
cd Frontend
npm install
```

## Run
```bash
npm run start      # alias for ng serve (http://localhost:4200)
```

## Build
```bash
npm run build      # outputs to dist/disc-ripper-ui
```

## Tests
```bash
npm run test       # Karma; set CHROME_BIN for headless runs in CI
npm run e2e        # if configured (Playwright)
```

## API base
`src/app/environments/environment.ts` sets `apiBase` (defaults to `http://localhost:8000`). Override via environment.ts or at deploy time if the backend is hosted elsewhere.

## Notes
- UI expects release/disc/job APIs; legacy “group” endpoints have been removed from the frontend.
- Labeling can target an active job or a disc (via disc_id) when no job is active. Release lists and exports are available from the history page.
