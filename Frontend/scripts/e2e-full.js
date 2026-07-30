#!/usr/bin/env node
/**
 * Full-stack E2E: start test Redis, E2E backend, build + serve frontend, run Playwright, teardown.
 *
 * 1. Start test Redis (Docker redis:alpine or redis-server --daemonize yes)
 * 2. Build frontend (npm run build)
 * 3. Start E2E backend (run_e2e_backend.py), wait for http://localhost:8000/healthz, then /readyz
 * 4. Serve frontend (http-server dist/disc-ripper-ui -p 4200)
 * 5. Run playwright test with E2E_USE_EXISTING=1, E2E_BASE_URL=http://localhost:4200, E2E_FULL=1
 * 6. Teardown: stop backend, http-server, and test Redis
 *
 * Run from Frontend: npm run e2e:full
 */

const { spawn, execSync } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

const repo = path.resolve(__dirname, '..', '..');
const frontend = path.join(repo, 'Frontend');
const backendPy = path.join(
  repo,
  'Backend',
  '.venv',
  process.platform === 'win32' ? 'Scripts' : 'bin',
  process.platform === 'win32' ? 'python.exe' : 'python'
);
const runE2eBackend = path.join(repo, 'Backend', 'scripts', 'run_e2e_backend.py');

let startedRedis = false;
let redisKind = null; // 'docker' | 'daemon'
let startedPg = false;
let backendPro = null;
let httpPro = null;
const BACKEND_PORT = parseInt(process.env.PORT || '8000', 10);
// Test Redis runs on a non-default port so a co-located production mkv-auto
// container's Redis on 6379 cannot intercept Celery broker traffic, progress
// pub/sub, or redis_cache writes. Backend honors this via REDIS_URL (see
// Backend/scripts/run_e2e_backend.py). Issue #378.
const TEST_REDIS_PORT = parseInt(process.env.E2E_REDIS_PORT || '6380', 10);
// Test Postgres on a non-default port: prod uses 5432, and a SQLite E2E DB
// hit "database is locked" under eager-Celery + nested SessionLocal because
// SQLite serializes all writes. Postgres's MVCC handles concurrent writers
// natively, which is what we get for free in production.
const TEST_PG_PORT = parseInt(process.env.E2E_PG_PORT || '5433', 10);
const TEST_PG_USER = process.env.E2E_PG_USER || 'postgres';
const TEST_PG_PASSWORD = process.env.E2E_PG_PASSWORD || 'e2e_pass';
const TEST_PG_DB = process.env.E2E_PG_DB || 'e2e';
let backendExitedBeforeReady = false;
let backendExitCode = null;

/** All script output goes through this helper for consistent [e2e-full] prefix. */
function log(msg) {
  const ts = new Date().toISOString();
  console.log(`[e2e-full ${ts}] ${msg}`);
}

function kill(child, name) {
  if (child && !child.killed) {
    try {
      child.kill('SIGTERM');
      log(`Sent SIGTERM to ${name}`);
    } catch (e) {
      log(`Error killing ${name}: ${e.message}`);
    }
  }
}

function isPortInUse(port) {
  return new Promise((resolve) => {
    const req = http.request(
      { hostname: '127.0.0.1', port, path: '/', method: 'GET' },
      () => resolve(true)
    );
    req.on('error', () => resolve(false));
    req.setTimeout(500, () => { req.destroy(); resolve(false); });
    req.end();
  });
}

function waitFor(url, maxAttempts = 60, intervalMs = 500) {
  return new Promise((resolve, reject) => {
    let n = 0;
    function run() {
      const u = new URL(url);
      const req = http.request(
        { hostname: u.hostname, port: u.port || (u.protocol === 'https:' ? 443 : 80), path: u.pathname, method: 'GET' },
        (res) => {
          if (res.statusCode >= 200 && res.statusCode < 400) {
            resolve();
            return;
          }
          next();
        }
      );
      req.on('error', next);
      req.setTimeout(2000, () => { req.destroy(); next(); });
      req.end();
    }
    function next() {
      n++;
      if (n >= maxAttempts) {
        reject(new Error(`${url} did not become ready after ${maxAttempts} attempts`));
        return;
      }
      setTimeout(run, intervalMs);
    }
    run();
  });
}

function teardown() {
  kill(backendPro, 'E2E backend');
  kill(httpPro, 'http-server');
  if (startedRedis) {
    if (redisKind === 'docker') {
      try {
        execSync('docker stop mkv_e2e_redis', { stdio: 'ignore' });
      } catch (_) {}
      try {
        execSync('docker rm mkv_e2e_redis', { stdio: 'ignore' });
      } catch (_) {}
      log('Stopped and removed mkv_e2e_redis');
    } else if (redisKind === 'daemon') {
      try {
        execSync(`redis-cli -p ${TEST_REDIS_PORT} shutdown`, { stdio: 'ignore' });
        log('Stopped redis-server');
      } catch (e) {
        log('Teardown Redis (daemon): ' + e.message);
      }
    }
  }
  if (startedPg) {
    try {
      execSync('docker stop mkv_e2e_pg', { stdio: 'ignore' });
    } catch (_) {}
    try {
      execSync('docker rm mkv_e2e_pg', { stdio: 'ignore' });
    } catch (_) {}
    log('Stopped and removed mkv_e2e_pg');
  }
}

function onExit(code) {
  teardown();
  process.exit(code != null ? code : 0);
}

process.on('SIGINT', () => onExit(130));
process.on('SIGTERM', () => onExit(143));

async function main() {
  log('e2e-full: starting full-stack E2E');

  // 0. Issue #378: refuse to run if a production mkv-auto container is up
  //    *and* would collide with the test stack (host port 8000 or host Redis
  //    on 6379). Override with E2E_ALLOW_PROD_CONTAINER=1 if you've already
  //    set PORT to dodge 8000 and trust the test Redis on 6380 to stay clear.
  try {
    const prodRunning = execSync(
      'docker ps --filter "name=^mkv-auto$" --format "{{.Names}}"',
      { stdio: ['ignore', 'pipe', 'ignore'] }
    ).toString().trim() === 'mkv-auto';
    if (prodRunning && !process.env.E2E_ALLOW_PROD_CONTAINER) {
      log('Refusing to run: production "mkv-auto" container is running on this host.');
      log('Either stop it (`docker stop mkv-auto`) before running E2E, or set');
      log('E2E_ALLOW_PROD_CONTAINER=1 to override (PORT must be set to a non-8000 port).');
      log('Issue #378 explains the collision modes.');
      process.exit(2);
    }
  } catch (_) {
    // docker not installed or no permission — assume no prod container.
  }

  // 1. Start test Redis on a dedicated port so prod's Redis on 6379 is left alone.
  try {
    execSync('docker rm -f mkv_e2e_redis 2>/dev/null', { stdio: 'ignore' });
    execSync(
      `docker run -d -p ${TEST_REDIS_PORT}:6379 --name mkv_e2e_redis redis:alpine`,
      { stdio: 'inherit' }
    );
    startedRedis = true;
    redisKind = 'docker';
    log(`Started test Redis (Docker mkv_e2e_redis on host port ${TEST_REDIS_PORT})`);
  } catch (e) {
    try {
      // Fallback: redis-server bound to the test port via --port flag.
      execSync(`redis-server --port ${TEST_REDIS_PORT} --daemonize yes`, { stdio: 'inherit' });
      startedRedis = true;
      redisKind = 'daemon';
      log(`Started test Redis (redis-server --port ${TEST_REDIS_PORT} --daemonize yes)`);
    } catch (e2) {
      log(`WARN: Could not start Redis via Docker or redis-server on port ${TEST_REDIS_PORT}.`);
      log('  Docker: ' + (e.message || e));
      log('  redis-server: ' + (e2.message || e2));
    }
  }

  // 1b. Start test Postgres on a dedicated port. SQLite serializes all writes
  //     and trips "database is locked" under eager Celery + nested SessionLocal,
  //     which blocks the postprocess intercept (see #378 / #195). Postgres
  //     handles concurrent writers natively via MVCC. Empty volume each run
  //     gives us a clean schema via ``alembic upgrade head`` in the backend.
  try {
    execSync('docker rm -f mkv_e2e_pg 2>/dev/null', { stdio: 'ignore' });
    execSync(
      `docker run -d -p ${TEST_PG_PORT}:5432 ` +
        `-e POSTGRES_USER=${TEST_PG_USER} ` +
        `-e POSTGRES_PASSWORD=${TEST_PG_PASSWORD} ` +
        `-e POSTGRES_DB=${TEST_PG_DB} ` +
        '--name mkv_e2e_pg postgres:16-alpine',
      { stdio: 'inherit' }
    );
    startedPg = true;
    log(`Started test Postgres (Docker mkv_e2e_pg on host port ${TEST_PG_PORT})`);
    // Wait for Postgres to actually accept connections (image takes a few seconds
    // to initdb on first run). pg_isready inside the container is the reliable
    // signal; the host-port LISTEN happens before initdb finishes.
    log('Waiting for Postgres readiness...');
    const start = Date.now();
    const deadline = start + 30000;
    while (Date.now() < deadline) {
      try {
        execSync(
          `docker exec mkv_e2e_pg pg_isready -U ${TEST_PG_USER} -d ${TEST_PG_DB}`,
          { stdio: 'ignore' }
        );
        log(`Postgres ready after ${Math.round((Date.now() - start) / 100) / 10}s`);
        break;
      } catch (_) {
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    if (Date.now() >= deadline) {
      log('WARN: Postgres did not pass pg_isready within 30s; backend may fail');
    }
  } catch (e) {
    log('ERROR: Could not start test Postgres. The E2E backend requires it.');
    log('  ' + (e.message || e));
    process.exit(2);
  }

  // 2. Build frontend
  log('Building frontend...');
  execSync('npm run build', { cwd: frontend, stdio: 'inherit' });

  // 2b. Inject window.MKVAUTO_API_BASE into the built index.html so the
  //     bundle's readiness check (and any other consumer that prefers the
  //     runtime override) hits the test backend at PORT instead of the
  //     baked ``/api`` (which would need NGINX) or ``localhost:8000``
  //     (which would hit a co-located prod container).
  const indexPath = path.join(frontend, 'dist', 'disc-ripper-ui', 'browser', 'index.html');
  try {
    const apiBaseForBundle = `http://127.0.0.1:${BACKEND_PORT}`;
    let html = fs.readFileSync(indexPath, 'utf8');
    const injection = `<script>window.MKVAUTO_API_BASE=${JSON.stringify(apiBaseForBundle)};</script>`;
    if (!html.includes('MKVAUTO_API_BASE=')) {
      html = html.replace('<head>', `<head>\n  ${injection}`);
      fs.writeFileSync(indexPath, html);
      log(`Injected runtime API base ${apiBaseForBundle} into ${indexPath}`);
    }
  } catch (e) {
    log(`WARN: failed to inject runtime API base into index.html: ${e.message || e}`);
  }

  // 3. Start E2E backend
  if (await isPortInUse(BACKEND_PORT)) {
    log(`Port ${BACKEND_PORT} is already in use — refusing to proceed because the test stack would silently bind nowhere and Playwright would hit the existing service. Stop that process or re-run with PORT=8001 (or another free port).`);
    process.exit(2);
  }
  // Pass the test Redis URL and the test API URL into the backend so workers'
  // callbacks (rip-complete, postprocess-complete, transfer-complete) and any
  // Redis-dependent feature use the test ports — not whatever is on 8000/6379.
  const pgUrl = `postgresql+psycopg2://${TEST_PG_USER}:${TEST_PG_PASSWORD}@localhost:${TEST_PG_PORT}/${TEST_PG_DB}`;
  const backendEnv = {
    ...process.env,
    PORT: String(BACKEND_PORT),
    REDIS_URL: process.env.REDIS_URL || `redis://localhost:${TEST_REDIS_PORT}/0`,
    API_URL: process.env.API_URL || `http://127.0.0.1:${BACKEND_PORT}`,
    E2E_DATABASE_URL: process.env.E2E_DATABASE_URL || pgUrl,
  };
  const py = fs.existsSync(backendPy) ? backendPy : 'python';
  backendPro = spawn(py, [runE2eBackend], { cwd: repo, stdio: 'pipe', env: backendEnv });
  backendPro.on('error', (err) => log('E2E backend spawn error: ' + err.message));
  backendPro.on('exit', (code, sig) => {
    backendExitCode = code ?? null;
    backendExitedBeforeReady = true;
    if (code != null && code !== 0) log('E2E backend exited: code=' + code);
    if (sig) log('E2E backend killed: signal=' + sig);
  });
  backendPro.stderr?.on('data', (b) => process.stderr.write(b));
  backendPro.stdout?.on('data', (b) => process.stdout.write(b));

  const healthzUrl = `http://127.0.0.1:${BACKEND_PORT}/healthz`;
  log('Waiting for ' + healthzUrl + ' ...');
  try {
    await waitFor(healthzUrl, 90, 600);
  } catch (e) {
    if (backendExitedBeforeReady && backendExitCode === 1) {
      log('Backend exited with code 1 before /healthz was ready. Common cause: port ' + BACKEND_PORT + ' already in use.');
    }
    throw e;
  }
  log('Backend /healthz ready');

  // /healthz only confirms the FastAPI process is up — the frontend bootstraps
  // by polling /readyz, which adds a SELECT 1 DB ping. If we hand off before
  // /readyz is green, the frontend's #app-loading-overlay covers <app-root>
  // for up to 90 s and the smoke spec times out at 15 s. Wait for the same
  // gate the frontend uses (issue #373).
  const readyzUrl = `http://127.0.0.1:${BACKEND_PORT}/readyz`;
  log('Waiting for ' + readyzUrl + ' ...');
  await waitFor(readyzUrl, 90, 600);
  log('Backend /readyz ready');

  // 4. Serve frontend (Angular output is in dist/.../browser).
  //    --proxy makes http-server fall back to index.html for unknown paths
  //    (SPA deep links like /library) instead of 404ing. Without it, specs
  //    that navigate straight to a client-side route never load the app.
  //    (In production the container's NGINX does this via try_files.)
  httpPro = spawn('npx', ['http-server', 'dist/disc-ripper-ui/browser', '-p', '4200', '--proxy', 'http://127.0.0.1:4200?'], { cwd: frontend, stdio: 'pipe' });
  httpPro.on('error', (err) => log('http-server spawn error: ' + err.message));
  httpPro.stderr?.on('data', (b) => process.stderr.write(b));
  await waitFor('http://127.0.0.1:4200', 45, 400);
  log('Frontend on :4200 ready');

  // 5. Run Playwright (use 127.0.0.1 to avoid IPv6 localhost issues).
  // Extra CLI args pass through: `npm run e2e:full -- <spec> --trace on`.
  const pwEnv = { ...process.env, E2E_USE_EXISTING: '1', E2E_BASE_URL: 'http://127.0.0.1:4200', E2E_FULL: '1', E2E_API_URL: 'http://127.0.0.1:' + BACKEND_PORT };
  const pw = spawn('npx', ['playwright', 'test', ...process.argv.slice(2)], { cwd: frontend, stdio: 'inherit', env: pwEnv });
  const exitCode = await new Promise((resolve) => pw.on('close', (code) => resolve(code ?? 0)));

  onExit(exitCode);
}

main().catch((err) => {
  log('e2e-full failed: ' + err.message);
  teardown();
  process.exit(1);
});
