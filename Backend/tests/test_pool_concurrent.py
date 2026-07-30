"""
Concurrent request tests for pool-sensitive endpoints.

Simulates multiple clients hitting coordinator and jobs endpoints in parallel
to validate that the app does not exhaust the DB connection pool (timeouts, 5xx).
Run with: pytest Backend/tests/test_pool_concurrent.py -v
"""
from __future__ import annotations

import time
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


@pytest.mark.asyncio
async def test_concurrent_coordinator_and_jobs_endpoints(client: TestClient) -> None:
    """
    Many concurrent requests to pool-sensitive endpoints must all succeed (200) and complete in time.

    Validates that the app does not exhaust the DB pool under load. #748
    rewrite: all 60 requests fire concurrently through one event loop
    (bounded gather) instead of a thread pool over the sync TestClient,
    which could deadlock in the client's blocking portal and hang the suite.
    """
    from tests.async_requests import gather_requests

    paths = [
        "/coordinator/initial-state",
        "/jobs/unfinished/workflow-contexts",
        "/jobs?limit=200",
    ]
    requests = [("GET", path, None) for _ in range(CONCURRENT_USERS) for path in paths]

    start = time.perf_counter()
    responses = await gather_requests(client.app, requests, timeout=MAX_WALL_TIME + 45)
    elapsed = time.perf_counter() - start

    all_statuses = [r.status_code for r in responses]
    assert len(all_statuses) == CONCURRENT_USERS * 3, "expected one result per request per user"
    failures = [s for s in all_statuses if s != 200]
    assert not failures, f"expected all 200, got failures: {failures}"

    # Batch must complete within threshold
    assert elapsed <= MAX_WALL_TIME, (
        f"concurrent batch took {elapsed:.1f}s, max allowed {MAX_WALL_TIME}s"
    )
