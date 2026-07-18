"""
E2E ASGI app wrapper.

Lets ``uvicorn`` run with ``workers > 1`` even though uvicorn uses the ``spawn``
multiprocessing context — each worker reimports this module from scratch, and
this module guarantees the MockDrive / MockMKV patches and the seeded MISS
records are reapplied **before** the FastAPI app is imported.

Without this, workers spawned after the parent's e2e_bootstrap call would serve
real makemkv / drive code paths and crash the test stack.

Usage (from ``run_e2e_backend.py``)::

    uvicorn.run("scripts.e2e_app:app", workers=2, ...)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

if os.environ.get("MKVAUTO_E2E") == "1":
    # SQLite ``now()`` is registered in the parent's event listener but uvicorn's
    # spawn-mode workers build a fresh engine that has no listeners attached.
    # Register the function here so workers' INSERTs with ``server_default=now()``
    # succeed. Mirrors the listener in run_e2e_backend.py.
    _db_url = os.environ.get("DATABASE_URL", "")
    if "sqlite" in _db_url.lower():
        import datetime
        from sqlalchemy import event
        from api.database import engine

        @event.listens_for(engine, "connect")
        def _sqlite_now(dbapi_conn, _connection_record):
            dbapi_conn.create_function(
                "now",
                0,
                lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            # Mirror run_e2e_backend.py: WAL + busy_timeout so concurrent
            # writers (outer API handler + eager Celery task's intercept
            # session) don't trip "database is locked". Issue #378.
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.execute("PRAGMA synchronous=NORMAL")
            finally:
                cur.close()

    _spec = importlib.util.spec_from_file_location(
        "e2e_bootstrap", str(_here / "e2e_bootstrap.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(_mod)

from api.main import app  # noqa: E402  (must follow bootstrap above)
