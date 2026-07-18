#!/bin/bash
# test.sh - Run test suites (backend, frontend, e2e) for MKV-Auto
# Used by: mkv test [backend|frontend|e2e|makemkv]

# Uses PROJECT_ROOT, FRONTEND_DIR, BACKEND_DIR, VENV from dev.sh (sourced before this)

# Default test DB/Redis (match dev containers; override with env)
export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:ripper_pass@localhost:5432/discs}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

run_backend_tests() {
  log "Running backend tests (pytest)..."
  (
    cd "$BACKEND_DIR"
    if [ ! -d "${VENV}/bin" ]; then
      echo "Error: Backend venv not found at $VENV. Run 'mkv dev start' or create the venv first."
      exit 1
    fi
    source "${VENV}/bin/activate"
    pip install -q -r requirements.txt pytest pytest-cov 2>/dev/null || true
    pytest --cov=. --cov-report=term "$@"
  )
}

run_frontend_tests() {
  log "Running frontend unit tests and build..."
  (
    cd "$FRONTEND_DIR"
    npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund 2>/dev/null || true
    npm test -- --watch=false "$@"
    npm run build
  )
}

run_e2e_tests() {
  log "Running E2E tests (full-stack; requires Redis and backend deps)..."
  (
    cd "$FRONTEND_DIR"
    npm run e2e:full
  )
}

run_makemkv_test() {
  log "Running MakeMKV updater E2E test (requires running test container)..."
  "${PROJECT_ROOT}/scripts/test-makemkv-updater.sh" "$@"
}

run_all_tests() {
  log "Running all tests (backend → frontend → e2e)..."
  run_backend_tests || exit 1
  run_frontend_tests || exit 1
  run_e2e_tests || exit 1
  log "All tests passed."
}
