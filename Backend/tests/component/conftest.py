"""Component test fixtures."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client(test_db):
    """TestClient with UDSServer patched and ``api.database.SessionLocal``
    pointed at the SQLite test DB.

    The ``test_db`` dependency is what makes the readiness gate (#490)
    happy: the middleware's ``SELECT 1`` ping runs against
    ``database.SessionLocal()`` and would otherwise try to reach
    Postgres at the default ``DATABASE_URL`` (host-side, unreachable
    from tests) and 503 every non-allowlisted route — including
    ``/drives/*`` — which is the failure mode #392 was tracking.
    """
    with patch("api.main.UDSServer"):
        yield TestClient(app)
