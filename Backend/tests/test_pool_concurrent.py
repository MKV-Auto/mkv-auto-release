"""
Concurrent request tests for pool-sensitive endpoints.

Simulates multiple clients hitting coordinator and jobs endpoints in parallel
to validate that the app does not exhaust the DB connection pool (timeouts, 5xx).
Run with: pytest Backend/tests/test_pool_concurrent.py -v
"""
from __future__ import annotations

import concurrent.futures
import time
from typing import List, Tuple

import pytest
from fastapi.testclient import TestClient

from api import database
from api.main import app
from api.routers import discdb, discs, jobs, movies, releases, system

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# Number of concurrent "users" (each issues 3 requests: initial-state, workflow-contexts, jobs)
CONCURRENT_USERS = 20
# Max wall-clock time for the whole batch (seconds).
# With Phase 1 refactor (no session across await) batch completes in ~13s; 15s allows headroom.
MAX_WALL_TIME = 15.0


def _client_session(client: TestClient) -> List[Tuple[str, int]]:
    """Simulate one user's page load: hit the three pool-sensitive endpoints. Returns [(path, status_code), ...]."""
    results = []
    r = client.get("/coordinator/initial-state")
    results.append(("/coordinator/initial-state", r.status_code))
    r = client.get("/jobs/unfinished/workflow-contexts")
    results.append(("/jobs/unfinished/workflow-contexts", r.status_code))
    r = client.get("/jobs?limit=200")
    results.append(("/jobs?limit=200", r.status_code))
    return results


@pytest.fixture
def client(test_db, monkeypatch):
    """FastAPI TestClient with test_db and get_cached_discs mock so coordinator uses test DB."""
    def override_get_db():
        try:
            session = test_db()
            yield session
        finally:
            session.close()

    app.dependency_overrides[jobs.get_db] = override_get_db
    app.dependency_overrides[releases.get_db] = override_get_db
    if hasattr(system, "get_db"):
        app.dependency_overrides[system.get_db] = override_get_db
    if hasattr(discs, "get_db"):
        app.dependency_overrides[discs.get_db] = override_get_db
    if hasattr(discdb, "get_db"):
        app.dependency_overrides[discdb.get_db] = override_get_db
    if hasattr(movies, "get_db"):
        app.dependency_overrides[movies.get_db] = override_get_db
    if hasattr(database, "get_db"):
        app.dependency_overrides[database.get_db] = override_get_db

    # Coordinator path uses database.SessionLocal() and get_cached_discs; test_db already
    # patches api.database.SessionLocal. Mock get_cached_discs so no real drive cache is needed.
    monkeypatch.setattr("core.disc_manager.get_cached_discs", lambda: [])

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_concurrent_coordinator_and_jobs_endpoints(client: TestClient) -> None:
    """
    Many concurrent requests to pool-sensitive endpoints must all succeed (200) and complete in time.

    Validates that the app does not exhaust the DB pool under load. Before refactor (session-across-await
    fixes), this test may timeout or get 500s; after refactor it should pass.
    """
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_USERS) as executor:
        futures = [executor.submit(_client_session, client) for _ in range(CONCURRENT_USERS)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    elapsed = time.perf_counter() - start

    # All requests must return 200
    all_statuses = []
    for session_results in results:
        for _path, status in session_results:
            all_statuses.append(status)

    assert len(all_statuses) == CONCURRENT_USERS * 3, "expected one result per request per user"
    failures = [s for s in all_statuses if s != 200]
    assert not failures, f"expected all 200, got failures: {failures}"

    # Batch must complete within threshold
    assert elapsed <= MAX_WALL_TIME, (
        f"concurrent batch took {elapsed:.1f}s, max allowed {MAX_WALL_TIME}s"
    )
