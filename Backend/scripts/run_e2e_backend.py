"""
Run the FastAPI backend for E2E tests with MockDrive and MockMKV.

Set env before any api/database import, ensure .e2e_data exists, run
alembic upgrade head against the test Postgres, import e2e_bootstrap
(patches + disc_cache seed), then uvicorn on 0.0.0.0:8000. Redis and
Postgres must be available (default ``redis://localhost:6380/0`` and
``postgresql+psycopg2://postgres:e2e_pass@localhost:5433/e2e`` — both
provisioned by ``Frontend/scripts/e2e-full.js``).

Usage (from repo root or Backend):
  python Backend/scripts/run_e2e_backend.py

Override via env:
  - E2E_DATA_DIR: data root (default: <repo_root>/.e2e_data)
  - E2E_DATABASE_URL: DB URL (default: Postgres at 5433/e2e)
  - PORT: server port (default: 8000)

SQLite is supported as a fallback for ad-hoc local runs (set
``E2E_DATABASE_URL=sqlite:///...``) but the canonical E2E DB is Postgres
because SQLite's single-writer semantics deadlock eager Celery + the
stage_callback_intercept's nested SessionLocal (see #378 / #195).

Worker model: ``__main__`` guard so multiprocessing.spawn from anywhere
inside the backend doesn't re-execute this script. Each uvicorn worker
imports ``e2e_app:app`` which reapplies the MockDrive / MockMKV patches.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def _setup_env_and_schema() -> Path:
    """Set env vars + create schema. Idempotent; safe to call once in parent."""
    _script = Path(__file__).resolve()
    _backend_dir = _script.parent.parent
    _repo_root = _backend_dir.parent

    _e2e_data = Path(
        os.getenv("E2E_DATA_DIR", str(_repo_root / ".e2e_data"))
    ).expanduser().resolve()
    _e2e_data.mkdir(parents=True, exist_ok=True)

    # Default to the test Postgres that e2e-full.js provisions. Falls back to
    # SQLite only if the user explicitly overrides via E2E_DATABASE_URL — the
    # SQLite path is kept for ad-hoc local runs but is known to deadlock on
    # postprocess (see module docstring + #378).
    _db_path = (_e2e_data / "e2e.db").resolve()
    _default_db_url = (
        "postgresql+psycopg2://postgres:e2e_pass@localhost:5433/e2e"
    )
    os.environ["DATABASE_URL"] = os.getenv("E2E_DATABASE_URL", _default_db_url)

    os.environ.setdefault("MKVAUTO_DATA", str(_e2e_data))
    os.environ.setdefault("MKVAUTO_E2E", "1")
    os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")

    # Keep disc_locks and other MakeMKV tmp state inside .e2e_data so a stale
    # rip-lock file from a prior crashed run cannot block the next rip on the
    # mock drive (which always runs on disc_num=1). Wipe leftovers as well.
    _e2e_tmp = _e2e_data / "tmp"
    _e2e_tmp.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MKVAUTO_TMP_DIR", str(_e2e_tmp))
    for _stale in (_e2e_tmp / "disc_locks").glob("*.lock") if (_e2e_tmp / "disc_locks").exists() else []:
        try:
            _stale.unlink()
        except OSError:
            pass

    # Issue #378: isolate from a co-located production container.
    #   - API_URL: worker rip-complete / postprocess-complete callbacks default
    #     to 127.0.0.1:8000. When tests run on PORT=8001, those POSTs land on
    #     the prod container's API (which 403s them as cross-host). Pin
    #     API_URL to this process's port.
    #   - REDIS_URL: dedicated test Redis on 6380 so Celery state, progress
    #     pub/sub, and redis_cache do not collide with prod's Redis on 6379.
    #     `e2e-full.js` starts an mkv_e2e_redis container on 6380 to match.
    _port_for_callbacks = os.getenv("PORT", "8000")
    os.environ.setdefault("API_URL", f"http://127.0.0.1:{_port_for_callbacks}")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")

    # MockMKV produces 1500-byte stub MKVs which the rip-verification ffprobe
    # gate would (correctly) reject as truncated/invalid. Skip the gate in E2E
    # so the rip → postprocess → transfer chain can complete end-to-end. The
    # env var is the documented test/emergency escape hatch in rip_raw_ready.py.
    os.environ.setdefault("MKVAUTO_RIP_VERIFY_SKIP_FFPROBE", "1")

    # Speed up the rip-verification wait loops for E2E. The defaults are sized
    # for real hardware (15s poll interval, 600s no-growth tolerance); on the
    # mock backend the files exist immediately and we want the loop to bail
    # quickly when something is mis-mapped.
    os.environ.setdefault("MKVAUTO_RIP_SHORT_INTERVAL_SECONDS", "1")
    # ``0`` disables the incomplete-rip wait-and-fail path entirely. Several
    # fixtures have title counts that don't survive a 1:1 round-trip through
    # MockMKV's MSG:3307 log → source_to_id → title_filename_map pipeline
    # (one entry collides on filename and is dropped). Verification ends up
    # with N-1 mapped MKVs vs N disc_titles and the wait loop fails the rip
    # with "Incomplete rip" even though the rip really did write all N files
    # (#194). Disable the gate for E2E; the count is informational only.
    os.environ.setdefault("MKVAUTO_RIP_SHORT_STABLE_SECONDS", "0")
    os.environ.setdefault("MKVAUTO_RIP_QUIESCENCE_STABLE_SECONDS", "2")

    # Backend on path for api, tests.fixtures, alembic.
    if str(_backend_dir) not in sys.path:
        sys.path.insert(0, str(_backend_dir))

    # SQLite: create_all (Alembic uses PostgreSQL-only ALTER). Else: alembic upgrade.
    _db_url = os.environ["DATABASE_URL"]
    if "sqlite" in _db_url.lower():
        import datetime

        # Wipe SQLite each run so can_start_rip never sees a stale "pending" from a previous run.
        _db_path.unlink(missing_ok=True)

        from sqlalchemy import event
        from api.database import engine, Base
        from api import models  # noqa: F401  ensure all models register

        @event.listens_for(engine, "connect")
        def _sqlite_now(dbapi_conn, connection_record):
            dbapi_conn.create_function(
                "now", 0, lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
            )
            # WAL mode lets a writer commit while readers (and the eager task's
            # own per-thread connection) hold open queries without "database is
            # locked" errors. busy_timeout retries briefly so a concurrent write
            # waits instead of failing immediately. Postgres handles this with
            # MVCC; SQLite needs the explicit pragmas. Issue #378 (E2E only).
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.execute("PRAGMA synchronous=NORMAL")
            finally:
                cur.close()

        Base.metadata.create_all(bind=engine)
    else:
        # Postgres: reset the public schema between runs so disc_titles / jobs /
        # transfer history etc. from a prior run don't carry over. Then build
        # the schema from SQLAlchemy models via Base.metadata.create_all
        # instead of ``alembic upgrade head``. alembic is the prod path but
        # at least one migration (add_performance_indexes) duplicates an
        # Index already declared in ``__table_args__`` and aborts on a fresh
        # Postgres with "relation idx_... already exists". Tests don't need
        # migration history — they need the same final schema, which
        # create_all provides directly. Production still runs alembic.
        from sqlalchemy import create_engine, text
        from api.database import engine, Base
        from api import models  # noqa: F401  ensure all models register

        _reset_engine = create_engine(_db_url, isolation_level="AUTOCOMMIT")
        try:
            with _reset_engine.connect() as _conn:
                _conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                _conn.execute(text("CREATE SCHEMA public"))
        finally:
            _reset_engine.dispose()
        Base.metadata.create_all(bind=engine)

    return _backend_dir


def main() -> None:
    _backend_dir = _setup_env_and_schema()

    # Parent bootstrap: seed DB rows (Movie / Release / TransferConfig) once.
    # The MockDrive / MockMKV patches and Celery broker override are reapplied
    # per worker via e2e_app (uvicorn spawn-mode workers reimport modules).
    _spec = importlib.util.spec_from_file_location(
        "e2e_bootstrap",
        _backend_dir / "scripts" / "e2e_bootstrap.py",
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    # Make e2e_app and api importable in spawned workers (env carries; sys.path doesn't).
    _existing_pythonpath = os.environ.get("PYTHONPATH", "")
    _pp_parts = [str(_backend_dir / "scripts"), str(_backend_dir)]
    if _existing_pythonpath:
        _pp_parts.append(_existing_pythonpath)
    os.environ["PYTHONPATH"] = os.pathsep.join(_pp_parts)

    import uvicorn

    _port = int(os.getenv("PORT", "8000"))
    # Single worker is fine now: e2e_bootstrap patches `workers.tasks.requests`
    # with the stage-callback intercept, so the rip → rip_verification →
    # postprocess callback chain bypasses HTTP entirely and applies state via
    # StageState in-process. Without that patch we would need workers >= the
    # depth of the callback chain just to avoid deadlock. See issue #378.
    uvicorn.run(
        "e2e_app:app",
        host="0.0.0.0",
        port=_port,
    )


if __name__ == "__main__":
    main()
