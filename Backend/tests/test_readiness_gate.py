"""Tests for the /readyz endpoint and the HTTP readiness gate middleware.

Backstory: when the container restarts, embedded Postgres needs a few seconds of WAL
recovery before accepting connections. The FastAPI app boots immediately and the
Angular frontend hits its bootstrap endpoints (`/coordinator/initial-state`, `/discs/options`,
etc) before PG is ready. Without the gate every endpoint 500s with a `psycopg2.OperationalError`
and the browser surfaces it as a CORS error (the 500 from a route handler doesn't get
CORS headers in Starlette's default exception path), leaving the user on a dead page.

The gate fixes this by returning 503 + Retry-After + explicit CORS headers for any
non-allowlisted route while the DB ping fails.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.main import app


@pytest.fixture
def client(e2e_test_environment):
    """Test client with e2e mocks (real DB available)."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_readiness_cache():
    """Clear the readiness TTL cache between tests so each test sees a fresh check."""
    state = api_main._readiness_state
    state["checked_at"] = 0.0
    state["ready"] = False
    state["error"] = None
    yield
    state["checked_at"] = 0.0
    state["ready"] = False
    state["error"] = None


def test_readyz_returns_200_when_db_reachable(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # No Retry-After on the success path — only present when not ready.
    assert "retry-after" not in {k.lower() for k in response.headers.keys()}


def test_readyz_returns_503_with_retry_after_when_db_down(client):
    """Patch the DB session to simulate Postgres in WAL recovery."""
    with patch.object(api_main.database, "SessionLocal") as fake:
        fake.side_effect = RuntimeError(
            "FATAL: the database system is not yet accepting connections"
        )
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    body = response.json()
    assert body["status"] == "starting"
    assert "not yet accepting connections" in (body.get("error") or "")


def test_readiness_gate_blocks_non_allowlisted_routes_when_db_down(client):
    """Real routes get 503 + Retry-After + CORS headers when DB ping fails."""
    with patch.object(api_main.database, "SessionLocal") as fake:
        fake.side_effect = RuntimeError("the database system is starting up")
        response = client.get("/jobs?limit=5")
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.headers.get("Access-Control-Allow-Origin") == "*", (
        "503 must carry CORS headers; otherwise the browser drops the response and "
        "shows a generic CORS failure (the original V for Vendetta dead-page bug)"
    )
    body = response.json()
    assert body["status"] == "starting"
    assert body["detail"] == "backend warming up"


def test_readiness_gate_allowlist_passes_through_when_db_down(client):
    """/healthz, /readyz, /system/setup/status, /docs etc. must NOT be gated.

    These endpoints are how the frontend (and any orchestrator) decides whether the
    backend is up — they cannot themselves be blocked by the readiness check.
    """
    with patch.object(api_main.database, "SessionLocal") as fake:
        fake.side_effect = RuntimeError("not ready")
        # /healthz is the cheap liveness probe; must always return 200.
        liveness = client.get("/healthz")
        assert liveness.status_code == 200
        assert liveness.json()["status"] == "ok"
        # /readyz itself must report degraded (503) but is reached, not gated.
        readiness = client.get("/readyz")
        assert readiness.status_code == 503
        # /system/setup/status is read by the frontend during bootstrap.
        setup = client.get("/system/setup/status")
        # Endpoint may itself touch the DB and return its own 500/error in the body,
        # but the readiness gate must NOT have shadowed it — i.e. status must NOT be 503
        # with the gate's "starting" body.
        if setup.status_code == 503:
            assert setup.json().get("detail") != "backend warming up"


def test_readiness_gate_passes_options_preflight_when_db_down(client):
    """OPTIONS preflights for CORS must always pass (no DB needed)."""
    with patch.object(api_main.database, "SessionLocal") as fake:
        fake.side_effect = RuntimeError("not ready")
        response = client.options(
            "/jobs",
            headers={
                "Origin": "http://localhost:4200",
                "Access-Control-Request-Method": "GET",
            },
        )
    # CORS middleware handles OPTIONS — should be 200 (or 204), never 503 from the gate.
    assert response.status_code in (200, 204)
    assert response.headers.get("Access-Control-Allow-Origin") == "*"


def test_readiness_state_caches_select_one_for_ttl(client):
    """The DB ping is cached for ~2s when ready so we don't hammer Postgres."""
    call_count = {"n": 0}

    real_session = api_main.database.SessionLocal

    def counting_factory(*args, **kwargs):
        call_count["n"] += 1
        return real_session(*args, **kwargs)

    with patch.object(api_main.database, "SessionLocal", side_effect=counting_factory):
        # Two consecutive ready=True checks within the cache window.
        r1 = client.get("/readyz")
        r2 = client.get("/readyz")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert call_count["n"] == 1, (
        f"expected exactly 1 SELECT 1 within TTL window, got {call_count['n']}"
    )
